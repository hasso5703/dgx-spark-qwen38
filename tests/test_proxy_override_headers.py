#!/usr/bin/env python3
"""A caller's x-override-* headers stay at the door, but the rid (found in a cloud review, 2026-10-18).

The SGLang lanes start their engine with SGLANG_ENABLE_REQUEST_HEADER_OVERRIDES=1 so that it takes
the request id this proxy names (x-override-rid, v6.14). The same switch makes the engine apply
every override header it knows to the request, from whoever sends it: in v0.5.21
(python/sglang/srt/entrypoints/request_headers.py) x-override-rid, -bootstrap-host, -bootstrap-port,
-bootstrap-room, -conversation-id, -routed-dp-rank, -disagg-prefill-dp-rank and -priority. The proxy
relayed every header a caller sent but the hop-by-hop ones, so any client of the port could set the
engine's internal scheduling and disaggregation fields on its own request. None of them is a
client's to set; the rid keeps the behaviour it had (the proxy's own, or the caller's when it names
one). The fake engine records what reached it."""
import http.server
import importlib.util
import json
import os
import re
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path


def _no_key():
    """The serving key this harness's proxy holds: none, whatever HOME holds. Its clients send
    no key; the door has its own tests, in tests/test_proxy_door.py."""
    raise FileNotFoundError("no serving key in this harness")


PROXY = Path(__file__).resolve().parents[1] / "keepalive-proxy.py"
# what SGLang v0.5.21 applies, and one it does not know yet: the rule is the prefix, not the list
OVERRIDES = {"x-override-priority": "9", "x-override-routed-dp-rank": "3",
             "x-override-disagg-prefill-dp-rank": "1", "x-override-bootstrap-host": "10.9.9.9",
             "x-override-bootstrap-port": "1", "x-override-bootstrap-room": "7",
             "x-override-conversation-id": "c-1", "X-Override-Some-Future-Field": "z"}


class Engine(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = []                     # (method, path, {lower-case name: value}) per request

    def log_message(self, *a):
        pass

    def _record(self):
        Engine.seen.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}))

    def _json(self, doc):
        out = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        self._record()
        self._json({"object": "list", "data": [{"id": "m", "object": "model"}]})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._record()
        self._json({"id": "c", "object": "chat.completion", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "four"}, "finish_reason": "stop"}]})


class ACallersOverridesStayAtTheDoor(unittest.TestCase):
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
        spec = importlib.util.spec_from_file_location(f"kp_overrides_{time.time_ns()}", PROXY)
        cls.mod = importlib.util.module_from_spec(spec)
        argv = list(sys.argv)
        cls.addClassCleanup(setattr, sys, "argv", argv)
        sys.argv = ["keepalive-proxy.py"]
        spec.loader.exec_module(cls.mod)
        cls.mod._api_key = _no_key
        cls.mod.log = lambda *a: None
        cls.proxy = cls.mod.Server(("127.0.0.1", 0), cls.mod.H)
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()
        cls.addClassCleanup(lambda: (cls.proxy.shutdown(), cls.proxy.server_close()))

    def setUp(self):
        Engine.seen = []

    def send(self, method, path, headers):
        data = json.dumps({"messages": [{"role": "user", "content": "2+2?"}], "stream": False}).encode() \
            if method == "POST" else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.proxy.server_address[1]}{path}", data=data,
                                     headers={"Content-Type": "application/json", **headers}, method=method)
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)
            r.read()
        # the engine is asked for its models too, before a generation (_open): the request is the last
        mine = [s for s in Engine.seen if s[0] == method and s[1] == path]
        self.assertTrue(mine, Engine.seen)
        return mine[-1][2]

    def test_none_reaches_the_engine_on_a_generation(self):
        got = self.send("POST", "/v1/chat/completions", {**OVERRIDES, "X-Client-Note": "kept"})
        leaked = sorted(k for k in got if k.startswith("x-override-") and k != "x-override-rid")
        self.assertEqual(leaked, [], got)
        self.assertEqual(got.get("x-client-note"), "kept", "an ordinary header of the caller must still go")

    def test_none_reaches_the_engine_on_the_other_routes_either(self):
        for method, path in (("POST", "/generate"), ("POST", "/v1/completions"), ("GET", "/v1/models")):
            got = self.send(method, path, OVERRIDES)
            leaked = sorted(k for k in got if k.startswith("x-override-") and k != "x-override-rid")
            self.assertEqual(leaked, [], f"{method} {path}: {got}")

    def test_the_rid_this_proxy_names_still_goes(self):
        got = self.send("POST", "/v1/chat/completions", OVERRIDES)
        self.assertRegex(got.get("x-override-rid", ""), re.compile(r"^[0-9a-f]{32}$"), got)

    def test_a_callers_own_rid_is_kept_as_before(self):
        got = self.send("POST", "/v1/chat/completions", {"x-override-rid": "rid-of-the-caller", **OVERRIDES})
        self.assertEqual(got.get("x-override-rid"), "rid-of-the-caller", got)


if __name__ == "__main__":
    unittest.main()
