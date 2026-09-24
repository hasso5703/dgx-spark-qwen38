#!/usr/bin/env python3
"""A client's own token count is answered, not refused as too long.

`/v1/messages/count_tokens` is how an Anthropic client learns whether a conversation fits,
and it generates nothing, so it cannot wedge the scheduler the size guard protects. The
guard applied to it all the same: a conversation past the lane's limit got the 400
"the prompt is too long for this lane" instead of its count, and one sent while the pool
was unmeasured got a 503 (found in review, 2026-09-24). And the pool the guard needs was
read with a 4 s timeout, while /server_info waits on the scheduler, which answers between
two steps of a long prefill: a big prompt sent then got a 503 "not measured yet". This runs
the real proxy against a fake engine whose pool is 100,000 tokens."""
import http.client
import http.server
import json
import os
import pathlib
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
WORDS = 150_000                     # past the 100,000-token pool, in 900 KB of text


class FakeEngine(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list = []
    pool = 100_000
    info_delay = 0.0                # a scheduler in the middle of a long prefill chunk

    def log_message(self, *a):
        pass

    def reply(self, obj, code=200):
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        FakeEngine.seen.append(("GET", self.path))
        if self.path.startswith("/server_info") or self.path.startswith("/get_server_info"):
            time.sleep(FakeEngine.info_delay)
            if not FakeEngine.pool:
                return self.reply({"error": "loading"}, 503)
            return self.reply({"max_total_num_tokens": FakeEngine.pool})
        if self.path.startswith("/v1/models"):
            return self.reply({"data": [{"id": "qwen3.8-27b"}]})
        return self.reply({})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        FakeEngine.seen.append(("POST", self.path))
        words = sum(len(str(m.get("content", "")).split()) for m in body.get("messages", []))
        if self.path == "/v1/messages/count_tokens":
            return self.reply({"input_tokens": words})
        if self.path == "/tokenize":
            return self.reply({"tokens": [], "count": words, "max_model_len": 262144})
        return self.reply({"id": "x", "type": "message", "content": [{"type": "text", "text": "ok"}]})


class Engine(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TheCountIsAnswered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Engine(("127.0.0.1", 0), FakeEngine)
        threading.Thread(target=cls.engine.serve_forever, daemon=True).start()
        cls.home = pathlib.Path(tempfile.mkdtemp(prefix="kp-count-"))
        (cls.home / ".config" / "qwen38").mkdir(parents=True)
        (cls.home / ".config" / "qwen38" / "api-key").write_text("k-test\n")
        cls.proxies = []
        cls.port = cls.start_proxy()

    @classmethod
    def start_proxy(cls):
        """A proxy of its own: the pool it measured stays cached for ten minutes."""
        port = free_port()
        env = {"PATH": os.environ["PATH"], "HOME": str(cls.home), "PROXY_BIND": "127.0.0.1",
               "UPSTREAM": f"http://127.0.0.1:{cls.engine.server_address[1]}",
               "PROMPT_CEILING_TOKENS": "0", "FLASH_PROMPT_CEILING_TOKENS": "0"}
        cls.proxies.append(subprocess.Popen([sys.executable, str(REPO / "keepalive-proxy.py"), str(port)], env=env,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        end = time.time() + 20
        while time.time() < end:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.1)
        return port

    @classmethod
    def tearDownClass(cls):
        for p in cls.proxies:
            p.terminate()
            p.wait(10)
        cls.engine.shutdown()
        cls.engine.server_close()
        shutil.rmtree(cls.home, ignore_errors=True)

    def setUp(self):
        FakeEngine.seen.clear()
        FakeEngine.pool = 100_000
        FakeEngine.info_delay = 0.0

    def post(self, path, port=None):
        body = json.dumps({"model": "qwen3.8-27b", "max_tokens": 16,
                           "messages": [{"role": "user", "content": " ".join(["word"] * WORDS)}]}).encode()
        c = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=30)
        try:
            c.request("POST", path, body, {"Content-Type": "application/json", "Authorization": "Bearer k-test"})
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def test_a_count_past_the_pool_is_answered(self):
        st, out = self.post("/v1/messages/count_tokens")
        self.assertEqual(st, 200, out[:300])
        self.assertEqual(json.loads(out), {"input_tokens": WORDS})

    def test_a_count_while_the_pool_is_unmeasured_is_answered(self):
        FakeEngine.pool = 0                 # /server_info answers 503: the engine is loading
        port = self.start_proxy()
        st, out = self.post("/v1/messages/count_tokens", port)
        self.assertEqual(st, 200, out[:300])
        st, out = self.post("/v1/messages", port)
        self.assertEqual(st, 503, "a generation that size still waits for a measured pool")

    def test_the_same_body_as_a_generation_is_still_refused(self):
        st, out = self.post("/v1/messages")
        self.assertEqual(st, 400, out[:300])
        self.assertIn(b"context_too_long", out)
        self.assertNotIn(("POST", "/v1/messages"), FakeEngine.seen, "the refused generation reached the engine")

    def test_a_slow_pool_read_is_waited_for(self):
        FakeEngine.pool, FakeEngine.info_delay = 1_000_000, 6.0
        port = self.start_proxy()            # nothing measured yet
        t0 = time.time()
        st, out = self.post("/v1/messages", port)
        self.assertEqual(st, 200, out[:300])
        self.assertGreater(time.time() - t0, 5.5, "the answer came before the pool could have been read")


if __name__ == "__main__":
    unittest.main(verbosity=2)
