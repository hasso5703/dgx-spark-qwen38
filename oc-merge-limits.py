#!/usr/bin/env python3
"""Merge this repo's opencode limits into an EXISTING opencode.json.

Usage: oc-merge-limits.py <target opencode.json> <provider> <model id> <context> <output> [--keep-lower]
       oc-merge-limits.py <target opencode.json> --compaction <preserve_recent_tokens>
       oc-merge-limits.py <target opencode.json> <provider> <model id> --add-variant <level>
       oc-merge-limits.py <target opencode.json> --autoupdate notify
       oc-merge-limits.py <target opencode.json> --add-providers <generated opencode.json>
       oc-merge-limits.py <target opencode.json> --remove-providers <API key file>

Only the "limit" object of the named provider/model is rewritten, in place,
by targeted text substitution: comments, ordering and the user's other
providers are left untouched. A dated backup is written first. Exit 0 with
"unchanged" when the limits already match, 3 when the provider/model is not
in the file (nothing to merge), 1 on a malformed file.

--keep-lower leaves a pair already at or below <context>/<output> as it is: on a 1m
install the numbers given are the table's bounds, and the pair a fit to the engine's
pool wrote under them (the end of the install, or the cockpit) is the one to keep.
Rewriting the bounds over it made every 1m run write twice, the table's pair here and
the fit's at the end, with a backup and an opencode restart for each: 79 backups in the
reference box's ~/.config/opencode by 2026-09-24, 19 of them written on the 23rd (found in
review).

--compaction writes the top-level "compaction" object instead. That block is not
per-model, which is why it has its own mode: `preserve_recent_tokens` is how much
of the recent conversation survives a compaction verbatim, and opencode's own
default clamps it at 15,000 tokens whatever the window. On a 262K lane that
throws away the window the moment compaction fires, so the installed value comes
from this repo's table instead. `prune` goes with it: opencode then clears stale
tool output (beyond the most recent 40,000 tokens of it, and never in the last
two turns) instead of summarising the whole conversation, which is the cheaper
way to stay under the ceiling.

--autoupdate writes the top-level "autoupdate" key. Left unset, opencode installs its
own patch releases (read out of its 1.18.27 binary: only `false`, `"notify"` or a
minor/major release stop it), so the version install.sh pins drifts on its own.
"notify" keeps the announcement and drops the self-install. A user's explicit
`false` is stricter and is kept.
"""
import json
import os
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


def _verify_compaction(path: str, keep: int) -> str:
    """'' when the file on disk really declares this compaction block."""
    try:
        doc = json.loads(re.sub(r"^\s*//.*$", "", open(path).read(), flags=re.M))
    except (OSError, json.JSONDecodeError) as e:
        return f"unreadable after write: {e}"
    c = doc.get("compaction")
    if not isinstance(c, dict):
        return "compaction block missing"
    if c.get("preserve_recent_tokens") != keep:
        return f"preserve_recent_tokens is {c.get('preserve_recent_tokens')!r}, expected {keep}"
    if c.get("prune") is not True:
        return f"prune is {c.get('prune')!r}, expected true"
    return ""


def merge_compaction(path: str, keep: int) -> int:
    """Write the top-level compaction block, keeping everything else byte for byte."""
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
    cur = doc.get("compaction")
    if isinstance(cur, dict) and cur.get("preserve_recent_tokens") == keep and cur.get("prune") is True:
        print(f"compaction already preserve_recent_tokens={keep}, prune=true: unchanged")
        return 0
    if isinstance(cur, dict):
        j = text.index('"compaction"')
        k = text.index("}", j)
        block = _set_key(text[j:k + 1], "preserve_recent_tokens", keep)
        block = re.sub(r'"prune"\s*:\s*(true|false)', '"prune": true', block)
        if '"prune"' not in block:
            block = re.sub(r"\s*\}\s*$", ', "prune": true}', block, count=1)
        new_text = text[:j] + block + text[k + 1:]
    else:
        i = text.index("{")          # the document's own opening brace
        new_text = (text[:i + 1]
                    + f'\n  "compaction": {{"preserve_recent_tokens": {keep}, "prune": true}},'
                    + text[i + 1:])
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)
    with open(path, "w") as f:
        f.write(new_text)
    bad = _verify_compaction(path, keep)
    if bad:
        shutil.copy2(backup, path)
        print(f"{path}: refused, the compaction block was not written as intended ({bad}); "
              f"restored from {backup}.")
        return 1
    print(f"compaction: preserve_recent_tokens={keep}, prune=true (backup {backup})")
    return 0


def merge_autoupdate(path: str, value: str) -> int:
    """Write the top-level autoupdate key, keeping everything else byte for byte."""
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
    cur = doc.get("autoupdate", None)
    if cur == value:
        print(f"autoupdate already {value!r}: unchanged")
        return 0
    if cur is False:
        print("autoupdate is false (no update at all), which already holds the pin: unchanged")
        return 0
    if "autoupdate" in doc:
        new_text, n = re.subn(r'"autoupdate"\s*:\s*(true|false|"[^"]*")', f'"autoupdate": {json.dumps(value)}',
                              text, count=1)
        if n != 1:
            print(f"{path}: autoupdate is set in a form this script does not rewrite; unchanged")
            return 1
    else:
        i = text.index("{")          # the document's own opening brace
        new_text = text[:i + 1] + f'\n  "autoupdate": {json.dumps(value)},' + text[i + 1:]
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)
    with open(path, "w") as f:
        f.write(new_text)
    try:
        ok = json.loads(re.sub(r"^\s*//.*$", "", open(path).read(), flags=re.M)).get("autoupdate") == value
    except (OSError, json.JSONDecodeError):
        ok = False
    if not ok:
        shutil.copy2(backup, path)
        print(f"{path}: refused, autoupdate was not written as intended; restored from {backup}.")
        return 1
    print(f"autoupdate: {value!r}, was {cur!r} (backup {backup})")
    return 0


def _verify_variant(path, provider, model, level) -> str:
    try:
        doc = json.loads(re.sub(r"^\s*//.*$", "", open(path).read(), flags=re.M))
    except json.JSONDecodeError as e:
        return f"file no longer parses: {e}"
    v = (((doc.get("provider") or {}).get(provider) or {}).get("models", {})
         .get(model, {}).get("variants") or {})
    got = (v.get(level) or {}).get("chat_template_kwargs", {}).get("reasoning_effort")
    return "" if got == level else f"variant {level} reads back as {got!r}"


def add_variant(path: str, provider: str, model: str, level: str) -> int:
    """Put a reasoning-effort variant into an existing opencode.json.

    install.sh writes a complete config into CONFIG_DIR, but the file opencode
    actually reads is the operator's own, and only the limits were ever merged
    into it. A level added to the generated artifact therefore never reached the
    picker: "lean" existed everywhere except where it could be selected.
    """
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
    mdl = ((doc.get("provider") or {}).get(provider) or {}).get("models", {}).get(model)
    if mdl is None:
        print(f"{provider}/{model} not in {path}: nothing to merge")
        return 3
    if (mdl.get("variants") or {}).get(level):
        print(f"{provider}/{model} already offers the {level} variant: unchanged")
        return 0
    entry = f'"{level}": {{"chat_template_kwargs": {{"reasoning_effort": "{level}"}}}}'
    i = text.index(f'"{provider}"')
    i = text.index(f'"{model}"', i)
    if mdl.get("variants") is not None:
        j = text.index('"variants"', i)
        k = text.index("{", text.index(":", j))          # opening brace of the object
        new_text = text[:k + 1] + "\n              " + entry + "," + text[k + 1:]
    else:
        j = text.index('"limit"', i)
        k = text.index("}", j)                            # end of the limit object
        new_text = text[:k + 1] + ',\n            "variants": {' + entry + "}" + text[k + 1:]
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)
    with open(path, "w") as f:
        f.write(new_text)
    bad = _verify_variant(path, provider, model, level)
    if bad:
        shutil.copy2(backup, path)
        print(f"{path}: refused, {bad}; restored from {backup}.")
        return 1
    print(f"{provider}/{model}: {level} variant added (backup {backup})")
    return 0


OURS = ("qwen38", "flashnext")


def _added_ok(text: str, before: dict, blocks: dict) -> str:
    """'' when text is the document `before` plus exactly `blocks` in its provider object."""
    try:
        doc = json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.M))
    except json.JSONDecodeError as e:
        return f"file no longer parses: {e}"
    prov = doc.get("provider")
    if not isinstance(prov, dict):
        return "provider object vanished"
    for name, block in blocks.items():
        if prov.get(name) != block:
            return f"provider {name} reads back differently"
    rest = dict(doc, provider={k: v for k, v in prov.items() if k not in blocks})
    return "" if rest == before else "something else in the file changed"


def add_providers(path: str, source: str) -> int:
    """Add this repo's providers that `source` has and the config at `path` lacks.

    install.sh gives a box with no opencode config its own, and never overwrites one
    that exists. A box that installed the 27B first and the flash lane later kept the
    first install's copy, with no flashnext provider: the limits merge found nothing
    to merge and the default model could not follow the lane. The served model answers
    under any name, so nothing failed; opencode sent the 27B's limits and label to the
    flash lane. Only a config that already has one of this repo's providers gains the
    others: a config with none is the user's own, and install.sh says what to merge.
    """
    try:
        text = open(path).read()
    except OSError as e:
        print(f"cannot read {path}: {e}")
        return 1
    try:
        doc = json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.M))
        src = json.loads(open(source).read())
    except (OSError, json.JSONDecodeError) as e:
        print(f"{path} or {source} is not valid JSON(C): {e}")
        return 1
    have = doc.get("provider") if isinstance(doc.get("provider"), dict) else {}
    if not any(p in have for p in OURS):
        print(f"none of this repo's providers in {path}: nothing to add to")
        return 3
    blocks = {p: b for p, b in (src.get("provider") or {}).items() if p in OURS and p not in have}
    if not blocks:
        print(f"{path} already lists this box's providers: unchanged")
        return 0
    entry = "".join(f"\n    {json.dumps(p)}: " + json.dumps(b, indent=2).replace("\n", "\n    ") + ","
                    for p, b in blocks.items())
    # The provider object's opening brace is the first candidate that yields exactly
    # the intended document: a commented-out line or a nested key cannot take it.
    new_text = None
    for m in re.finditer(r'"provider"\s*:\s*\{', text):
        tail = text[m.end():]
        cand = text[:m.end()] + entry + ("" if tail[:1].isspace() else "\n    ") + tail
        if not _added_ok(cand, doc, blocks):
            new_text = cand
            break
    if new_text is None:
        print(f"{path}: refused, found no place for the {', '.join(blocks)} provider; "
              f"copy it by hand from {source}")
        return 1
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)
    with open(path, "w") as f:
        f.write(new_text)
    bad = _added_ok(open(path).read(), doc, blocks)
    if bad:
        shutil.copy2(backup, path)
        print(f"{path}: refused, {bad}; restored from {backup}.")
        return 1
    print(f"{', '.join(blocks)} provider added to {path}, for the lane installed since (backup {backup})")
    return 0


def _skip_ws(text: str, i: int) -> int:
    """Index of the next character that is neither whitespace nor in a // comment."""
    while i < len(text):
        if text[i].isspace():
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = len(text) if j < 0 else j + 1
        else:
            break
    return i


def _string_end(text: str, i: int) -> int:
    """Index after the JSON string whose opening quote is at i."""
    i += 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text[i] == '"':
            return i + 1
        else:
            i += 1
    raise ValueError("unterminated string")


def _value_end(text: str, i: int) -> int:
    """Index after the JSON value that starts at i."""
    if text[i] == '"':
        return _string_end(text, i)
    if text[i] not in "{[":
        return re.compile(r"[^,}\]\s]+").match(text, i).end()
    depth = 0
    while i < len(text):
        if text[i] == '"':
            i = _string_end(text, i)
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = len(text) if j < 0 else j + 1
            continue
        if text[i] in "{[":
            depth += 1
        elif text[i] in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced brackets")


def _member_span(text: str, key_at: int):
    """(start, end) of the member whose key opens at key_at, with one separating comma,
    so that text[:start] + text[end:] is still JSON(C); a member alone on its lines
    takes those lines with it."""
    try:
        i = _skip_ws(text, _string_end(text, key_at))
        if text[i] != ":":
            return None
        end = _value_end(text, _skip_ws(text, i + 1))
    except (ValueError, IndexError, AttributeError):
        return None
    start = key_at
    j = _skip_ws(text, end)
    if j < len(text) and text[j] == ",":
        end = j + 1
    else:
        k = key_at - 1
        while k >= 0 and text[k].isspace():
            k -= 1
        if k >= 0 and text[k] == ",":
            start = k
    s = start
    while s > 0 and text[s - 1] in " \t":
        s -= 1
    if (s == 0 or text[s - 1] == "\n") and text[end:end + 1] == "\n":
        start, end = s, end + 1
    return start, end


def _without(text: str, key: str, want: dict):
    """text without the member `key`: the first candidate whose removal parses to want."""
    for m in re.finditer(re.escape(json.dumps(key)) + r"\s*:", text):
        span = _member_span(text, m.start())
        if span is None:
            continue
        cand = text[:span[0]] + text[span[1]:]
        try:
            if json.loads(re.sub(r"^\s*//.*$", "", cand, flags=re.M)) == want:
                return cand
        except json.JSONDecodeError:
            pass
    return None


def remove_providers(path: str, key_file: str) -> int:
    """Take out of an opencode.json the providers that read this box's API key file.

    uninstall.sh --yes deletes that file, and opencode then refuses to start at all,
    every provider included, while a {file:} reference points at a file that is gone
    ("bad file reference", opencode 1.18.32). So the providers that read it go first,
    with model and small_model when they name one of them. A file left with nothing
    but what install.sh writes ($schema, autoupdate, compaction, no provider) was this
    repo's, and is moved to a backup instead of being kept empty.
    """
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
    refs = {f"{{file:{key_file}}}"}
    home = os.path.expanduser("~")
    if key_file.startswith(home + "/"):
        refs.add("{file:~" + key_file[len(home):] + "}")
    prov = doc.get("provider") if isinstance(doc.get("provider"), dict) else {}
    gone = [p for p, b in prov.items()
            if isinstance(b, dict) and (b.get("options") or {}).get("apiKey") in refs]
    if not gone:
        print(f"no provider in {path} reads {key_file}: unchanged")
        return 0
    steps = []                      # (key, the document once it is gone), in order
    cur = json.loads(json.dumps(doc))
    for p in gone:
        del cur["provider"][p]
        steps.append((p, json.loads(json.dumps(cur))))
    for k in ("model", "small_model"):
        if isinstance(cur.get(k), str) and cur[k].split("/", 1)[0] in gone:
            del cur[k]
            steps.append((k, json.loads(json.dumps(cur))))
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    if set(cur) <= {"$schema", "autoupdate", "compaction", "provider"} and not cur.get("provider"):
        shutil.move(path, backup)
        print(f"{path} held only this box's providers ({', '.join(gone)}): moved to {backup}")
        return 0
    new_text = text
    for key, want in steps:
        new_text = _without(new_text, key, want)
        if new_text is None:
            print(f"{path}: refused, could not take {key!r} out by an edit that leaves the rest "
                  f"as it is; remove the {', '.join(gone)} provider by hand")
            return 1
    shutil.copy2(path, backup)
    with open(path, "w") as f:
        f.write(new_text)
    try:
        ok = json.loads(re.sub(r"^\s*//.*$", "", open(path).read(), flags=re.M)) == cur
    except (OSError, json.JSONDecodeError):
        ok = False
    if not ok:
        shutil.copy2(backup, path)
        print(f"{path}: refused, the file did not read back as intended; restored from {backup}.")
        return 1
    print(f"removed the {', '.join(gone)} provider from {path}: it read {key_file} (backup {backup})")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 4 and argv[2] == "--compaction":
        return merge_compaction(argv[1], int(argv[3]))
    if len(argv) == 4 and argv[2] == "--autoupdate":
        return merge_autoupdate(argv[1], argv[3])
    if len(argv) == 4 and argv[2] == "--add-providers":
        return add_providers(argv[1], argv[3])
    if len(argv) == 4 and argv[2] == "--remove-providers":
        return remove_providers(argv[1], argv[3])
    if len(argv) == 6 and argv[4] == "--add-variant":
        return add_variant(argv[1], argv[2], argv[3], argv[5])
    keep_lower = argv[6:] == ["--keep-lower"]
    if keep_lower:
        argv = argv[:6]
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
    have_ctx, have_out = limit.get("context"), limit.get("output")
    if (keep_lower and isinstance(have_ctx, int) and isinstance(have_out, int)
            and 0 < have_ctx <= ctx and 0 < have_out <= out and limit.get("input") in (None, have_ctx)):
        print(f"{provider}/{model} limits {have_ctx}/{have_out} are within {ctx}/{out}: unchanged "
              f"(a fit to the pool, kept)")
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
