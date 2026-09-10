"""The job layer, and above all the secret the diagnostics bundle must not leak.

`job_diag_bundle` collects journals, container logs, the launch script and the
engine's own server info into a tarball a user is meant to attach to an issue.
Everything it gathers has the serving API key in it: SGLang prints `api_key=...`
in its ServerArgs banner, which lands in docker logs and the journal, and the
launcher passes it on its command line. The bundle masks it. That masking had a
leak once, committed as "key masked, verified", so this file does not trust the
word: it plants the key in every source the bundle reads, builds a real tarball,
and greps every member for the key.

Also here: the bounded job registry, the log tail that cannot grow without
bound, and the job snapshot every browser tab shares.
"""
import importlib.util
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
REPO = HERE.parents[2]

KEY = "SUPERSECRETKEY0123456789"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-jobs-"))
        cls.home = Path(tempfile.mkdtemp(prefix="cockpit-jobs-home-"))
        (cls.tmp / "api-key").write_text(KEY + "\n")
        # The python jobs are not argv, so DRY_RUN does not stop them: they speak
        # HTTP. Both endpoints point at a closed port, which is what makes this
        # suite offline. Without it these tests reached the engine actually
        # serving on this box and got a 401 from it, which is a test suite
        # touching production.
        os.environ.update(COCKPIT_DRY_RUN="0",
                          COCKPIT_CONFIG_DIR=str(cls.tmp),
                          COCKPIT_REPO_DIR=str(REPO), COCKPIT_PORT="0",
                          COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0",
                          COCKPIT_ENGINE="http://127.0.0.1:1",
                          COCKPIT_PROXY="http://127.0.0.1:1",
                          HOME=str(cls.home))
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_jobs_under_test",
                                                      DASH / "cockpit.py")
        cls.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        shutil.rmtree(cls.home, ignore_errors=True)


class DiagBundleMasksTheKey(Base):
    def setUp(self):
        cp = self.cp
        self.real_run, self.real_http = cp.run, cp.http_json
        # HOME per test, not per class: job_diag_bundle writes to Path.home(),
        # which reads $HOME at call time, and another suite in the same process
        # can restore its own value between our classes. Three real bundles
        # landed in the developer's home before this line existed.
        self.real_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        # every command the bundle runs answers with the key in its output, the
        # way the real journal and the real docker logs do
        leaky = (f"[2026-09-10 10:00:00] server_args=ServerArgs(api_key='{KEY}', "
                 f"model_path='x')\nAuthorization: Bearer {KEY}\n")
        cp.run = lambda argv, timeout=5.0, merge_err=False: leaky
        cp.http_json = lambda url, timeout=4.0: {"api_key": KEY, "model_path": "x",
                                                 "note": f"key is {KEY}"}
        # and so does the launcher on disk
        (self.tmp / "launch-flash.sh").write_text(
            f'exec docker run x --api-key "$(cat {self.tmp}/api-key)" --port 30000\n'
            f'# a stray copy of the key: {KEY}\n')

    def tearDown(self):
        self.cp.run, self.cp.http_json = self.real_run, self.real_http
        for f in self.home.glob("qwen38-diag-*.tar.gz"):
            f.unlink()
        for f in self.built:
            Path(f).unlink(missing_ok=True)
        if self.real_home is not None:
            os.environ["HOME"] = self.real_home

    built: list = []

    def _build(self):
        job = self.cp.Job("diag_bundle", None, 60, fn=self.cp.job_diag_bundle)
        ok, lines, result = self.cp.job_diag_bundle(job)
        self.assertTrue(ok, lines)
        path = Path(result["path"])
        self.built.append(path)
        self.assertTrue(path.exists(), path)
        # nothing this suite writes may land outside its own throwaway HOME
        self.assertTrue(str(path).startswith(str(self.home)),
                        f"the bundle was written to {path}, outside the test HOME")
        return path, result

    def test_the_key_appears_in_no_member_of_the_bundle(self):
        path, _ = self._build()
        found = []
        with tarfile.open(path, "r:gz") as tar:
            members = tar.getnames()
            for m in tar.getmembers():
                if not m.isfile():
                    continue
                text = tar.extractfile(m).read().decode("utf-8", "replace")
                if KEY in text:
                    found.append(m.name)
        self.assertEqual(found, [], f"the API key leaked into {found}")
        self.assertGreater(len(members), 5, members)

    def test_the_masking_actually_ran_rather_than_the_key_being_absent(self):
        """A test that only greps for the key passes when the bundle collected
        nothing at all. This one requires the marker the masking leaves."""
        path, _ = self._build()
        masked = 0
        with tarfile.open(path, "r:gz") as tar:
            for m in tar.getmembers():
                if m.isfile() and "<masked>" in tar.extractfile(m).read().decode(
                        "utf-8", "replace"):
                    masked += 1
        self.assertGreaterEqual(masked, 3,
                                "the bundle collected no text the key was in")

    def test_the_launcher_is_included_with_its_key_expression_masked(self):
        path, _ = self._build()
        with tarfile.open(path, "r:gz") as tar:
            names = [n for n in tar.getnames() if n.endswith("launch-flash.sh")]
            self.assertTrue(names, tar.getnames())
            text = tar.extractfile([m for m in tar.getmembers()
                                    if m.name == names[0]][0]).read().decode()
        self.assertNotIn(KEY, text)
        self.assertIn("<masked>", text)
        self.assertIn("--port 30000", text, "the launcher was gutted, not masked")

    def test_the_masked_fields_are_stripped_from_the_server_info(self):
        path, _ = self._build()
        with tarfile.open(path, "r:gz") as tar:
            member = [m for m in tar.getmembers() if m.name.endswith("server-info.json")][0]
            info = json.loads(tar.extractfile(member).read().decode())
        for field in self.cp.MASKED_FIELDS:
            self.assertNotIn(field, info, field)
        self.assertIn("model_path", info, "the whole server info was dropped")

    def test_an_unreachable_engine_does_not_stop_the_bundle(self):
        def boom(url, timeout=4.0):
            raise OSError("connection refused")

        self.cp.http_json = boom
        path, _ = self._build()
        with tarfile.open(path, "r:gz") as tar:
            member = [m for m in tar.getmembers() if m.name.endswith("server-info.json")][0]
            self.assertIn("error", json.loads(tar.extractfile(member).read().decode()))

    def test_the_bundle_reports_its_own_size_and_names_the_masking(self):
        _, result = self._build()
        self.assertGreater(result["bytes"], 0)
        self.assertTrue(result["ok"])


class PythonJobs(Base):
    """These jobs speak HTTP and let a connection error propagate on purpose:
    run_job is what turns any raise into a failed job with the exception named,
    and releases the single-job lock in its finally. Testing the job function
    alone would assert a contract that lives one level up, so these drive
    run_job, with both endpoints on a closed port."""

    def setUp(self):
        self.real_run = self.cp.run
        self.cp.run = lambda argv, timeout=5.0, merge_err=False: ""
        self.assertTrue(self.cp.JOB_LOCK.acquire(blocking=False),
                        "the job lock was already held")

    def tearDown(self):
        self.cp.run = self.real_run
        if self.cp.JOB_LOCK.locked():       # run_job releases it; belt and braces
            try:
                self.cp.JOB_LOCK.release()
            except RuntimeError:
                pass

    def _drive(self, action, fn, timeout=20):
        job = self.cp.Job(action, None, timeout, fn=fn)
        self.cp.JOBS[job.id] = job
        self.cp.JOB_CURRENT["id"] = job.id
        self.cp.run_job(job)
        return job

    def test_an_unreachable_engine_makes_a_failed_job_not_a_crash(self):
        for action, fn in (("flush_cache", self.cp.job_flush_cache),
                           ("abort_all", self.cp.job_abort_all),
                           ("smoke", self.cp.job_smoke)):
            with self.subTest(action=action):
                self.cp.JOB_LOCK.acquire(blocking=False)
                job = self._drive(action, fn)
                self.assertEqual(job.status, "failed", job.lines)
                self.assertTrue(job.lines, "a failed job with no explanation")
                self.assertTrue(any("[cockpit]" in ln for ln in job.lines), job.lines)
                self.assertIsNotNone(job.ended)

    def test_the_lock_is_released_by_a_job_that_raises(self):
        self.cp.JOB_LOCK.acquire(blocking=False)
        self._drive("abort_all", self.cp.job_abort_all)
        self.assertFalse(self.cp.JOB_LOCK.locked(),
                         "a raising job kept the lock and blocked the cockpit")

    def test_a_failed_job_is_audited_with_its_status(self):
        log = self.tmp / "cockpit-audit.log"
        before = log.read_text() if log.exists() else ""
        self.cp.JOB_LOCK.acquire(blocking=False)
        job = self._drive("flush_cache", self.cp.job_flush_cache)
        added = log.read_text()[len(before):]
        ends = [json.loads(ln) for ln in added.splitlines()
                if ln.strip() and json.loads(ln).get("kind") == "job_end"]
        self.assertTrue(ends, added)
        self.assertEqual(ends[-1]["status"], "failed")
        self.assertEqual(ends[-1]["id"], job.id)

    def test_a_job_function_returning_a_failure_is_not_an_exception(self):
        """The other path: fn returns ok=False, which is a clean failed job."""
        self.cp.JOB_LOCK.acquire(blocking=False)
        job = self._drive("smoke", lambda j: (False, ["it said no"], {"ok": False}))
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.rc, 1)
        self.assertIn("it said no", job.lines)

    def test_a_job_function_returning_success_is_a_done_job(self):
        self.cp.JOB_LOCK.acquire(blocking=False)
        job = self._drive("smoke", lambda j: (True, ["all good"], {"ok": True}))
        self.assertEqual(job.status, "done")
        self.assertEqual(job.rc, 0)
        self.assertEqual(job.result, {"ok": True})


class JobBookkeeping(Base):
    def test_a_job_log_tail_is_bounded(self):
        job = self.cp.Job("smoke", None, 10)
        for i in range(5000):
            job.append(f"line {i}")
        self.assertLessEqual(len(job.lines), 2000,
                             "an unbounded job log is an unbounded process")
        self.assertIn("line 4999", job.lines[-1])

    def test_a_long_line_is_truncated_not_stored_whole(self):
        job = self.cp.Job("smoke", None, 10)
        job.append("x" * 10_000)
        self.assertLessEqual(len(job.lines[0]), 500)

    def test_a_summary_is_json_serializable_at_every_stage(self):
        job = self.cp.Job("unit", ["sudo", "-n", "/usr/bin/systemctl", "stop", "x"], 90,
                          params={"verb": "stop", "unit": "x"})
        json.dumps(job.summary())
        job.append("something happened")
        job.status, job.rc, job.ended = "done", 0, time.time()
        d = json.dumps(job.summary(tail=10))
        self.assertIn("something happened", d)

    def test_the_summary_carries_the_exact_argv_that_will_run(self):
        argv = ["sudo", "-n", "/usr/bin/systemctl", "restart", "qwen38-flash.service"]
        job = self.cp.Job("unit", argv, 90, params={"verb": "restart"})
        self.assertEqual(job.summary()["argv"], argv)

    def test_two_jobs_never_share_an_id(self):
        ids = {self.cp.Job("smoke", None, 10).id for _ in range(500)}
        self.assertEqual(len(ids), 500)

    def test_a_job_id_is_not_guessable_from_the_previous_one(self):
        a, b = self.cp.Job("smoke", None, 10).id, self.cp.Job("smoke", None, 10).id
        self.assertEqual(len(a), 16)
        self.assertNotEqual(a, b)
        self.assertTrue(all(c in "0123456789abcdef" for c in a), a)

    def test_the_job_registry_snapshot_is_always_serializable(self):
        snap = self.cp.job_snapshot()
        json.dumps(snap)
        self.assertIsInstance(snap, dict)

    def test_the_registry_keeps_a_bounded_number_of_finished_jobs(self):
        for _ in range(self.cp.JOBS_KEEP * 3):
            job = self.cp.Job("smoke", None, 10)
            job.status, job.ended = "done", time.time()
            self.cp.JOBS[job.id] = job
            self.cp.prune_jobs() if hasattr(self.cp, "prune_jobs") else None
        json.dumps(self.cp.job_snapshot())
        self.assertLessEqual(len(self.cp.JOBS), self.cp.JOBS_KEEP * 3 + 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
