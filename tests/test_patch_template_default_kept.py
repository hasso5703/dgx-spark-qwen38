#!/usr/bin/env python3
"""The lean level's default survives a run that does not name it.

LEAN_DEFAULT=0 installs the lean level without making it the default. patch-template.py
read the choice from the environment of each run, and neither a plain ./install.sh nor
the cockpit's Switch passes it, so a box installed with LEAN_DEFAULT=0 went back to lean at
the next of either; LEAN.md presents it as a kept choice. And the refusal of an unknown
level said "lean (default)" whatever the default was (found in review, 2026-09-24).
"""
import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "patch-template.py"
spec = importlib.util.spec_from_file_location("pt_kept", SCRIPT)
pt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pt)
SHA = "d" * 40
REPO_ID = "org/model"


class TheDefaultIsKept(unittest.TestCase):
    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp(prefix="lean-kept-"))
        snap = self.base / "hub" / "models--org--model" / "snapshots" / SHA
        snap.mkdir(parents=True)
        (snap / "chat_template.jinja").write_text(
            "HEAD\n" + pt.EFFORT_ANCHOR + "\n" + pt.MSG_ANCHOR + "\nMID\n" + pt.LEAN_ANCHOR + pt.SYSTEM_ANCHOR + "\nTAIL\n")
        self.out = self.base / "out.jinja"

    def run_patch(self, lean_default=None):
        env = {k: v for k, v in os.environ.items() if k != "LEAN_DEFAULT"}
        if lean_default is not None:
            env["LEAN_DEFAULT"] = lean_default
        r = subprocess.run([sys.executable, str(SCRIPT), str(self.base), str(self.out), SHA, REPO_ID],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return self.out.read_text()

    def test_a_run_without_the_variable_keeps_xhigh(self):
        self.assertIn("reasoning_effort|default('xhigh')", self.run_patch("0"))
        kept = self.run_patch()
        self.assertIn("reasoning_effort|default('xhigh')", kept, "the plain run went back to lean")

    def test_the_refusal_names_the_real_default(self):
        text = self.run_patch("0")
        self.assertIn("lean, xhigh (default), medium, and low", text)
        self.assertNotIn("lean (default)", text)

    def test_naming_it_again_moves_it(self):
        self.run_patch("0")
        self.assertIn("reasoning_effort|default('lean')", self.run_patch("1"))

    def test_a_fresh_install_defaults_to_lean(self):
        text = self.run_patch()
        self.assertIn("reasoning_effort|default('lean')", text)
        self.assertIn("lean (default), xhigh, medium, and low", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
