"""An answer from GitHub counts as an answer, whatever the tag it names.

A release tagged outside semver (a "nightly", say) left the update check neither answered
nor failed, and it asked again every minute: GitHub's whole anonymous budget of 60 an
hour, for the box's address (found in review, 2026-09-24)."""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]

# The environment this module sets for the cockpit it loads is handed back when it ends,
# so the next module in the same process starts from what this one found.
ENV_BEFORE = {}


def setUpModule():
    ENV_BEFORE.update(os.environ)


def tearDownModule():
    for name in set(os.environ) - set(ENV_BEFORE):
        del os.environ[name]
    os.environ.update(ENV_BEFORE)


class TheBudget(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-upd-"))
        (cls.tmp / "api-key").write_text("k\n")
        os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.tmp), COCKPIT_REPO_DIR=str(DASH.parent),
                          COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0")
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_update_budget", DASH / "cockpit.py")
        cls.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_non_semver_tag_is_asked_once_in_six_hours(self):
        calls, clock = [], [1_000_000.0]
        self.cp._get_json = lambda url, timeout=5.0: (calls.append(url), {"tag_name": "nightly"})[1]
        real = self.cp.time

        class Clock:                                  # this module's clock only, not the process's
            def __getattr__(self, name):
                return getattr(real, name)

            def time(self):
                return clock[0]
        self.cp.time = Clock()
        self.cp._RELEASE.update(latest=None, ts=0.0, fails=0, answered=False)
        try:
            for _ in range(60):                      # an hour, one sample a minute
                self.cp.collect_update()
                clock[0] += 61
            self.assertEqual(len(calls), 1, f"{len(calls)} requests in an hour")
            clock[0] += 6 * 3600
            self.cp.collect_update()
            self.assertEqual(len(calls), 2)
        finally:
            self.cp.time = real


if __name__ == "__main__":
    unittest.main(verbosity=2)
