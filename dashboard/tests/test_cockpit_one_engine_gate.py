"""The cockpit's server refuses a second engine, whatever the page shows.

Two engines at once on this box's unified memory is the livelock the lifecycle module
exists to prevent, and the page disables the button; but the server's own refusal (the
409 of start_action) was tested nowhere: with `if reasons:` made `if False:`, every
cockpit test stayed green (found in review, 2026-09-24). This drives the real Handler
over HTTP, logged in and with a CSRF token, with COCKPIT_DRY_RUN=1 and a throwaway config
dir. The engine states are set by hand, as the lifecycle collector would set them: no
collector runs here, so nothing reads this box's own units."""
import http.client
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
REPO = HERE.parents[2]
API_KEY = "test-key-not-a-real-one"
SGLANG, FLASH, IMAGE = "qwen38-sglang.service", "qwen38-flash.service", "qwen38-image.service"


class NeverTwoEngines(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env, path = dict(os.environ), list(sys.path)
        cls.addClassCleanup(lambda: (os.environ.clear(), os.environ.update(env), sys.path.__setitem__(slice(None), path)))
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-gate-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        (cls.tmp / "api-key").write_text(API_KEY + "\n")
        os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.tmp), COCKPIT_REPO_DIR=str(REPO),
                          COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0")
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_gate_under_test", DASH / "cockpit.py")
        cls.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cp)
        cls.srv = cls.cp.Server(("127.0.0.1", 0), cls.cp.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.srv.server_close)
        cls.addClassCleanup(cls.srv.shutdown)

    def setUp(self):
        self.cp.LOGIN_FAILS.clear()

    def req(self, path, body, cookie=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        try:
            c.request("POST", path, json.dumps(body).encode(),
                      {"Content-Type": "application/json", **({"Cookie": cookie} if cookie else {})})
            r = c.getresponse()
            return r.status, r.getheader("Set-Cookie") or "", r.read()
        finally:
            c.close()

    def act(self, name, params, states):
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"] = dict(states)
        st, raw, _ = self.req("/api/login", {"key": API_KEY})
        self.assertEqual(st, 200)
        cookie = raw.split(";")[0]
        st, _, data = self.req("/api/csrf", {}, cookie)
        self.assertEqual(st, 200)
        before = set(self.cp.JOBS)
        st, _, data = self.req("/api/action", {"name": name, "params": params,
                                               "csrf": json.loads(data)["token"]}, cookie)
        return st, json.loads(data), set(self.cp.JOBS) - before

    def audit_kinds(self):
        log = Path(self.cp.AUDIT_LOG)
        self.assertEqual(log.parent, self.tmp, "the audit log is not the throwaway one")
        return [json.loads(ln).get("kind") for ln in log.read_text().splitlines() if ln.strip()]

    def wait_idle(self):
        end = time.time() + 15
        while time.time() < end:
            if self.cp.JOB_LOCK.acquire(blocking=False):
                self.cp.JOB_LOCK.release()
                return
            time.sleep(0.1)
        self.fail("the dry-run job never gave the lock back")

    def test_a_second_engine_is_refused_while_another_holds_the_pool(self):
        for other, state in ((FLASH, "ready"), (FLASH, "loading-weights"), (FLASH, "orphan"),
                             (FLASH, "wedged"), (IMAGE, "ready"), (IMAGE, "starting")):
            for verb in ("start", "restart"):
                st, out, jobs = self.act("unit", {"verb": verb, "unit": SGLANG}, {other: state, SGLANG: "stopped"})
                self.assertEqual(st, 409, f"{verb} beside {other} {state}: {out}")
                self.assertEqual(out.get("error"), "blocked")
                self.assertTrue(any(other in r for r in out.get("reasons", [])), out)
                self.assertEqual(jobs, set(), f"{verb} beside {other} {state} started a job")

    def test_a_switch_waits_for_a_boot_to_settle(self):
        st, out, jobs = self.act("switch", {"target": "flash"}, {SGLANG: "loading-weights"})
        self.assertEqual(st, 409, out)
        self.assertEqual(jobs, set())

    def test_the_refusal_is_audited(self):
        self.act("unit", {"verb": "start", "unit": FLASH}, {SGLANG: "ready"})
        self.assertEqual(self.audit_kinds()[-1], "action_blocked")

    def test_nothing_running_lets_it_through(self):
        st, out, jobs = self.act("unit", {"verb": "start", "unit": SGLANG}, {FLASH: "stopped", IMAGE: "stopped"})
        self.assertEqual(st, 202, out)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(out.get("dry_run"), "a real systemctl would have run")
        self.wait_idle()
        # stopping one engine while another runs is always allowed: that is how you get to one
        st, out, jobs = self.act("unit", {"verb": "stop", "unit": FLASH}, {FLASH: "ready", SGLANG: "ready"})
        self.assertEqual(st, 202, out)
        self.wait_idle()


if __name__ == "__main__":
    unittest.main(verbosity=2)
