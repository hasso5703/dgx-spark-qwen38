#!/usr/bin/env python3
"""The cockpit's two installers refuse root, as install.sh and install-image.sh do.

`sudo ./dashboard/install-dashboard.sh` rendered User=root and root's sudoers allowlist
and kept the unit's bind, and install-agent.sh ran opencode as root, so every tool call
of the Agent tab ran as root (found in review, 2026-09-24). Root is faked by an `id` on
PATH, as in test_install_root_refusal.py; sudo, systemctl and visudo are stubs that
record and refuse, so even a script with no refusal cannot change anything here."""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ("dashboard/install-dashboard.sh", "dashboard/install-agent.sh")

ID = ('#!/bin/sh\ncase "$*" in\n  -u) echo 0 ;;\n  -un) echo root ;;\n  -g) echo 0 ;;\n  -gn) echo root ;;\n'
      '  *) exec /usr/bin/id "$@" ;;\nesac\n')
REFUSE = '#!/bin/sh\necho "$(basename "$0") $*" >> "$HOME/privileged.log"\nexit 1\n'


class TheyRefuseRoot(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp(prefix="dash-root-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        for name, body in (("id", ID), ("sudo", REFUSE), ("systemctl", REFUSE), ("visudo", REFUSE),
                           ("tailscale", REFUSE), ("opencode", REFUSE)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)

    def run_script(self, script, **env):
        r = subprocess.run(["bash", str(REPO / script)], capture_output=True, text=True, timeout=60, cwd=str(REPO),
                           env={"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), **env})
        log = self.home / "privileged.log"
        return r.returncode, r.stdout + r.stderr, log.read_text() if log.exists() else ""

    def test_root_is_refused_before_anything_privileged(self):
        for script in SCRIPTS:
            rc, out, log = self.run_script(script)
            self.assertNotEqual(rc, 0, script)
            self.assertIn("not as root", out, f"{script}: {out[-300:]}")
            self.assertEqual(log, "", f"{script} reached a privileged command first: {log}")

    def test_the_message_names_the_login_under_sudo(self):
        for script in SCRIPTS:
            _, out, _ = self.run_script(script, SUDO_USER="alice")
            self.assertIn("your login is alice: drop the sudo", out, script)

    def test_allow_root_gets_past_the_refusal(self):
        for script in SCRIPTS:
            _, out, _ = self.run_script(script, ALLOW_ROOT="1")
            self.assertNotIn("not as root", out, script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
