#!/usr/bin/env python3
"""The serving key is checked before a body is read (keepalive-proxy v6.34).

Without the identity wall, the engine's own key check is this port's one gate, and the
proxy applies it itself: a request that does not carry the key the engine admits gets the
engine's own 401 before a byte of its body is read, with no engine work behind it, and the
routes the engine leaves open (/health*, /metrics*) stay open. The rule is the engine's,
and the matrix below is what the live engine answered on 2026-10-08 (SGLang's
srt/utils/auth.py, the same in both pinned images)."""
import http.client
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
KEY = "door-test-key-0123456789"

# (Authorization value, does the engine admit it), as measured on the live engine.
ENGINE_RULE = [
    (f"Bearer {KEY}", True),
    (f"bearer {KEY}", True),
    (f"BEARER {KEY}", True),
    (f"  Bearer {KEY}", True),
    (f"Bearer {KEY} ", True),
    (f"Bearer  {KEY}", False),
    (f"Bearer\t{KEY}", False),
    (f"Token {KEY}", False),
    (KEY, False),
    ("Bearer wrong", False),
    (f"Bearer {KEY}x", False),
    ("Bearer", False),
    (None, False),
]


class TheRuleItself(unittest.TestCase):
    """engine_admits and the key it compares with, in process (the coverage floor reads these)."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("kproxy_door", HERE.parents[1] / "keepalive-proxy.py")
        cls.m = importlib.util.module_from_spec(spec)
        sys.argv = ["keepalive-proxy.py"]
        spec.loader.exec_module(cls.m)

    def test_the_measured_matrix(self):
        for auth, admitted in ENGINE_RULE:
            with self.subTest(auth=auth):
                self.assertEqual(self.m.engine_admits(auth, KEY), admitted)

    def test_a_non_ascii_bearer_is_refused_not_raised(self):
        self.assertFalse(self.m.engine_admits("Bearer cl\xe9", KEY))

    def test_the_key_comes_from_the_upstream_setting_then_the_file(self):
        old_up, old_read = self.m.UPSTREAM_API_KEY, self.m._api_key
        try:
            self.m.UPSTREAM_API_KEY = "from-env"
            self.m._api_key = lambda: "from-file"
            self.assertEqual(self.m._door_key(), "from-env")
            self.m.UPSTREAM_API_KEY = ""
            self.assertEqual(self.m._door_key(), "from-file")
            self.m._api_key = lambda: ""
            self.assertIsNone(self.m._door_key(), "an empty key file compares with nothing")

            def missing():
                raise FileNotFoundError("no key here")
            self.m._api_key = missing
            self.assertIsNone(self.m._door_key())
        finally:
            self.m.UPSTREAM_API_KEY, self.m._api_key = old_up, old_read


class Engine(http.server.BaseHTTPRequestHandler):
    """Answers everything and writes down every request it was asked, so a test can prove
    the engine was never reached. It checks no key: whatever reaches it is relayed."""
    seen = []
    lock = threading.Lock()

    def log_message(self, *a):
        pass

    def _send(self, obj):
        out = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        with Engine.lock:
            Engine.seen.append(("GET", self.path, self.headers.get("Authorization")))
        if self.path.startswith("/v1/models"):
            self._send({"object": "list", "data": [{"id": "qwen3.8-test", "object": "model"}]})
        elif self.path in ("/server_info", "/get_server_info"):
            self._send({"max_total_num_tokens": 1_000_000})
        else:
            self._send({"ok": True})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        with Engine.lock:
            Engine.seen.append(("POST", self.path, self.headers.get("Authorization")))
        if self.path == "/tokenize":
            self._send({"count": 10})
            return
        self._send({"id": "x", "object": "chat.completion", "model": "qwen3.8-test",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


def _free_port():
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


def _start_proxy(home, upstream_port, extra_env=None):
    port = _free_port()
    err = tempfile.NamedTemporaryFile(prefix="door-proxy-stderr-", delete=False)
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "PROXY_BIND": "127.0.0.1",
           "UPSTREAM": f"http://127.0.0.1:{upstream_port}", **(extra_env or {})}
    proc = subprocess.Popen([sys.executable, str(HERE.parents[1] / "keepalive-proxy.py"), str(port)],
                            env=env, stdout=err, stderr=err)
    end = time.time() + 20
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    return proc, port, err.name


def _home(with_key=True):
    home = Path(tempfile.mkdtemp(prefix="door-home-"))
    (home / ".config/qwen38").mkdir(parents=True)
    if with_key:
        (home / ".config/qwen38/api-key").write_text(KEY + "\n")
    return home


class TheDoor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=cls.eng.serve_forever, daemon=True).start()
        cls.home = _home()
        cls.proc, cls.port, cls.err = _start_proxy(cls.home, cls.eng.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.eng.shutdown()
        cls.eng.server_close()
        shutil.rmtree(cls.home, ignore_errors=True)
        try:
            text = Path(cls.err).read_text(errors="replace")
        finally:
            os.unlink(cls.err)
        # A traceback is an error even when the status the client got was right: the
        # door-full path of an earlier draft wrote its 503 and then raised.
        assert "Traceback" not in text, text[-2000:]

    def setUp(self):
        with Engine.lock:
            Engine.seen = []

    def request(self, method, path, body=None, auth=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            c.putrequest(method, path, skip_accept_encoding=True)
            if auth is not None:
                c.putheader("Authorization", auth)
            for k, v in (headers or {}).items():
                c.putheader(k, v)
            if body is not None:
                c.putheader("Content-Type", "application/json")
                c.putheader("Content-Length", str(len(body)))
            c.endheaders()
            if body is not None:
                c.send(body)
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def chat(self, auth, content="hi"):
        body = json.dumps({"model": "qwen3.8-test", "messages": [{"role": "user", "content": content}],
                           "max_tokens": 4}).encode()
        return self.request("POST", "/v1/chat/completions", body, auth)

    def test_the_engine_rule_decides_and_a_refusal_costs_the_engine_nothing(self):
        for auth, admitted in ENGINE_RULE:
            with self.subTest(auth=auth):
                with Engine.lock:
                    Engine.seen = []
                status, raw = self.chat(auth)
                if admitted:
                    self.assertEqual(status, 200, raw[:200])
                    self.assertIn(("POST", "/v1/chat/completions"), [s[:2] for s in Engine.seen])
                else:
                    self.assertEqual(status, 401, raw[:200])
                    self.assertEqual(raw, b'{"error":"Unauthorized"}', "the engine's own answer")
                    self.assertEqual(Engine.seen, [], "a refused request reached the engine")

    def test_a_large_body_without_the_key_is_refused_with_no_count_and_no_relay(self):
        body = json.dumps({"model": "qwen3.8-test",
                           "messages": [{"role": "user", "content": "1 " * 1_200_000}]}).encode()
        status, raw = self.request("POST", "/v1/chat/completions", body, None)
        self.assertEqual(status, 401, raw[:200])
        self.assertEqual(Engine.seen, [], "no /server_info, no /tokenize, no relay")

    def test_the_body_is_not_read_before_the_refusal(self):
        """Headers announcing 50 MB, and not one byte of it sent: the 401 still comes, at
        once. Before v6.34 the proxy waited for the body, and only then relayed it."""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                      b"Content-Type: application/json\r\nContent-Length: 50000000\r\n\r\n")
            s.settimeout(5)
            head = b""
            t0 = time.time()
            while b"\r\n\r\n" not in head:
                piece = s.recv(4096)
                if not piece:
                    break
                head += piece
        finally:
            s.close()
        self.assertTrue(head.startswith(b"HTTP/1.1 401"), head[:80])
        self.assertLess(time.time() - t0, 3)
        self.assertEqual(Engine.seen, [])

    def test_the_routes_the_engine_leaves_open_need_no_key(self):
        for path in ("/health", "/health_generate", "/metrics"):
            with self.subTest(path=path):
                with Engine.lock:
                    Engine.seen = []
                status, _ = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIn(("GET", path), [s[:2] for s in Engine.seen])

    def test_a_read_without_the_key_is_refused_too(self):
        for path in ("/v1/models", "/server_info", "/get_model_info"):
            with self.subTest(path=path):
                with Engine.lock:
                    Engine.seen = []
                status, raw = self.request("GET", path)
                self.assertEqual(status, 401, raw[:200])
                self.assertEqual(Engine.seen, [])
        status, raw = self.request("GET", "/v1/models", auth=f"Bearer {KEY}")
        self.assertEqual(status, 200, raw[:200])

    def test_an_escaped_open_route_is_still_open_and_an_escaped_closed_one_still_closed(self):
        """The door judges the path the engine routes on (decoded once), never the raw one."""
        status, _ = self.request("GET", "/%68ealth")
        self.assertEqual(status, 200)
        status, _ = self.request("GET", "/%76%31/models")
        self.assertEqual(status, 401)

    def test_systemone_keeps_its_envelope(self):
        body = json.dumps({"state": "s", "model": "jev-latest",
                           "questions": {"q": {"type": "noul", "instructions": "i"}}}).encode()
        status, raw = self.request("POST", "/v1/systemone", body, "Bearer wrong")
        self.assertEqual(status, 401, raw[:200])
        detail = json.loads(raw)["detail"]
        self.assertEqual(detail["error_type"], "engine_error")
        self.assertIn("key", detail["message"])
        self.assertEqual(Engine.seen, [], "no model lookup, no pool read, no branch")

    def test_a_client_that_asks_first_is_refused_before_it_sends(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                      b"Expect: 100-continue\r\nContent-Length: 1000000\r\n\r\n")
            head = b""
            while b"\r\n\r\n" not in head:
                piece = s.recv(4096)
                if not piece:
                    break
                head += piece
        finally:
            s.close()
        self.assertTrue(head.startswith(b"HTTP/1.1 401"), head[:80])
        self.assertEqual(Engine.seen, [])

    def test_a_client_with_the_key_that_asks_first_is_invited(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                      b"Authorization: Bearer " + KEY.encode() + b"\r\n"
                      b"Expect: 100-continue\r\nContent-Length: 2\r\n\r\n")
            s.settimeout(5)
            head = s.recv(4096)
        finally:
            s.close()
        self.assertTrue(head.startswith(b"HTTP/1.1 100"), head[:80])


class WithoutAKeyToCompare(unittest.TestCase):
    """A box whose key the proxy cannot read: the engine's own check decides, as before."""

    @classmethod
    def setUpClass(cls):
        cls.eng = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=cls.eng.serve_forever, daemon=True).start()
        cls.home = _home(with_key=False)
        cls.proc, cls.port, cls.err = _start_proxy(cls.home, cls.eng.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.eng.shutdown()
        cls.eng.server_close()
        shutil.rmtree(cls.home, ignore_errors=True)
        os.unlink(cls.err)

    def test_a_request_without_a_key_is_relayed(self):
        with Engine.lock:
            Engine.seen = []
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            c.request("POST", "/v1/chat/completions",
                      json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}),
                      {"Content-Type": "application/json"})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            r.read()
        finally:
            c.close()
        self.assertIn(("POST", "/v1/chat/completions"), [s[:2] for s in Engine.seen])


class TheIdentityWallIsUnchanged(unittest.TestCase):
    """With the wall on, clients hold bearers of their own, which the engine's rule would
    refuse: the wall decides, and the door stands aside."""

    @classmethod
    def setUpClass(cls):
        cls.eng = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=cls.eng.serve_forever, daemon=True).start()
        cls.home = _home()
        keys = cls.home / "client-keys.json"
        keys.write_text(json.dumps({"alice-token": "alice"}))
        cls.proc, cls.port, cls.err = _start_proxy(
            cls.home, cls.eng.server_address[1],
            {"QWEN38_CLIENT_KEYS_FILE": str(keys), "QWEN38_UPSTREAM_API_KEY": KEY})

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.eng.shutdown()
        cls.eng.server_close()
        shutil.rmtree(cls.home, ignore_errors=True)
        os.unlink(cls.err)

    def post(self, auth):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            c.request("POST", "/v1/chat/completions",
                      json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}),
                      {"Content-Type": "application/json", "Authorization": auth})
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def test_a_named_client_is_admitted_with_the_engine_key_upstream(self):
        with Engine.lock:
            Engine.seen = []
        status, raw = self.post("Bearer alice-token")
        self.assertEqual(status, 200, raw[:200])
        self.assertIn(("POST", "/v1/chat/completions", f"Bearer {KEY}"), Engine.seen)

    def test_an_unlisted_bearer_meets_the_wall(self):
        status, raw = self.post(f"Bearer {KEY}")
        self.assertEqual(status, 401)
        self.assertIn(b"missing or unknown client key", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
