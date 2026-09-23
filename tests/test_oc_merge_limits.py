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

    # 6. --compaction writes the top-level block, on a config that has one and on
    #    one that does not, and never touches the rest of the file. opencode's own
    #    default caps preserve_recent_tokens at 15,000 whatever the window, which
    #    on a 262K lane throws the window away the moment compaction fires.
    def compact(path, keep):
        r = subprocess.run([sys.executable, SCRIPT, path, "--compaction", str(keep)],
                           capture_output=True, text=True)
        return r.returncode, (r.stdout + r.stderr).strip()

    def doc_of(path):
        return json.loads(re.sub(r"^\s*//.*$", "", open(path).read(), flags=re.M))

    p = write(tmp, wrap('{"context": 1, "input": 1, "output": 1}'))
    rc, out = compact(p, 50000)
    assert rc == 0 and "preserve_recent_tokens=50000" in out, out
    d = doc_of(p)
    assert d["compaction"] == {"preserve_recent_tokens": 50000, "prune": True}, d.get("compaction")
    assert "user comment kept" in open(p).read(), "the JSONC comment was dropped"
    assert d["provider"]["other"]["models"]["x"]["limit"] == {"context": 1, "input": 1, "output": 1}
    rc, out = compact(p, 50000)
    assert rc == 0 and "unchanged" in out, out
    rc, out = compact(p, 60000)                       # an existing block is rewritten in place
    assert rc == 0 and doc_of(p)["compaction"]["preserve_recent_tokens"] == 60000
    assert doc_of(p)["compaction"]["prune"] is True

    # A block that already exists with other keys keeps them, and prune is turned on.
    body = ('{\n  "compaction": {"auto": true, "preserve_recent_tokens": 9, "prune": false},\n'
            '  "provider": {"qwen38": {"models": {"qwen3.8-27b": {"limit": '
            '{"context": 1, "input": 1, "output": 1}}}}}\n}\n')
    p = write(tmp, body)
    rc, out = compact(p, 50000)
    assert rc == 0, out
    d = doc_of(p)["compaction"]
    assert d == {"auto": True, "preserve_recent_tokens": 50000, "prune": True}, d

    # And the verifier refuses a block that does not carry what was asked.
    assert m._verify_compaction(p, 50000) == ""
    assert "expected 7" in m._verify_compaction(p, 7)

    # 7. --add-providers: the config a box's first install wrote gains the provider of a
    #    lane installed since, and nothing else in it moves. The 27B first and the flash
    #    lane later left the copy with no flashnext: the served model answers under any
    #    name, so opencode sent the 27B's limits and label to the flash lane.
    def add(path, source):
        r = subprocess.run([sys.executable, SCRIPT, path, "--add-providers", source],
                           capture_output=True, text=True)
        return r.returncode, (r.stdout + r.stderr).strip()

    flash = {"npm": "@ai-sdk/openai-compatible", "name": "Qwen3.8-Flash-Next (DGX Spark)",
             "options": {"baseURL": "http://127.0.0.1:30001/v1", "apiKey": "{file:/k}"},
             "models": {"qwen3.8-flash-next": {"limit": {"context": 1, "input": 1, "output": 1}}}}
    gen = os.path.join(tmp, "generated.json")
    with open(gen, "w") as f:
        json.dump({"provider": {"qwen38": {"models": {"qwen3.8-27b": {}}}, "flashnext": flash}}, f, indent=2)
    # a commented-out "provider" line and a nested "provider" key come before the real one
    p = write(tmp, '{\n  // "provider": { a line the user commented out\n'
                   '  "agent": {"a": {"provider": {"z": 1}}},\n'
                   '  "provider": {\n    // mine\n'
                   '    "qwen38": {"models": {"qwen3.8-27b": {"limit": {"context": 5}}}},\n'
                   '    "other": {}\n  }\n}\n')
    before = doc_of(p)
    rc, out = add(p, gen)
    assert rc == 0 and "flashnext provider added" in out, out
    after = doc_of(p)
    assert after["provider"].pop("flashnext") == flash, "the provider was not copied as generated"
    assert after == before, "adding a provider moved something else"
    assert "// mine" in open(p).read() and "commented out" in open(p).read(), "a comment was dropped"
    rc, out = add(p, gen)
    assert rc == 0 and "unchanged" in out, out

    # A config with none of this repo's providers is the user's own: exit 3, untouched.
    for body in ('{"provider": {"anthropic": {}}}\n', '{"model": "x"}\n'):
        p = write(tmp, body)
        rc, out = add(p, gen)
        assert rc == 3 and open(p).read() == body, f"{body!r}: rc={rc} {out}"

    # The check that picks the insertion point refuses anything else that moved.
    base, blk = {"provider": {"qwen38": {}}, "x": 1}, {"flashnext": {"a": 1}}
    assert m._added_ok('{"provider": {"flashnext": {"a": 1}, "qwen38": {}}, "x": 1}', base, blk) == ""
    assert "changed" in m._added_ok('{"provider": {"flashnext": {"a": 1}, "qwen38": {}}, "x": 2}', base, blk)
    assert "differently" in m._added_ok('{"provider": {"flashnext": {"a": 2}, "qwen38": {}}, "x": 1}', base, blk)

    # 8. --remove-providers: uninstall.sh --yes deletes the API key file, and opencode
    #    then refuses to start at all while a {file:} reference points at it ("bad file
    #    reference", opencode 1.18.32, every provider included). The providers that read
    #    it go first; a file with nothing else of the user's goes to a backup.
    key = os.path.join(tmp, ".config", "qwen38", "api-key")

    def remove(path):
        r = subprocess.run([sys.executable, SCRIPT, path, "--remove-providers", key],
                           capture_output=True, text=True)
        return r.returncode, (r.stdout + r.stderr).strip()

    def box(ref=None):
        return {"npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": "http://127.0.0.1:30001/v1", "apiKey": ref or "{file:" + key + "}"},
                "models": {"qwen3.8-27b": {"limit": {"context": 1, "input": 1, "output": 1}}}}

    # install.sh's own copy: nothing of the user's in it, so it is moved aside whole
    p = write(tmp, json.dumps({"$schema": "https://opencode.ai/config.json", "autoupdate": "notify",
                               "provider": {"qwen38": box(), "flashnext": box()},
                               "compaction": {"preserve_recent_tokens": 1, "prune": True},
                               "model": "qwen38/qwen3.8-27b", "small_model": "qwen38/qwen3.8-27b"}, indent=2))
    rc, out = remove(p)
    assert rc == 0 and "moved to" in out and not os.path.exists(p), out

    # a user's own config: the box's provider and the model naming it go, nothing else
    body = ('{\n  // mine\n  "$schema": "https://opencode.ai/config.json",\n  "model": "qwen38/qwen3.8-27b",\n'
            '  "plugin": ["auto-continue.js"],\n  "provider": {\n    // the box\n'
            '    "qwen38": ' + json.dumps(box()) + ',\n'
            '    "anthropic": {"options": {"apiKey": "{env:ANTHROPIC_API_KEY}"}}\n  },\n'
            '  "agent": {"build": {"model": "anthropic/claude"}}\n}\n')
    p = write(tmp, body)
    want = doc_of(p)
    del want["provider"]["qwen38"], want["model"]
    rc, out = remove(p)
    assert rc == 0 and "removed the qwen38 provider" in out, out
    assert doc_of(p) == want, doc_of(p)
    assert "// mine" in open(p).read() and "// the box" in open(p).read(), "a comment was dropped"
    rc, out = remove(p)
    assert rc == 0 and "unchanged" in out, out

    # the first "model" in the file is an agent's, not the one that names the default:
    # the edit that goes is the one that leaves exactly the intended document
    p = write(tmp, '{\n  "agent": {"build": {"model": "qwen38/qwen3.8-27b"}},\n'
                   '  "model": "qwen38/qwen3.8-27b",\n'
                   '  "provider": {"qwen38": ' + json.dumps(box()) + ', "x": {}}\n}\n')
    rc, out = remove(p)
    assert rc == 0, out
    assert doc_of(p) == {"agent": {"build": {"model": "qwen38/qwen3.8-27b"}}, "provider": {"x": {}}}, open(p).read()

    # the ~ spelling of the same file is the same reference
    home = os.path.expanduser("~")
    p = write(tmp, json.dumps({"provider": {"qwen38": box("{file:~/.config/qwen38/api-key}"), "x": {}}}))
    r = subprocess.run([sys.executable, SCRIPT, p, "--remove-providers", home + "/.config/qwen38/api-key"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and doc_of(p) == {"provider": {"x": {}}}, r.stdout

    # first and last member of the provider object both leave valid JSON
    for body in ('{"provider": {"x": {}, "qwen38": ' + json.dumps(box()) + '}, "y": 1}',
                 '{"provider": {"qwen38": ' + json.dumps(box()) + ', "x": {}}, "y": 1}'):
        p = write(tmp, body)
        rc, out = remove(p)
        assert rc == 0 and doc_of(p) == {"provider": {"x": {}}, "y": 1}, open(p).read()

    print("test_oc_merge_limits: OK")


if __name__ == "__main__":
    sys.exit(main())
