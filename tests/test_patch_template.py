#!/usr/bin/env python3
"""patch-template.py on a fixture built from the script's own anchor
constants: both patches applied, idempotent message on a pre-patched template,
exact-revision snapshot selection, custom repo argument. No network."""
import importlib.util
import os
import subprocess
import sys
import tempfile
import shutil

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_DIR, "patch-template.py")

spec = importlib.util.spec_from_file_location("pt", SCRIPT)
pt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pt)


def make_fixture(base: str, repo: str, sha: str, body: str) -> str:
    d = f"{base}/hub/models--{repo.replace('/', '--')}/snapshots/{sha}"
    os.makedirs(d, exist_ok=True)
    with open(f"{d}/chat_template.jinja", "w") as f:
        f.write(body)
    return f"{d}/chat_template.jinja"


def run(base: str, out: str, rev: str, repo: str) -> str:
    r = subprocess.run([sys.executable, SCRIPT, base, out, rev, repo],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def main() -> None:
    base = tempfile.mkdtemp(prefix="tpl-fixture-")
    try:
        sha = "c" * 40
        repo = "org/custom-model"
        # Every anchor the script looks for, in the order the upstream template has
        # them. A fixture missing one makes the patcher refuse the whole template,
        # which is the behaviour tested below for a REAL upstream change and not
        # something this fixture should be triggering by accident.
        stock_body = ("HEAD\n" + pt.EFFORT_ANCHOR + "\n" + pt.MSG_ANCHOR + "\nMID\n"
                      + pt.LEAN_ANCHOR + pt.SYSTEM_ANCHOR + "\nTAIL\n")
        make_fixture(base, repo, sha, stock_body)
        out = os.path.join(base, "out.jinja")
        log = run(base, out, sha, repo)
        assert log.count("applied") == 4, log
        patched = open(out).read()
        assert "'minimal'" in patched, "effort patch must map the minimal tier"
        assert "resolved_reasoning_effort == 'lean'" in patched, "lean patch must add the level"
        assert "default('lean')" in patched, "lean must become the default"
        assert "lean (default), xhigh, medium, and low" in patched, "the refusal must name lean"
        assert "<system-reminder>" in patched, "system patch must render reminders"
        assert patched.startswith("HEAD\n") and patched.endswith("TAIL\n")
        # The same template again leaves the file as it is, mtime included: the engine
        # reads it at start, and install.sh keeps a running engine only when nothing it
        # reads was written after it started (every run used to rewrite it, 2026-09-23).
        os.utime(out, (1_000_000_000, 1_000_000_000))
        log_same = run(base, out, sha, repo)
        assert "patched template unchanged" in log_same, log_same
        assert os.stat(out).st_mtime == 1_000_000_000, "an identical template was rewritten"
        assert open(out).read() == patched
        # A template that already carries both fixes: succeed, change nothing.
        make_fixture(base, repo, sha, patched)
        log2 = run(base, out, sha, repo)
        assert log2.count("already present") == 4, log2
        assert open(out).read() == patched
        # Exact-revision selection with a newer decoy snapshot.
        decoy = make_fixture(base, repo, "d" * 40, "DECOY " + stock_body)
        os.utime(decoy, None)
        run(base, out, sha, repo)
        assert not open(out).read().startswith("DECOY"), "pinned snapshot must win"
        print("test_patch_template: OK")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
