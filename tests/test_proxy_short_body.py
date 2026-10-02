#!/usr/bin/env python3
"""A body that never arrived whole is not sent on to the engine (keepalive-proxy v6.29).

A caller that closed before the last byte of its body left the proxy a short read, and the
proxy sent the engine what had come: of 300 requests closed with a reset at once after
sending, 29 reached the engine with an empty body (measured 2026-10-02). SGLang refuses that,
so nothing was generated, but the relay simulation's engine fell over it about once in ten
runs, which failed tests/proxy_fuzz.py on v1.20.5 already. These run the proxy in this
process against an engine that counts what reaches it."""
import http.server
import importlib.util
import json
import os
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

PROXY = Path(__file__).resolve().parents[1] / "keepalive-proxy.py"


class Engine(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        want = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(want)
        self.server.got.append((self.path, want, len(body)))
        out = json.dumps({"id": "x", "object": "chat.completion", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "four"}, "finish_reason": "stop"}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)


class ABodyThatNeverArrivedWhole(unittest.TestCase):
    def setUp(self):
        self.engine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        self.engine.got = []
        threading.Thread(target=self.engine.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.engine.shutdown(), self.engine.server_close()))
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ.update(UPSTREAM=f"http://127.0.0.1:{self.engine.server_port}", KEEPALIVE_S="1")
        os.environ.pop("PROXY_HOLD_UNITS", None)
        spec = importlib.util.spec_from_file_location(f"kp_short_{time.time_ns()}", PROXY)
        self.mod = importlib.util.module_from_spec(spec)
        argv = list(sys.argv)
        self.addCleanup(setattr, sys, "argv", argv)
        sys.argv = ["keepalive-proxy.py"]
        spec.loader.exec_module(self.mod)
        self.lines = []
        self.mod.log = self.lines.append
        self.proxy = self.mod.Server(("127.0.0.1", 0), self.mod.H)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.proxy.shutdown(), self.proxy.server_close()))

    def send(self, body, announced):
        s = socket.create_connection(("127.0.0.1", self.proxy.server_address[1]), timeout=10)
        s.sendall(f"POST /v1/chat/completions HTTP/1.1\r\nHost: p\r\nContent-Type: application/json\r\n"
                  f"Content-Length: {announced}\r\n\r\n".encode() + body)
        s.shutdown(socket.SHUT_WR)                  # the caller is done sending
        got = b""
        try:
            while True:
                c = s.recv(65536)
                if not c:
                    break
                got += c
        except OSError:
            pass
        s.close()
        return got

    def ended(self, text, within=5):
        end = time.time() + within
        while time.time() < end and not any(text in ln for ln in self.lines):
            time.sleep(0.02)
        return [ln for ln in self.lines if text in ln]

    def test_a_short_body_never_reaches_the_engine(self):
        whole = json.dumps({"messages": [{"role": "user", "content": "What is 2+2?"}], "stream": False}).encode()
        self.send(whole[:10], announced=len(whole))
        self.assertTrue(self.ended("CLIENT GONE before its body"), self.lines)
        self.assertTrue(any(f"before its body arrived whole (10 of {len(whole)} bytes)" in ln for ln in self.lines),
                        self.lines)
        time.sleep(0.3)
        self.assertEqual(self.engine.got, [], "the engine was asked with a body that never came")

    def test_an_empty_read_is_one_too(self):
        whole = json.dumps({"messages": [{"role": "user", "content": "hi"}], "stream": False}).encode()
        self.send(b"", announced=len(whole))
        self.assertTrue(self.ended("CLIENT GONE before its body"), self.lines)
        time.sleep(0.3)
        self.assertEqual(self.engine.got, [])

    def test_a_whole_body_is_sent_on_as_it_came(self):
        """The control, by a caller that stays: one that closes its sending side after the
        body is one that left, for a non-streamed request (the proxy watches for it)."""
        import http.client
        whole = json.dumps({"messages": [{"role": "user", "content": "What is 2+2?"}], "stream": False}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_address[1], timeout=10)
        conn.request("POST", "/v1/chat/completions", body=whole, headers={"Content-Type": "application/json"})
        got = conn.getresponse().read()
        conn.close()
        self.assertIn(b"four", got)
        self.assertEqual(self.engine.got, [("/v1/chat/completions", len(whole), len(whole))])
        self.assertFalse([ln for ln in self.lines if "before its body" in ln], self.lines)


if __name__ == "__main__":
    unittest.main()
