"""A command that did not answer is not an answer.

run() gives "" on a timeout, and two readers took that for a fact: a git status that timed
out on a loaded box reported the checkout clean, and a docker inspect that did reported a
container without the request-id override (found in review, 2026-09-24). They read an
exit status now, and say unknown."""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]


class Unanswered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-unanswered-"))
        (cls.tmp / "api-key").write_text("k\n")
        os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.tmp), COCKPIT_REPO_DIR=str(DASH.parent),
                          COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0")
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_unanswered", DASH / "cockpit.py")
        cls.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_run_ok_tells_a_timeout_from_an_empty_answer(self):
        self.assertEqual(self.cp.run_ok(["true"]), (True, ""))
        self.assertEqual(self.cp.run_ok(["false"]), (False, ""))
        self.assertEqual(self.cp.run_ok(["sleep", "5"], timeout=0.2), (False, ""))

    def test_a_git_status_that_timed_out_is_not_a_clean_tree(self):
        real = self.cp.subprocess.run

        def slow_git(argv, **kw):
            if argv[:1] == ["git"] and "status" in argv:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout", 5))
            return real(argv, **kw)
        self.cp.subprocess.run = slow_git
        try:
            out = self.cp.collect_repo()
        finally:
            self.cp.subprocess.run = real
        self.assertIsNone(out["dirty"])
        self.assertIsNone(out["untracked"])

    def test_the_page_says_unknown(self):
        js = (DASH / "static" / "app.js").read_text()
        self.assertIn("d.dirty == null ? 'unknown (git did not answer)'", js)
        self.assertIn("'unknown (docker did not answer)'", js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
