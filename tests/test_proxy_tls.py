#!/usr/bin/env python3
"""v6.16's TLS door: it opens only when a certificate is named, and when it
does, nothing plain is left beside it.

The dangerous bugs here are the obvious ones (plaintext still answering next
to the TLS socket, starting with a certificate that cannot be loaded,
wrapping too late so early children accepted plain) and one that is not: the
proxy's keepalive and abort logic leans on raw-socket behavior (setsockopt,
close orderings) that a wrapped socket might not offer. So these tests drive
the real production entry point, subprocess and all, against a fake engine.

The certificate is generated with openssl: self-signed, exactly like the
tailnet-CA certificate the feature is meant to use. The client here pins
nothing and trusts nothing (CERT_NONE): under test is the door's hinge, not
the chain of trust an operator brings.
"""
import http.server
import json
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class Engine(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or "0")
        self.rfile.read(n)
        body = json.dumps({"id": "chatcmpl-test",
                           "choices": [{"message": {"role": "assistant", "content": "ok"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")


def start_engine():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def base_env(upstream_port, extra=None):
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": tempfile.mkdtemp(),
           "UPSTREAM": "http://127.0.0.1:%d" % upstream_port}
    if extra:
        env.update(extra)
    return env


def run_proxy(port, env, capture_stderr=False):
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "keepalive-proxy.py"), str(port)], env=env,
        stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL)
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.5):
                return proc
        except OSError:
            if proc.poll() is not None:
                tail = proc.stderr.read().decode()[-400:] if capture_stderr else ""
                raise AssertionError(f"proxy exited early rc={proc.returncode} {tail}")
    proc.terminate()
    raise AssertionError("proxy never listened")


def https_post(port, path, obj):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request("https://127.0.0.1:%d%s" % (port, path),
                                 data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        return r.status, r.read()


def plain_post(port, path, obj):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, r.read()


BODY = {"model": "x", "messages": []}


class TlsDoor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine, cls.engine_port = start_engine()
        tmp = tempfile.mkdtemp()
        cert, key = tmp + "/cert.pem", tmp + "/key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                        "-nodes", "-days", "2", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                        "-keyout", key, "-out", cert],
                       check=True, capture_output=True)
        cls.cert, cls.key = cert, key

    @classmethod
    def tearDownClass(cls):
        cls.engine.shutdown()
        cls.engine.server_close()

    def test_cert_named_speaks_tls_and_nothing_plain_answers_beside_it(self):
        port = free_port()
        proc = run_proxy(port, base_env(self.engine_port, {"QWEN38_TLS_CERT": self.cert,
                                                           "QWEN38_TLS_KEY": self.key}))
        try:
            status, body = https_post(port, "/v1/chat/completions", BODY)
            self.assertEqual(status, 200)
            self.assertIn("chatcmpl-test", body.decode())
            with self.assertRaises(Exception):
                plain_post(port, "/v1/chat/completions", BODY)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_no_cert_named_keeps_the_plain_socket_exactly_as_before(self):
        port = free_port()
        proc = run_proxy(port, base_env(self.engine_port))
        try:
            status, _ = plain_post(port, "/v1/chat/completions", BODY)
            self.assertEqual(status, 200)
            with self.assertRaises(Exception):
                https_post(port, "/v1/chat/completions", BODY)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_unusable_cert_refuses_to_start(self):
        port = free_port()
        proc = subprocess.Popen(
            [sys.executable, str(REPO / "keepalive-proxy.py"), str(port)],
            env=base_env(self.engine_port, {"QWEN38_TLS_CERT": "/nonexistent/cert.pem"}),
            stderr=subprocess.PIPE)
        rc = proc.wait(timeout=10)
        proc.stderr.close()
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
