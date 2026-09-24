#!/usr/bin/env python3
"""SECURITY.md says what the cockpit's user can reach as root, as the code has it.

It said the cockpit reached root through "exactly one surface", its sudoers lines, while
install.sh requires the user to be in the docker group (root-equivalent by itself) and
two of those lines install a file the user writes into /etc/systemd/system, next to
daemon-reload and restart: whoever writes it runs what it says as root (found in review,
2026-09-24). These tie the page to the two facts, so it cannot drift back."""
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SECURITY = (REPO / "SECURITY.md").read_text()
FLAT = re.sub(r"\s+", " ", SECURITY)


class TheCockpitsRootIsStatedAsItIs(unittest.TestCase):
    def test_no_single_surface_claim(self):
        self.assertNotIn("reaches root through exactly one", FLAT)

    def test_the_docker_group_is_named_while_install_sh_requires_it(self):
        if "usermod -aG docker" in (REPO / "install.sh").read_text():
            self.assertIn("`docker` group", FLAT)
            self.assertIn("root-equivalent", FLAT)

    def test_the_staged_unit_install_is_named_while_the_allowlist_has_it(self):
        sudoers = (REPO / "dashboard" / "sudoers-cockpit.template").read_text()
        if re.search(r"/usr/bin/install .*switch-stage /etc/systemd/system/", sudoers):
            self.assertIn("switch-stage", FLAT)
            self.assertIn("runs what it says as root", FLAT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
