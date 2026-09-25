#!/usr/bin/env python3
"""systemone-check.py --hosted sends every shape, and says so when it cannot.

It sent the first 6 of its 16 shapes to the hosted Jev, which leaves out every edge of the
contract (the caps, the one-option choice, the empty and the unicode option names), and
with no TypeSafe key file it skipped the section without a word, so --hosted could pass
having sent nothing (found in review, 2026-09-24). A fake post() stands in for both ends.
"""
import importlib.util
import io
import pathlib
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("systemone_check_hosted", REPO / "systemone-check.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)


class TheHostedComparison(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="so-hosted-"))
        self.addCleanup(shutil.rmtree, self.t, ignore_errors=True)
        self.sent = []

    def fake_post(self, base, path, payload, key, timeout=300):
        self.sent.append(base)
        return 200, {}, {"answers": {qid: {"type": "noul", "noul": 0.5} for qid in payload["questions"]}}, 0.1

    def run_parity(self, key_file):
        rep = sc.Report()
        with mock.patch.object(sc, "post", self.fake_post), redirect_stdout(io.StringIO()):
            sc.hosted_parity(rep, "http://127.0.0.1:9", "k", hosted_key=key_file)
        return rep

    def test_every_shape_goes_to_both(self):
        key = self.t / "typesafe-api-key"
        key.write_text("ts-key\n")
        rep = self.run_parity(key)
        n = len(sc.shapes())
        self.assertEqual(n, 16)
        self.assertEqual(self.sent.count(sc.HOSTED), n)
        self.assertEqual(len(self.sent), 2 * n)
        self.assertEqual(rep.failed, 0)

    def test_no_key_is_a_failed_check_and_nothing_is_sent(self):
        rep = self.run_parity(self.t / "absent")
        self.assertEqual(self.sent, [])
        self.assertEqual(rep.failed, 1)


if __name__ == "__main__":
    unittest.main()
