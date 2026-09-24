#!/usr/bin/env python3
"""conc-check.py's verdict is its exit status too.

It printed wrong answers, cross-request contamination and failed requests, and exited 0
whatever it found (found in review, 2026-09-24), so no caller could gate on it. A fake
engine answers every task right, one wrong, or refuses.
"""
import contextlib
import http.server
import importlib.util
import io
import json
import os
import pathlib
import shutil
import tempfile
import threading
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
MODE = ["right"]


def load():
    home = tempfile.mkdtemp(prefix="conc-home-")
    (pathlib.Path(home) / ".config" / "qwen38").mkdir(parents=True)
    (pathlib.Path(home) / ".config" / "qwen38" / "api-key").write_text("test-key\n")
    real = os.environ.get("HOME")
    os.environ["HOME"] = home                  # the key is read at import
    try:
        spec = importlib.util.spec_from_file_location("conc_check_exit", REPO / "conc-check.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if real is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = real
        shutil.rmtree(home, ignore_errors=True)


cc = load()


class Fake(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.send(200, {"model_path": "m"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        prompt = body["messages"][0]["content"]
        if MODE[0] == "error":
            return self.send(500, {"error": "boom"})
        name, _, want = next(t for t in cc.TASKS if t[1] in prompt)
        answer = "no idea" if (MODE[0] == "wrong" and name == cc.TASKS[0][0]) else want
        self.send(200, {"choices": [{"message": {"content": answer}}]})


class TheExitStatus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.old_base, cc.BASE = cc.BASE, f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cc.BASE = cls.old_base
        cls.srv.shutdown()
        cls.srv.server_close()

    def status(self, mode):
        MODE[0] = mode
        with contextlib.redirect_stdout(io.StringIO()):
            return cc.main(["--serial", "4", "--conc-n", "8", "--conc", "2"])

    def test_every_answer_right_is_0(self):
        self.assertEqual(self.status("right"), 0)

    def test_a_wrong_answer_is_1(self):
        self.assertEqual(self.status("wrong"), 1)

    def test_a_failed_request_is_3(self):
        self.assertEqual(self.status("error"), 3)


if __name__ == "__main__":
    unittest.main()
