#!/usr/bin/env python3
"""v6.16's per-client identity wall: QWEN38_CLIENT_KEYS_FILE turns the proxy
into the component that knows WHO sent the request, or refuses to pretend.

The bug class here is the wall that is silently absent: a keys file that is
missing, empty or malformed must not degrade into "no wall, carry on", and a
request without a listed bearer must not reach the engine riding somebody
else's journal line. The 401 speaks the dialect of the caller it is refusing
(a Claude client parses authentication_error, an OpenAI client parses
error.type), and /health stays open: a monitoring endpoint behind an
identity wall stops being monitoring.

The proxy is imported fresh per scenario (environment first, then exec_module),
and every request crosses a real socket into the real handler: the module
executes where coverage can see it, which matters, because the wall is
exactly the kind of code a floor was set to keep covered.

v6.17's lesson is the engine below: unlike the other proxy test engines, this
one enforces a key, because the real one does. v6.16's wall identified every
labeled client and admitted none (their tokens met the engine's own key check
verbatim), and no test could see it against an engine that lets everything
through. The admission scenarios at the bottom exist so that hole cannot
reopen unnoticed.
"""
import http.server
import importlib.util
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BODY = json.dumps({"model": "x", "messages": []}).encode()


class Engine(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Unlike the other proxy test engines, this one enforces a key, because
    # the real one does: v6.16's tests passed against an engine that lets
    # everything through, which is exactly why the verbatim-forwarding hole
    # survived them. require_key names the engine's key; last_auth records
    # what the proxy actually sent upstream on the last request.
    require_key = None
    last_auth = None
    last_path = None          # the path the proxy sent upstream, decoded or not

    def log_message(self, *a):
        pass

    def _admit(self):
        Engine.last_auth = self.headers.get("Authorization")
        Engine.last_path = self.path
        if Engine.require_key and Engine.last_auth != "Bearer " + Engine.require_key:
            body = json.dumps({"error": {"message": "engine: bad key",
                                         "type": "invalid_request_error"}}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False
        return True

    def do_POST(self):
        if not self._admit():
            return
        n = int(self.headers.get("Content-Length") or "0")
        self.rfile.read(n)
        body = json.dumps({"id": "chatcmpl-test", "choices": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_PUT = do_POST

    def do_GET(self):
        if not self._admit():
            return
        if self.path.split("?")[0] in ("/server_info", "/get_server_info"):
            # What the real engine answers (checked on the reference box, 2026-09-24):
            # its own serving key in clear, at the top and in every internal state.
            body = json.dumps({"api_key": "engine-K", "admin_api_key": "admin-K",
                               "max_total_num_tokens": 1000, "served_model_name": "m",
                               "internal_states": [{"api_key": "engine-K",
                                                    "admin_api_key": "admin-K"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")


_fresh = [0]


def fresh_proxy_module(upstream_port, keys_path=None, upstream_key=None):
    """Import the proxy with the environment the scenario calls for.

    Module import is when the keys wall is built, so the scenario has to
    exist before the exec, and the SystemExit of a refused start surfaces
    right here: fail closed is observable at import, not only in a journal.
    """
    _fresh[0] += 1
    spec = importlib.util.spec_from_file_location(
        "kproxy_keys_%d" % _fresh[0], REPO / "keepalive-proxy.py")
    mod = importlib.util.module_from_spec(spec)
    names = ("UPSTREAM", "QWEN38_CLIENT_KEYS_FILE", "QWEN38_UPSTREAM_API_KEY")
    saved = [os.environ.get(k) for k in names]
    os.environ["UPSTREAM"] = "http://127.0.0.1:%d" % upstream_port
    if keys_path is None:
        os.environ.pop("QWEN38_CLIENT_KEYS_FILE", None)
    else:
        os.environ["QWEN38_CLIENT_KEYS_FILE"] = keys_path
    if upstream_key is None:
        os.environ.pop("QWEN38_UPSTREAM_API_KEY", None)
    else:
        os.environ["QWEN38_UPSTREAM_API_KEY"] = upstream_key
    try:
        spec.loader.exec_module(mod)
    finally:
        for key, value in zip(names, saved):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return mod


def post(port, path, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=BODY, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def get(port, path):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def call(port, method, path, token=None):
    """(status, body) of any method, with or without a bearer."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = BODY if method != "GET" else None
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class ClientKeys(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=cls.engine.serve_forever, daemon=True).start()
        cls.eport = cls.engine.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.engine.shutdown()
        cls.engine.server_close()

    def setUp(self):
        Engine.require_key = None
        Engine.last_auth = None

    def keys_file(self, text):
        fd, path = tempfile.mkstemp(suffix=".json", text=True)
        with open(fd, "w") as f:
            f.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def live(self, keys_path=None, upstream_key=None):
        mod = fresh_proxy_module(self.eport, keys_path, upstream_key)
        srv = mod.Server(("127.0.0.1", 0), mod.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", srv.server_address[1]), 0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        return srv.server_address[1]

    def test_the_wall_is_off_by_default_anonymous_still_relayed(self):
        port = self.live()
        status, _ = post(port, "/v1/chat/completions")
        self.assertEqual(status, 200)

    def test_unlisted_key_is_401_in_the_openai_dialect(self):
        port = self.live(self.keys_file('{"tok-alice":"alice"}'))
        for tok in (None, "wrong"):
            status, body = post(port, "/v1/chat/completions", tok)
            self.assertEqual(status, 401)
            self.assertIn("client key", json.loads(body)["error"]["message"])

    def test_unlisted_key_is_401_in_the_anthropic_dialect(self):
        port = self.live(self.keys_file('{"tok-alice":"alice"}'))
        status, body = post(port, "/v1/messages")
        self.assertEqual(status, 401)
        doc = json.loads(body)
        self.assertEqual(doc["type"], "error")
        self.assertEqual(doc["error"]["type"], "authentication_error")

    def test_listed_key_is_200_and_the_label_rides_the_journal(self):
        port = self.live(self.keys_file('{"tok-alice":"alice"}'))
        capture = io.StringIO()
        real = sys.stderr
        sys.stderr = capture
        try:
            for path in ("/v1/chat/completions", "/v1/messages"):
                status, _ = post(port, path, "tok-alice")
                self.assertEqual(status, 200)
        finally:
            sys.stderr = real
        self.assertIn("key=alice", capture.getvalue())

    def test_an_escaped_path_does_not_walk_past_the_wall(self):
        """SGLang decodes percent-escapes before it routes (checked live: GET /%76%31/models
        answers with the model list), so "/%76%31/chat/completions" reached the engine while
        this proxy's checks, which all match the raw string, saw a path that was not /v1/.
        Without a listed key, and with the engine's own key attached by the proxy."""
        Engine.require_key = "engine-K"
        Engine.last_auth = None
        port = self.live(self.keys_file('{"tok-alice":"alice"}'), upstream_key="engine-K")
        for path in ("/v1/chat/completions", "/%76%31/chat/completions",
                     "/v1/%63%68%61%74/completions", "/%76%31/chat/completions?stream=false"):
            status, body = post(port, path)
            self.assertEqual(status, 401, (path, body))
            self.assertIn("client key", json.loads(body)["error"]["message"])
        self.assertIsNone(Engine.last_auth, "not one of them reached the engine")
        status, _ = post(port, "/%76%31/chat/completions", "tok-alice")
        self.assertEqual(status, 200, "a listed key still gets through the decoded path")
        self.assertEqual(Engine.last_path, "/v1/chat/completions",
                         "and the engine is sent the path this proxy checked")

    def test_a_path_that_hides_a_query_separator_is_refused(self):
        port = self.live(self.keys_file('{"tok-alice":"alice"}'))
        status, body = post(port, "/v1/chat/completions%3Fstream=true", "tok-alice")
        self.assertEqual(status, 400, body)
        self.assertIn("encoded query", json.loads(body)["error"]["message"])

    def test_health_stays_open_without_a_key(self):
        port = self.live(self.keys_file('{"tok-alice":"alice"}'))
        self.assertEqual(get(port, "/health"), 200)

    # Every route the engine serves, not only POSTs under /v1/. Until v1.18.7 the wall
    # only looked at POST /v1/..., while the proxy attached the engine's own key to
    # everything it relayed: with no key at all, GET /server_info, POST /generate,
    # /flush_cache and /abort_request all reached the engine as the engine (found in
    # review, 2026-09-24).
    ROUTES = (("GET", "/v1/models"), ("GET", "/server_info"), ("GET", "/get_server_info"),
              ("GET", "/metrics"), ("POST", "/generate"), ("POST", "/flush_cache"),
              ("POST", "/abort_request"), ("POST", "/invocations"), ("PUT", "/v1/chat/completions"))

    def test_every_route_but_health_needs_a_listed_key(self):
        Engine.require_key = "engine-K"
        port = self.live(self.keys_file('{"tok-alice":"alice"}'), upstream_key="engine-K")
        for method, path in self.ROUTES:
            for tok in (None, "wrong", "engine-K"):
                with self.subTest(method=method, path=path, token=tok):
                    Engine.last_auth = None
                    status, body = call(port, method, path, tok)
                    self.assertEqual(status, 401, body)
                    self.assertIn("client key", body.decode())
                    self.assertIsNone(Engine.last_auth, "the engine was never reached")

    def test_a_listed_key_still_reaches_every_route(self):
        Engine.require_key = "engine-K"
        port = self.live(self.keys_file('{"tok-alice":"alice"}'), upstream_key="engine-K")
        for method, path in self.ROUTES:
            with self.subTest(method=method, path=path):
                status, _ = call(port, method, path, "tok-alice")
                self.assertEqual(status, 200)
                self.assertEqual(Engine.last_auth, "Bearer engine-K")

    def test_server_info_never_carries_the_engine_key_through_the_proxy(self):
        """/server_info returns the engine's serving key in clear. Behind the wall that key
        is exactly what a named client must not learn; without the wall the caller already
        holds it. Either way the relayed answer has no reason to carry it."""
        Engine.require_key = "engine-K"
        walled = self.live(self.keys_file('{"tok-alice":"alice"}'), upstream_key="engine-K")
        open_ = self.live()
        for port, tok in ((walled, "tok-alice"), (open_, "engine-K")):
            for path in ("/server_info", "/get_server_info"):
                with self.subTest(port=port, path=path):
                    status, body = call(port, "GET", path, tok)
                    self.assertEqual(status, 200)
                    self.assertNotIn(b"engine-K", body)
                    self.assertNotIn(b"admin-K", body)
                    doc = json.loads(body)
                    self.assertEqual(doc["max_total_num_tokens"], 1000, "the rest is intact")

    def test_a_doubly_encoded_path_is_refused_in_both_modes(self):
        """canonical_path decodes once and SGLang decodes again, so /%2576%2531/... became
        /%76%31/... here (not /v1/, so no wall and no guard) and /v1/... in the engine."""
        Engine.require_key = "engine-K"
        walled = self.live(self.keys_file('{"tok-alice":"alice"}'), upstream_key="engine-K")
        open_ = self.live(upstream_key="engine-K")
        for port, tok in ((walled, None), (walled, "tok-alice"), (open_, "engine-K")):
            for path in ("/%2576%2531/chat/completions", "/v1/%2563hat/completions",
                         "/%252576%252531/models"):
                with self.subTest(port=port, token=tok, path=path):
                    Engine.last_auth = None
                    status, body = call(port, "POST", path, tok)
                    self.assertEqual(status, 400, body)
                    self.assertIsNone(Engine.last_auth, "the engine was never reached")

    def test_upstream_key_admits_named_client_as_engine_key(self):
        Engine.require_key = "engine-K"
        port = self.live(self.keys_file('{"tok-alice":"alice"}'), upstream_key="engine-K")
        status, _ = post(port, "/v1/chat/completions", "tok-alice")
        self.assertEqual(status, 200)
        self.assertEqual(Engine.last_auth, "Bearer engine-K")

    def test_verbatim_without_upstream_key_named_client_meets_engine_check(self):
        Engine.require_key = "engine-K"
        port = self.live(self.keys_file('{"tok-alice":"alice"}'))
        status, body = post(port, "/v1/chat/completions", "tok-alice")
        self.assertEqual(status, 401)
        self.assertNotIn("client key", json.loads(body)["error"]["message"])
        self.assertEqual(Engine.last_auth, "Bearer tok-alice")

    def test_upstream_key_stays_dormant_when_wall_off(self):
        Engine.require_key = "engine-K"
        port = self.live(upstream_key="engine-K")
        status, _ = post(port, "/v1/chat/completions", "engine-K")
        self.assertEqual(status, 200)
        self.assertEqual(Engine.last_auth, "Bearer engine-K")

    def test_an_unreadable_keys_file_says_so(self):
        """docs/clients.md installed the file owned by root until v1.18.7; the proxy runs as
        the user and reads it itself, so it refused to start saying "missing, malformed or
        empty" about a file that was there and well formed."""
        path = self.keys_file('{"tok-alice":"alice"}')
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o600)
        err = io.StringIO()
        real, sys.stderr = sys.stderr, err
        try:
            with self.assertRaises(SystemExit):
                fresh_proxy_module(self.eport, path)
        finally:
            sys.stderr = real
        self.assertIn("not readable by this user", err.getvalue())

    def test_missing_empty_broken_keys_files_refuse_to_be_imported(self):
        for path, text in ((None, "/nonexistent/keys.json"), ("{}", "{}"), ("{not json", "{not json")):
            real = "/nonexistent/keys.json" if path is None else self.keys_file(text)
            with self.assertRaises(SystemExit, msg=repr(text)):
                fresh_proxy_module(self.eport, real)


if __name__ == "__main__":
    unittest.main(verbosity=2)
