"""A text lane that stopped is told what is known about why (doctor.py's crash signatures).

Once per run: the run's own journal (its systemd invocation) and the kernel's lines of the 15
minutes before, through the exact sudo argv collect_kernel uses. A journal that did not answer
is read again; the API key never reaches the page; a doctor.py that does not load costs the
cockpit nothing but the causes."""
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

ENV_BEFORE = dict(os.environ)


def tearDownModule():
    for name in set(os.environ) - set(ENV_BEFORE):
        del os.environ[name]
    os.environ.update(ENV_BEFORE)


HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
CUBLAS = ("RuntimeError: CUDA error: CUBLAS_STATUS_INTERNAL_ERROR when calling `cublasGemmEx( handle, opa, "
          "opb, m, n, k, &falpha, a, CUDA_R_16BF, lda, b, CUDA_R_16BF, ldb, &fbeta, c, CUDA_R_16BF)`")


def iso(t):
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


class TheCrashCause(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-crash-"))
        (cls.tmp / "api-key").write_text("sk-the-real-key\n")
        os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.tmp), COCKPIT_REPO_DIR=str(DASH.parent),
                          COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0")
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_crash", DASH / "cockpit.py")
        cls.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.cp.CRASH_CAUSE.clear()
        self.events, self.calls = [], []
        self.cp.add_event = lambda kind, text: self.events.append((kind, text))
        self.now = time.time()
        self.journal = ["[2026-10-02 15:01:16] Decode batch, #running-req: 4", CUBLAS]
        self.kernel = [f"{iso(self.now - 3600)} gx10 kernel: NVRM: Xid (PCI:000f:01:00): 13, too old",
                       f"{iso(self.now - 5)} gx10 kernel: NVRM: Xid (PCI:000f:01:00): 31, pid=4242, name=python3",
                       "not a dated line"]
        self.journal_ok = True

        def fake(argv, timeout=5.0, merge_err=False):
            self.calls.append(list(argv))
            if argv[0] == "journalctl":
                r = self.cp.Ran("\n".join(self.journal) + "\n" if self.journal_ok else "")
                r.ok = self.journal_ok
                return r
            return self.cp.Ran("\n".join(self.kernel) + "\n")
        self.cp.run = fake

    def test_a_known_cause_with_the_xid_of_the_same_minutes(self):
        rec = self.cp.crash_cause("qwen38-sglang.service", "inv-1", now=self.now)
        c = rec["cause"]
        self.assertEqual(c["id"], "cublas-internal-error")
        self.assertEqual(c["xids"], [31], "the Xid of an hour ago is another story")
        self.assertEqual(self.events, [("crash", "qwen38-sglang stopped: cuBLAS internal error in a GEMM (Xid 31)")])
        self.assertIn(["journalctl", "_SYSTEMD_INVOCATION_ID=inv-1", "-n", "400", "--no-pager", "-o", "cat"], self.calls)
        self.assertIn(["sudo", "-n", "/usr/bin/journalctl", "-k", "--since", "-1h", "--no-pager", "-o", "short-iso"],
                      self.calls, "the one sudo argv the cockpit already has, nothing new in sudoers")

    def test_once_per_run(self):
        self.cp.crash_cause("qwen38-sglang.service", "inv-1", now=self.now)
        n = len(self.calls)
        self.cp.crash_cause("qwen38-sglang.service", "inv-1", now=self.now + 2)
        self.assertEqual(len(self.calls), n, "the same run is not read twice")
        self.cp.crash_cause("qwen38-sglang.service", "inv-2", now=self.now + 60)
        self.assertGreater(len(self.calls), n, "a new run is")

    def test_the_key_never_reaches_the_page(self):
        self.journal = [CUBLAS + " api_key=sk-the-real-key"]
        c = self.cp.crash_cause("qwen38-flash.service", "inv-k", now=self.now)["cause"]
        self.assertNotIn("sk-the-real-key", c["line"])
        self.assertIn("<masked>", c["line"])

    def test_nothing_known_is_said_so(self):
        self.journal, self.kernel = ["[2026-10-02] Decode batch, #running-req: 1"], []
        rec = self.cp.crash_cause("qwen38-flash.service", "inv-3", now=self.now)
        self.assertIsNone(rec["cause"])
        self.assertEqual(self.events, [("crash", "qwen38-flash stopped: nothing known in its last 400 lines")])

    def test_a_journal_that_did_not_answer_is_read_again(self):
        self.journal_ok = False
        self.assertIsNone(self.cp.crash_cause("qwen38-flash.service", "inv-4", now=self.now))
        self.assertEqual(self.events, [])
        self.journal_ok = True
        self.assertEqual(self.cp.crash_cause("qwen38-flash.service", "inv-4", now=self.now)["cause"]["id"],
                         "cublas-internal-error")

    def test_a_doctor_that_does_not_load_costs_only_the_causes(self):
        saved = list(self.cp._DOCTOR)
        self.cp._DOCTOR.clear()
        repo = self.cp.REPO_DIR
        self.cp.REPO_DIR = self.tmp          # no doctor.py there
        try:
            rec = self.cp.crash_cause("qwen38-flash.service", "inv-5", now=self.now)
            self.assertIsNone(rec["cause"])
            self.assertEqual(len(self.events), 1)
            self.assertIn("crash causes unavailable", self.events[0][1])
            self.cp.crash_cause("qwen38-flash.service", "inv-6", now=self.now)
            self.assertEqual(len(self.events), 1, "said once, not at every crash")
        finally:
            self.cp.REPO_DIR = repo
            self.cp._DOCTOR[:] = saved

    def test_iso_epoch(self):
        self.assertIsNone(self.cp._iso_epoch("garbage line"))
        self.assertIsNone(self.cp._iso_epoch(""))
        self.assertAlmostEqual(self.cp._iso_epoch(iso(self.now) + " host kernel: x"), int(self.now), delta=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
