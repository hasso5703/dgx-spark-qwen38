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
# The flash lane's two cookbook-verified tiers, the same strings install.sh
# renders into the launcher. Kept here so a recipe read from the repo matches
# the launcher on the box flag for flag.
# The draft's own quantization is not here: it belongs to the checkpoint, and
# install.sh renders it next to the target's scheme. See FLASH_QUANT_ARGS.
_MTP = ("--mamba-radix-cache-strategy extra_buffer "
        "--speculative-algorithm NEXTN --speculative-num-steps 3 "
        "--speculative-eagle-topk 1 --speculative-num-draft-tokens 4")
TIER_ARGS = {
    "context": "--max-running-requests 4 --max-mamba-cache-size 20 " + _MTP,
    "concurrency": "--max-running-requests 8 --max-mamba-cache-size 40 " + _MTP,
    "throughput": ("--max-running-requests 24 --max-mamba-cache-size 96 "
                   "--mamba-radix-cache-strategy extra_buffer_lazy"),
}

# Placeholders a recipe keeps on purpose: they name where this particular box put
# things, and a recipe is host-independent by design (drift() ignores a
# placeholder on either side). Everything else in a lane template carries a
# serving flag and MUST be substituted by builtin(), which is asserted there.
HOST_PLACEHOLDERS = ("__HOME__", "__USER__", "__GROUP__", "__HF_CACHE__",
                     "__PLE_DIR__", "__PORT__", "__PROXY_PORT__", "__PROMPT_CEILING__")

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
    # Pins the KV pool instead of letting the boot profile it: what SGLang measures at
    # boot depends on the memory free at that instant, and on unified memory a boot
    # launched seconds after the other lane stopped draws a smaller pool (the flash
    # lane measured 189,056 against 249,408, same launcher, 2026-09-03).
    "max_total_tokens": ("--max-total-tokens", int, (4096, 1_100_000)),
    "chunked_prefill": ("--chunked-prefill-size", int, (256, 32768)),
    "max_mamba_cache_size": ("--max-mamba-cache-size", int, (1, 4096)),
    "attention_backend": ("--attention-backend", str, ("flashinfer", "triton", "trtllm_mha", "fa3")),
    "prefill_attention": ("--prefill-attention-backend", str, ("flashinfer", "triton", "trtllm_mha", "fa3")),
    "decode_attention": ("--decode-attention-backend", str, ("flashinfer", "triton", "trtllm_mha", "fa3")),
    "quantization": ("--quantization", str, ("modelopt_fp4", "fp8", "none")),
    # The mixed-precision export resolves its own scheme and is served with no
    # --quantization at all, but it does need the MoE runner pinned: the auto
    # default picks flashinfer_trtllm on GB10 and the NVFP4 MoE method rejects it.
    "moe_runner_backend": ("--moe-runner-backend", str, ("flashinfer_cutlass", "flashinfer_trtllm", "triton", "auto")),
    "fp4_gemm_backend": ("--fp4-gemm-backend", str, ("flashinfer_cutlass", "flashinfer_cudnn", "flashinfer_cutedsl", "flashinfer_trtllm", "marlin", "auto")),
    # Compressed QSA addresses the KV pool in pages; the flash lane serves 64,
    # which is also what the engine sets by itself for this model.
    "page_size": ("--page-size", int, (1, 256)),
    # Where the 47.7 GiB N-gram table lives: pinned host RAM (upstream default,
    # useless on unified memory) or a sparse file the gather kernel reads through
    # the host page tables (sglang#37068, the only thing that fits one GB10).
    "ple_offload_backend": ("--ple-offload-backend", str, ("file", "pinned")),
    # The reduced draft vocabulary. A path, so it is checked for shape only: the
    # engine reads it with torch.load and slices the target head to those rows.
    "speculative_token_map": ("--speculative-token-map", str, None),
    "mamba_cache_strategy": ("--mamba-radix-cache-strategy", str, ("extra_buffer", "extra_buffer_lazy", "no_buffer", "auto")),
    # Only the FP8 targets ask for this; the NVFP4 checkpoints carry KV scales in
    # their own quant config. It is worth about half the KV pool, so a recipe that
    # omits it is not the recipe that was measured.
    "kv_cache_dtype": ("--kv-cache-dtype", str, ("fp8_e4m3", "auto", "bf16")),
}
# Serving switches: flags that carry no value, so FLAGS above cannot hold them and
# drift() was blind to the whole class. One of them is why this exists: the flash
# lane shipped without --sleep-on-idle and its scheduler burned a full core for
# 12 h 21 min of measured idle (2026-09-10), while the drift panel, whose job is
# to say where the box differs from the repo, had nothing to report.
SWITCHES = (
    "--sleep-on-idle",              # park the scheduler instead of busy-spinning a core
    "--allow-auto-truncate",        # truncate an oversize prompt instead of erroring
    "--enable-torch-compile",       # 27B lane: compiled decode
    "--disable-flashinfer-autotune",
    "--disable-prefill-cuda-graph",
    "--ple-offload-embedding",      # flash lane: the only reason 176B fits one GB10
    "--trust-remote-code",
)


def _switches(text: str) -> dict[str, bool]:
    """Which value-less serving flags this invocation passes.

    Token equality, never a substring: --disable-radix-cache must not answer for
    --disable-radix, and a flag named inside prose is already gone (the caller
    strips comment lines)."""
    tokens = {t.strip("()\'\"\\,") for t in text.split()}
    return {f: f in tokens for f in SWITCHES}


DRAFT_FLAGS = {
    "algorithm": ("--speculative-algorithm", str),
    # Belongs to the checkpoint: an NVFP4 export whose MTP tensors stayed BF16
    # must say "unquant", and one whose MTP experts are fp8 block-scaled must
    # not. Getting this wrong loads a quantized head as if it were dense.
    "quantization": ("--speculative-draft-model-quantization", str),
    "repo": ("--speculative-draft-model-path", str),
    "revision": ("--speculative-draft-model-revision", str),
    "steps": ("--speculative-num-steps", int),
    "draft_tokens": ("--speculative-num-draft-tokens", int),
}
BUILTIN_IDS = ("stock", "uncensored", "fp8", "uncensored-fp8", "flash", "flash-nvda",
               "flash-uncensored")


def parse_assignments(text: str) -> dict[str, str]:
    """Shell assignments -> literal value (default of ${V:-x} kept).

    FIRST occurrence wins. install.sh declares every pin once in its header and
    then reassigns some of those names inside its convergence logic (a box
    already serving the throughput tier sets FLASH_TIER=throughput before
    rendering); reading the last assignment reported that runtime branch as the
    repo's pin, which made a latency recipe come out with the other tier's
    concurrency."""
    out = {}
    for m in ASSIGN_RE.finditer(text):
        key = m.group(1)
        if key in out:
            continue
        out[key] = m.group(4) if m.group(4) is not None else m.group(3)
    return out


def _flag(text: str, flag: str):
    # ")" is excluded because the flash launcher carries its tier as a bash
    # array, TIER=(--max-running-requests 24 ... extra_buffer_lazy), and the
    # closing parenthesis is not part of the last flag's value.
    m = re.search(re.escape(flag) + r"[ =]+([^\s\\'\")]+)", text)
    return m.group(1) if m else None


def profile_from_text(text: str) -> dict:
    """Engine invocation (template, launcher or unit) -> comparable profile.

    Placeholders (__X__) survive as-is; builtin() substitutes them, drift()
    ignores them. Unknown flags are not represented (drift is on known keys)."""
    # Full-line comments are prose, and prose names flags: "the --max-total-tokens
    # pin below" is not a flag with the value "pin". Only the invocation counts.
    text = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
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
            "drafter": drafter, "serve": serve, "switches": _switches(text), "env": env}


def _deref(value: str | None, assigns: dict[str, str]) -> str | None:
    """One level of "$OTHER" / "${OTHER}" indirection, resolved from assigns.

    install.sh declares the two serving images as the pinned bases by default
    (SERVE_IMAGE="${SERVE_IMAGE:-$IMAGE}"), because since v1.8 the served image
    IS the official one and only OVERLAY=1 puts a locally built tag there. The
    pin parser sees the reference, so a recipe read from the repo has to follow
    it or it reports "$IMAGE" as the image."""
    if isinstance(value, str) and value.startswith("$"):
        name = value[1:].strip("{}")
        return assigns.get(name, value)
    return value


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
    lane = "flash" if recipe_id.startswith("flash") else "27b"
    key = "27b-1m" if (lane == "27b" and context_mode == "1m") else lane
    text = templates[LANE_TEMPLATE[key]]
    if lane == "flash":
        pfx = {"flash-nvda": "FLASH_NVDA", "flash-uncensored": "FLASH_UNC"}.get(recipe_id, "FLASH")
        repo, rev = assigns[f"{pfx}_REPO"], assigns[f"{pfx}_REV"]
        image = _deref(assigns["FLASH_SERVE_IMAGE"], assigns)
        base = assigns.get("FLASH_IMAGE")
        # Since v1.8 the served image IS the pinned official one, so there is no
        # overlay to name unless this box installed with OVERLAY=1.
        overlay = "flash-sglang" if image != base else None
    else:
        pfx = {"stock": "STOCK", "uncensored": "UNC", "fp8": "FP8",
               "uncensored-fp8": "UNCFP8"}[recipe_id]
        repo, rev = assigns[f"{pfx}_REPO"], assigns[f"{pfx}_REV"]
        image = _deref(assigns["SERVE_IMAGE"], assigns)
        base = assigns.get("IMAGE")
        overlay = "dflash2" if image != base else None
    # The unit templates carry the KV cache choice as a placeholder because it is
    # per-target; substitute it the way install.sh does so an FP8 recipe shows the
    # flag that defines it instead of leaving a placeholder behind.
    kv = "--kv-cache-dtype fp8_e4m3 " if recipe_id in ("fp8", "uncensored-fp8") else ""
    # The flash lane's tier, its checkpoint's own flag pair and the two numbers
    # install.sh renders are placeholders in the template, so the profile has to
    # be read AFTER substitution or the tier's flags are invisible and the memory
    # fraction reads as a string.
    if recipe_id == "flash-nvda":
        quant_args = "--moe-runner-backend flashinfer_cutlass "
    elif lane == "flash":
        # flash and flash-uncensored are the same tree, so the same flags, and
        # their in-checkpoint MTP tensors are BF16 in an NVFP4 export
        quant_args = ("--quantization modelopt_fp4 "
                      "--speculative-draft-model-quantization unquant ")
    else:
        quant_args = ""
    tier = assigns.get("FLASH_TIER", "context")
    tier_args = (TIER_ARGS.get(tier) or TIER_ARGS["context"]) if lane == "flash" else ""
    # The reduced draft vocabulary is rendered as a whole line, and only when the
    # tier speculates, exactly as install.sh decides it. Leaving the placeholder
    # unsubstituted made every flash recipe report a drift against a launcher
    # that carries the flag (caught by the live HTTP smoke, 2026-09-08).
    map_size = assigns.get("SPEC_TOKEN_MAP_SIZE", "0")
    map_line = ""
    if lane == "flash" and "--speculative-algorithm" in tier_args and map_size not in ("0", ""):
        map_line = f"TIER+=(--speculative-token-map /out/token-map-{map_size}.pt)"
    mapping = {"__KV_CACHE_ARGS__": kv,
               "__MODEL__": repo, "__MODEL_REV_ARGS__": f"--revision {rev}",
               "__MODEL_REV__": rev, "__IMAGE__": image,
               "__FLASH_QUANT_ARGS__": quant_args,
               "__FLASH_TIER_ARGS__": tier_args,
               "__SPEC_TOKEN_MAP_LINE__": map_line,
               "__FLASH_MEM_FRACTION__": assigns.get("FLASH_MEM_FRACTION", "0.85"),
               "__PLE_RSS_BUDGET_GB__": assigns.get("PLE_RSS_BUDGET_GB", "8"),
               "__DRAFT2_REV__": assigns.get("DRAFT2_REV", "__DRAFT2_REV__"),
               "__DRAFT_REV__": assigns.get("DRAFT_REV", "__DRAFT_REV__")}
    rendered = _subst(text, mapping)
    left = sorted(set(re.findall(r"__[A-Z][A-Z0-9_]*__", rendered))) 
    unexpected = [ph for ph in left if ph not in HOST_PLACEHOLDERS]
    if unexpected:
        raise KeyError(f"builtin({recipe_id!r}) left {unexpected} unsubstituted: a "
                       f"placeholder that carries a serving flag must be rendered here, "
                       f"or every box reports a drift it does not have (this happened to "
                       f"the reduced draft vocabulary, caught by the live HTTP smoke)")
    prof = profile_from_text(rendered)
    prof = dict(prof)
    prof["serve"] = dict(prof["serve"])
    if kv:
        prof["serve"]["kv_cache_dtype"] = "fp8_e4m3"
    elif lane == "27b":
        prof["serve"].pop("kv_cache_dtype", None)
    drafter = {k: _subst(v, mapping) for k, v in prof["drafter"].items()}
    env = {k: _subst(v, mapping) for k, v in prof["env"].items()}
    return {
        "id": recipe_id, "lane": lane, "builtin": True,
        "engine": {"family": "sglang", "image": image, "base_image": base, "overlay": overlay},
        "model": {"repo": repo, "revision": rev},
        "drafter": drafter, "serve": dict(prof["serve"]),
        "switches": dict(prof["switches"]), "env": env,
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
                if rng is None:
                    # Free-form string (a path). Shape only: no whitespace, no
                    # shell metacharacters, because it is rendered into a
                    # launcher, and bounded so a recipe cannot carry a payload.
                    if (not isinstance(v, str) or not v or len(v) > 512
                            or re.search(r"[\s;&|`$<>(){}\\'\"]", v)):
                        errs.append(f"serve.{k}: a plain path, no whitespace or shell metacharacters")
                elif v not in rng:
                    errs.append(f"serve.{k}: one of {list(rng)}")
            elif isinstance(v, bool) or not isinstance(v, (int, float)):
                errs.append(f"serve.{k}: number expected")
            elif not rng[0] <= v <= rng[1]:
                errs.append(f"serve.{k}: {rng[0]} to {rng[1]}")
        if "context_length" not in serve:
            errs.append("serve.context_length: required")
    switches = recipe.get("switches", {})
    if not isinstance(switches, dict):
        errs.append("switches: object of flag -> true/false")
    else:
        for k, v in switches.items():
            if k not in SWITCHES:
                errs.append(f"switches.{k}: unknown switch (allowed: {list(SWITCHES)})")
            elif not isinstance(v, bool):
                errs.append(f"switches.{k}: true or false")
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
    skeys = set(recipe.get("switches", {})) | set(installed.get("switches", {}))
    for k in sorted(skeys):
        cmp(f"switch.{k}", recipe.get("switches", {}).get(k, False),
            installed.get("switches", {}).get(k, False))
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
