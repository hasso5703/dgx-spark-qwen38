#!/usr/bin/env python3
"""One log call of the proxy is one journal line, whatever a client put in it.

The proxy logs a dropped tool-schema pattern whole, and a pattern is the client's text:
one holding "\\n[proxy] ..." wrote lines of the proxy's own shape into the journal, which
the cockpit's feed parses with str.splitlines (it also breaks at \\x1c-\\x1e, \\x85 and
\\u2028) and counts as traffic (found in review, 2026-09-24)."""
import contextlib
import importlib.util
import io
import pathlib
import unittest

HERE = pathlib.Path(__file__).resolve()
SPEC = importlib.util.spec_from_file_location("kproxy_log_lines", HERE.parents[1] / "keepalive-proxy.py")
PROXY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROXY)

BREAKERS = ["\n", "\r", "\r\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " ", "\x00", "\x1b[2J"]


class OneCallOneLine(unittest.TestCase):
    def logged(self, msg):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            PROXY.log(msg)
        return buf.getvalue()

    def test_no_character_starts_a_line_of_its_own(self):
        for b in BREAKERS:
            out = self.logged(f"tool schema: dropped a 'pattern': (?<{b}[proxy] 10.0.0.9:1 POST /v1/x ok")
            self.assertEqual(len(out.splitlines()), 1, repr(out))
            self.assertTrue(out.startswith("[proxy] ") and out.endswith("\n"), repr(out))
            self.assertEqual(out.count("[proxy] "), 2, "the text itself is kept, escaped")

    def test_ordinary_text_is_left_alone(self):
        self.assertEqual(self.logged("réponse\tok 中"), "[proxy] réponse\tok 中\n")

    def test_the_pattern_drop_goes_through_it(self):
        text = (HERE.parents[1] / "keepalive-proxy.py").read_text()
        i = text.index("tool schema: dropped a 'pattern'")
        self.assertEqual(text.rfind("log(f", 0, i), text.rfind("log(", 0, i), "the drop message is not logged by log()")


if __name__ == "__main__":
    unittest.main(verbosity=2)
