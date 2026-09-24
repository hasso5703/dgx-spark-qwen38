#!/usr/bin/env python3
"""switch-model.sh with no target prints its usage instead of switching to stock.

Run bare, it switched the box to stock: it re-enabled the 27B lane and restarted what
follows it, where the usage was all anyone wanted (found in review, 2026-09-24). Only the
script's head runs here, up to its target check, so a regression that moved the check
cannot turn this test into a real switch."""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "switch-model.sh").read_text()
CASE = TEXT.index('case "$CHOICE" in stock|')
HEAD = TEXT[:TEXT.index("\n", TEXT.index(" esac", CASE)) + 1]


def run(*args, **env):
    d = pathlib.Path(tempfile.mkdtemp(prefix="switch-usage-"))
    (d / "switch-model.sh").write_text(HEAD + 'echo "CHOICE=$CHOICE"\n')
    r = subprocess.run(["bash", str(d / "switch-model.sh"), *args], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(d), **env})
    return r.returncode, r.stdout, r.stderr


class ATargetIsRequired(unittest.TestCase):
    def test_the_check_comes_before_anything_else_runs(self):
        body = TEXT[:CASE]
        for word in ("sudo ", "systemctl ", "docker ", "curl "):
            self.assertNotIn("\n" + word, body, f"{word.strip()} runs before the target is checked")

    def test_bare_prints_the_usage(self):
        rc, out, err = run()
        self.assertEqual(rc, 2)
        self.assertIn("usage: ./switch-model.sh <stock|", err)
        self.assertNotIn("CHOICE=", out)

    def test_a_target_goes_through(self):
        self.assertEqual(run("fp8")[1].strip(), "CHOICE=fp8")
        self.assertEqual(run(MODEL_CHOICE="flash")[1].strip(), "CHOICE=flash")

    def test_an_unknown_target_is_named(self):
        rc, _, err = run("nope")
        self.assertEqual(rc, 1)
        self.assertIn("unknown target: nope", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
