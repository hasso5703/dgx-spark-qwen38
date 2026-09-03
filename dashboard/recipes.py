"""Recipes: what a lane serves, as data (step 4 of the cockpit plan).

A recipe pins one lane completely: engine image, model and revision, drafter
and its parameters, serving keys and environment. The built-in recipes are
DERIVED from the repo itself (install.sh pins + the unit/launcher templates),
so they cannot drift from what install.sh renders; the same parser reads the
installed unit or launcher, which is how drift between recipe and box is
found. Custom recipes are JSON files (stdlib only, no YAML dependency) in
~/.config/qwen38/recipes/. Everything here is pure: no shell, no network, no
filesystem except the two explicit loaders at the bottom.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

LANES = ("27b", "flash")
FAMILIES = ("sglang",)
ALGORITHMS = ("DFLASH", "NEXTN", "none")
# The 27B lane has two context modes and install.sh remembers which one is on the
# box. Comparing a 1M installation against the native template reported the three
# markers of 1M (context length, mem fraction, the overwrite env) as drift forever,
# which is a false alarm on the one panel whose job is to raise true ones.
LANE_TEMPLATE = {"27b": "qwen38-sglang.service.template",
                 "27b-1m": "qwen38-sglang-1m.service.template",
                 "flash": "qwen38-flash-launch.sh.template"}
CONTEXT_MODES = ("native", "1m")

HEX40 = re.compile(r"^[0-9a-f]{40}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
REPO_RE = re.compile(r"^[A-Za-z0-9][\w.-]*/[\w.-]+$")
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+|@sha256:[0-9a-f]{64})$")
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# VAR="literal", VAR=literal or VAR="${VAR:-default}" (trailing comment allowed)
ASSIGN_RE = re.compile(
    r'^\s*([A-Z][A-Z0-9_]*)=("?)(\$\{\1:-([^}]*)\}|[^"$\s#]*)\2\s*(#.*)?$', re.M)

# serve key -> (engine flag, type, (min, max) or allowed values or None)
FLAGS = {
    "context_length": ("--context-length", int, (4096, 1_010_000)),
    "mem_fraction": ("--mem-fraction-static", float, (0.30, 0.95)),
    "max_running_requests": ("--max-running-requests", int, (1, 64)),
    "chunked_prefill": ("--chunked-prefill-size", int, (256, 32768)),
    "max_mamba_cache_size": ("--max-mamba-cache-size", int, (1, 4096)),
    "attention_backend": ("--attention-backend", str, ("flashinfer", "triton", "trtllm_mha", "fa3")),
    "prefill_attention": ("--prefill-attention-backend", str, ("flashinfer", "triton", "trtllm_mha", "fa3")),
    "decode_attention": ("--decode-attention-backend", str, ("flashinfer", "triton", "trtllm_mha", "fa3")),
    "quantization": ("--quantization", str, ("modelopt_fp4", "fp8", "none")),
    "mamba_cache_strategy": ("--mamba-radix-cache-strategy", str, ("extra_buffer", "no_buffer")),
    # Only the FP8 targets ask for this; the NVFP4 checkpoints carry KV scales in
    # their own quant config. It is worth about half the KV pool, so a recipe that
    # omits it is not the recipe that was measured.
    "kv_cache_dtype": ("--kv-cache-dtype", str, ("fp8_e4m3", "auto", "bf16")),
}
DRAFT_FLAGS = {
    "algorithm": ("--speculative-algorithm", str),
    "repo": ("--speculative-draft-model-path", str),
    "revision": ("--speculative-draft-model-revision", str),
    "steps": ("--speculative-num-steps", int),
    "draft_tokens": ("--speculative-num-draft-tokens", int),
}
BUILTIN_IDS = ("stock", "uncensored", "fp8", "uncensored-fp8", "flash")


def parse_assignments(text: str) -> dict[str, str]:
    """Top-level shell assignments -> literal value (default of ${V:-x} kept)."""
    out = {}
    for m in ASSIGN_RE.finditer(text):
        out[m.group(1)] = m.group(4) if m.group(4) is not None else m.group(3)
    return out


def _flag(text: str, flag: str):
    m = re.search(re.escape(flag) + r"[ =]+([^\s\\'\"]+)", text)
    return m.group(1) if m else None


def profile_from_text(text: str) -> dict:
    """Engine invocation (template, launcher or unit) -> comparable profile.

    Placeholders (__X__) survive as-is; builtin() substitutes them, drift()
    ignores them. Unknown flags are not represented (drift is on known keys)."""
    lines = text.splitlines()
    image = None
    for i, ln in enumerate(lines):
        if "sglang.launch_server" in ln and i > 0:
            image = lines[i - 1].strip().rstrip("\\").strip() or None
            break
    serve = {}
    for key, (flag, typ, _rng) in FLAGS.items():
        raw = _flag(text, flag)
        if raw is not None:
            try:
                serve[key] = typ(raw)
            except ValueError:
                serve[key] = raw
    drafter = {"algorithm": "none", "repo": None, "revision": None}
    for key, (flag, typ) in DRAFT_FLAGS.items():
        raw = _flag(text, flag)
        if raw is not None:
            try:
                drafter[key] = typ(raw)
            except ValueError:
                drafter[key] = raw
    env = {}
    for m in re.finditer(r"-e\s+([A-Z][A-Z0-9_]*)=([^\s\\]+)", text):
        env[m.group(1)] = m.group(2)
    rev = _flag(text, "--revision")
    return {"engine": {"family": "sglang", "image": image},
            "model": {"repo": _flag(text, "--model-path"), "revision": rev},
            "drafter": drafter, "serve": serve, "env": env}


def _subst(value, mapping: dict[str, str]):
    if isinstance(value, str):
        for k, v in mapping.items():
            value = value.replace(k, v)
    return value


def builtin(recipe_id: str, assigns: dict[str, str], templates: dict[str, str],
            context_mode: str = "native") -> dict:
    """One built-in recipe from install.sh assignments + the lane's template.

    context_mode picks which 27B unit template the recipe is derived from, because
    both are this repo's, and a box runs one of them."""
    if recipe_id not in BUILTIN_IDS:
        raise KeyError(recipe_id)
    if context_mode not in CONTEXT_MODES:
        raise ValueError(f"context_mode: {context_mode!r} not in {CONTEXT_MODES}")
    lane = "flash" if recipe_id == "flash" else "27b"
    key = "27b-1m" if (lane == "27b" and context_mode == "1m") else lane
    prof = profile_from_text(templates[LANE_TEMPLATE[key]])
    if lane == "flash":
        repo, rev, image = assigns["FLASH_REPO"], assigns["FLASH_REV"], assigns["FLASH_SERVE_IMAGE"]
        base = assigns.get("FLASH_IMAGE")
        overlay = "flash-sglang"
    else:
        pfx = {"stock": "STOCK", "uncensored": "UNC", "fp8": "FP8",
               "uncensored-fp8": "UNCFP8"}[recipe_id]
        repo, rev, image = assigns[f"{pfx}_REPO"], assigns[f"{pfx}_REV"], assigns["SERVE_IMAGE"]
        base = None
        overlay = "dflash2"
    # The unit templates carry the KV cache choice as a placeholder because it is
    # per-target; substitute it the way install.sh does so an FP8 recipe shows the
    # flag that defines it instead of leaving a placeholder behind.
    kv = "--kv-cache-dtype fp8_e4m3 " if recipe_id in ("fp8", "uncensored-fp8") else ""
    prof = dict(prof)
    prof["serve"] = dict(prof["serve"])
    if kv:
        prof["serve"]["kv_cache_dtype"] = "fp8_e4m3"
    else:
        prof["serve"].pop("kv_cache_dtype", None)
    mapping = {"__KV_CACHE_ARGS__": kv,
               "__MODEL__": repo, "__MODEL_REV_ARGS__": f"--revision {rev}",
               "__MODEL_REV__": rev, "__IMAGE__": image,
               "__DRAFT2_REV__": assigns.get("DRAFT2_REV", "__DRAFT2_REV__"),
               "__DRAFT_REV__": assigns.get("DRAFT_REV", "__DRAFT_REV__")}
    drafter = {k: _subst(v, mapping) for k, v in prof["drafter"].items()}
    env = {k: _subst(v, mapping) for k, v in prof["env"].items()}
    return {
        "id": recipe_id, "lane": lane, "builtin": True,
        "engine": {"family": "sglang", "image": image, "base_image": base, "overlay": overlay},
        "model": {"repo": repo, "revision": rev},
        "drafter": drafter, "serve": dict(prof["serve"]), "env": env,
        "validation": {"needle_depths": [60000, 120000] if lane == "flash" else [30000, 100000],
                       "canaries": 4},
    }


def builtins(assigns: dict[str, str], templates: dict[str, str],
             context_mode: str = "native") -> list[dict]:
    return [builtin(i, assigns, templates, context_mode) for i in BUILTIN_IDS]


def context_mode_of(profile: dict | None) -> str:
    """Which 27B context mode an installed invocation is: the 1M unit is the one
    that asks for more than the checkpoint's native window."""
    ctx = (profile or {}).get("serve", {}).get("context_length")
    return "1m" if isinstance(ctx, int) and ctx > 262144 else "native"


def validate(recipe: dict, reserved_ids: tuple = ()) -> list[str]:
    """Schema + closed enums + ranges. Empty list = valid. Never raises."""
    errs: list[str] = []
    if not isinstance(recipe, dict):
        return ["recipe must be an object"]
    rid = recipe.get("id")
    if not isinstance(rid, str) or not ID_RE.match(rid):
        errs.append("id: lowercase letters, digits and dashes, 1 to 32 chars")
    elif rid in reserved_ids:
        errs.append(f"id: '{rid}' is reserved")
    if recipe.get("lane") not in LANES:
        errs.append(f"lane: one of {list(LANES)}")
    eng = recipe.get("engine") or {}
    if not isinstance(eng, dict) or eng.get("family") not in FAMILIES:
        errs.append(f"engine.family: one of {list(FAMILIES)}")
    img = eng.get("image") if isinstance(eng, dict) else None
    if not isinstance(img, str) or not IMAGE_RE.match(img):
        errs.append("engine.image: name:tag or name@sha256:<64 hex>")
    elif img.endswith(":latest"):
        errs.append("engine.image: a moving tag (latest) is not a pin")
    mod = recipe.get("model") or {}
    if not isinstance(mod, dict) or not isinstance(mod.get("repo"), str) or not REPO_RE.match(mod["repo"]):
        errs.append("model.repo: owner/name")
    if not isinstance(mod, dict) or not isinstance(mod.get("revision"), str) or not HEX40.match(mod["revision"]):
        errs.append("model.revision: 40 hex characters (a commit, not a branch)")
    dr = recipe.get("drafter") or {"algorithm": "none"}
    if not isinstance(dr, dict) or dr.get("algorithm") not in ALGORITHMS:
        errs.append(f"drafter.algorithm: one of {list(ALGORITHMS)}")
    else:
        algo = dr["algorithm"]
        if algo == "DFLASH":
            if not isinstance(dr.get("repo"), str) or not REPO_RE.match(dr["repo"]):
                errs.append("drafter.repo: owner/name required for DFLASH")
            if not isinstance(dr.get("revision"), str) or not HEX40.match(dr["revision"]):
                errs.append("drafter.revision: 40 hex characters required for DFLASH")
        elif dr.get("repo") not in (None, ""):
            errs.append(f"drafter.repo: {algo} uses the model's own head, no repo")
        for k in ("steps", "draft_tokens"):
            v = dr.get(k)
            if v is not None and (not isinstance(v, int) or not 1 <= v <= 16):
                errs.append(f"drafter.{k}: integer 1 to 16")
    serve = recipe.get("serve")
    if not isinstance(serve, dict) or not serve:
        errs.append("serve: object with at least context_length")
    else:
        for k, v in serve.items():
            if k not in FLAGS:
                errs.append(f"serve.{k}: unknown key (allowed: {sorted(FLAGS)})")
                continue
            _flag_name, typ, rng = FLAGS[k]
            if typ is str:
                if v not in rng:
                    errs.append(f"serve.{k}: one of {list(rng)}")
            elif isinstance(v, bool) or not isinstance(v, (int, float)):
                errs.append(f"serve.{k}: number expected")
            elif not rng[0] <= v <= rng[1]:
                errs.append(f"serve.{k}: {rng[0]} to {rng[1]}")
        if "context_length" not in serve:
            errs.append("serve.context_length: required")
    env = recipe.get("env", {})
    if not isinstance(env, dict):
        errs.append("env: object of NAME=value strings")
    else:
        for k, v in env.items():
            if not isinstance(k, str) or not ENV_KEY_RE.match(k):
                errs.append(f"env.{k}: NAME must be [A-Z][A-Z0-9_]*")
            if not isinstance(v, str) or len(v) > 200 or re.search(r"\s", v):
                errs.append(f"env.{k}: value must be a string without whitespace (max 200)")
    return errs


def drift(recipe: dict, installed: dict) -> list[dict]:
    """Where the installed invocation differs from the recipe, key by key.
    Placeholder values (__X__) on either side are skipped, never reported."""
    rows = []

    def cmp(path, want, have):
        if isinstance(want, str) and want.startswith("__") or isinstance(have, str) and have.startswith("__"):
            return
        if want != have:
            rows.append({"key": path, "recipe": want, "installed": have})

    cmp("engine.image", recipe["engine"].get("image"), installed["engine"].get("image"))
    cmp("model.repo", recipe["model"].get("repo"), installed["model"].get("repo"))
    cmp("model.revision", recipe["model"].get("revision"), installed["model"].get("revision"))
    for k in ("algorithm", "repo", "revision", "steps", "draft_tokens"):
        cmp(f"drafter.{k}", recipe.get("drafter", {}).get(k), installed.get("drafter", {}).get(k))
    keys = set(recipe.get("serve", {})) | set(installed.get("serve", {}))
    for k in sorted(keys):
        cmp(f"serve.{k}", recipe.get("serve", {}).get(k), installed.get("serve", {}).get(k))
    ekeys = set(recipe.get("env", {})) | set(installed.get("env", {}))
    for k in sorted(ekeys):
        if k in ("HF_HUB_OFFLINE", "TORCHINDUCTOR_CACHE_DIR", "SGLANG_QWEN4_PLE_MMAP_DIR", "SGLANG_QWEN4_PLE_TAG"):
            continue  # plumbing set by install.sh, not recipe material
        cmp(f"env.{k}", recipe.get("env", {}).get(k), installed.get("env", {}).get(k))
    return rows


def presence(recipe: dict, registry: dict) -> dict:
    """Is what the recipe needs on this box? True/False, None = not knowable
    from the registry (model outside the managed set)."""
    images = {i.get("ref") for i in registry.get("images", [])}
    # Repos this repo pins: one of them missing from the cache is MISSING, not unknown.
    # Without this a target that was never downloaded read "n/a", which says nothing
    # about whether switching to it would work.
    managed = set(registry.get("managed_repos") or [])
    known: dict[str, set] = {}
    busy: set = set()
    for m in registry.get("models", []):
        known.setdefault(m["repo_id"], set()).update(r["rev"] for r in m.get("revisions", []))
        if m.get("incomplete"):
            busy.add(m["repo_id"])

    def cached(repo, rev):
        if repo is None:
            return None
        if repo not in known:
            return False if repo in managed else None
        if repo in busy:
            return False          # a snapshot with blobs still arriving is not servable
        return rev in known[repo]

    dr = recipe.get("drafter", {})
    return {"image": recipe["engine"].get("image") in images,
            "downloading": recipe["model"].get("repo") in busy,
            "model": cached(recipe["model"].get("repo"), recipe["model"].get("revision")),
            "drafter": cached(dr.get("repo"), dr.get("revision")) if dr.get("algorithm") == "DFLASH" else None}


# ── the two loaders (filesystem, read-only) ───────────────────────────────────
def load_templates(repo_dir: Path) -> dict[str, str]:
    return {name: (repo_dir / name).read_text() for name in LANE_TEMPLATE.values()}


def load_custom(directory: Path) -> list[dict]:
    """[{file, recipe|None, errors}] for every *.json in the directory."""
    out = []
    if not directory.is_dir():
        return out
    for f in sorted(directory.glob("*.json")):
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError) as e:
            out.append({"file": f.name, "recipe": None, "errors": [f"unreadable JSON: {e}"]})
            continue
        errs = validate(rec, reserved_ids=BUILTIN_IDS)
        if isinstance(rec, dict):
            rec = {**rec, "builtin": False}
        out.append({"file": f.name, "recipe": rec, "errors": errs})
    return out
