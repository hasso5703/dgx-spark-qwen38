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

The file is edited the way oc-merge-limits.py edits it, whose reader and writer
this uses: only "model", "small_model" and the served entry's "name" change, by a
targeted edit, after a backup, in one step. And "model" and "small_model" follow the
lane only when they are unset or already name one of this box's providers: a default
the user pointed elsewhere (another provider, a hosted model) is theirs, and is kept.
The whole file used to be rewritten by json.dump at every install and switch, with no
backup and in place: every non-ASCII character came back as an escape, every comment
made the file unreadable to it (exit 3, and the default no longer followed the lane),
and any other default model was replaced (found in review, 2026-09-24).

Usage: oc-point-default.py --label <choice> <window> prints the served entry's name,
for install.sh's generator to write the same one.

Exit 0 on success and on a missing provider (nothing to point at); exit 1 when the edit
could not be written; exit 2 on usage errors, exit 3 when the file is not usable JSON(C).
"""
import copy
import importlib.util
import json
import os
import sys

_spec = importlib.util.spec_from_file_location(
    "oc_merge_limits", os.path.join(os.path.dirname(os.path.abspath(__file__)), "oc-merge-limits.py"))
ocm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ocm)

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
    if len(sys.argv) == 4 and sys.argv[1] == "--label":
        print(label_for(sys.argv[2], sys.argv[3]) or "")
        return
    if len(sys.argv) != 5:
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(2)
    path, lane, choice, window = sys.argv[1:5]
    want = "flashnext/qwen3.8-flash-next" if lane == "flash" else "qwen38/qwen3.8-27b"
    prov, mid = want.split("/")
    try:
        text = ocm.read(path)
        cfg = ocm.load(text)
        if not isinstance(cfg, dict):
            raise ValueError("the document is not an object")
    except (OSError, ValueError) as e:
        print(f"could not read {path}: {e}")
        sys.exit(3)
    if not isinstance(cfg.get("provider"), dict) or prov not in cfg["provider"]:
        print(f"NOTE: provider '{prov}' is not in {path} (config predates this target);")
        print("      re-run ./install.sh once to regenerate it, keeping your choices.")
        return
    new = copy.deepcopy(cfg)
    new_text = text
    kept = []
    for key in ("model", "small_model"):
        cur = cfg.get(key)
        if cur == want:
            continue
        if isinstance(cur, str) and cur.split("/", 1)[0] not in ocm.OURS:
            kept.append((key, cur))
            continue
        if cur is None and key == "small_model" and new.get("model") != want:
            continue            # unset under a default kept elsewhere: opencode derives it from that one
        new[key] = want
        new_text = ocm.set_member(new_text, [], key, json.dumps(want))
    shown = ""
    name = label_for(choice, window)
    m = ocm._dig(cfg, "provider", prov, "models", mid)
    if name and isinstance(m, dict):
        shown = f", shown as {name!r}"
        if m.get("name") != name:
            new["provider"][prov]["models"][mid]["name"] = name
            new_text = ocm.set_member(new_text, ["provider", prov, "models", mid], "name", json.dumps(name))
    moved = cfg.get("model") != new.get("model") or cfg.get("small_model") != new.get("small_model")
    if new_text == text:
        print(f"opencode default model {'left as it is' if kept else 'already ' + want} "
              f"({path}){shown}: unchanged")
    else:
        saved, bad = ocm.commit(path, new_text, new)
        if bad:
            print(f"could not update {path}: {bad}")
            sys.exit(1)
        what = f"opencode default model -> {want}" if moved else f"opencode entry {want}"
        print(f"{what} ({path}){shown} (backup {saved})")
    for key, cur in kept:
        print(f"NOTE: {key} in {path} is {cur}, not this box's: left as it is. This box serves")
        print(f"      {want}; pick it in opencode, or set \"{key}\" to it yourself.")


if __name__ == "__main__":
    main()
