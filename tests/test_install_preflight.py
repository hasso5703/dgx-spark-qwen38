#!/usr/bin/env python3
"""The three port refusals of install.sh must say their own name.

install.sh validates PORT and PROXY_PORT long before the step functions are
defined, and until v1.10.2 `die` lived down at those steps: a bad PORT printed
"die: command not found" and exited through the ERR trap, so the one message
written to tell the operator what was wrong never appeared. These are the
first lines of the installer that can reject an invocation, which makes them
the first thing a user hits, which is exactly where a clean refusal matters.

Each case passes its ports through the environment (the convergence block
keeps an explicit value and reads nothing), runs against an empty HOME, and
fails if the output contains the ERR trap's generic wording instead of the
message: the guard is the failure mode itself, not just the exit code."""
import os
import subprocess
import tempfile
import unittest

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(REPO_DIR, "install.sh")


def run_install(**ports):
    """install.sh with only these ports in a minimal environment.

    The env is deliberately sparse: PATH and HOME apart, nothing the caller's
    shell happens to export can change which refusal fires. HOME is a temp dir
    so the convergence reads (guarded by -r anyway) can never find a real
    installed unit, and nothing on disk is touched either way: all three
    refusals exit before the preflight, on any machine."""
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin",
           "HOME": tempfile.mkdtemp()}
    env.update({k: str(v) for k, v in ports.items()})
    r = subprocess.run([INSTALL], capture_output=True, text=True, env=env, timeout=30)
    return r.returncode, r.stdout + r.stderr


class PortRefusals(unittest.TestCase):
    def test_non_numeric_port_refused_by_name(self):
        rc, out = run_install(PORT="abc")
        self.assertEqual(rc, 1)
        self.assertIn("PORT must be a number (got 'abc')", out)

    def test_non_numeric_proxy_port_refused_by_name(self):
        rc, out = run_install(PORT="30000", PROXY_PORT="30000x")
        self.assertEqual(rc, 1)
        self.assertIn("PROXY_PORT must be a number (got '30000x')", out)

    def test_engine_and_proxy_may_not_share_a_port(self):
        rc, out = run_install(PORT="30000", PROXY_PORT="30000")
        self.assertEqual(rc, 1)
        self.assertIn("cannot share a port", out)

    def test_no_refusal_hides_behind_a_missing_function(self):
        # The bug itself: die was defined below these checks, so every refusal
        # above died as "command not found" plus the ERR trap's generic line,
        # and the operator never saw which value was rejected.
        for ports in ({"PORT": "abc"},
                      {"PORT": "30000", "PROXY_PORT": "x1"},
                      {"PORT": "30000", "PROXY_PORT": "30000"}):
            rc, out = run_install(**ports)
            self.assertEqual(rc, 1, ports)
            self.assertNotIn("command not found", out, ports)
            self.assertNotIn("command not found".replace("not", "introuvable"), out, ports)
            self.assertNotIn("failed at line", out, ports)
            self.assertIn("ERROR:", out, ports)


if __name__ == "__main__":
    unittest.main(verbosity=2)
