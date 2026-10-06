#!/usr/bin/env python3
"""One Server header and one Date on every answer the proxy relays (keepalive-proxy v6.33).

send_response writes the proxy's own Server and Date, and the engine's (uvicorn's
`server: uvicorn` and `date`) were relayed beside them: two of each on every relayed answer,
which RFC 9110 (5.3) does not allow. aiohttp refuses such an answer by default in 3.13.4 and in
its strict mode since then, so a LiteLLM gateway in front of the proxy answered "Duplicate
'Server' header found." (issue #39). The fake engine here writes both as uvicorn does, in
lower case, beside a header of its own that must still come through once."""
import collections
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

    def _head(self, status, ctype, length=None):
        self.send_response_only(status)
        self.send_header("date", self.date_time_string())
        self.send_header("server", "uvicorn")
        self.send_header("content-type", ctype)
        self.send_header("x-request-id", "engine-1")
        if length is None:
            self.send_header("connection", "close")
            self.close_connection = True
        else:
            self.send_header("content-length", str(length))
        self.end_headers()

    def do_GET(self):
        out = json.dumps({"object": "list", "data": [{"id": "m", "object": "model"}]}).encode()
        self._head(200, "application/json", len(out)); self.wfile.write(out)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if "refuse" in json.dumps(body):
            out = json.dumps({"object": "error", "message": "refused", "code": 400}).encode()
            self._head(400, "application/json", len(out)); self.wfile.write(out); return
        if body.get("stream"):
            self._head(200, "text/event-stream; charset=utf-8")
            chunk = {"id": "c", "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": {"content": "four"}, "finish_reason": "stop"}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()); return
        out = json.dumps({"id": "c", "object": "chat.completion", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "four"}, "finish_reason": "stop"}]}).encode()
        self._head(200, "application/json", len(out)); self.wfile.write(out)


class EveryRelayedAnswerHasOneServerAndOneDate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=cls.engine.serve_forever, daemon=True).start()
        cls.addClassCleanup(lambda: (cls.engine.shutdown(), cls.engine.server_close()))
        saved = dict(os.environ)
        cls.addClassCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ.update(UPSTREAM=f"http://127.0.0.1:{cls.engine.server_port}", KEEPALIVE_S="5")
        os.environ.pop("PROXY_HOLD_UNITS", None)
        os.environ.pop("QWEN38_CLIENT_KEYS_FILE", None)
        spec = importlib.util.spec_from_file_location(f"kp_headers_{time.time_ns()}", PROXY)
        cls.mod = importlib.util.module_from_spec(spec)
        argv = list(sys.argv)
        cls.addClassCleanup(setattr, sys, "argv", argv)
        sys.argv = ["keepalive-proxy.py"]
        spec.loader.exec_module(cls.mod)
        cls.mod.log = lambda *a: None
        cls.proxy = cls.mod.Server(("127.0.0.1", 0), cls.mod.H)
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()
        cls.addClassCleanup(lambda: (cls.proxy.shutdown(), cls.proxy.server_close()))

    def answer(self, method, path, body=None):
        """The status line and the header names of the answer, as they came on the wire."""
        s = socket.create_connection(("127.0.0.1", self.proxy.server_address[1]), timeout=10)
        data = json.dumps(body).encode() if body is not None else b""
        req = f"{method} {path} HTTP/1.1\r\nHost: p\r\n"
        if body is not None:
            req += f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n"
        s.sendall((req + "\r\n").encode() + data)
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
        lines = got.partition(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
        return lines[0], [ln.split(":", 1)[0].strip().lower() for ln in lines[1:]]

    def assertOneOfEach(self, status, names, want):
        self.assertIn(f" {want} ", status + " ")
        counts = collections.Counter(names)
        self.assertEqual(counts["server"], 1, names)
        self.assertEqual(counts["date"], 1, names)
        self.assertEqual([n for n, c in counts.items() if c > 1], [], names)
        self.assertEqual(counts["x-request-id"], 1, f"the engine's own header was lost: {names}")

    def test_a_get_relayed_whole(self):
        self.assertOneOfEach(*self.answer("GET", "/v1/models"), 200)

    def test_a_non_streamed_generation(self):
        self.assertOneOfEach(*self.answer("POST", "/v1/chat/completions",
                                          {"messages": [{"role": "user", "content": "2+2?"}], "stream": False}), 200)

    def test_a_stream(self):
        self.assertOneOfEach(*self.answer("POST", "/v1/chat/completions",
                                          {"messages": [{"role": "user", "content": "2+2?"}], "stream": True}), 200)

    def test_an_error_of_the_engine(self):
        self.assertOneOfEach(*self.answer("POST", "/v1/chat/completions",
                                          {"messages": [{"role": "user", "content": "refuse"}], "stream": False}), 400)


if __name__ == "__main__":
    unittest.main()
