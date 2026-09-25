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
import socket
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
                     "/api/jobs", "/api/config", "/api/systemone"):
            st, _, _ = self.req("GET", path)
            self.assertIn(st, (401, 404), f"{path} answered {st} with no session")


class AStalledReaderDoesNotHoldTheState(Base):
    """/api/state wrote its answer while holding STATE_LOCK, which every sampler takes to
    publish, the 1 s tier that carries the memory floor's abort included. A reader that
    stops reading mid-answer held them all until the write gave up: about 15 minutes for
    a phone that loses its network, Linux retrying with tcp_retries2=15 (found in review,
    2026-09-24). The state is padded past any socket buffer so the write has to wait."""

    def test_the_lock_is_free_while_a_reader_stalls(self):
        cookie = self.login()
        with self.cp.STATE_LOCK:
            self.cp.STATE["zz_pad"] = {"data": "x" * 16_000_000, "ts": 0}
        self.addCleanup(self.cp.STATE.pop, "zz_pad", None)
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        s.connect(("127.0.0.1", self.port))
        try:
            s.sendall(f"GET /api/state HTTP/1.1\r\nHost: x\r\nCookie: {cookie}\r\n\r\n".encode())
            time.sleep(1.0)                    # the answer is being written, and nobody reads
            got = self.cp.STATE_LOCK.acquire(timeout=3)
            if got:
                self.cp.STATE_LOCK.release()
        finally:
            s.close()
        self.assertTrue(got, "a reader that stalls holds the lock every sampler publishes under")


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

    def raw_post(self, length_header, body=b"", close_write=False):
        """A POST whose Content-Length http.client would never write for us."""
        import socket
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            s.sendall(b"POST /api/login HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                      b"Content-Length: " + length_header + b"\r\n\r\n" + body)
            if close_write:
                s.shutdown(socket.SHUT_WR)
            return s.recv(200).split(b"\r\n")[0]
        finally:
            s.close()

    def test_a_negative_length_is_refused_without_reading_the_body(self):
        # int() accepts "-1", `-1 > cap` is false, and rfile.read(-1) reads until the
        # client hangs up: with no session at all, a client could make the cockpit
        # hold as much memory as it cared to send (256 MiB took it from 22 to 281 MiB
        # on the reference box, 2026-09-24). The answer must come while the client is
        # still connected, not when it gives up.
        first = self.raw_post(b"-1", b"x" * 65536)
        self.assertTrue(first.startswith(b"HTTP/1."), first)
        self.assertIn(b" 400 ", first + b" ")

    def test_a_length_that_is_not_a_number_is_a_400_not_a_dropped_connection(self):
        # int() also takes "+5", which RFC 9110 does not (Content-Length = 1*DIGIT)
        for bad in (b"abc", b"1e3", b"+5", b"\xb2", b"0x10", b"5 5"):
            with self.subTest(length=bad):
                self.assertIn(b" 400 ", self.raw_post(bad, close_write=True) + b" ")
        st, _, _ = self.req("GET", "/api/health")
        self.assertEqual(st, 200)


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

    def test_a_sibling_named_like_the_directory_is_outside_it(self):
        """/static/ is served before the session check, and containment was a string
        prefix: a neighbour such as dashboard/static.bak/ passed for the static directory,
        and /static/../static.bak/x was served to anyone (found in review, 2026-09-24).
        A throwaway static directory with such a neighbour, for this test only."""
        root = Path(tempfile.mkdtemp(prefix="cockpit-static-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "static").mkdir()
        (root / "static" / "login.html").write_text("<p>the page</p>")
        (root / "static.bak").mkdir()
        (root / "static.bak" / "notes.txt").write_text("NOT-FOR-ANYONE")
        saved = self.cp.STATIC_DIR
        self.cp.STATIC_DIR = root / "static"
        self.addCleanup(setattr, self.cp, "STATIC_DIR", saved)
        st, _, body = self.req("GET", "/static/login.html")
        self.assertEqual((st, body), (200, b"<p>the page</p>"), "the throwaway directory is not the one served")
        for path in ("/static/../static.bak/notes.txt", "/static/./../static.bak/notes.txt"):
            st, _, body = self.req("GET", path)
            self.assertEqual(st, 404, f"{path} was served ({len(body)} bytes)")
            self.assertNotIn(b"NOT-FOR-ANYONE", body)

    def test_security_headers_are_on_every_kind_of_answer(self):
        for path in ("/login", "/api/health"):
            _, hdrs, _ = self.req("GET", path)
            csp = hdrs.get("Content-Security-Policy", "")
            self.assertIn("default-src 'self'", csp, path)
            self.assertIn("frame-ancestors 'none'", csp, path)
            self.assertEqual(hdrs.get("X-Content-Type-Options"), "nosniff", path)
            self.assertEqual(hdrs.get("Referrer-Policy"), "no-referrer", path)


class ReadRoutes(Base):
    """The whole GET surface. Every one of these was uncovered, and one of them
    is a boundary: /api/logs/<name> takes a name from the URL and hands it to a
    command, so the allowlist is the only thing between a URL and an arbitrary
    journal unit."""

    def get(self, path, cookie=None):
        st, hdrs, data = self.req("GET", path, cookie=cookie)
        return st, (json.loads(data) if data and hdrs.get(
            "Content-Type", "").startswith("application/json") else data)

    def test_the_action_registry_is_published_without_its_argv(self):
        """The UI needs the parameters to render a selector; it must never be
        handed the command template."""
        cookie = self.login()
        st, body = self.get("/api/actions", cookie)
        self.assertEqual(st, 200)
        self.assertEqual(set(body), set(self.cp.ACTIONS))
        for name, spec in body.items():
            self.assertEqual(set(spec), {"danger", "params"}, name)
        self.assertNotIn("sudo", json.dumps(body))
        self.assertNotIn("systemctl", json.dumps(body))

    def test_the_state_endpoint_returns_the_sampler_state(self):
        cookie = self.login()
        st, body = self.get("/api/state", cookie)
        self.assertEqual(st, 200)
        self.assertIn("config", body)

    def test_the_cached_snapshots_answer_or_explain_themselves(self):
        """upstream, registry and recipes each reach the network or the disk and
        each isolates its own failure: a 500 with a reason, never a traceback
        through the handler."""
        cookie = self.login()
        for path in ("/api/upstream", "/api/registry", "/api/recipes"):
            st, body = self.get(path, cookie)
            self.assertIn(st, (200, 500), path)
            self.assertIsInstance(body, dict, path)
            if st == 500:
                self.assertIn("error", body, path)

    def test_refresh_bypasses_the_cache_without_breaking_the_answer(self):
        cookie = self.login()
        for path in ("/api/registry?refresh=1", "/api/recipes?refresh=1"):
            st, body = self.get(path, cookie)
            self.assertIn(st, (200, 500), path)
            self.assertIsInstance(body, dict, path)

    def test_the_inventory_is_parsed_into_typed_rows(self):
        cookie = self.login()
        st, body = self.get("/api/inventory", cookie)
        self.assertEqual(st, 200)
        self.assertIn("items", body)
        for item in body["items"]:
            self.assertEqual(set(item), {"kind", "what"})
            self.assertIn(item["kind"], ("unit", "drop-ins", "backup", "config",
                                         "legacy", "launcher", "image", "weights",
                                         "ple-file"))

    def test_only_allowlisted_log_sources_are_readable(self):
        """The name comes from the URL. Anything not on the list is a 404, so a
        URL can never name a journal unit or a container of its own choosing."""
        cookie = self.login()
        # qwen38-flash is NOT in this list: it is the container's real name and
        # therefore allowed. The refused set is everything else.
        for name in ("sshd", "docker", "../../etc/passwd", "cloudflared.service",
                     "qwen38-flash.service.evil", "", "..%2f..%2fetc", "*",
                     "qwen38-flash%20", "QWEN38-FLASH", "unsloth-studio.service"):
            st, body = self.get(f"/api/logs/{name}", cookie)
            self.assertEqual(st, 404, f"/api/logs/{name} answered {st}")
            self.assertEqual(body["error"], "unknown source", name)
        # and the allowlist is exactly these: the lanes' containers and this repo's units.
        # A truthiness check of the union could not fail (found in review, 2026-09-24).
        self.assertEqual(set(self.cp.CONTAINERS), {"qwen38-sglang", "qwen38-flash"})
        self.assertEqual(set(self.cp.JOURNAL_UNITS), {
            "qwen38-sglang.service", "qwen38-flash.service", "qwen38-image.service",
            "qwen38-keepalive.service", "opencode-web.service"})

    def test_a_known_log_source_answers_with_bounded_lines(self):
        cookie = self.login()
        known = list(self.cp.CONTAINERS) + list(self.cp.JOURNAL_UNITS)
        self.assertTrue(known)
        for name in known:
            st, body = self.get(f"/api/logs/{name}", cookie)
            self.assertEqual(st, 200, name)
            self.assertEqual(body["name"], name)
            self.assertIsInstance(body["lines"], list)
            self.assertLessEqual(len(body["lines"]), 120, name)

    def test_an_unknown_job_id_is_a_404(self):
        cookie = self.login()
        for jid in ("nope", "../../etc", "", "0" * 64):
            st, body = self.get(f"/api/jobs/{jid}", cookie)
            self.assertEqual(st, 404, jid)
            self.assertEqual(body["error"], "no such job")

    def test_a_known_job_is_returned_with_its_log_tail(self):
        cookie = self.login()
        job = self.cp.Job("smoke", None, 10)
        job.append("a line the tail must carry")
        self.cp.JOBS[job.id] = job
        try:
            st, body = self.get(f"/api/jobs/{job.id}", cookie)
            self.assertEqual(st, 200)
            self.assertEqual(body["id"], job.id)
            self.assertIn("a line the tail must carry", body["lines"])
        finally:
            self.cp.JOBS.pop(job.id, None)

    # What only each page has: the login form, and the app's lane selector. "<" and the
    # product name are in both, so either page passed for the other (found in review,
    # 2026-09-24).
    LOGIN_MARK, APP_MARK = b'<form class="card" id="f">', b'id="switchsel"'

    def test_the_root_serves_the_login_page_without_a_session(self):
        st, _, body = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(self.LOGIN_MARK, body)
        self.assertNotIn(self.APP_MARK, body)

    def test_the_root_serves_the_app_with_a_session(self):
        cookie = self.login()
        st, _, body = self.req("GET", "/", cookie=cookie)
        self.assertEqual(st, 200)
        self.assertIn(self.APP_MARK, body)
        self.assertNotIn(self.LOGIN_MARK, body)

    def test_an_unknown_api_route_is_a_404_and_not_a_static_file(self):
        cookie = self.login()
        for path in ("/api/nope", "/api/", "/nope", "/api/state/extra"):
            st, body = self.get(path, cookie)
            self.assertEqual(st, 404, path)

    def test_the_event_stream_sends_state_and_stops_when_the_client_leaves(self):
        """The SSE endpoint is an infinite loop by design; what is asserted is
        that it starts with a real payload and that a client walking away does
        not raise in the handler thread."""
        import http.client
        cookie = self.login()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/api/stream", None, {"Cookie": cookie})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn("text/event-stream", r.getheader("Content-Type", ""))
        chunk = r.read(64)
        self.assertTrue(chunk.startswith(b"data: "), chunk[:40])
        c.close()                      # walk away mid-stream
        time.sleep(0.2)
        st, _, _ = self.req("GET", "/api/health")
        self.assertEqual(st, 200, "the server did not survive a client leaving the stream")


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
        # The checkout's own path is not a parameter: a clone under a folder named like a
        # target (~/image-lab/...) failed here on unchanged code (found in review,
        # 2026-09-24). What follows it is still checked.
        repo = str(self.cp.REPO_DIR)
        for name, spec in self.cp.ACTIONS.items():
            if not spec["argv"]:
                continue
            for params in self._every_combination(spec):
                argv = spec["argv"](params)
                for val in params.values():
                    if not isinstance(val, str):
                        continue
                    for a in argv:
                        rest = a[len(repo):] if a.startswith(repo + "/") else a
                        if val in rest:
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
        # the argv itself, which the name promises and nothing read (found in review,
        # 2026-09-24): an audit that recorded None passed
        self.assertEqual(starts[0]["argv"],
                         ["python3", str(self.cp.REPO_DIR / "oc-fit-limits.py"), "--restart-agent"])
        self.assertEqual(starts[0]["argv"], out["argv"], "the audit and the answer disagree")


class UpdateCheck(Base):
    """An update nobody is told about is an update nobody installs. The answer existed
    before this behind a button in a tab; what is tested here is that it is now a fact
    the cockpit volunteers, that it never claims an update that is not there, and that a
    box with no network says so instead of looking broken."""

    def setUp(self):
        super().setUp()
        self.cp._RELEASE.update(latest=None, ts=0.0, fails=0, answered=False)

    def tearDown(self):
        self.cp._RELEASE.update(latest=None, ts=0.0, fails=0, answered=False)
        self.cp.UPDATE_CHECK = True

    def test_a_version_number_is_read_as_numbers_not_as_a_string(self):
        """v1.9.0 sorts after v1.15.0 as text, which would announce an update that is
        eleven releases old."""
        self.assertEqual(self.cp._semver("v1.15.2"), (1, 15, 2))
        self.assertEqual(self.cp._semver("1.15"), (1, 15, 0))
        self.assertGreater(self.cp._semver("v1.15.0"), self.cp._semver("v1.9.0"))
        for bad in ("main", "", None, "v1.15.2-rc1", "latest"):
            self.assertIsNone(self.cp._semver(bad), bad)

    def _with_latest(self, tag, installed="v1.15.0"):
        self.cp._get_json = lambda url, timeout=5.0: {"tag_name": tag}
        real_run = self.cp.run
        self.cp.run = lambda argv, **k: installed if "describe" in argv else real_run(argv, **k)
        try:
            return self.cp.collect_update()
        finally:
            self.cp.run = real_run

    def test_a_newer_published_release_is_reported_as_behind(self):
        out = self._with_latest("v1.15.2")
        self.assertTrue(out["behind"])
        self.assertEqual(out["latest"], "v1.15.2")
        self.assertEqual(out["installed"], "v1.15.0")

    def test_the_same_release_is_not_an_update(self):
        self.assertFalse(self._with_latest("v1.15.0")["behind"])

    def test_a_box_ahead_of_the_newest_tag_is_never_nagged(self):
        """This repo is developed on a box that runs it, and that box is regularly ahead
        of the newest published tag. A plain inequality would nag it forever."""
        self.assertFalse(self._with_latest("v1.15.0", installed="v1.16.0")["behind"])

    def test_a_tag_that_is_not_a_version_is_ignored_rather_than_compared(self):
        out = self._with_latest("nightly")
        self.assertFalse(out["behind"])
        self.assertIsNone(out["latest"])

    def test_no_network_says_unknown_and_never_behind(self):
        def boom(url, timeout=5.0):
            raise OSError("no route to host")

        self.cp._get_json = boom
        out = self.cp.collect_update()
        self.assertIsNone(out["latest"])
        self.assertFalse(out["behind"])
        self.assertGreaterEqual(self.cp._RELEASE["fails"], 1)

    def test_a_failing_check_backs_off_instead_of_asking_every_tier(self):
        calls = []

        def boom(url, timeout=5.0):
            calls.append(1)
            raise OSError("offline")

        self.cp._get_json = boom
        for _ in range(5):
            self.cp.collect_update()
        self.assertEqual(len(calls), 1, "an offline box asked GitHub once per collection")

    def test_the_first_retry_after_a_failure_is_a_minute_not_an_hour(self):
        """This unit starts in the same second network-online.target does, so the probe
        most likely to fail is the first one of a fresh boot. An hour-scale first step
        left a rebooted box unable to learn about an update for two hours (measured on
        the reference box, 2026-09-22): the box that most needs the check is the one that
        just came back."""
        calls = []

        def boom(url, timeout=5.0):
            calls.append(time.time())
            raise OSError("no route to host")

        self.cp._get_json = boom
        self.cp.collect_update()
        self.assertEqual(len(calls), 1)
        # one failure: the next probe is due a minute later, not an hour
        self.cp._RELEASE["ts"] = time.time() - 61
        self.cp.collect_update()
        self.assertEqual(len(calls), 2, "a box that failed once waited more than a minute")
        # and it keeps doubling rather than hammering
        self.cp._RELEASE["ts"] = time.time() - 61
        self.cp.collect_update()
        self.assertEqual(len(calls), 2, "the second failure did not back off")

    def test_the_operator_can_turn_the_outbound_check_off(self):
        asked = []
        self.cp._get_json = lambda url, timeout=5.0: (asked.append(url), {"tag_name": "v9.9.9"})[1]
        self.cp.UPDATE_CHECK = False
        out = self.cp.collect_update()
        self.assertEqual(asked, [], "COCKPIT_UPDATE_CHECK=0 still called GitHub")
        self.assertIs(out["checked"], False)
        self.assertNotIn("latest", out)

    def test_code_older_than_the_files_on_disk_is_reported(self):
        self.cp.CODE_AT_START = dict(self.cp.code_fingerprint())
        self.cp.CODE_AT_START["static/app.js"] = "0:0"
        self.cp.UPDATE_CHECK = False
        self.assertIn("static/app.js", self.cp.collect_update().get("stale_code", []))


class SystemOneRoute(Base):
    """The System One tab is a browser sending someone else's state to a lane. Two
    things matter and neither is the answer: the serving key never leaves this process,
    and the browser cannot choose anything but the state and the questions."""

    def test_the_run_route_needs_a_session_and_a_csrf_token(self):
        st, _, _ = self.req("POST", "/api/systemone", {"state": "s", "questions": {"q": {}}})
        self.assertEqual(st, 401, "a typed decision was accepted with no session")
        cookie = self.login()
        st, _, body = self.req("POST", "/api/systemone",
                               {"state": "s", "questions": {"q": {}}}, cookie=cookie)
        self.assertEqual(st, 403, body)

    def test_a_call_with_nothing_to_ask_is_refused_before_the_proxy(self):
        cookie = self.login()
        for payload, why in (({"state": "", "questions": {"q": {"type": "noul"}}}, "empty state"),
                             ({"state": "s", "questions": {}}, "no question"),
                             ({"state": "s", "questions": []}, "questions as a list"),
                             ({"state": 5, "questions": {"q": {}}}, "a state that is not text")):
            payload["csrf"] = self.csrf(cookie)
            st, _, body = self.req("POST", "/api/systemone", payload, cookie=cookie)
            self.assertEqual(st, 400, f"{why} was forwarded: {body}")

    def test_a_state_past_the_cap_is_refused_with_the_number(self):
        cookie = self.login()
        big = {"state": "x" * (self.cp.SYSTEMONE_MAX_STATE + 1),
               "questions": {"q": {"type": "noul", "instructions": "i"}},
               "csrf": self.csrf(cookie)}
        st, _, body = self.req("POST", "/api/systemone", big, cookie=cookie)
        self.assertEqual(st, 400)
        self.assertIn(str(self.cp.SYSTEMONE_MAX_STATE), json.loads(body)["error"])

    def test_the_browser_chooses_the_state_and_the_questions_and_nothing_else(self):
        """A page that could name the path or the key would be a page that can reach
        anything this process can reach."""
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data)
            seen["auth"] = req.headers.get("Authorization")
            raise self.cp.urllib.error.HTTPError(req.full_url, 422, "x", {}, None)

        cookie = self.login()
        real = self.cp.urllib.request.urlopen
        self.cp.urllib.request.urlopen = fake_urlopen
        try:
            self.req("POST", "/api/systemone",
                     {"state": "s", "questions": {"q": {"type": "noul", "instructions": "i"}},
                      "model": "jev-latest", "path": "/v1/chat/completions",
                      "upstream": "http://evil.example", "csrf": self.csrf(cookie)},
                     cookie=cookie)
        finally:
            self.cp.urllib.request.urlopen = real
        self.assertTrue(seen["url"].endswith("/v1/systemone"), seen["url"])
        self.assertNotIn("evil", seen["url"])
        self.assertEqual(set(seen["body"]), {"state", "questions", "model"})
        self.assertTrue(seen["auth"].startswith("Bearer "))

    def a_text_lane_is_ready(self):
        """The probe is only sent with a text lane up; without one the answer is its own
        test (test_page_facts.SystemOneIsAnsweredByATextLane)."""
        with self.cp.LIFE_LOCK:
            saved = dict(self.cp.LIFE.get("states", {}))
            self.cp.LIFE["states"] = {"qwen38-sglang.service": "ready"}

        def restore():
            with self.cp.LIFE_LOCK:
                self.cp.LIFE["states"] = saved
        self.addCleanup(restore)

    def test_the_probe_reports_a_proxy_that_does_not_serve_the_route(self):
        """A cockpit whose proxy predates v6.19 must say so rather than look broken."""
        def fake_urlopen(req, timeout=None):
            raise self.cp.urllib.error.HTTPError(req.full_url, 404, "x", {}, None)

        self.a_text_lane_is_ready()
        real = self.cp.urllib.request.urlopen
        self.cp.urllib.request.urlopen = fake_urlopen
        try:
            self.cp.SYSTEMONE_CACHE.update(data=None, ts=0.0)
            out = self.cp.systemone_available(max_age=0.0)
        finally:
            self.cp.urllib.request.urlopen = real
            self.cp.SYSTEMONE_CACHE.update(data=None, ts=0.0)
        self.assertFalse(out["available"])
        self.assertIn("v6.19", out["reason"])

    def test_the_probe_reads_a_schema_refusal_as_the_route_being_served(self):
        def fake_urlopen(req, timeout=None):
            raise self.cp.urllib.error.HTTPError(req.full_url, 422, "x", {}, None)

        self.a_text_lane_is_ready()
        real = self.cp.urllib.request.urlopen
        self.cp.urllib.request.urlopen = fake_urlopen
        try:
            self.cp.SYSTEMONE_CACHE.update(data=None, ts=0.0)
            out = self.cp.systemone_available(max_age=0.0)
        finally:
            self.cp.urllib.request.urlopen = real
            self.cp.SYSTEMONE_CACHE.update(data=None, ts=0.0)
        self.assertTrue(out["available"])
        self.assertEqual(out["reason"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
