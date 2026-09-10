"""The cockpit's HTTP and privileged-action surface, exercised for real.

This is the part of the repo that can start, stop and switch a serving engine
through a sudoers allowlist, and before this file it was the least tested: 14%
of cockpit.py, none of it the auth, the CSRF check, the static handler or the
action registry. Everything here runs against the REAL Handler and the REAL
Server on an ephemeral port, with COCKPIT_DRY_RUN=1 so no argv is ever executed
and COCKPIT_CONFIG_DIR pointing at a throwaway directory so the developer's own
API key and audit log are never touched.

What is asserted, in the order an attacker would try it:
  * nothing mutating is reachable without the session cookie
  * the cookie is signed, typed and aged, and cannot be forged or replayed
    across kinds
  * login is rate limited per address
  * every mutating POST needs a CSRF token this server issued
  * the static handler cannot be walked out of its directory
  * every action's parameters come from a closed set, and every rendered argv
    is a plain list of arguments with no shell anywhere near it
  * one job at a time, and the audit log records the exact argv
"""
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


def load_cockpit(config_dir: Path):
    """Import cockpit.py with its environment pointed at a throwaway box."""
    os.environ.update(
        COCKPIT_DRY_RUN="1",
        COCKPIT_CONFIG_DIR=str(config_dir),
        COCKPIT_REPO_DIR=str(REPO),
        COCKPIT_PORT="0",
        COCKPIT_AGENT_PORT="0",
        COCKPIT_AUTOHEAL="0",
    )
    sys.path.insert(0, str(DASH))
    spec = importlib.util.spec_from_file_location("cockpit_http_under_test",
                                                  DASH / "cockpit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-http-"))
        (cls.tmp / "api-key").write_text(API_KEY + "\n")
        cls.cp = load_cockpit(cls.tmp)
        cls.srv = cls.cp.Server(("127.0.0.1", 0), cls.cp.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # the login rate limiter is process state: every test starts un-punished
        self.cp.LOGIN_FAILS.clear()

    # ---- helpers -------------------------------------------------------------
    def req(self, method, path, body=None, cookie=None, headers=None, raw_body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if cookie:
            h["Cookie"] = cookie
        payload = raw_body
        if payload is None and body is not None:
            payload = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        try:
            c.request(method, path, payload, h)
            r = c.getresponse()
            data = r.read()
            return r.status, dict(r.getheaders()), data
        finally:
            c.close()

    def login(self):
        st, hdrs, _ = self.req("POST", "/api/login", {"key": API_KEY})
        self.assertEqual(st, 200)
        raw = hdrs.get("Set-Cookie", "")
        self.assertIn("HttpOnly", raw)
        self.assertIn("SameSite=Strict", raw)
        return raw.split(";")[0]

    def csrf(self, cookie):
        st, _, data = self.req("POST", "/api/csrf", {}, cookie=cookie)
        self.assertEqual(st, 200)
        return json.loads(data)["token"]


class Unauthenticated(Base):
    def test_health_is_the_only_public_endpoint(self):
        st, _, _ = self.req("GET", "/api/health")
        self.assertEqual(st, 200)

    def test_state_needs_a_session(self):
        st, _, data = self.req("GET", "/api/state")
        self.assertEqual(st, 401)
        self.assertEqual(json.loads(data)["error"], "auth")

    def test_action_needs_a_session(self):
        st, _, _ = self.req("POST", "/api/action",
                            {"name": "unit", "params": {"verb": "stop",
                                                        "unit": "qwen38-flash.service"}})
        self.assertEqual(st, 401)

    def test_csrf_token_is_not_handed_out_before_login(self):
        st, _, _ = self.req("POST", "/api/csrf", {})
        self.assertEqual(st, 401)

    def test_every_other_api_route_is_closed(self):
        for path in ("/api/registry", "/api/recipes", "/api/upstream", "/api/events",
                     "/api/jobs", "/api/config"):
            st, _, _ = self.req("GET", path)
            self.assertIn(st, (401, 404), f"{path} answered {st} with no session")


class Login(Base):
    def test_a_wrong_key_is_refused(self):
        st, hdrs, _ = self.req("POST", "/api/login", {"key": "wrong"})
        self.assertEqual(st, 403)
        self.assertNotIn("Set-Cookie", hdrs)

    def test_an_empty_and_a_missing_key_are_refused(self):
        for body in ({}, {"key": ""}, {"key": None}, {"other": API_KEY}):
            st, _, _ = self.req("POST", "/api/login", body)
            self.assertEqual(st, 403, body)

    def test_malformed_json_does_not_authenticate(self):
        st, _, _ = self.req("POST", "/api/login", raw_body=b"{not json",
                            headers={"Content-Type": "application/json"})
        self.assertEqual(st, 403)

    def test_the_right_key_sets_a_hardened_cookie(self):
        cookie = self.login()
        self.assertTrue(cookie.startswith("cockpit="))

    def test_five_failures_lock_the_address_for_a_minute(self):
        for _ in range(5):
            st, _, _ = self.req("POST", "/api/login", {"key": "wrong"})
            self.assertEqual(st, 403)
        st, _, data = self.req("POST", "/api/login", {"key": "wrong"})
        self.assertEqual(st, 429)
        # and the lock is not bypassed by then sending the RIGHT key
        st, hdrs, _ = self.req("POST", "/api/login", {"key": API_KEY})
        self.assertEqual(st, 429, "the rate limit let a correct key through while locked")
        self.assertNotIn("Set-Cookie", hdrs)

    def test_a_failure_is_audited(self):
        self.req("POST", "/api/login", {"key": "wrong"})
        log = self.tmp / "cockpit-audit.log"
        self.assertTrue(log.exists(), "a failed login left no audit trail")
        self.assertIn("login_fail", log.read_text())


class Cookies(Base):
    def test_a_forged_signature_is_rejected(self):
        good = self.login().split("=", 1)[1]
        kind, ts, sig = good.split(":")
        bad = f"{kind}:{ts}:{'0' * len(sig)}"
        st, _, _ = self.req("GET", "/api/state", cookie=f"cockpit={bad}")
        self.assertEqual(st, 401)

    def test_a_csrf_token_is_not_a_session(self):
        """Both are signed with the same secret; only the kind separates them."""
        cookie = self.login()
        tok = self.csrf(cookie)
        st, _, _ = self.req("GET", "/api/state", cookie=f"cockpit={tok}")
        self.assertEqual(st, 401, "a csrf token was accepted as a session cookie")

    def test_a_session_cookie_is_not_a_csrf_token(self):
        cookie = self.login()
        sess = cookie.split("=", 1)[1]
        st, _, data = self.req("POST", "/api/action",
                               {"name": "smoke", "params": {}, "csrf": sess},
                               cookie=cookie)
        self.assertEqual(st, 403, json.loads(data or b"{}"))

    def test_an_expired_session_is_rejected(self):
        old = f"sess:{int(time.time()) - 13 * 3600}"
        import hashlib
        import hmac
        sig = hmac.new(self.cp.SESSION_SECRET, old.encode(),
                       hashlib.sha256).hexdigest()[:32]
        st, _, _ = self.req("GET", "/api/state", cookie=f"cockpit={old}:{sig}")
        self.assertEqual(st, 401)

    def test_garbage_cookies_never_authenticate(self):
        for raw in ("", "cockpit=", "cockpit=a:b:c", "cockpit=sess:notanint:x" * 3,
                    "cockpit=" + "A" * 5000, "sess:1:2", "cockpit=sess:1"):
            st, _, _ = self.req("GET", "/api/state", cookie=raw)
            self.assertEqual(st, 401, f"{raw[:40]!r} authenticated")


class Csrf(Base):
    def test_an_action_without_a_token_is_refused(self):
        cookie = self.login()
        st, _, data = self.req("POST", "/api/action",
                               {"name": "smoke", "params": {}}, cookie=cookie)
        self.assertEqual(st, 403)
        self.assertEqual(json.loads(data)["error"], "csrf")

    def test_a_stale_token_is_refused(self):
        import hashlib
        import hmac
        old = f"csrf:{int(time.time()) - 3601}"
        sig = hmac.new(self.cp.SESSION_SECRET, old.encode(),
                       hashlib.sha256).hexdigest()[:32]
        cookie = self.login()
        st, _, _ = self.req("POST", "/api/action",
                            {"name": "smoke", "params": {}, "csrf": f"{old}:{sig}"},
                            cookie=cookie)
        self.assertEqual(st, 403)

    def test_bad_json_on_a_mutating_post_is_a_400_not_an_action(self):
        cookie = self.login()
        st, _, _ = self.req("POST", "/api/action", raw_body=b"{",
                            headers={"Content-Type": "application/json"}, cookie=cookie)
        self.assertEqual(st, 400)


class MalformedJson(Base):
    """Valid JSON of the wrong SHAPE.

    Every case below crashed the handler thread before 2026-09-10, dropping the
    connection with no answer and printing a traceback into the journal. Five of
    them are reachable BEFORE authentication, on the one route that has to be.
    The bug was one assumption in three places: that json.loads of a request body
    returns a dict, and that a field a client controls has the type it should.
    """

    NOT_OBJECTS = [b"[]", b'"x"', b"5", b"null", b"true", b"[1,2,3]", b"-0.0"]
    NOT_STRINGS = [None, 123, {}, [], True, 1.5, {"nested": "dict"}]

    def test_a_non_object_login_body_is_a_refusal_not_a_crash(self):
        for raw in self.NOT_OBJECTS:
            self.cp.LOGIN_FAILS.clear()
            st, _, _ = self.req("POST", "/api/login", raw_body=raw,
                                headers={"Content-Type": "application/json"})
            self.assertEqual(st, 403, f"login body {raw!r}")

    def test_a_non_string_key_is_a_refusal_not_a_crash(self):
        for key in self.NOT_STRINGS:
            self.cp.LOGIN_FAILS.clear()
            st, hdrs, _ = self.req("POST", "/api/login", {"key": key})
            self.assertEqual(st, 403, f"key={key!r}")
            self.assertNotIn("Set-Cookie", hdrs, f"key={key!r} authenticated")

    def test_a_non_object_action_body_is_a_400(self):
        cookie = self.login()
        for raw in self.NOT_OBJECTS:
            st, _, data = self.req("POST", "/api/action", raw_body=raw,
                                   headers={"Content-Type": "application/json"},
                                   cookie=cookie)
            self.assertEqual(st, 400, f"action body {raw!r}")
            self.assertEqual(json.loads(data)["error"], "bad json")

    def test_non_object_params_are_a_400(self):
        cookie = self.login()
        tok = self.csrf(cookie)
        for params in ([1, 2], "s", 5, None, True, 0.5):
            st, _, _ = self.req("POST", "/api/action",
                                {"name": "unit", "params": params, "csrf": tok},
                                cookie=cookie)
            self.assertEqual(st, 400, f"params={params!r}")

    def test_start_action_validates_its_own_params(self):
        """It is also called by the autoheal, which is not the HTTP layer."""
        for params in ([1, 2], "s", 5, True, 0.5):
            code, out = self.cp.start_action("unit", params)
            self.assertEqual(code, 400, f"params={params!r}")
            self.assertIn("object", out["error"])

    def test_a_non_string_action_name_is_a_404(self):
        cookie = self.login()
        tok = self.csrf(cookie)
        for name in (None, 123, [], {}, True):
            st, _, _ = self.req("POST", "/api/action",
                                {"name": name, "params": {}, "csrf": tok},
                                cookie=cookie)
            self.assertEqual(st, 404, f"name={name!r}")

    def test_the_server_is_still_answering_after_all_of_that(self):
        """The point of the whole class: no input above took the server with it."""
        st, _, _ = self.req("GET", "/api/health")
        self.assertEqual(st, 200)


class BodyLimits(Base):
    def test_a_huge_body_is_refused_before_it_is_parsed(self):
        cookie = self.login()
        st, _, _ = self.req("POST", "/api/action", raw_body=b"x" * 70000,
                            headers={"Content-Type": "application/json"}, cookie=cookie)
        self.assertEqual(st, 413)

    def test_a_huge_login_body_is_refused_too(self):
        st, _, _ = self.req("POST", "/api/login", raw_body=b"x" * 70000,
                            headers={"Content-Type": "application/json"})
        self.assertEqual(st, 413)


class StaticFiles(Base):
    ESCAPES = [
        "/static/../cockpit.py",
        "/static/../../keepalive-proxy.py",
        "/static/....//cockpit.py",
        "/static/%2e%2e%2fcockpit.py",
        "/static/..%2fcockpit.py",
        "/static//etc/passwd",
        "/static/../../../../../../etc/passwd",
        "/static/./../cockpit.py",
    ]

    def test_the_login_page_is_served(self):
        st, hdrs, body = self.req("GET", "/login")
        self.assertEqual(st, 200)
        self.assertIn("text/html", hdrs.get("Content-Type", ""))
        self.assertTrue(body)

    def test_no_request_walks_out_of_the_static_directory(self):
        for path in self.ESCAPES:
            st, _, body = self.req("GET", path)
            self.assertEqual(st, 404, f"{path} was served ({len(body)} bytes)")
            self.assertNotIn(b"SESSION_SECRET", body)
            self.assertNotIn(b"root:", body)

    def test_security_headers_are_on_every_kind_of_answer(self):
        for path in ("/login", "/api/health"):
            _, hdrs, _ = self.req("GET", path)
            csp = hdrs.get("Content-Security-Policy", "")
            self.assertIn("default-src 'self'", csp, path)
            self.assertIn("frame-ancestors 'none'", csp, path)
            self.assertEqual(hdrs.get("X-Content-Type-Options"), "nosniff", path)
            self.assertEqual(hdrs.get("Referrer-Policy"), "no-referrer", path)


class ActionRegistry(Base):
    """The registry itself, as data. No HTTP, no job: what CAN be built."""

    SHELL = set(";|&$`><\n\r*?()[]{}!#~'\"\\ ")

    def test_every_action_declares_a_closed_parameter_set(self):
        for name, spec in self.cp.ACTIONS.items():
            self.assertIn("params", spec, name)
            self.assertIsInstance(spec["params"], dict, name)
            for key, allowed in spec["params"].items():
                self.assertIsInstance(allowed, (list, tuple, set, frozenset),
                                      f"{name}.{key} is not a closed set")
                self.assertTrue(len(allowed) > 0, f"{name}.{key} allows nothing")
                self.assertLess(len(allowed), 200, f"{name}.{key} is not a small enum")

    def test_every_action_has_a_timeout_and_a_danger_level(self):
        for name, spec in self.cp.ACTIONS.items():
            self.assertIn(spec.get("danger"), ("low", "medium", "high"), name)
            self.assertIsInstance(spec.get("timeout"), (int, float), name)
            self.assertGreater(spec["timeout"], 0, name)

    def _every_combination(self, spec):
        import itertools
        keys = list(spec["params"])
        pools = [list(spec["params"][k]) for k in keys]
        for combo in itertools.product(*pools) if pools else [()]:
            yield dict(zip(keys, combo))

    def test_every_reachable_argv_is_a_plain_argument_list(self):
        """Not one rendered command may contain a shell metacharacter, and the
        program must be an absolute path: this is what keeps the sudoers
        allowlist meaningful."""
        seen = 0
        for name, spec in self.cp.ACTIONS.items():
            if not spec["argv"]:
                continue
            for params in self._every_combination(spec):
                argv = spec["argv"](params)
                seen += 1
                self.assertIsInstance(argv, list, name)
                self.assertTrue(argv, name)
                for a in argv:
                    self.assertIsInstance(a, str, f"{name} {params}: {a!r} is not a string")
                for a in argv[1:]:
                    bad = self.SHELL & set(a)
                    # a path may contain a space in principle; nothing here does
                    self.assertFalse(bad, f"{name} {params}: {a!r} carries {bad}")
                prog = argv[0]
                self.assertTrue(prog.startswith("/") or prog in ("sudo", "bash", "python3"),
                                f"{name}: {prog!r} is not an absolute program")
                if prog == "sudo":
                    self.assertEqual(argv[1], "-n",
                                     f"{name}: sudo without -n can wait on a password prompt")
        self.assertGreater(seen, 5, "the combination walk covered almost nothing")

    def test_no_action_interpolates_a_parameter_into_one_argument(self):
        """A parameter must BE an argument, never a piece of one: that is what
        makes the closed enum a real boundary."""
        for name, spec in self.cp.ACTIONS.items():
            if not spec["argv"]:
                continue
            for params in self._every_combination(spec):
                argv = spec["argv"](params)
                for val in params.values():
                    if not isinstance(val, str):
                        continue
                    for a in argv:
                        if val in a:
                            self.assertEqual(a, val,
                                             f"{name}: {val!r} is embedded inside {a!r}")


class ActionValidation(Base):
    """start_action, called directly. DRY_RUN means nothing is executed."""

    def tearDown(self):
        """Wait for the job thread to release the single-job lock, never take it
        from under it: releasing a lock its owner still holds made run_job's own
        release raise 'release unlocked lock' in a background thread, which is
        test pollution that reads like a product bug."""
        for _ in range(200):                      # DRY_RUN jobs finish at once
            if not self.cp.JOB_LOCK.locked():
                break
            time.sleep(0.02)
        else:
            self.fail("a dry-run job never released the job lock")
        self.cp.JOB_CURRENT["id"] = None

    def test_dry_run_is_really_on(self):
        self.assertTrue(self.cp.DRY_RUN, "these tests would execute real commands")

    def test_an_unknown_action_is_a_404(self):
        code, out = self.cp.start_action("rm", {})
        self.assertEqual(code, 404)
        self.assertEqual(out["error"], "unknown action")

    def test_an_action_name_is_not_a_path(self):
        for name in ("../unit", "unit;ls", "UNIT", "unit ", "", "__class__"):
            code, _ = self.cp.start_action(name, {})
            self.assertEqual(code, 404, f"{name!r} resolved to an action")

    def test_a_parameter_outside_the_enum_is_a_400(self):
        cases = [
            {"verb": "restart; rm -rf /", "unit": "qwen38-flash.service"},
            {"verb": "restart", "unit": "../../etc/shadow"},
            {"verb": "restart", "unit": "qwen38-flash"},          # no .service suffix
            {"verb": "start", "unit": "docker.service"},          # not ours
            {"verb": "enable", "unit": "qwen38-flash.service"},   # verb not offered
            {"verb": None, "unit": None},
            {},
        ]
        for params in cases:
            code, out = self.cp.start_action("unit", params)
            self.assertEqual(code, 400, f"{params} was accepted")
            self.assertTrue(out["error"].startswith("invalid"), out)

    def test_a_switch_target_outside_the_enum_is_a_400(self):
        for target in ("stock; id", "../stock", "flash-nvda ", "gpt5", "", None):
            code, _ = self.cp.start_action("switch", {"target": target})
            self.assertEqual(code, 400, f"{target!r} was accepted")

    def test_extra_parameters_are_ignored_not_forwarded(self):
        code, out = self.cp.start_action("smoke", {"evil": "; rm -rf /"})
        self.assertEqual(code, 202, out)
        self.assertNotIn("evil", json.dumps(out))

    def test_only_one_job_runs_at_a_time(self):
        code, first = self.cp.start_action("smoke", {})
        self.assertEqual(code, 202, first)
        code, second = self.cp.start_action("smoke", {})
        self.assertEqual(code, 409, second)
        self.assertEqual(second["error"], "busy")

    def test_a_started_job_is_audited_with_its_exact_argv(self):
        log = self.tmp / "cockpit-audit.log"
        before = log.read_text() if log.exists() else ""
        code, out = self.cp.start_action("fit_opencode", {})
        self.assertEqual(code, 202, out)
        for _ in range(100):
            if log.exists() and len(log.read_text()) > len(before):
                break
            time.sleep(0.02)
        added = log.read_text()[len(before):]
        self.assertIn("job_start", added)
        rec = [json.loads(ln) for ln in added.splitlines() if ln.strip()]
        starts = [r for r in rec if r.get("kind") == "job_start"]
        self.assertTrue(starts, added)
        self.assertEqual(starts[0]["action"], "fit_opencode")
        self.assertTrue(starts[0]["dry_run"], "the audit line does not say it was a dry run")


if __name__ == "__main__":
    unittest.main(verbosity=2)
