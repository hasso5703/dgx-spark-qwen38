#!/usr/bin/env python3
"""Merge this repo's opencode limits into an EXISTING opencode.json.

Usage: oc-merge-limits.py <target opencode.json> <provider> <model id> <context> <output>

Only the "limit" object of the named provider/model is rewritten, in place,
by targeted text substitution: comments, ordering and the user's other
providers are left untouched. A dated backup is written first. Exit 0 with
"unchanged" when the limits already match, 3 when the provider/model is not
in the file (nothing to merge), 1 on a malformed file.
"""
import json
import re
import shutil
import sys
import time


def _set_key(block: str, key: str, val: int) -> str:
    """Set "key": val inside one JSON object's text, inserting it when absent."""
    new, n = re.subn(rf'"{key}"\s*:\s*\d+', f'"{key}": {val}', block)
    if n:
        return new
    # The block carries its own '"limit":' prefix, so "is this object empty" has
    # to look at the braces, not at whether a colon appears anywhere in the text.
    if re.search(r"\{\s*\}\s*$", block):
        return re.sub(r"\{\s*\}\s*$", f'{{"{key}": {val}}}', block, count=1)
    return re.sub(r"\s*\}\s*$", f', "{key}": {val}}}', block, count=1)


def _verify(path: str, provider: str, model: str, ctx: int, out: int) -> str:
    """'' when the file on disk really declares these limits, else what is wrong."""
    try:
        doc = json.loads(re.sub(r"^\s*//.*$", "", open(path).read(), flags=re.M))
    except (OSError, json.JSONDecodeError) as e:
        return f"unreadable after write: {e}"
    lim = ((doc.get("provider") or {}).get(provider) or {}).get("models", {}).get(model, {}).get("limit")
    if not isinstance(lim, dict):
        return "limit object vanished"
    for key, want in (("context", ctx), ("input", ctx), ("output", out)):
        if lim.get(key) != want:
            return f"{key} is {lim.get(key)!r}, expected {want}"
    return ""


def main(argv: list[str]) -> int:
    if len(argv) != 6:
        print(__doc__)
        return 2
    path, provider, model, ctx, out = argv[1], argv[2], argv[3], int(argv[4]), int(argv[5])
    try:
        text = open(path).read()
    except OSError as e:
        print(f"cannot read {path}: {e}")
        return 1
    try:
        doc = json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.M))
    except json.JSONDecodeError as e:
        print(f"{path} is not valid JSON(C): {e}")
        return 1
    limit = ((doc.get("provider") or {}).get(provider) or {}).get("models", {}).get(model, {}).get("limit")
    if limit is None:
        print(f"{provider}/{model} not in {path}: nothing to merge")
        return 3
    if limit.get("context") == ctx and limit.get("input") == ctx and limit.get("output") == out:
        print(f"{provider}/{model} limits already {ctx}/{out}: unchanged")
        return 0
    # locate this model's "limit" object in the raw text (provider -> model -> limit)
    i = text.index(f'"{provider}"')
    i = text.index(f'"{model}"', i)
    j = text.index('"limit"', i)
    k = text.index("}", j)
    block = text[j:k + 1]
    new = block
    for key, val in (("context", ctx), ("input", ctx), ("output", out)):
        new = _set_key(new, key, val)
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)
    with open(path, "w") as f:
        f.write(text[:j] + new + text[k + 1:])
    # Never report a write we did not make. A regex that matches nothing used to
    # leave the key untouched and still print the new value: a limit block
    # without "output" came out with only "context" rewritten, and opencode then
    # ran on its own default cap, which is the failure this tool exists to stop.
    bad = _verify(path, provider, model, ctx, out)
    if bad:
        shutil.copy2(backup, path)
        print(f"{path}: refused, the limit block was not written as intended ({bad}); "
              f"restored from {backup}. Edit the block by hand or delete it and re-run.")
        return 1
    print(f"{provider}/{model} limits: {limit.get('context')}/{limit.get('output')} -> {ctx}/{out} "
          f"(backup {backup})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
