#!/usr/bin/env python3
"""An install that cannot use sudo refuses by name, before it downloads anything.

Step 8/10 died silent without a sudo ticket, three times in one afternoon: background
runs and passwordless contexts give sudo no tty to ask on, and the first sudo call was a
bare `sudo cp` under set -e. The refusal by name came first; since v1.18.7 sudo is also
asked for at the end of step 1, so a box where it cannot be used learns it before the
pulls instead of after them (tests/test_install_sudo_prompt.py holds the prompt itself).
This test stages the failing case on the real installer (a sudo that always fails,
everything else real) and asserts the refusal names sudo -v, comes at step 1, before
step 2, and never reaches the unit backup. Skipped off the reference class of machine:
step 1 checks the GPU, docker and the disk for real.
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


def _pins_cached():
    """Steps 1-7 verify the pinned bytes before anything else, so without the
    local cache the run dies at step 1 instead of reaching the step-8 refusal
    under test. ci-local runs this file under a witness HOME with no cache on
    purpose: skipping there is the test respecting the witness, not dodging.
    """
    cache = Path(os.environ.get("HF_CACHE", Path.home() / ".cache" / "huggingface"))
    if not cache.is_dir():
        return False
    try:
        proc = subprocess.run(["docker", "images", "--format", "{{.Repository}}"],
                              capture_output=True, text=True, timeout=60)
    except Exception:
        return False
    return proc.returncode == 0 and "lmsysorg/sglang" in proc.stdout.split()


def _why_skip():
    """Why this suite cannot run here, or "" when it can."""
    if platform.machine() != "aarch64" or not shutil.which("docker"):
        return "needs the reference class of machine (aarch64, docker, cached pins)"
    if not _pins_cached():
        return "needs the pinned bytes cached (steps 1-7 verify them first)"
    return ""


_SKIP = _why_skip()


# Decorated, not raised from setUpClass: a SkipTest out of setUpClass makes
# unittest report the whole class as a single skip with testsRun 0, so this
# file declared two tests and ran none wherever it skipped, which is every
# GitHub runner. The CI gate that counts declared against ran caught it on
# 2026-09-14. A decorator skips each test, and each one is still counted.
@unittest.skipIf(_SKIP, _SKIP or "runnable here")
class SudoTicketRefusal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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

    def test_the_refusal_names_sudo_before_anything_is_pulled(self):
        self.assertEqual(self.rc, 1)
        self.assertIn("1/10", self.out)
        self.assertNotIn("2/10", self.out, "nothing is pulled before sudo is known to work")
        self.assertIn("sudo -v", self.out)

    def test_nothing_privileged_was_touched_first(self):
        self.assertNotIn("previous unit backed up", self.out)
        self.assertNotIn("Install failed at line", self.out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
