#!/usr/bin/env python3
"""bench-agent's "TTFT per 1k added prompt tokens" is in the unit it prints.

It is the line the README calls the one to read: near zero means the prefix cache is
reused, a number that tracks the prompt means it is not. It divided seconds by tokens,
multiplied by 1,000 and printed "ms", so the value was 1,000 times too small: an engine
that re-prefilled the whole prompt (about 1,000 ms per 1k) printed "+1 ms", under a
sentence saying that near zero is a cache that works (found in review, 2026-09-24).

This runs the script as written against a fake engine whose first token arrives after a
known time per prompt token, and reads the slope back.
"""
import http.server
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
MS_PER_TOKEN = 0.5          # 500 ms per 1k prompt tokens: the slope the script must print


class Engine(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "m"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = sum(len(m["content"]) for m in req["messages"]) // 4
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        import time
        time.sleep(prompt * MS_PER_TOKEN / 1000.0)      # a prefill that tracks the prompt
        out = req["max_tokens"]
        for data in ({"choices": [{"delta": {"content": "x" * 8}}]},
                     {"choices": [], "usage": {"prompt_tokens": prompt, "completion_tokens": out}}):
            self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True


class TheSlopeIsInMilliseconds(unittest.TestCase):
    def test_a_prefill_that_tracks_the_prompt_reads_as_one(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            env = dict(os.environ, QWEN38_API_KEY="k", HOME=tempfile.mkdtemp(prefix="ba-slope-"))
            r = subprocess.run([sys.executable, str(REPO / "bench-agent.py"), "--port",
                                str(srv.server_address[1]), "--turns", "4", "--turn-tokens", "8",
                                "--prefix-tokens", "2000"],
                               capture_output=True, text=True, env=env, timeout=120)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        m = re.search(r"TTFT per 1k added prompt tokens\s+([+-]\d+) ms", r.stdout)
        self.assertIsNotNone(m, r.stdout)
        slope = int(m.group(1))
        # the fake engine charges 500 ms per 1k prompt tokens; allow for timing noise
        self.assertGreater(slope, 300, r.stdout)
        self.assertLess(slope, 800, r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
