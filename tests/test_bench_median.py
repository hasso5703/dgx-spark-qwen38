#!/usr/bin/env python3
"""bench.sh leaves out of its median a run it could not time.

stream() returns -1 when a run gave it nothing to time (no token, or a single one), and
that -1 went into the greedy median as a speed: two such runs of four pulled the median
of 60 tok/s down to about 29 (found in review, 2026-09-24). A fake engine answers the code
probe with one token and every other probe with several, spaced so their rate is defined.
"""
import http.server
import json
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "bench.sh"


class Fake(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        raw = json.dumps({"data": [{"id": "qwen3.8-27b"}]}).encode() if self.path.startswith("/v1/models") else b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        tokens = 1 if "LRUCache" in body["messages"][0]["content"] else 6
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for i in range(tokens):
            self.wfile.write(b"data: " + json.dumps({"choices": [{"delta": {"content": f"t{i} "}}]}).encode() + b"\n\n")
            self.wfile.flush()
            time.sleep(0.02)
        self.wfile.write(b"data: " + json.dumps({"choices": [], "usage": {"completion_tokens": tokens}}).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class TheMedian(unittest.TestCase):
    def test_a_run_that_could_not_be_timed_is_left_out(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        home = tempfile.mkdtemp(prefix="bench-median-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        os.makedirs(os.path.join(home, ".config", "qwen38"))
        with open(os.path.join(home, ".config", "qwen38", "api-key"), "w") as f:
            f.write("test-key\n")
        try:
            r = subprocess.run(["bash", str(BENCH)], capture_output=True, text=True, timeout=120,
                               env={**os.environ, "PORT": str(srv.server_port), "HOME": home})
        finally:
            srv.shutdown()
            srv.server_close()
        out = r.stdout + r.stderr
        reasoning = re.search(r"reasoning \(greedy, deterministic\): ([\d.]+) / ([\d.]+) tok/s", out)
        median = re.search(r"greedy median: (-?[\d.]+) tok/s", out)
        self.assertTrue(reasoning and median, out)
        want = statistics.median([float(reasoning.group(1)), float(reasoning.group(2))])
        self.assertAlmostEqual(float(median.group(1)), want, delta=0.11, msg=out)


if __name__ == "__main__":
    unittest.main()
