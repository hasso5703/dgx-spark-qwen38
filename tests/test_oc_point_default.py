#!/usr/bin/env python3
"""oc-point-default.py: the default model and the served entry's picker name
follow the installed lane, in any opencode.json-shaped file.

This edits the file opencode really reads, so every case asserts on the file
on disk afterwards: default model, small model, shown name, and everything
else byte-identical. A lane change that leaves the previous lane's default in
place strands every agent session on a model nothing serves (reference box,
stock install over an fp8/flash era config)."""
import json
import os
import subprocess
import sys
import tempfile

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_DIR, "oc-point-default.py")


def run(*args):
    r = subprocess.run([sys.executable, SCRIPT, *args],
                       capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def write(tmp, doc):
    p = os.path.join(tmp, "oc.json")
    with open(p, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    return p


def read(p):
    with open(p) as f:
        return json.load(f)


def stale_fp8_config():
    return {
        "provider": {
            "qwen38": {"models": {"qwen3.8-27b": {
                "name": "Qwen3.8-27B FP8 official + DFlash2 (local, 1M)",
                "limit": {"context": 700000, "input": 700000, "output": 200000}}}},
            "flashnext": {"models": {"qwen3.8-flash-next": {
                "name": "Qwen3.8-Flash-Next NVFP4 + MTP (local, 262K)",
                "limit": {"context": 175000, "input": 175000, "output": 64000}}}},
        },
        "model": "flashnext/qwen3.8-flash-next",
        "small_model": "flashnext/qwen3.8-flash-next",
    }


def main() -> None:
    tmp = tempfile.mkdtemp()

    # 1. The reported bug: stock 1M installed over an fp8/flash era config.
    # Default flips lanes, the served entry is renamed, the other lane keeps
    # its entry, limits untouched.
    p = write(tmp, stale_fp8_config())
    rc, out = run(p, "27b", "stock", "1010000")
    assert rc == 0, out
    assert "opencode default model -> qwen38/qwen3.8-27b" in out, out
    cfg = read(p)
    assert cfg["model"] == "qwen38/qwen3.8-27b", cfg["model"]
    assert cfg["small_model"] == "qwen38/qwen3.8-27b"
    assert cfg["provider"]["qwen38"]["models"]["qwen3.8-27b"]["name"] == \
        "Qwen3.8-27B NVFP4 + DFlash2 (local, 1M)"
    assert cfg["provider"]["flashnext"]["models"]["qwen3.8-flash-next"]["name"] == \
        "Qwen3.8-Flash-Next NVFP4 + MTP (local, 262K)"
    assert cfg["provider"]["qwen38"]["models"]["qwen3.8-27b"]["limit"]["context"] == 700000

    # 2. Window suffixes: 1M, explicit K, unknown.
    p = write(tmp, stale_fp8_config())
    rc, _ = run(p, "flash", "flash-uncensored", "262144")
    assert rc == 0
    cfg = read(p)
    assert cfg["model"] == "flashnext/qwen3.8-flash-next"
    assert cfg["provider"]["flashnext"]["models"]["qwen3.8-flash-next"]["name"] == \
        "Qwen3.8-Flash-Next NVFP4 abliterated + MTP (local, 262K)"
    p = write(tmp, stale_fp8_config())
    rc, _ = run(p, "27b", "fp8", "")
    assert rc == 0
    assert read(p)["provider"]["qwen38"]["models"]["qwen3.8-27b"]["name"] == \
        "Qwen3.8-27B FP8 official + DFlash2 (local)"

    # 3. Unknown choice still flips the default (the lane is what matters),
    # but renames nothing it cannot name.
    p = write(tmp, stale_fp8_config())
    rc, out = run(p, "27b", "future-thing", "262144")
    assert rc == 0, out
    cfg = read(p)
    assert cfg["model"] == "qwen38/qwen3.8-27b"
    assert cfg["provider"]["qwen38"]["models"]["qwen3.8-27b"]["name"] == \
        "Qwen3.8-27B FP8 official + DFlash2 (local, 1M)"

    # 4. A config predating the lane's provider: untouched, NOTE, exit 0.
    p = write(tmp, {"provider": {"qwen38": {"models": {}}}, "model": "qwen38/qwen3.8-27b"})
    before = open(p, "rb").read()
    rc, out = run(p, "flash", "flash", "262144")
    assert rc == 0, out
    assert "NOTE" in out and "flashnext" in out, out
    assert open(p, "rb").read() == before

    # 5. Not JSON: exit 3, file untouched.
    p = os.path.join(tmp, "broken.json")
    with open(p, "w") as f:
        f.write("{not json")
    rc, _ = run(p, "27b", "stock", "1010000")
    assert rc == 3, rc
    assert open(p).read() == "{not json"

    # 6. Idempotent: pointing twice changes nothing the second time.
    p = write(tmp, stale_fp8_config())
    run(p, "27b", "stock", "1010000")
    once = open(p, "rb").read()
    rc, _ = run(p, "27b", "stock", "1010000")
    assert rc == 0
    assert open(p, "rb").read() == once

    # 7. Usage: exit 2, no traceback.
    r = subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True)
    assert r.returncode == 2, r.returncode
    assert "Traceback" not in r.stderr, r.stderr

    print("test_oc_point_default: OK")


if __name__ == "__main__":
    main()
