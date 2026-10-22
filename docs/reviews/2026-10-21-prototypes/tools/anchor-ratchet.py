#!/usr/bin/env python3
"""The string anchors of the test files may only go down.

An anchor is `x.index("...")` where x is the text of a file of the repo (a script, a template, the
page) or a piece cut from it: a test that slices a script on an exact string breaks on a refactor
that changes no behaviour. The same call on what a program printed, or on a list of arguments, is
not one. Each test file's count is held to its line in tests/anchor-ceiling.json; a file that is not
there may have none.

  tools/anchor-ratchet.py            check every test file against its ceiling (exit 1 above it)
  tools/anchor-ratchet.py --write    write today's counts as the ceilings: only ever lower them

The count follows names, not values: a name bound to `read_text()` (or `.read()`), to a slice or a
method of such a name, to a `re.search` over one, or a small helper that returns one. It errs low:
an anchor on a text it cannot follow to a file is not counted.
"""
import json
import pathlib
import re
import subprocess
import sys

_here = pathlib.Path(__file__).resolve().parent
_top = subprocess.run(["git", "-C", str(_here), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
REPO = pathlib.Path(_top.stdout.strip()) if _top.returncode == 0 else _here.parent
CEILING = REPO / "tests" / "anchor-ceiling.json"
CALL = re.compile(r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*|\([^()]*\)|\[[^\[\]]*\])*)\.index\(\s*(?:f|r|rb|b)?['\"]")
BIND = re.compile(r"^\s*((?:self\.|cls\.)?[A-Za-z_]\w*)\s*(?::[^=\n]*)?=\s*(.+)$", re.M)
HELPER = re.compile(r"^\s*def ([A-Za-z_]\w*)\([^)]*\)[^:]*:\s*\n((?:\s+.*\n){1,6})", re.M)
READ = re.compile(r"read_text\(|\.read\(\)|read_bytes\(")


def norm(name):
    return re.sub(r"^(self|cls)\.", "", name)


def file_texts(src):
    """The names that hold a file's text, or a piece of one, in this test file's source."""
    names = {m.group(1) for m in HELPER.finditer(src) if READ.search(m.group(2)) and "return" in m.group(2)}
    binds = [(norm(m.group(1)), m.group(2).strip()) for m in BIND.finditer(src)]

    def from_file(rhs):
        if re.match(r"(?:self\.|cls\.)?\w*run\w*\(|subprocess\.", rhs):
            return False                       # what running something printed is output
        if READ.search(rhs):
            return True
        lead = re.match(r"(?:self\.|cls\.)?([A-Za-z_]\w*)\s*[\[.(]", rhs)
        if lead and lead.group(1) in names:
            return True
        return bool(re.match(r"re\.(search|match|findall)\(", rhs)) and any(
            re.search(rf"(?<![\w.]){re.escape(k)}\b", rhs) for k in names)
    while True:
        grown = {n for n, rhs in binds if n not in names and from_file(rhs)}
        if not grown:
            return names
        names |= grown


def count(path):
    src = path.read_text()
    names = file_texts(src)
    n = 0
    for m in CALL.finditer(src):
        base = norm(re.split(r"[\[(.]", norm(m.group(1)), maxsplit=1)[0])
        n += base in names or bool(READ.search(m.group(1)))
    return n


def counts():
    files = sorted([*REPO.glob("tests/test_*.py"), *REPO.glob("dashboard/tests/test_*.py")])
    return {str(f.relative_to(REPO)): c for f in files if (c := count(f))}


def main():
    now = counts()
    if sys.argv[1:] == ["--write"]:
        old = json.loads(CEILING.read_text()) if CEILING.exists() else {}
        up = sorted(f for f, c in now.items() if f in old and c > old[f])
        if up:
            sys.exit(f"refusing to raise a ceiling: {', '.join(up)}")
        CEILING.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n")
        print(f"{sum(now.values())} anchors in {len(now)} test files written to {CEILING.relative_to(REPO)}")
        return 0
    ceiling = json.loads(CEILING.read_text()) if CEILING.exists() else {}
    over = [(f, c, ceiling.get(f, 0)) for f, c in sorted(now.items()) if c > ceiling.get(f, 0)]
    under = [f for f, c in ceiling.items() if now.get(f, 0) < c]
    for f, c, cap in over:
        print(f"{f}: {c} anchors, its ceiling is {cap}: test what the script does (a function of a sourced"
              " library, or tests/installer_wall.py), not where its text sits")
    print(f"{sum(now.values())} anchors in {len(now)} test files, ceiling {sum(ceiling.values())}"
          + (f"; {len(under)} files went down: lower their ceilings with --write" if under else ""))
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
