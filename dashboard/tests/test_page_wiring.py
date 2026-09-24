#!/usr/bin/env python3
"""Every action button on the page has a click handler.

app.js binds the action bar in one loop (`.actbar [data-act]`). A button with a data-act
anywhere else needs its own binding, and the Setup tab's "Fit the limits to this engine"
had none from 5d08667 to v1.18.6: it looked like every other button, the banner sent
people to it when the limits did not fit, and a click did nothing (found in review,
2026-09-24, confirmed in headless Chromium). This reads the page the way the browser
does, by element, and asks app.js for a binding of each button outside the bar.
"""
import html.parser
import pathlib
import re
import unittest

DASH = pathlib.Path(__file__).resolve().parents[1]
PAGE = (DASH / "static/index.html").read_text()
APP = (DASH / "static/app.js").read_text()


class Buttons(html.parser.HTMLParser):
    """(data-act, id, inside the action bar) for every element that carries a data-act."""

    def __init__(self):
        super().__init__()
        self.stack, self.found = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        in_bar = any("actbar" in (c or "").split() for c in self.stack)
        if "data-act" in a:
            self.found.append((a["data-act"], a.get("id"), in_bar))
        if tag not in ("input", "br", "img", "meta", "link", "hr", "source"):
            self.stack.append(a.get("class"))

    def handle_endtag(self, tag):
        if tag not in ("input", "br", "img", "meta", "link", "hr", "source") and self.stack:
            self.stack.pop()


def buttons():
    p = Buttons()
    p.feed(PAGE)
    return p.found


class EveryActionButtonIsWired(unittest.TestCase):
    def test_the_bar_is_bound_by_its_loop(self):
        self.assertIn("document.querySelectorAll('.actbar [data-act]')", APP)
        self.assertTrue(any(in_bar for _, _, in_bar in buttons()), "the parser sees the bar")

    def test_a_button_outside_the_bar_has_a_binding_of_its_own(self):
        outside = [(act, id_) for act, id_, in_bar in buttons() if not in_bar]
        self.assertTrue(outside, "the page has buttons outside the bar (the Setup tab's Fit)")
        for act, id_ in outside:
            with self.subTest(act=act, id=id_):
                by_id = id_ and re.search(r"\$\('%s'\)\.addEventListener\('click'" % re.escape(id_), APP)
                by_act = re.search(r"\[data-act=\"%s\"\]'\)\.forEach\(b =>\s*b\.addEventListener\('click'"
                                   % re.escape(act), APP)
                self.assertTrue(by_id or by_act, f"no click handler for data-act={act!r} id={id_!r}")

    def test_the_fit_modal_has_a_title(self):
        # without one the confirmation read "Confirm: fit_opencode" (the job strip has its
        # own label map, so the TITLES block itself is what is read)
        i = APP.index("const TITLES = {")
        titles = APP[i:APP.index("};", i)]
        self.assertIn("fit_opencode: () =>", titles)



class TheOldProxyWarningIsNotAFloatComparison(unittest.TestCase):
    """The Zombie guard panel warned below v6.14 with parseFloat(v) < 6.14, and as a float
    "6.9" is above "6.14": v6.2 to v6.9 read as proxies that abort (found in review,
    2026-09-24). The server compares the parts as numbers and sends the answer."""

    def test_the_page_reads_the_servers_answer(self):
        body = APP[APP.index("function rGuard(d)"):]
        body = body[:body.index("\nfunction ")]
        self.assertIn("g.predates_abort === true", body)
        self.assertNotRegex(body, r"parseFloat\(v\)")

if __name__ == "__main__":
    unittest.main(verbosity=2)
