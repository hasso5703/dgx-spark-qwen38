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

The file is read the way opencode reads it: jsonc-parser with allowTrailingComma (read in
the 1.18.32 binary), so // and /* */ comments anywhere outside a string and a comma before
a closing brace or bracket. Every edit finds its member by walking the document from the
root, lands only if the result reads back as exactly the intended document, and replaces
the file in one step after a backup under a name no earlier backup has.
"""
import copy
import json
import os
import re
import shutil
import sys
import tempfile
import time



# --- reading and editing the file as opencode reads it --------------------------------
# Only whole-line // comments used to be understood here, so a config with a comment at the
# end of a line, a /* */ block or a trailing comma, all of which opencode accepts, made every
# edit refuse, silently behind install.sh's `|| true`: the limits stayed where they were, and
# --remove-providers left in place a provider reading the key file uninstall.sh deletes,
# after which opencode does not start. And an edit went to the first place the key appeared
# in the text, a commented-out block or another object's member of the same name included
# (found in review, 2026-09-24).

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


def blank(text: str) -> str:
    """text with its comments and trailing commas turned into spaces, line breaks kept: the
    same length, so an offset in one is the same place in the other."""
    out = list(text)
    i, n, comma = 0, len(text), None
    while i < n:
        c = text[i]
        if c == '"':
            i = _string_end(text, i)
            comma = None
            continue
        if c == "/" and text[i + 1:i + 2] in ("/", "*"):
            if text[i + 1] == "/":
                j = text.find("\n", i)
                j = n if j < 0 else j
            else:
                j = text.find("*/", i + 2)
                if j < 0:
                    raise ValueError(f"unterminated comment at char {i}")
                j += 2
            for k in range(i, j):
                if text[k] not in "\r\n":
                    out[k] = " "
            i = j
            continue
        if c == ",":
            comma = i
        elif c in "}]":
            if comma is not None:
                out[comma] = " "
            comma = None
        elif not c.isspace():
            comma = None
        i += 1
    return "".join(out)


def load(text: str):
    """The document a JSON(C) text holds, as opencode reads it; ValueError when it is not one."""
    return json.loads(blank(text))


def read(path: str) -> str:
    """The file as it is, line endings included."""
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _dig(doc, *keys):
    """doc[k1][k2]..., or None when a key is missing or what holds it is not an object."""
    for k in keys:
        if not isinstance(doc, dict):
            return None
        doc = doc.get(k)
    return doc


def _skip_ws(clean: str, i: int) -> int:
    """Index of the next character that is not whitespace, in a blank()ed text."""
    while i < len(clean) and clean[i].isspace():
        i += 1
    return i


def _value_end(clean: str, i: int) -> int:
    """Index after the JSON value that starts at i, in a blank()ed text."""
    if clean[i] == '"':
        return _string_end(clean, i)
    if clean[i] not in "{[":
        return re.compile(r"[^,}\]\s]+").match(clean, i).end()
    depth = 0
    while i < len(clean):
        if clean[i] == '"':
            i = _string_end(clean, i)
            continue
        if clean[i] in "{[":
            depth += 1
        elif clean[i] in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced brackets")


def _members(clean: str, i: int):
    """(key, key_at, value_start, value_end) of each member of the object whose opening
    brace is at i, in a blank()ed text that parses."""
    out = []
    j = _skip_ws(clean, i + 1)
    while clean[j] != "}":
        kend = _string_end(clean, j)
        v = _skip_ws(clean, _skip_ws(clean, kend) + 1)
        vend = _value_end(clean, v)
        out.append((json.loads(clean[j:kend]), j, v, vend))
        j = _skip_ws(clean, vend)
        if clean[j] == ",":
            j = _skip_ws(clean, j + 1)
    return out


def _locate(clean: str, path):
    """(key_at, value_start, value_end) of the member at `path`, a list of keys from the
    document's root (key_at is None for the root itself), or None when a key is missing or
    what holds it is not an object. Of two equal keys, the last, as json.loads reads them."""
    v = _skip_ws(clean, 0)
    span = (None, v, _value_end(clean, v))
    for key in path:
        if clean[span[1]] != "{":
            return None
        hits = [m for m in _members(clean, span[1]) if m[0] == key]
        if not hits:
            return None
        span = hits[-1][1:]
    return span


def set_member(text: str, path, key: str, value: str) -> str:
    """text with the member `key` of the object at `path` set to the JSON text `value`: its
    value replaced where it stands, or the member put first in the object, which is valid
    whatever follows (an empty object gets no comma)."""
    clean = blank(text)
    obj = _locate(clean, path)
    if obj is None or clean[obj[1]] != "{":
        raise ValueError(f"no object at {'/'.join(path) or 'the root'}")
    o = obj[1]
    hits = [m for m in _members(clean, o) if m[0] == key]
    if hits:
        _, _, vs, ve = hits[-1]
        return text[:vs] + value + text[ve:]
    nl = "\r\n" if "\r\n" in text else "\n"
    first = _skip_ws(clean, o + 1)
    gap = clean[o + 1:first]
    indent = gap[gap.rindex("\n") + 1:] if "\n" in gap else ""
    member = (json.dumps(key) + ": " + value).replace("\n", nl + indent)
    if clean[first] == "}":
        return text[:o + 1] + member + text[o + 1:]
    if "\n" not in gap:
        return text[:o + 1] + member + ", " + text[o + 1:]
    return text[:o + 1] + nl + indent + member + "," + text[o + 1:]


def _member_span(clean: str, key_at: int):
    """(start, end) of the member whose key opens at key_at, with one separating comma,
    so that text[:start] + text[end:] is still JSON(C); a member alone on its lines
    takes those lines with it. On a blank()ed text, so a comment line between the member
    and the comma before it hides nothing."""
    try:
        i = _skip_ws(clean, _string_end(clean, key_at))
        if clean[i] != ":":
            return None
        end = _value_end(clean, _skip_ws(clean, i + 1))
    except (ValueError, IndexError, AttributeError):
        return None
    start = key_at
    j = _skip_ws(clean, end)
    if j < len(clean) and clean[j] == ",":
        end = j + 1
    else:
        k = key_at - 1
        while k >= 0 and clean[k].isspace():
            k -= 1
        if k >= 0 and clean[k] == ",":
            start = k
    s = start
    while s > 0 and clean[s - 1] in " \t":
        s -= 1
    if s == 0 or clean[s - 1] == "\n":
        for eol in ("\r\n", "\n"):
            if clean[end:end + len(eol)] == eol:
                start, end = s, end + len(eol)
                break
    return start, end


def remove_member(text: str, path) -> str:
    """text without the member at `path`."""
    clean = blank(text)
    m = _locate(clean, path)
    span = None if m is None or m[0] is None else _member_span(clean, m[0])
    if span is None:
        raise ValueError(f"no member at {'/'.join(path)}")
    return text[:span[0]] + text[span[1]:]


def backup(path: str) -> str:
    """A copy of path under a name no earlier backup has. install.sh edits one config up to
    five times within a second, and with a name to the second each backup replaced the one
    before: the last edit's was the only one left, and it no longer held the original
    (found in review, 2026-09-24)."""
    base = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    for n in range(1000):
        name = base if n == 0 else f"{base}-{n}"
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as dst, open(path, "rb") as src:
            shutil.copyfileobj(src, dst)
        shutil.copystat(path, name)
        return name
    raise OSError(f"no free backup name next to {path}")


def replace(path: str, new_text: str) -> None:
    """Put new_text in path in one step: a full disk or a crash leaves the old file, never a
    cut one, and a path that is a symlink stays one."""
    real = os.path.realpath(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real), prefix=f".{os.path.basename(real)}.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)
            f.flush()
            os.fsync(f.fileno())
        shutil.copymode(real, tmp)
        os.replace(tmp, real)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def commit(path: str, new_text: str, want) -> tuple[str, str]:
    """(backup, "") once path holds new_text, which has to read back as exactly `want`;
    ("", why) with the file untouched otherwise."""
    try:
        if load(new_text) != want:
            return "", "the edit does not read back as intended"
    except ValueError as e:
        return "", f"the edit does not parse: {e}"
    try:
        saved = backup(path)
        replace(path, new_text)
    except OSError as e:
        return "", f"could not write it: {e}"
    return saved, ""


def _open(path: str):
    """(text, document), or None once the reason it cannot be used is printed."""
    try:
        text = read(path)
    except (OSError, UnicodeDecodeError) as e:
        print(f"cannot read {path}: {e}")
        return None
    try:
        doc = load(text)
    except ValueError as e:
        print(f"{path} is not valid JSON(C): {e}")
        return None
    if not isinstance(doc, dict):
        print(f"{path} is not valid JSON(C): the document is not an object")
        return None
    return text, doc


def _reread(path: str):
    try:
        return load(read(path))
    except (OSError, ValueError) as e:
        raise ValueError(f"unreadable after write: {e}")


def _verify(path: str, provider: str, model: str, ctx: int, out: int) -> str:
    """'' when the file on disk really declares these limits, else what is wrong."""
    try:
        doc = _reread(path)
    except ValueError as e:
        return str(e)
    lim = _dig(doc, "provider", provider, "models", model, "limit")
    if not isinstance(lim, dict):
        return "limit object vanished"
    for key, want in (("context", ctx), ("input", ctx), ("output", out)):
        if lim.get(key) != want:
            return f"{key} is {lim.get(key)!r}, expected {want}"
    return ""


def _verify_compaction(path: str, keep: int) -> str:
    """'' when the file on disk really declares this compaction block."""
    try:
        doc = _reread(path)
    except ValueError as e:
        return str(e)
    c = doc.get("compaction")
    if not isinstance(c, dict):
        return "compaction block missing"
    if c.get("preserve_recent_tokens") != keep:
        return f"preserve_recent_tokens is {c.get('preserve_recent_tokens')!r}, expected {keep}"
    if c.get("prune") is not True:
        return f"prune is {c.get('prune')!r}, expected true"
    return ""


def _landed(path: str, saved: str, bad: str, what: str) -> bool:
    """Never report a write that is not on disk: put the backup back when it is not."""
    if not bad:
        return True
    shutil.copy2(saved, path)
    print(f"{path}: refused, {what} was not written as intended ({bad}); restored from {saved}.")
    return False


def merge_compaction(path: str, keep: int) -> int:
    """Write the top-level compaction block, keeping everything else byte for byte."""
    opened = _open(path)
    if opened is None:
        return 1
    text, doc = opened
    cur = doc.get("compaction")
    if isinstance(cur, dict) and cur.get("preserve_recent_tokens") == keep and cur.get("prune") is True:
        print(f"compaction already preserve_recent_tokens={keep}, prune=true: unchanged")
        return 0
    want = copy.deepcopy(doc)
    if isinstance(cur, dict):
        want["compaction"] = dict(cur, preserve_recent_tokens=keep, prune=True)
        new_text = set_member(text, ["compaction"], "prune", "true")
        new_text = set_member(new_text, ["compaction"], "preserve_recent_tokens", str(keep))
    else:
        want["compaction"] = {"preserve_recent_tokens": keep, "prune": True}
        new_text = set_member(text, [], "compaction", f'{{"preserve_recent_tokens": {keep}, "prune": true}}')
    saved, bad = commit(path, new_text, want)
    if bad:
        print(f"{path}: refused, {bad}; unchanged.")
        return 1
    if not _landed(path, saved, _verify_compaction(path, keep), "the compaction block"):
        return 1
    print(f"compaction: preserve_recent_tokens={keep}, prune=true (backup {saved})")
    return 0


def merge_autoupdate(path: str, value: str) -> int:
    """Write the top-level autoupdate key, keeping everything else byte for byte."""
    opened = _open(path)
    if opened is None:
        return 1
    text, doc = opened
    cur = doc.get("autoupdate", None)
    if cur == value:
        print(f"autoupdate already {value!r}: unchanged")
        return 0
    if cur is False:
        print("autoupdate is false (no update at all), which already holds the pin: unchanged")
        return 0
    want = dict(doc, autoupdate=value)
    saved, bad = commit(path, set_member(text, [], "autoupdate", json.dumps(value)), want)
    if bad:
        print(f"{path}: refused, {bad}; unchanged.")
        return 1
    try:
        got = _reread(path).get("autoupdate")
    except ValueError as e:
        got = e
    if not _landed(path, saved, "" if got == value else f"it reads {got!r}", "autoupdate"):
        return 1
    print(f"autoupdate: {value!r}, was {cur!r} (backup {saved})")
    return 0


def _verify_variant(path, provider, model, level) -> str:
    try:
        doc = _reread(path)
    except ValueError as e:
        return str(e)
    v = _dig(doc, "provider", provider, "models", model, "variants")
    got = _dig(v, level, "chat_template_kwargs", "reasoning_effort")
    return "" if got == level else f"variant {level} reads back as {got!r}"


def add_variant(path: str, provider: str, model: str, level: str) -> int:
    """Put a reasoning-effort variant into an existing opencode.json.

    install.sh writes a complete config into CONFIG_DIR, but the file opencode
    actually reads is the operator's own, and only the limits were ever merged
    into it. A level added to the generated artifact therefore never reached the
    picker: "lean" existed everywhere except where it could be selected.
    """
    opened = _open(path)
    if opened is None:
        return 1
    text, doc = opened
    mdl = _dig(doc, "provider", provider, "models", model)
    if mdl is None:
        print(f"{provider}/{model} not in {path}: nothing to merge")
        return 3
    variants = mdl.get("variants") if isinstance(mdl, dict) else None
    if not isinstance(mdl, dict) or variants is not None and not isinstance(variants, dict):
        print(f"{path}: refused, {provider}/{model} or its variants is not an object; add the "
              f"{level} variant by hand")
        return 1
    if (variants or {}).get(level):
        print(f"{provider}/{model} already offers the {level} variant: unchanged")
        return 0
    entry = {"chat_template_kwargs": {"reasoning_effort": level}}
    want = copy.deepcopy(doc)
    where = ["provider", provider, "models", model]
    if variants is not None:
        want["provider"][provider]["models"][model]["variants"][level] = entry
        new_text = set_member(text, where + ["variants"], level, json.dumps(entry))
    else:
        want["provider"][provider]["models"][model]["variants"] = {level: entry}
        new_text = set_member(text, where, "variants", json.dumps({level: entry}))
    saved, bad = commit(path, new_text, want)
    if bad:
        print(f"{path}: refused, {bad}; unchanged.")
        return 1
    if not _landed(path, saved, _verify_variant(path, provider, model, level), f"the {level} variant"):
        return 1
    print(f"{provider}/{model}: {level} variant added (backup {saved})")
    return 0


OURS = ("qwen38", "flashnext")


def _added_ok(text: str, before: dict, blocks: dict) -> str:
    """'' when text is the document `before` plus exactly `blocks` in its provider object."""
    try:
        doc = load(text)
    except ValueError as e:
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
    opened = _open(path)
    if opened is None:
        return 1
    text, doc = opened
    try:
        src = json.loads(read(source))
    except (OSError, ValueError) as e:
        print(f"{source} is not valid JSON: {e}")
        return 1
    have = doc.get("provider") if isinstance(doc.get("provider"), dict) else {}
    if not any(p in have for p in OURS):
        print(f"none of this repo's providers in {path}: nothing to add to")
        return 3
    blocks = {p: b for p, b in (src.get("provider") or {}).items() if p in OURS and p not in have}
    if not blocks:
        print(f"{path} already lists this box's providers: unchanged")
        return 0
    new_text = text
    for p, b in reversed(list(blocks.items())):    # each goes first, so the last one first
        new_text = set_member(new_text, ["provider"], p, json.dumps(b, indent=2))
    bad = _added_ok(new_text, doc, blocks)
    if bad:
        print(f"{path}: refused, {bad}; copy the {', '.join(blocks)} provider by hand from {source}")
        return 1
    want = copy.deepcopy(doc)
    want["provider"].update(copy.deepcopy(blocks))
    saved, bad = commit(path, new_text, want)
    if bad:
        print(f"{path}: refused, {bad}; unchanged.")
        return 1
    try:
        bad = _added_ok(read(path), doc, blocks)
    except OSError as e:
        bad = f"unreadable after write: {e}"
    if not _landed(path, saved, bad, f"the {', '.join(blocks)} provider"):
        return 1
    print(f"{', '.join(blocks)} provider added to {path}, for the lane installed since (backup {saved})")
    return 0


def remove_providers(path: str, key_file: str) -> int:
    """Take out of an opencode.json the providers that read this box's API key file.

    uninstall.sh --yes deletes that file, and opencode then refuses to start at all,
    every provider included, while a {file:} reference points at a file that is gone
    ("bad file reference", opencode 1.18.32). So the providers that read it go first,
    with model and small_model when they name one of them. A file left with nothing
    but what install.sh writes ($schema, autoupdate, compaction, no provider) was this
    repo's, and is moved to a backup instead of being kept empty.
    """
    opened = _open(path)
    if opened is None:
        return 1
    text, doc = opened
    refs = {f"{{file:{key_file}}}"}
    home = os.path.expanduser("~")
    if key_file.startswith(home + "/"):
        refs.add("{file:~" + key_file[len(home):] + "}")
    prov = doc.get("provider") if isinstance(doc.get("provider"), dict) else {}
    gone = [p for p, b in prov.items() if _dig(b, "options", "apiKey") in refs]
    if not gone:
        print(f"no provider in {path} reads {key_file}: unchanged")
        return 0
    steps = []                      # (path of the member, the document once it is gone), in order
    cur = copy.deepcopy(doc)
    for p in gone:
        del cur["provider"][p]
        steps.append((["provider", p], copy.deepcopy(cur)))
    for k in ("model", "small_model"):
        if isinstance(cur.get(k), str) and cur[k].split("/", 1)[0] in gone:
            del cur[k]
            steps.append(([k], copy.deepcopy(cur)))
    if set(cur) <= {"$schema", "autoupdate", "compaction", "provider"} and not cur.get("provider"):
        saved = backup(path)
        os.unlink(path)
        print(f"{path} held only this box's providers ({', '.join(gone)}): moved to {saved}")
        return 0
    new_text = text
    for where, want in steps:
        try:
            cand = remove_member(new_text, where)
            ok = load(cand) == want
        except ValueError:
            ok = False
        if not ok:
            print(f"{path}: refused, could not take {where[-1]!r} out by an edit that leaves the rest "
                  f"as it is; remove the {', '.join(gone)} provider by hand")
            return 1
        new_text = cand
    saved, bad = commit(path, new_text, cur)
    if bad:
        print(f"{path}: refused, {bad}; unchanged.")
        return 1
    try:
        ok = _reread(path) == cur
    except ValueError:
        ok = False
    if not _landed(path, saved, "" if ok else "it reads back differently", "the removal"):
        return 1
    print(f"removed the {', '.join(gone)} provider from {path}: it read {key_file} (backup {saved})")
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
    opened = _open(path)
    if opened is None:
        return 1
    text, doc = opened
    limit = _dig(doc, "provider", provider, "models", model, "limit")
    if limit is None:
        print(f"{provider}/{model} not in {path}: nothing to merge")
        return 3
    if not isinstance(limit, dict):
        print(f"{path}: refused, the limit of {provider}/{model} is not an object; edit it by hand")
        return 1
    if limit.get("context") == ctx and limit.get("input") == ctx and limit.get("output") == out:
        print(f"{provider}/{model} limits already {ctx}/{out}: unchanged")
        return 0
    have_ctx, have_out = limit.get("context"), limit.get("output")
    if (keep_lower and isinstance(have_ctx, int) and isinstance(have_out, int)
            and 0 < have_ctx <= ctx and 0 < have_out <= out and limit.get("input") in (None, have_ctx)):
        print(f"{provider}/{model} limits {have_ctx}/{have_out} are within {ctx}/{out}: unchanged "
              f"(a fit to the pool, kept)")
        return 0
    want = copy.deepcopy(doc)
    want["provider"][provider]["models"][model]["limit"].update(context=ctx, input=ctx, output=out)
    new_text = text
    for key, val in (("output", out), ("input", ctx), ("context", ctx)):    # each goes first
        new_text = set_member(new_text, ["provider", provider, "models", model, "limit"], key, str(val))
    saved, bad = commit(path, new_text, want)
    if bad:
        print(f"{path}: refused, {bad}; unchanged. Edit the block by hand or delete it and re-run.")
        return 1
    # Never report a write we did not make. A regex that matches nothing used to
    # leave the key untouched and still print the new value: a limit block
    # without "output" came out with only "context" rewritten, and opencode then
    # ran on its own default cap, which is the failure this tool exists to stop.
    if not _landed(path, saved, _verify(path, provider, model, ctx, out), "the limit block"):
        return 1
    print(f"{provider}/{model} limits: {limit.get('context')}/{limit.get('output')} -> {ctx}/{out} "
          f"(backup {saved})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
