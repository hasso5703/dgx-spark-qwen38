#!/usr/bin/env python3
"""A refusal the proxy sends before reading a body reaches the client.

The 413 for a body past MAX_BODY_BYTES went out without the body being read, and the
socket closed at once: the kernel answered the rest of the upload with a reset, and the
reset destroyed the 413 on its way, so a client sending 20 MB past a 1 MB cap saw a broken
pipe five times in five (found in review, 2026-09-24). And a client that asks first
(Expect: 100-continue) was invited to send it all before being refused."""
import http.client
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
CAP = 1024 * 1024


class TheRefusalArrives(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = pathlib.Path(tempfile.mkdtemp(prefix="kp-413-"))
        (cls.home / ".config" / "qwen38").mkdir(parents=True)
        (cls.home / ".config" / "qwen38" / "api-key").write_text("k-test\n")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        env = {"PATH": os.environ["PATH"], "HOME": str(cls.home), "PROXY_BIND": "127.0.0.1",
               "UPSTREAM": "http://127.0.0.1:9", "MAX_BODY_BYTES": str(CAP)}
        cls.proxy = subprocess.Popen([sys.executable, str(REPO / "keepalive-proxy.py"), str(cls.port)], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.time() + 20
        while time.time() < end:
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proxy.terminate()
        cls.proxy.wait(10)
        shutil.rmtree(cls.home, ignore_errors=True)

    def test_a_client_that_just_sends_reads_the_413(self):
        body = b"x" * (20 * CAP)
        for _ in range(5):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
            try:
                c.request("POST", "/v1/chat/completions", body, {"Content-Type": "application/json"})
                r = c.getresponse()
                self.assertEqual(r.status, 413)
                self.assertIn(b"body_too_large", r.read())
            finally:
                c.close()

    def test_a_client_that_asks_first_is_refused_before_sending(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                      b"Expect: 100-continue\r\nContent-Length: %d\r\n\r\n" % (20 * CAP))
            head = b""
            while b"\r\n\r\n" not in head:
                piece = s.recv(4096)
                if not piece:
                    break
                head += piece
        finally:
            s.close()
        self.assertTrue(head.startswith(b"HTTP/1.1 413"), head[:80])


if __name__ == "__main__":
    unittest.main(verbosity=2)
