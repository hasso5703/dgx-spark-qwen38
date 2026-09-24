#!/usr/bin/env python3
"""bench-agent.py ends a loop the lane stopped answering with exit 3 and a sentence.

A turn read past its timeout, a connection the engine dropped, or a lane that went away
between two turns ended in a Python traceback and exit 1 (found in review, 2026-09-24): only
an HTTP refusal was caught, and the post() written to catch the rest was never called. A
partial loop is not a measurement, which is what exit 3 says elsewhere in this tool.
"""
import http.server
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "bench-agent.py"
MODE = ["hang"]


class Fake(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if MODE[0] == "hang":
            time.sleep(3)                   # past --timeout 1
            return
        self.close_connection = True        # "reset": gone without an answer


class TheExits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def run_tool(self, port):
        return subprocess.run([sys.executable, str(TOOL), "--port", str(port), "--model", "fake", "--turns", "2",
                               "--turn-tokens", "4", "--prefix-tokens", "50", "--timeout", "1"],
                              capture_output=True, text=True, timeout=60,
                              env=dict(os.environ, QWEN38_API_KEY="not-the-real-key"))

    def test_a_turn_past_its_timeout(self):
        MODE[0] = "hang"
        r = self.run_tool(self.server.server_address[1])
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_a_connection_the_engine_dropped(self):
        MODE[0] = "reset"
        r = self.run_tool(self.server.server_address[1])
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_a_lane_that_is_gone(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                           # nothing listens there now
        r = self.run_tool(port)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
