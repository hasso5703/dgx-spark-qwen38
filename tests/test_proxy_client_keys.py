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

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or "0")
        self.rfile.read(n)
        body = json.dumps({"id": "chatcmpl-test", "choices": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")


_fresh = [0]


def fresh_proxy_module(upstream_port, keys_path=None):
    """Import the proxy with the environment the scenario calls for.

    Module import is when the keys wall is built, so the scenario has to
    exist before the exec, and the SystemExit of a refused start surfaces
    right here: fail closed is observable at import, not only in a journal.
    """
    _fresh[0] += 1
    spec = importlib.util.spec_from_file_location(
        "kproxy_keys_%d" % _fresh[0], REPO / "keepalive-proxy.py")
    mod = importlib.util.module_from_spec(spec)
    saved = [os.environ.get(k) for k in ("UPSTREAM", "QWEN38_CLIENT_KEYS_FILE")]
    os.environ["UPSTREAM"] = "http://127.0.0.1:%d" % upstream_port
    if keys_path is None:
        os.environ.pop("QWEN38_CLIENT_KEYS_FILE", None)
    else:
        os.environ["QWEN38_CLIENT_KEYS_FILE"] = keys_path
    try:
        spec.loader.exec_module(mod)
    finally:
        for key, value in zip(("UPSTREAM", "QWEN38_CLIENT_KEYS_FILE"), saved):
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

    def keys_file(self, text):
        fd, path = tempfile.mkstemp(suffix=".json", text=True)
        with open(fd, "w") as f:
            f.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def live(self, keys_path=None):
        mod = fresh_proxy_module(self.eport, keys_path)
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

    def test_health_stays_open_without_a_key(self):
        port = self.live(self.keys_file('{"tok-alice":"alice"}'))
        self.assertEqual(get(port, "/health"), 200)

    def test_missing_empty_broken_keys_files_refuse_to_be_imported(self):
        for path, text in ((None, "/nonexistent/keys.json"), ("{}", "{}"), ("{not json", "{not json")):
            real = "/nonexistent/keys.json" if path is None else self.keys_file(text)
            with self.assertRaises(SystemExit, msg=repr(text)):
                fresh_proxy_module(self.eport, real)


if __name__ == "__main__":
    unittest.main(verbosity=2)
