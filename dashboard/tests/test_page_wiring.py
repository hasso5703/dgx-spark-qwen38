#!/usr/bin/env python3
"""Every action control on the page does something when it is pressed.

The Settings tab's "Fit the limits to this engine" looked like every other button from
5d08667 to v1.18.6, the banner sent people to it when the limits did not fit, and a click
did nothing: it had no listener (found in review, 2026-09-24, confirmed in Chromium). The
page as rebuilt gives its action controls their data-act in script, so this runs the page
and presses each one: every control that names an action must open the sheet, under a
title of its own rather than the action's wire name.
"""
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pagejs  # noqa: E402
from test_page_behaviour import HELPERS  # noqa: E402

DASH = pathlib.Path(__file__).resolve().parents[1]
JS = {p.name: p.read_text() for p in (DASH / "static" / "js").glob("*.js")}
ACTIONS = ("unit", "switch", "flush_cache", "abort_all", "fit_opencode", "diag_bundle", "smoke")


def run(test, body, **kw):
    return pagejs.run(test, HELPERS + body, **kw)


class EveryActionButtonIsWired(unittest.TestCase):
    def test_every_control_that_names_an_action_opens_the_sheet(self):
        r = run(self, r"""
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')}),
              engine_fast: {load: [{num_reqs: 0}], healthy: true}});
        openMenu();
        const seen = [];
        for (const b of document.querySelectorAll('[data-act]')){
          const act = b.dataset.act; b.disabled = false; b.click();
          seen.push({act, opened: !$('scrim').hidden, title: txt('sh-title'), cmd: txt('sh-cmd')});
          closeSheet();
        }
        report(seen);
        """)
        acts = {s["act"] for s in r}
        self.assertTrue({"smoke", "flush_cache", "abort_all", "diag_bundle", "fit_opencode"} <= acts, acts)
        for s in r:
            with self.subTest(act=s["act"]):
                self.assertTrue(s["opened"], f"{s['act']} does nothing when pressed")
                self.assertNotIn("_", s["title"], "a title of its own, not the action's wire name")
                self.assertTrue(s["cmd"], "the sheet shows the command")

    def test_every_action_the_server_knows_has_a_title_a_verb_and_an_explanation(self):
        base = JS["base.js"]
        for block in ("const TITLE = {", "const VERB = {", "const EXPLAIN = {", "const ACTION_PHRASE = {"):
            i = base.index(block)
            body = base[i:base.index("\nconst ", i + 1)]
            for act in ACTIONS:
                with self.subTest(block=block, act=act):
                    self.assertRegex(body, r"\b%s: " % re.escape(act))

    def test_the_settings_fit_button_is_bound(self):
        self.assertIn("$('oc-fitbtn').addEventListener('click'", JS["ops.js"])


class TheOldProxyWarningIsNotAFloatComparison(unittest.TestCase):
    """The guard panel warned below v6.14 with parseFloat(v) < 6.14, and as a float "6.9" is
    above "6.14": v6.2 to v6.9 read as proxies that abort (found in review, 2026-09-24). The
    server compares the parts as numbers and sends the answer."""

    def test_the_page_reads_the_servers_answer(self):
        ops = JS["ops.js"]
        body = ops[ops.index("on('reqguard'"):]
        body = body[:body.index("\n});")]
        self.assertIn("g.predates_abort", body)
        self.assertNotRegex(body, r"parseFloat\(")


if __name__ == "__main__":
    unittest.main(verbosity=2)
