#!/usr/bin/env python3
"""The cockpit installer takes the binds its servers can serve: IPv4 only.

The cockpit and its relay speak IPv4, and DASH_BIND=:: was accepted (the installer even had
a probe address for it): the unit failed at every start and systemd restarted it forever
(found in review, 2026-09-24). This runs dashboard/install-dashboard.sh's own lines."""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "dashboard" / "install-dashboard.sh").read_text()
START = TEXT.index("installed(){")
BLOCK = TEXT[START:TEXT.index("\nesac\n", TEXT.index("# The health probe below needs an address", START)) + 6]


def run(**env):
    d = pathlib.Path(tempfile.mkdtemp(prefix="dash-bind-"))
    script = ('set -euo pipefail\ndie(){ echo "DIE: $*"; exit 1; }\n'
              f'INSTALLED="{d}/none.service"\n' + BLOCK + '\necho "BIND=$BIND PROBE=$PROBE"\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", **env})
    return r.stdout


class OnlyIPv4(unittest.TestCase):
    def test_ipv4_binds_pass(self):
        self.assertIn("BIND=0.0.0.0 PROBE=127.0.0.1", run(DASH_BIND="0.0.0.0"))
        self.assertIn("BIND=100.114.54.60 PROBE=100.114.54.60", run(DASH_BIND="100.114.54.60"))
        self.assertIn("BIND=127.0.0.1", run())

    def test_ipv6_and_nonsense_are_refused(self):
        for bad in ("::", "[::]", "::1", "300.1.1.1", "1.2.3", "localhost"):
            out = run(DASH_BIND=bad)
            self.assertIn("DIE: DASH_BIND takes an IPv4 address", out, bad)
        self.assertIn("DIE: DASH_AGENT_BIND takes an IPv4 address", run(DASH_AGENT_BIND="::"))
        self.assertIn("BIND=127.0.0.1", run(DASH_AGENT_BIND="tailscale"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
