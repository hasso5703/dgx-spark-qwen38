"""A GPU driver refusal is reported when it happens, however many older ones leave the hour.

The collector counted NV_ERR_NO_MEMORY lines over the last hour and reported the count
growing: when as many old refusals left the window as new ones came in, the count stood
still and the new ones were never said (found in review, 2026-09-24)."""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# every module hands the next one the environment it found (test_env_isolation.py)
ENV_BEFORE = dict(os.environ)


def tearDownModule():
    for name in set(os.environ) - set(ENV_BEFORE):
        del os.environ[name]
    os.environ.update(ENV_BEFORE)


HERE = Path(__file__).resolve()
DASH = HERE.parents[1]


def line(ts):
    return f"{ts} spark kernel: NVRM: nvAssertOkFailedNoLog: Assertion failed: NV_ERR_NO_MEMORY"


class TheRefusals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-kernel-"))
        (cls.tmp / "api-key").write_text("k\n")
        os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.tmp), COCKPIT_REPO_DIR=str(DASH.parent),
                          COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0")
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_kernel", DASH / "cockpit.py")
        cls.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def sample(self, lines):
        self.cp.run = lambda argv, timeout=5.0, merge_err=False: "\n".join(lines) + "\n"
        return self.cp.collect_kernel()

    def test_a_refusal_that_replaces_an_older_one_is_said(self):
        events = []
        self.cp.add_event = lambda kind, text: events.append((kind, text))
        self.cp.KERNEL_LAST.clear()
        self.cp.KERNEL_LAST["count"] = None
        self.sample([line("2026-09-24T10:00:01+0200"), line("2026-09-24T10:30:00+0200")])
        self.assertEqual(events, [], "the first sample only sets the baseline")
        out = self.sample([line("2026-09-24T10:30:00+0200"), line("2026-09-24T11:05:00+0200")])
        self.assertEqual(out["nvrm_oom_1h"], 2)
        self.assertEqual(len(events), 1, events)
        self.assertIn("refused 1 allocation", events[0][1])
        self.sample([line("2026-09-24T10:30:00+0200"), line("2026-09-24T11:05:00+0200")])
        self.assertEqual(len(events), 1, "the same lines were reported twice")

    def test_a_read_that_did_not_answer_is_no_reading_and_moves_no_baseline(self):
        """A journalctl that timed out read as a quiet hour, "0", and became the baseline:
        the next read reported every refusal of the hour as new (found 2026-10-02)."""
        events = []
        self.cp.add_event = lambda kind, text: events.append((kind, text))
        self.cp.KERNEL_LAST.clear()
        self.cp.KERNEL_LAST["count"] = None
        two = [line("2026-09-24T10:00:01+0200"), line("2026-09-24T10:30:00+0200")]
        self.sample(two)

        def silent(argv, timeout=5.0, merge_err=False):
            r = self.cp.Ran("")
            r.ok = False
            return r
        self.cp.run = silent
        out = self.cp.collect_kernel()
        self.assertEqual((out["nvrm_oom_1h"], out["nvrm_last"]), (None, None), out)
        out = self.sample(two)
        self.assertEqual(out["nvrm_oom_1h"], 2)
        self.assertEqual(events, [], "refusals already seen were said again after a failed read")


if __name__ == "__main__":
    unittest.main(verbosity=2)
