#!/usr/bin/env python3
"""The install summary sends clients to the proxy.

It printed http://<host>:$PORT for the OpenAI and Anthropic endpoints: the engine's own
port, bound to loopback since v1.17, so unreachable from the machine the summary is read
for, and without the proxy's guards, which refuse what takes the engine down (found in
review, 2026-09-24). The README has told clients to use :30001 since v1.5.
"""
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()


class TheSummaryNamesTheProxy(unittest.TestCase):
    def test_every_client_endpoint_is_the_proxy(self):
        lines = [ln for ln in TEXT.splitlines() if re.search(r'echo "  (OpenAI|Anthropic|Clients) +:', ln)]
        self.assertGreaterEqual(len(lines), 3, "the summary's endpoint lines moved")
        for ln in lines:
            self.assertIn("$PROXY_PORT", ln, ln)
            self.assertNotRegex(ln, r":\$PORT\b", ln)



class TheNoServiceSummaryNamesWhatItLeft(unittest.TestCase):
    """It said the install lived in two folders, delete those to remove it: the serving
    image, opencode, the oc launcher and the provider added to the user's opencode config
    were left out, and that provider reads the key in one of the two folders, so deleting
    it as told left an opencode that refuses to start (found in review, 2026-09-24)."""

    def test_it_points_at_the_inventory_and_the_provider(self):
        block = TEXT[TEXT.index("Prepared (no systemd"):TEXT.index("exit 0", TEXT.index("Prepared (no systemd"))]
        self.assertIn("./uninstall.sh --list", block)
        self.assertIn("provider", block)
        self.assertNotIn("delete those to remove", block)

if __name__ == "__main__":
    unittest.main(verbosity=2)
