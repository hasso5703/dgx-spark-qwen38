#!/usr/bin/env python3
"""patch-yarn.py on synthetic fixtures: both config shapes (text_config nested
like the targets, root-level like the DFlash2 draft), key preservation,
idempotence, backup creation, and exact-revision selection. No network."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_DIR, "patch-yarn.py")


def make_fixture(base: str, repo: str, sha: str, nested: bool) -> str:
    d = f"{base}/hub/models--{repo.replace('/', '--')}/snapshots/{sha}"
    os.makedirs(d, exist_ok=True)
    inner = {"max_position_embeddings": 262144,
             "rope_parameters": {"rope_theta": 10000000, "partial_rotary_factor": 0.25}}
    cfg = {"architectures": ["X"], "text_config": inner} if nested else dict(inner)
    with open(f"{d}/config.json", "w") as f:
        json.dump(cfg, f)
    return f"{d}/config.json"


def run(base: str, repo: str, rev: str) -> str:
    r = subprocess.run([sys.executable, SCRIPT, base, repo, rev],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def main() -> None:
    base = tempfile.mkdtemp(prefix="yarn-fixture-")
    try:
        sha = "a" * 40
        for repo, nested in (("org/target", True), ("org/draft", False)):
            path = make_fixture(base, repo, sha, nested)
            out1 = run(base, repo, sha)
            assert "applied to" in out1, out1
            out2 = run(base, repo, sha)
            assert "already applied" in out2, out2
            c = json.load(open(path))
            tc = c.get("text_config", c)
            assert tc["max_position_embeddings"] == 1010000
            rp = tc["rope_parameters"]
            assert rp["rope_type"] == "yarn" and rp["factor"] == 4.0
            assert rp["original_max_position_embeddings"] == 262144
            assert rp["rope_theta"] == 10000000, "existing rope keys must be preserved"
            assert rp["partial_rotary_factor"] == 0.25
            if nested:
                assert c["architectures"] == ["X"], "non-rope root keys must be preserved"
            backup = json.load(open(path + ".pre-yarn"))
            btc = backup.get("text_config", backup)
            assert btc["max_position_embeddings"] == 262144, "backup must be the original"
        # Revision selection: with two snapshots, the requested sha wins even
        # when another snapshot is newer.
        sha2 = "b" * 40
        p2 = make_fixture(base, "org/target", sha2, nested=True)
        os.utime(p2, None)  # newest mtime
        out = run(base, "org/target", sha)
        assert "already applied" in out and sha in out, out
        c2 = json.load(open(p2))
        assert c2["text_config"]["max_position_embeddings"] == 262144, \
            "the other snapshot must be untouched"
        print("test_patch_yarn: OK")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
