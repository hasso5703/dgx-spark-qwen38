"""What happens when several things happen at once.

Every other suite drives one request at a time, which is not how this stack is
used: a person clicks Switch in one browser tab while the autoheal fires and a
second tab is already polling, and the cockpit's whole safety story is that only
ONE privileged action ever runs. The proxy has the same shape on the other side,
where four requests share an engine.

Threads make failures probabilistic, so these tests hammer rather than sample,
and every one of them asserts a property that must hold at EVERY interleaving
rather than a sequence of events:

  * exactly one action starts, whatever the number of simultaneous callers;
  * the loser is told it lost (409 busy), never silently dropped;
  * the lock is always released, so the next action is possible;
  * the audit log has one line per started job and no torn lines;
  * shared collector state stays a dict readers can serialize at any moment.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
REPO = HERE.parents[2]

RACERS = 24          # simultaneous callers per attempt
ATTEMPTS = 8         # attempts per test: a race that fails 1 in 5 must be seen


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-conc-"))
        (cls.tmp / "api-key").write_text("k\n")
        os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.tmp),
                          COCKPIT_REPO_DIR=str(REPO), COCKPIT_PORT="0",
                          COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0")
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_conc_under_test",
                                                      DASH / "cockpit.py")
        cls.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def wait_idle(self, timeout=6.0):
        end = time.time() + timeout
        while time.time() < end:
            if not self.cp.JOB_LOCK.locked():
                return True
            time.sleep(0.01)
        return False

    def setUp(self):
        self.assertTrue(self.wait_idle(), "a previous test left the job lock held")
        self.cp.JOB_CURRENT["id"] = None


class OneJobAtATime(Base):
    def _storm(self, action="smoke", params=None):
        """Fire RACERS calls at the same instant and return their codes."""
        gate = threading.Barrier(RACERS)
        out = []
        lock = threading.Lock()

        def go():
            gate.wait()
            code, body = self.cp.start_action(action, dict(params or {}))
            with lock:
                out.append((code, body))

        with ThreadPoolExecutor(max_workers=RACERS) as pool:
            list(pool.map(lambda _: go(), range(RACERS)))
        return out

    def test_exactly_one_of_many_simultaneous_actions_starts(self):
        for attempt in range(ATTEMPTS):
            results = self._storm()
            started = [b for c, b in results if c == 202]
            busy = [b for c, b in results if c == 409]
            self.assertEqual(len(started), 1,
                             f"attempt {attempt}: {len(started)} jobs started at once")
            self.assertEqual(len(busy), RACERS - 1, f"attempt {attempt}")
            for b in busy:
                self.assertEqual(b["error"], "busy")
                self.assertIn("already running", b["message"])
            self.assertTrue(self.wait_idle(), f"attempt {attempt}: the lock stayed held")

    def test_every_caller_gets_an_answer_and_none_is_dropped(self):
        results = self._storm()
        self.assertEqual(len(results), RACERS)
        for code, body in results:
            self.assertIn(code, (202, 409))
            self.assertIsInstance(body, dict)
        self.assertTrue(self.wait_idle())

    def test_a_rejected_parameter_never_takes_the_lock(self):
        """A 400 must not cost the next caller its turn: the validation happens
        before the lock is acquired, and this proves the order."""
        for _ in range(ATTEMPTS):
            code, _ = self.cp.start_action("unit", {"verb": "nope", "unit": "nope"})
            self.assertEqual(code, 400)
            self.assertFalse(self.cp.JOB_LOCK.locked(),
                             "a refused action took the job lock with it")

    def test_an_unknown_action_never_takes_the_lock(self):
        code, _ = self.cp.start_action("definitely-not-an-action", {})
        self.assertEqual(code, 404)
        self.assertFalse(self.cp.JOB_LOCK.locked())

    def test_the_lock_is_released_even_when_starting_raises(self):
        """start_action catches everything around the job start precisely so the
        lock cannot leak; this makes it raise on purpose to prove it."""
        spec = self.cp.ACTIONS["smoke"]
        original = spec["argv"]
        spec["argv"] = lambda p: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            code, body = self.cp.start_action("smoke", {})
            self.assertEqual(code, 500, body)
            self.assertIn("RuntimeError", body["error"])
            self.assertFalse(self.cp.JOB_LOCK.locked(),
                             "the job lock leaked on a failed start")
        finally:
            spec["argv"] = original

    def test_the_audit_log_has_one_unbroken_line_per_started_job(self):
        log = self.tmp / "cockpit-audit.log"
        before = len(log.read_text().splitlines()) if log.exists() else 0
        starts = 0
        for _ in range(ATTEMPTS):
            results = self._storm()
            starts += sum(1 for c, _ in results if c == 202)
            self.assertTrue(self.wait_idle())
        time.sleep(0.2)
        lines = log.read_text().splitlines()[before:]
        for line in lines:
            json.loads(line)              # a torn line raises here
        job_starts = [json.loads(x) for x in lines
                      if '"kind": "job_start"' in x or '"kind":"job_start"' in x]
        self.assertEqual(len(job_starts), starts,
                         "the audit log and the started jobs disagree")


class SharedStateStaysReadable(Base):
    def test_the_state_snapshot_can_be_serialized_while_collectors_write(self):
        """The SSE stream json.dumps(STATE) under STATE_LOCK while samplers write
        into it. A reader that catches a half-updated dict would raise, and the
        browser would lose its stream."""
        stop = threading.Event()
        errors = []

        def writer(name):
            i = 0
            while not stop.is_set():
                with self.cp.STATE_LOCK:
                    self.cp.STATE[name] = {"data": {"n": i, "list": list(range(20))},
                                           "ts": time.time()}
                i += 1

        def reader():
            while not stop.is_set():
                try:
                    with self.cp.STATE_LOCK:
                        json.dumps(self.cp.STATE)
                except Exception as e:          # noqa: BLE001
                    errors.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=writer, args=(f"probe{i}",), daemon=True)
                   for i in range(4)]
        threads += [threading.Thread(target=reader, daemon=True) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(1.0)
        stop.set()
        for t in threads:
            t.join(timeout=3)
        self.assertEqual(errors[:5], [], f"{len(errors)} readers failed")

    def test_the_event_ring_survives_concurrent_writers(self):
        stop = threading.Event()
        errors = []

        def spam(tag):
            while not stop.is_set():
                try:
                    self.cp.add_event("test", f"{tag} says something")
                except Exception as e:          # noqa: BLE001
                    errors.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=spam, args=(i,), daemon=True) for i in range(6)]
        for t in threads:
            t.start()
        time.sleep(0.6)
        stop.set()
        for t in threads:
            t.join(timeout=3)
        self.assertEqual(errors[:5], [])
        # and the ring is still a bounded deque of dicts a reader can serialize
        with self.cp.EVENTS_LOCK:
            snapshot = list(self.cp.EVENTS)
        json.dumps(snapshot)
        self.assertLessEqual(len(snapshot), self.cp.EVENTS.maxlen,
                             "the event ring grew past its bound")
        self.assertTrue(all(isinstance(e, dict) for e in snapshot))

    def test_the_cpu_tracker_never_returns_nonsense_under_parallel_sampling(self):
        """CpuTracker keeps the previous /proc/stat reading in a plain dict and is
        sampled by more than one tier. Whatever the interleaving, every per-cpu
        percentage it returns must stay a percentage: a torn previous reading
        would produce a negative delta or one far above 100."""
        out = []

        def sample():
            for _ in range(60):
                out.append(self.cp.CPU.sample())

        threads = [threading.Thread(target=sample, daemon=True) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertTrue(out, "no sample was taken")
        for rows in out:
            self.assertIsInstance(rows, dict)
            for name, pct in rows.items():
                self.assertTrue(name.startswith("cpu"), name)
                self.assertIsInstance(pct, float)
                self.assertGreaterEqual(pct, 0.0, f"{name} read {pct}%")
                self.assertLessEqual(pct, 100.0, f"{name} read {pct}%")
        self.assertIn("cpu", out[-1], "the aggregate cpu row disappeared")


if __name__ == "__main__":
    unittest.main(verbosity=2)
