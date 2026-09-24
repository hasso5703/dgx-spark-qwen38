#!/usr/bin/env python3
"""Every command of the cockpit's sudoers allowlist names its arguments, or none with "".

In sudoers a command written with no arguments may be run with ANY: the py-spy wrapper's
line allowed whatever arguments a caller added (without effect today, since the wrapper
reads none; found in review, 2026-09-24). An empty "" allows none, and the cockpit calls
it bare."""
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
LINES = [ln for ln in (REPO / "dashboard" / "sudoers-cockpit.template").read_text().splitlines()
         if ln.startswith("__USER__")]


class EveryCommandIsExact(unittest.TestCase):
    def test_no_line_leaves_the_arguments_open(self):
        self.assertTrue(LINES)
        for ln in LINES:
            command = ln.split("NOPASSWD:", 1)[1].strip()
            self.assertGreater(len(command.split()), 1, f"any arguments allowed: {ln}")

    def test_the_cockpit_calls_the_wrapper_bare(self):
        ck = (REPO / "dashboard" / "cockpit.py").read_text()
        self.assertRegex(ck, re.escape('run(["sudo", "-n", "/usr/local/bin/qwen38-pyspy-scheduler"]'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
