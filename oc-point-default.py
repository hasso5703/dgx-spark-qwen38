#!/usr/bin/env python3
"""Point an opencode.json at the served lane: default model + picker name.

Usage: oc-point-default.py <config.json> <lane: 27b|flash> <choice> <window>
<window> is the served context window in tokens (1010000 or 262144, empty if
unknown): it sets the "(local, 1M|262K)" suffix on the served entry's name.

The per-target picker names live here and ONLY here: install.sh (generated
artifact and the config opencode actually reads) and switch-model.sh (both
configs) all call this, so a new target cannot update one spelling and
forget the other two. A config without the lane's provider is left untouched
with a NOTE (it predates the target: re-run ./install.sh to regenerate it).

Exit 0 on success and on a missing provider (nothing to point at); exit 2 on
usage errors, exit 3 when the file is not usable JSON.
"""
import json
import sys

# The served-model-names are shared per lane (three 27B targets serve
# qwen3.8-27b, three flash targets serve qwen3.8-flash-next), so opencode
# shows one entry whatever is loaded behind it. Its label has to say WHICH
# checkpoint, otherwise the picker still reads "NVFP4" while the box serves
# FP8 (field case 2026-08-31).
LABEL = {"stock": "Qwen3.8-27B NVFP4 + DFlash2",
         "uncensored": "Qwen3.8-27B NVFP4 abliterated + DFlash2",
         "fp8": "Qwen3.8-27B FP8 official + DFlash2",
         "uncensored-fp8": "Qwen3.8-27B FP8 abliterated + DFlash2",
         "flash": "Qwen3.8-Flash-Next NVFP4 + MTP",
         # The three flash targets share one served-model-name too, so the same
         # rule applies: without an entry here the picker keeps saying NVFP4
         # while the box serves the abliterated build.
         "flash-uncensored": "Qwen3.8-Flash-Next NVFP4 abliterated + MTP",
         "flash-nvda": "Qwen3.8-Flash-Next NVFP4 NVIDIA export + MTP"}


def label_for(choice: str, window: str) -> str | None:
    base = LABEL.get(choice)
    if not base:
        return None
    try:
        ctx = int(window or 0)
    except ValueError:
        ctx = 0
    if ctx >= 1_000_000:
        suffix = " (local, 1M)"
    elif ctx:
        suffix = f" (local, {ctx // 1000}K)"
    else:
        suffix = " (local)"
    return base + suffix


def main() -> None:
    if len(sys.argv) != 5:
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(2)
    path, lane, choice, window = sys.argv[1:5]
    want = "flashnext/qwen3.8-flash-next" if lane == "flash" else "qwen38/qwen3.8-27b"
    prov, mid = want.split("/")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        print(f"could not read {path}: {e}")
        sys.exit(3)
    if prov not in cfg.get("provider", {}):
        print(f"NOTE: provider '{prov}' is not in {path} (config predates this target);")
        print("      re-run ./install.sh once to regenerate it, keeping your choices.")
        return
    cfg["model"] = want
    cfg["small_model"] = want
    shown = ""
    name = label_for(choice, window)
    if name:
        m = cfg["provider"][prov].get("models", {}).get(mid)
        if isinstance(m, dict):
            m["name"] = name
            shown = f", shown as {name!r}"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"opencode default model -> {want} ({path}){shown}")


if __name__ == "__main__":
    main()
