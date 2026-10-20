"""Local registry: what lives on this box (models, revisions, images).

Pure parsing/classification helpers + one filesystem scanner. The scanner
touches metadata only (never file contents) and dedups hardlinked blobs by
inode so a multi-revision model is not double-counted.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import time
from pathlib import Path

# Which pin variable serves which checkpoint (kept in lockstep with
# switch-model.sh; the pin parser itself is generic).
PIN_MODELS = {
    "STOCK_REV": "RadixArk/Qwen3.8-27B-NVFP4",
    "UNC_REV": "edp1096/Huihui-RadixArk-Qwen3.8-27B-abliterated-NVFP4",
    "FP8_REV": "Qwen/Qwen3.8-27B-FP8",
    "UNCFP8_REV": "edp1096/Huihui-Qwen3.8-27B-abliterated-FP8",
    "FLASH_REV": "RadixArk/Qwen3.8-Flash-Next-NVFP4",
    "FLASH_NVDA_REV": "nvidia/Qwen3.8-Flash-Next-NVFP4",
    "FLASH_UNC_REV": "dealignai/Qwen3.8-Flash-Next-ABLITERATED-NVFP4",
    # the flash lane's other engine (the b12x vLLM container), pinned by install.sh
    "FLASH_LOCAL_REV": "nvidia/Qwen3.8-Flash-Next-FP4-Local",
    "DRAFT_REV": "RadixArk/Qwen3.8-27B-DSpark",
    "DRAFT2_REV": "maurienne-ai/Qwen3.8-27B-DFlash2-NVFP4-RTNcal",
    # The other lanes, each pinned by its own installer. Until 2026-10-14 their weights read
    # as "other models", with no pin and no status.
    "IMAGE_MODEL_PIN_REV": "Qwen/Qwen-Image-2.1",
    "IMAGE_TURBO_PIN_REV": "Qwen/Qwen-Image-2.1-Turbo",
    "VIDEO_MODEL_PIN_REV": "MiniMaxAI/MiniMax-H3",
    "VIDEO_TURBO_REV": "larryvrh/MiniMax-H3-Turbo-Lora",
    "KOLIBRI_FP8_REV": "Aleph-Alpha/Kolibri-1",
    "KOLIBRI_NVFP4_REV": "Quantinine/Kolibri-1-NVFP4A16-FP8",
    "NEMOTRON_BOLT_REV": "nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB",
    "NEMOTRON_BOLT_DRAFT_REV": "nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB-LiLiCorr",
    "NEMOTRON_SUPER_REV": "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4",
    "NEMOTRON_SUPER_DRAFT_REV": "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4-LiLiCorr",
}

# Where the pins are written: the text lanes' in the switch and the installer, every other
# lane's in its own installer.
PIN_FILES = ("switch-model.sh", "run.sh", "install.sh",
             "install-image.sh", "install-video.sh", "install-kolibri.sh", "install-nemotron.sh",
             "install-nemotron-super.sh")

PIN_RE = re.compile(r'^\s*([A-Z][A-Z0-9_]*REV)="?\$?\{?[A-Z0-9_:-]*?([0-9a-f]{40})',
                    re.MULTILINE)

# Images that belong to this stack (everything else is none of our business). The Kolibri and
# Nemotron lanes run images built on the box, which carry a tag and no digest: their builds were
# missing from Library's engine images (found on the reference box, 2026-10-16). The flash lane's
# FP4-Local config runs the b12x vLLM image, by digest and under its pin tag: unmatched, it was
# missing there too and its recipe always read "image absent" (found in review, 2026-10-20).
ENGINE_IMAGE_RE = re.compile(
    r"^(lmsysorg/sglang|qwen38-flash|qwen38-dflash2|qwen38-sglang|qwen38-kolibri:|qwen38-nemotron:|vllm"
    r"|eugr/spark-vllm-b12x[:@]|qwen38-pinned:flash-local-)")


# Where a lane's unit or launcher says its Hugging Face cache is: a text lane mounts it into
# its container, the image and video lanes set HF_HOME. install.sh takes HF_CACHE.
CACHE_RE = re.compile(r"-v\s+(\S+?):/root/\.cache/huggingface\b|HF_HOME=(\S+)")


def cache_dirs(texts) -> list[str]:
    """The Hugging Face caches the given unit and launcher texts name, in order, once each."""
    out: list[str] = []
    for t in texts:
        for m in CACHE_RE.finditer(t or ""):
            d = (m.group(1) or m.group(2)).strip("\"'")
            if d not in out:
                out.append(d)
    return out


def parse_pins(text: str) -> dict[str, str]:
    """VAR=<40-hex> pairs from shell text (default-expansions included)."""
    return {m.group(1): m.group(2) for m in PIN_RE.finditer(text)}


def parse_docker_images(lines: list[str]) -> list[dict]:
    """'ref size id' rows -> engine-stack images, tagged as ours.

    The caller feeds BOTH shapes docker can print: repo:tag and repo@digest. An
    image pulled by digest has no tag at all, so `{{.Repository}}:{{.Tag}}`
    prints `<none>:<none>` for it and a digest-pinned recipe could never match
    what is on the box: the cockpit said "image missing" about the very image
    the lane was running (2026-09-08, when the flash lane moved to a digest pin).
    Rows whose reference is unusable are dropped rather than carried as noise.
    """
    rows = []
    seen = set()
    for ln in lines:
        parts = ln.split()
        if len(parts) < 3:
            continue
        ref, size, iid = parts[0], parts[1], parts[2]
        if "<none>" in ref or ref in seen:
            continue
        seen.add(ref)
        rows.append({"ref": ref, "size": size, "id": iid,
                     "engine": bool(ENGINE_IMAGE_RE.match(ref))})
    return rows


# A blob huggingface_hub is still writing has been written to within this long.
ACTIVE_S = 600


def allowed(rel: str, allow) -> bool:
    """Whether a snapshot path counts under allow patterns, as ckpt-fetch.py (and
    huggingface_hub) read them: fnmatch, case kept, a trailing / standing for the folder."""
    return not allow or any(fnmatch.fnmatchcase(rel, p + "*" if p.endswith("/") else p) for p in allow)


def snapshot_complete(snap: Path, allow=None) -> bool:
    """Every weight file each of the snapshot's indexes names is in it (a file appears there
    only once its blob is whole): the root index of a checkpoint, what install.sh's
    ckpt_cached checks, or one per component in a diffusers layout. With no index, a weight
    file at all. A snapshot folder exists before the first byte of its first file. Only what
    the allow patterns cover counts, as in ckpt-fetch.py's complete(): the video lane serves
    one partition of MiniMax-H3, and the index of another a full pull began does not make
    the lane's checkpoint incomplete (found building the models view, 2026-10-14)."""
    indexes = [p for p in snap.rglob("*.safetensors.index.json") if allowed(p.relative_to(snap).as_posix(), allow)]
    if not indexes:
        # a link counts once its blob is there, as in an index (a link to a blob gone did)
        return any(allowed(p.relative_to(snap).as_posix(), allow) and p.exists() for p in snap.rglob("*.safetensors"))
    for idx in indexes:
        try:
            shards = set(json.loads(idx.read_text())["weight_map"].values())
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False
        if not shards or not all((idx.parent / s).exists() for s in shards):
            return False
    return True


VIDEO_ALLOW_RE = re.compile(r'if \[ "\$VID_VARIANT" = "fl2va" \]; then VID_ALLOW="([^"]+)"\s*else VID_ALLOW="([^"]+)"')


def video_allow(switch_text: str, variant: str):
    """The patterns the video lane's switch fetches and serves for a variant, read from
    switch-model.sh, where they are written; None when it no longer reads that way."""
    m = VIDEO_ALLOW_RE.search(switch_text or "")
    if not m:
        return None
    return (m.group(1) if variant == "fl2va" else m.group(2)).split()


def scan_hf_cache(root: Path) -> list[dict]:
    """[{repo_id, disk_bytes, incomplete, active, revisions:[{rev, bytes, complete}]}] for
    models--* dirs.

    disk_bytes = physical blobs (deduped); a revision's bytes are the blobs its
    snapshot links to, each once (two of its files can link one blob: MiniMax-H3's
    partitions share their weights, and counted per file its one revision read twice
    its size), so a blob two revisions share counts in each, and once in disk_bytes.
    """
    out = []
    now = time.time()
    for d in sorted(root.glob("models--*")):
        repo_id = d.name[len("models--"):].replace("--", "/")
        seen: set[int] = set()
        disk = 0
        incomplete = active = 0
        blobs = d / "blobs"
        if blobs.is_dir():
            for f in blobs.iterdir():
                # huggingface_hub writes <sha>.incomplete while a blob is still arriving.
                # One that nothing writes to any more is what an interrupted download
                # left, and it marked the repo "downloading" for good (found in review,
                # 2026-09-24): only a recent one is a download in progress.
                if f.name.endswith(".incomplete"):
                    incomplete += 1
                    try:
                        active += now - f.stat().st_mtime < ACTIVE_S
                    except OSError:
                        pass
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                if st.st_ino in seen:
                    continue
                seen.add(st.st_ino)
                disk += st.st_size
        revs = []
        snaps = d / "snapshots"
        if snaps.is_dir():
            for rd in sorted(snaps.iterdir()):
                size, mine = 0, set()
                for base, _dirs, files in os.walk(rd):
                    for fn in files:
                        try:
                            st = (Path(base) / fn).stat()
                        except OSError:
                            continue
                        if (st.st_dev, st.st_ino) not in mine:
                            mine.add((st.st_dev, st.st_ino))
                            size += st.st_size
                revs.append({"rev": rd.name, "bytes": size, "complete": snapshot_complete(rd)})
        out.append({"repo_id": repo_id, "disk_bytes": disk,
                    "incomplete": incomplete, "active": active, "revisions": revs})
    return out


REV_RE = re.compile(r"^[0-9a-f]{40}$")


def deletion_plan(repo_dir: Path, rev: str) -> dict:
    """What deleting one cached revision removes: its snapshot folder and the blobs no other
    snapshot of the repo links to, with the bytes that frees. {"refused": why} for a name
    that is not a revision here, and for a revision a ref names, which a load by name reads.
    Nothing is removed here: the cockpit's job does that, after the page showed this."""
    if not REV_RE.match(rev or ""):
        return {"refused": "not a revision hash"}
    snaps = repo_dir / "snapshots"
    target = snaps / rev
    if not target.is_dir():
        return {"refused": "this revision is not in the cache"}
    refs = repo_dir / "refs"
    if refs.is_dir():
        for ref in sorted(refs.rglob("*")):
            try:
                named = ref.is_file() and ref.read_text().strip() == rev
            except OSError:
                named = True                 # unreadable: assume it names it
            if named:
                return {"refused": f"refs/{ref.relative_to(refs)} names it, and a load by name reads it"}
    blobs = (repo_dir / "blobs").resolve()
    mine: set[str] = set()
    others: set[str] = set()
    for d in snaps.iterdir():
        bucket = mine if d.name == rev else others
        for base, _dirs, files in os.walk(d):
            for fn in files:
                link = Path(base) / fn
                if link.is_symlink():
                    bucket.add(os.path.realpath(link))
    # only this repo's own blobs, whatever a link says
    only = sorted(b for b in mine - others if Path(b).parent == blobs)
    size = 0
    for b in only:
        try:
            size += os.stat(b).st_size
        except OSError:
            pass
    return {"snapshot": str(target), "blobs": only, "bytes": size}


def classify(models: list[dict], pins: dict[str, str]) -> list[dict]:
    """Mark every cached revision pinned (by which var) or stray."""
    rev2var = {rev: var for var, rev in pins.items()}
    enriched = []
    for m in models:
        pin_vars = [v for v, mid in PIN_MODELS.items() if mid == m["repo_id"]]
        revs = []
        for r in m["revisions"]:
            var = rev2var.get(r["rev"])
            status = ("pinned" if var and var in pin_vars
                      else "pin-elsewhere" if var
                      else "stray" if pin_vars
                      else "unmanaged")
            revs.append({**r, "status": status, "pin": var})
        enriched.append({**m, "revisions": revs,
                         "managed": bool(pin_vars)})
    return enriched
