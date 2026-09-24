"""In a dry run the automatic belts are said and audited, and nothing is sent.

The dry run exists to show every mutating action and every automatic belt as usual without
executing them. The memory floor raised instead of acting, which left a failed audit line
and no event, and the pool guard faked a 400, which reads "not idle after all", and stood
down in silence (found in review, 2026-09-24)."""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]


class TheFloor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-dry-"))
        (cls.tmp / "api-key").write_text("k\n")
        os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.tmp), COCKPIT_REPO_DIR=str(DASH.parent),
                          COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0")
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_dry_belts", DASH / "cockpit.py")
        cls.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_memory_floor_is_said_and_not_sent(self):
        cp = self.cp
        self.assertTrue(cp.DRY_RUN)
        events, audits, sent = [], [], []
        cp.add_event = lambda kind, text: events.append((kind, text))
        cp.audit = lambda rec: audits.append(rec)
        cp.engine_abort_all = lambda *a, **k: sent.append(1)
        cp.engine_load = lambda timeout=3: [{"num_reqs": 2, "num_waiting_reqs": 0}]
        cp.mem_available_gib = lambda: 0.5
        cp.MEM_FLOOR["last_abort"] = 0.0
        cp.collect_engine_fast()
        self.assertEqual(sent, [], "a dry run sent the abort")
        self.assertTrue([e for e in events if e[0] == "mem_floor" and "dry run: no abort sent" in e[1]], events)
        self.assertTrue([a for a in audits if a.get("kind") == "mem_floor" and a.get("dry_run")], audits)

    def test_the_pool_guard_speaks_in_a_dry_run(self):
        src = (DASH / "cockpit.py").read_text()
        i = src.index("flush not sent (dry run)")
        block = src[i - 1500:i + 600]
        self.assertNotIn('raise urllib.error.HTTPError(ENGINE_BASE, 400, "dry run', block)
        self.assertIn("if not DRY_RUN:", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
