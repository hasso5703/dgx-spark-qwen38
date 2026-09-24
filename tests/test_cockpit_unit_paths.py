#!/usr/bin/env python3
"""The cockpit unit starts whether or not its config folder exists yet.

ReadWritePaths= names paths systemd binds into the unit's mount namespace, and a path that
is not there fails the start at that step (226/NAMESPACE) unless it is marked optional
with "-": a cockpit installed on its own, or a removed config folder, never started
(found in review, 2026-09-24). /etc/systemd/system always exists, and the Switch button
needs it bound, so it stays mandatory."""
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
UNIT = (REPO / "dashboard" / "qwen38-dashboard.service.template").read_text()


class ThePaths(unittest.TestCase):
    def test_the_config_folder_is_optional(self):
        rw = [ln.split("=", 1)[1] for ln in UNIT.splitlines() if ln.startswith("ReadWritePaths=")]
        self.assertIn("-__HOME__/.config/qwen38", rw)
        self.assertIn("/etc/systemd/system", rw)
        home = [p for p in rw if "__HOME__" in p]
        self.assertTrue(all(p.startswith("-") for p in home), home)


if __name__ == "__main__":
    unittest.main(verbosity=2)
