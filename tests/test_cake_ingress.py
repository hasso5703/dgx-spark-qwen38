#!/usr/bin/env python3
"""cake-ingress start says active only when the shaper is in place.

It ran every step under `set -u` only, so a refused sudo, a kernel without sch_cake or a
RATE_DOWN tc cannot read went on to "cake-ingress: active" and exit 0, and the unit read
as started (found in review, 2026-09-24). As a user the script calls its tools through
`sudo -n`, so a fake sudo first on PATH plays them here and nothing reaches the system:
every call is logged, none is executed.
"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "extras" / "cake-ingress" / "cake-ingress"

# Logs its argv; fails (exit 2) on a call whose text contains $FAKE_FAIL.
FAKE_SUDO = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$*" in *"$FAKE_FAIL"*) [ -n "$FAKE_FAIL" ] && { echo "RTNETLINK answers: Operation not supported" >&2; exit 2; } ;; esac
exit 0
"""


@unittest.skipIf(os.geteuid() == 0, "as root the script calls the real tools, not sudo")
class TheStart(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="cake-"))
        self.addCleanup(shutil.rmtree, self.t, ignore_errors=True)
        (self.t / "sudo").write_text(FAKE_SUDO)
        (self.t / "sudo").chmod(0o755)
        self.log = self.t / "calls.log"

    def start(self, fail=""):
        env = {**os.environ, "PATH": f"{self.t}:{os.environ['PATH']}", "FAKE_LOG": str(self.log),
               "FAKE_FAIL": fail, "IF": "lo", "IFB": "ifbtest9", "RATE_DOWN": "950Mbit"}
        r = subprocess.run(["bash", str(SCRIPT), "start"], env=env, capture_output=True, text=True, timeout=30)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        self.assertTrue(calls and all(c.startswith("-n /usr/sbin/") for c in calls), calls)
        return r.returncode, r.stdout + r.stderr, calls

    def test_every_step_done_is_active(self):
        rc, out, _ = self.start()
        self.assertEqual(rc, 0, out)
        self.assertIn("active", out)

    def test_a_shaper_that_was_not_installed_is_not_active(self):
        rc, out, calls = self.start(fail="root cake bandwidth")
        self.assertEqual(rc, 1, out)
        self.assertNotIn("active", out)
        self.assertIn("failed", out)
        # what had been set is taken down again
        self.assertTrue(any("qdisc del dev lo clsact" in c for c in calls[calls.index(next(
            c for c in calls if "root cake bandwidth" in c)):]), calls)

    def test_a_missing_module_stops_before_anything_is_redirected(self):
        rc, out, calls = self.start(fail="sch_cake")
        self.assertEqual(rc, 1, out)
        self.assertFalse(any("mirred" in c for c in calls), calls)


if __name__ == "__main__":
    unittest.main()
