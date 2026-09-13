#!/usr/bin/env python3
"""Step 8/9 died silent without a sudo ticket, three times in one afternoon.

Background runs and passwordless contexts give sudo no tty to ask on, and the
first of the 24 sudo calls was a bare `sudo cp` under set -e: the install died
with no message at the exact line where the real work starts. The fix refuses
by name before anything privileged is touched. This test stages that exact
situation (a sudo that always fails, everything else real and cached) and
asserts the refusal names sudo, happens at step 8/9, and never reaches the
unit backup. Skipped off the reference class of machine: it replays install
steps 1-7 for real, which needs aarch64, docker and the cached pins.
"""
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class SudoTicketRefusal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if platform.machine() != "aarch64" or not shutil.which("docker"):
            raise unittest.SkipTest(
                "needs the reference class of machine (aarch64, docker, cached pins)")
        stub = tempfile.mkdtemp()
        sudo = os.path.join(stub, "sudo")
        with open(sudo, "w") as f:
            f.write("#!/bin/sh\necho 'stub-sudo refuses' >&2\nexit 1\n")
        os.chmod(sudo, os.stat(sudo).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        env = dict(os.environ)
        env["PATH"] = stub + ":/usr/local/bin:/usr/bin:/bin"
        proc = subprocess.run(["./install.sh"], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=300)
        cls.rc = proc.returncode
        cls.out = proc.stdout + proc.stderr

    def test_the_refusal_names_sudo_at_step_8(self):
        self.assertEqual(self.rc, 1)
        self.assertIn("8/9", self.out)
        self.assertIn("sudo", self.out)
        self.assertIn("sudo -v", self.out)

    def test_nothing_privileged_was_touched_first(self):
        self.assertNotIn("previous unit backed up", self.out)
        self.assertNotIn("Install failed at line", self.out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
