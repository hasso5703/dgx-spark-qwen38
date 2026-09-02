#!/usr/bin/env python3
"""oc-merge-limits.py against the config shapes users actually have.

This edits the file opencode really reads, so the invariant is narrow: after a
run that reports success, the block on disk declares exactly the limits that
were asked for, and nothing else in the file moved. A limit block missing a key
used to defeat that: the regex matched nothing, the key stayed absent, and the
tool printed the new value anyway."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_DIR, "oc-merge-limits.py")


def load_module():
    spec = importlib.util.spec_from_file_location("ocm", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def run(path, provider, model, ctx, out):
    r = subprocess.run([sys.executable, SCRIPT, path, provider, model, str(ctx), str(out)],
                       capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def limits_of(path, provider="qwen38", model="qwen3.8-27b"):
    doc = json.loads(re.sub(r"^\s*//.*$", "", open(path).read(), flags=re.M))
    return doc["provider"][provider]["models"][model].get("limit")


def write(tmp, body):
    p = os.path.join(tmp, "oc.json")
    with open(p, "w") as f:
        f.write(body)
    return p


def wrap(limit_block):
    return ('{\n  // user comment kept\n  "provider": {\n'
            '    "other": {"models": {"x": {"limit": {"context": 1, "input": 1, "output": 1}}}},\n'
            '    "qwen38": {"models": {"qwen3.8-27b": {"name": "local", "limit": '
            + limit_block + '}}}\n  }\n}\n')


def main() -> None:
    tmp = tempfile.mkdtemp()

    # 1. Full block: rewritten, comment kept, other providers untouched, idempotent.
    p = write(tmp, wrap('{"context": 194048, "input": 194048, "output": 64000}'))
    rc, out = run(p, "qwen38", "qwen3.8-27b", 480000, 160000)
    assert rc == 0 and "-> 480000/160000" in out, out
    assert limits_of(p) == {"context": 480000, "input": 480000, "output": 160000}
    assert "user comment kept" in open(p).read(), "the JSONC comment was dropped"
    doc = json.loads(re.sub(r"^\s*//.*$", "", open(p).read(), flags=re.M))
    assert doc["provider"]["other"]["models"]["x"]["limit"] == {"context": 1, "input": 1, "output": 1}
    rc, out = run(p, "qwen38", "qwen3.8-27b", 480000, 160000)
    assert rc == 0 and "unchanged" in out, out

    # 2. The bug: a block missing "output" (and "input") was partially written and
    #    reported as a success, leaving opencode on its own default cap.
    for block in ('{"context": 194048}',
                  '{"context": 194048, "input": 194048}',
                  '{"output": 64000}',
                  '{}'):
        p = write(tmp, wrap(block))
        rc, out = run(p, "qwen38", "qwen3.8-27b", 480000, 160000)
        assert rc == 0, f"{block}: rc={rc} {out}"
        assert limits_of(p) == {"context": 480000, "input": 480000, "output": 160000}, \
            f"{block} -> {limits_of(p)}"

    # 3. Keys the user added are preserved, not clobbered by the rewrite.
    p = write(tmp, wrap('{"context": 1, "input": 1, "output": 1, "custom": "keep"}'))
    rc, _ = run(p, "qwen38", "qwen3.8-27b", 480000, 160000)
    assert rc == 0
    assert limits_of(p).get("custom") == "keep", "an unknown key in the block was lost"

    # 4. A provider or model this config does not have is a no-op, exit 3, so
    #    oc-fit-limits.py can treat it as "nothing to merge here".
    p = write(tmp, wrap('{"context": 1, "input": 1, "output": 1}'))
    before = open(p).read()
    for prov, mod in (("nope", "qwen3.8-27b"), ("qwen38", "nope")):
        rc, out = run(p, prov, mod, 1, 1)
        assert rc == 3, f"{prov}/{mod}: rc={rc} {out}"
    assert open(p).read() == before, "a no-op run rewrote the file"

    # 5. The verifier is what makes the report trustworthy: it must reject a file
    #    whose block does not carry the requested numbers.
    m = load_module()
    p = write(tmp, wrap('{"context": 5, "input": 5, "output": 5}'))
    assert m._verify(p, "qwen38", "qwen3.8-27b", 5, 5) == ""
    assert "context is 5" in m._verify(p, "qwen38", "qwen3.8-27b", 9, 5)
    assert "output is 5" in m._verify(p, "qwen38", "qwen3.8-27b", 5, 9)
    assert "vanished" in m._verify(p, "qwen38", "nope", 5, 5)

    print("test_oc_merge_limits: OK")


if __name__ == "__main__":
    sys.exit(main())
