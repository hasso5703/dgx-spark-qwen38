"""Local registry: what lives on this box (models, revisions, images).

Pure parsing/classification helpers + one filesystem scanner. The scanner
touches metadata only (never file contents) and dedups hardlinked blobs by
inode so a multi-revision model is not double-counted.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Which pin variable serves which checkpoint (kept in lockstep with
# switch-model.sh; the pin parser itself is generic).
PIN_MODELS = {
    "STOCK_REV": "RadixArk/Qwen3.8-27B-NVFP4",
    "UNC_REV": "edp1096/Huihui-RadixArk-Qwen3.8-27B-abliterated-NVFP4",
    "FP8_REV": "Qwen/Qwen3.8-27B-FP8",
    "UNCFP8_REV": "edp1096/Huihui-Qwen3.8-27B-abliterated-FP8",
    "FLASH_REV": "RadixArk/Qwen3.8-Flash-Next-NVFP4",
    "DRAFT_REV": "RadixArk/Qwen3.8-27B-DSpark",
    "DRAFT2_REV": "z-lab/Qwen3.8-27B-DFlash2",
}

PIN_RE = re.compile(r'^\s*([A-Z][A-Z0-9_]*REV)="?\$?\{?[A-Z0-9_:-]*?([0-9a-f]{40})',
                    re.MULTILINE)

# Images that belong to this stack (everything else is none of our business).
ENGINE_IMAGE_RE = re.compile(
    r"^(lmsysorg/sglang|qwen38-flash|qwen38-dflash2|qwen38-sglang|vllm)")


def parse_pins(text: str) -> dict[str, str]:
    """VAR=<40-hex> pairs from shell text (default-expansions included)."""
    return {m.group(1): m.group(2) for m in PIN_RE.finditer(text)}


def parse_docker_images(lines: list[str]) -> list[dict]:
    """'repo:tag size id' rows -> engine-stack images, tagged as ours."""
    rows = []
    for ln in lines:
        parts = ln.split()
        if len(parts) < 3:
            continue
        ref, size, iid = parts[0], parts[1], parts[2]
        rows.append({"ref": ref, "size": size, "id": iid,
                     "engine": bool(ENGINE_IMAGE_RE.match(ref))})
    return rows


def scan_hf_cache(root: Path) -> list[dict]:
    """[{repo_id, disk_bytes, revisions:[{rev, bytes}]}] for models--* dirs.

    disk_bytes = physical blobs (deduped); a revision's bytes resolve its
    snapshot symlinks, so shared blobs count in every revision that uses
    them (logical size) but only once in disk_bytes.
    """
    out = []
    for d in sorted(root.glob("models--*")):
        repo_id = d.name[len("models--"):].replace("--", "/")
        seen: set[int] = set()
        disk = 0
        incomplete = 0
        blobs = d / "blobs"
        if blobs.is_dir():
            for f in blobs.iterdir():
                # huggingface_hub writes <sha>.incomplete while a blob is still
                # arriving: a snapshot directory exists long before it is usable,
                # so its mere presence must never be read as "the model is here".
                if f.name.endswith(".incomplete"):
                    incomplete += 1
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
                size = 0
                for base, _dirs, files in os.walk(rd):
                    for fn in files:
                        try:
                            size += (Path(base) / fn).stat().st_size
                        except OSError:
                            pass
                revs.append({"rev": rd.name, "bytes": size})
        out.append({"repo_id": repo_id, "disk_bytes": disk,
                    "incomplete": incomplete, "revisions": revs})
    return out


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
