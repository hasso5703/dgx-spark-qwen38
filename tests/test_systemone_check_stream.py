#!/usr/bin/env python3
"""systemone-check.py counts an ordinary chat completion as clean only when it is a whole answer.

Its mixed-load check sends ordinary chat traffic beside the typed decisions and grades both.
A streamed completion was graded clean as soon as its body held "data:", and through the
keepalive proxy every stream holds that: the keepalive frames it injects while the engine
is quiet are `data:` chunks, a stream the engine cut mid-answer keeps the chunks it had, and
the abort for corrupted output is a `data:` error event followed by `data: [DONE]`. So the
check could report "both clean" for completions that never answered. These run the tool's
own load loop through the real proxy, in front of a fake engine that answers each way.
"""
import http.server
import importlib.util
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
KEEPALIVE_S = 0.3
MAX_SILENCE_S = 1.5


def chunk(delta, finish=None):
    return ("data: " + json.dumps({"id": "chatcmpl-1", "object": "chat.completion.chunk", "model": "qwen3.8-27b",
                                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n").encode()


class Engine(http.server.BaseHTTPRequestHandler):
    """Answers every completion the way Engine.mode says."""
    protocol_version = "HTTP/1.1"
    mode = "whole"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path.startswith("/abort_request"):
            out = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        mode = Engine.mode
        if not body.get("stream"):
            content = "" if mode == "empty" else "red\ngreen\nblue"
            out = json.dumps({"id": "chatcmpl-1", "object": "chat.completion", "model": "qwen3.8-27b",
                              "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                           "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        w = self.wfile
        try:
            w.write(chunk({"role": "assistant", "content": ""}))
            w.flush()
            if mode == "whole":
                for word in ("red\n", "green\n", "blue"):
                    w.write(chunk({"content": word}))
                w.write(chunk({}, "stop"))
                w.write(b"data: [DONE]\n\n")
            elif mode == "cut":
                time.sleep(KEEPALIVE_S * 3)        # the proxy fills the wait with keepalives
                w.write(chunk({"content": "red\n"}))
                # and the engine goes away: no finish_reason, no [DONE]
            elif mode == "silent":
                time.sleep(MAX_SILENCE_S + KEEPALIVE_S * 3)   # keepalives, then the proxy drops it
            elif mode == "bangs":
                for _ in range(40):
                    w.write(chunk({"content": "!!!!!!!!"}))
                w.write(chunk({}, "length"))
                w.write(b"data: [DONE]\n\n")
            w.flush()
        except Exception:
            pass


class TheMixedLoadGrade(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        cls.engine.daemon_threads = True
        threading.Thread(target=cls.engine.serve_forever, daemon=True).start()
        cls.saved_env = {k: os.environ.get(k) for k in
                         ("UPSTREAM", "KEEPALIVE_S", "MAX_SILENCE_S", "CLIENT_IO_S", "PROMPT_CEILING_TOKENS")}
        os.environ.update(UPSTREAM=f"http://127.0.0.1:{cls.engine.server_port}", KEEPALIVE_S=str(KEEPALIVE_S),
                          MAX_SILENCE_S=str(MAX_SILENCE_S), CLIENT_IO_S="10", PROMPT_CEILING_TOKENS="0")
        spec = importlib.util.spec_from_file_location(f"kp_so_{time.time_ns()}", REPO / "keepalive-proxy.py")
        cls.proxy_mod = importlib.util.module_from_spec(spec)
        argv, sys.argv = sys.argv, ["keepalive-proxy.py"]
        try:
            spec.loader.exec_module(cls.proxy_mod)
        finally:
            sys.argv = argv
        cls.proxy = cls.proxy_mod.Server(("127.0.0.1", 0), cls.proxy_mod.H)
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()
        spec = importlib.util.spec_from_file_location("systemone_check", REPO / "systemone-check.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.engine.shutdown()
        cls.engine.server_close()
        for k, v in cls.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def grade(self, mode, stream):
        """One pass of the tool's own load loop against the engine answering `mode`."""
        Engine.mode = mode
        stats = {"ok": 0, "bad": 0, "lat": [], "model": "qwen3.8-27b"}
        stop = threading.Event()
        t = threading.Thread(target=self.tool.classic_load,
                             args=(f"http://127.0.0.1:{self.proxy.server_address[1]}", "k", stop, stats, stream))
        t.start()
        deadline = time.time() + 20
        while stats["ok"] + stats["bad"] < 1 and time.time() < deadline:
            time.sleep(0.02)
        stop.set()
        t.join(30)
        self.assertGreaterEqual(stats["ok"] + stats["bad"], 1, "the load loop never finished a request")
        return stats

    def test_a_whole_streamed_answer_is_clean(self):
        s = self.grade("whole", True)
        self.assertGreater(s["ok"], 0, s)
        self.assertEqual(s["bad"], 0, s)

    def test_a_stream_cut_after_keepalives_is_not_clean(self):
        s = self.grade("cut", True)
        self.assertEqual(s["ok"], 0, s)

    def test_a_stream_of_keepalives_only_is_not_clean(self):
        s = self.grade("silent", True)
        self.assertEqual(s["ok"], 0, s)

    def test_the_abort_for_corrupted_output_is_not_clean(self):
        s = self.grade("bangs", True)
        self.assertEqual(s["ok"], 0, s)

    def test_a_whole_plain_answer_is_clean(self):
        s = self.grade("whole", False)
        self.assertGreater(s["ok"], 0, s)
        self.assertEqual(s["bad"], 0, s)

    def test_an_empty_plain_answer_is_not_clean(self):
        s = self.grade("empty", False)
        self.assertEqual(s["ok"], 0, s)


if __name__ == "__main__":
    unittest.main()
