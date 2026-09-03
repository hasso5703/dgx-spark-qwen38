"""Offline tests for agent_relay.py: a fake opencode server (Basic auth, plain,
chunked, server-sent events, WebSocket echo) behind the relay, and the pure
helpers. No browser, no network beyond loopback."""
import base64
import hashlib
import http.client
import http.server
import json
import os
import socket
import socketserver
import struct
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_relay as ar  # noqa: E402

GOOD_AUTH = "Basic " + base64.b64encode(b"cockpit:secret").decode()
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
RELEASE = threading.Event()          # lets the SSE handler send its second event
SEEN: list = []                       # requests as the upstream saw them


class FakeOpencode(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200, extra=()):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _auth(self) -> bool:
        if self.headers.get("Authorization") == GOOD_AUTH:
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Secure Area"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        SEEN.append({"method": "GET", "path": self.path, "headers": dict(self.headers.items())})
        if not self._auth():
            return
        if self.headers.get("Upgrade", "").lower() == "websocket":
            return self.websocket()
        path = self.path.split("?", 1)[0]
        if path == "/echo":
            return self._json({"method": "GET", "path": self.path, "headers": dict(self.headers.items())})
        if path == "/len":
            body = bytes(range(256)) * 800          # 204,800 bytes with a Content-Length
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", '"abc"')
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/chunked":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for piece in (b"alpha-", b"beta-", b"gamma"):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
            self.wfile.write(b"0\r\n\r\n")
            return
        if path == "/sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            first = b"data: one\n\n"
            self.wfile.write(b"%x\r\n%s\r\n" % (len(first), first))
            self.wfile.flush()
            RELEASE.wait(timeout=10)
            second = b"data: two\n\n"
            self.wfile.write(b"%x\r\n%s\r\n" % (len(second), second))
            self.wfile.write(b"0\r\n\r\n")
            return
        if path == "/nobody":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/cookie":
            return self._json({"ok": True}, extra=(("Set-Cookie", "cockpit=evil; Path=/"),
                                                    ("Set-Cookie", "theme=dark; Path=/")))
        if path == "/global/health":
            return self._json({"healthy": True, "version": "9.9.9"})
        self._json({"error": "not found"}, 404)

    def do_HEAD(self):
        SEEN.append({"method": "HEAD", "path": self.path, "headers": dict(self.headers.items())})
        if not self._auth():
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", "204800")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        SEEN.append({"method": "POST", "path": self.path, "headers": dict(self.headers.items()), "len": length})
        if not self._auth():
            return
        self._json({"len": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                    "ctype": self.headers.get("Content-Type")})

    def websocket(self):
        key = self.headers["Sec-WebSocket-Key"].encode()
        accept = base64.b64encode(hashlib.sha1(key + WS_GUID).digest()).decode()
        self.wfile.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                         b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n")
        self.wfile.flush()
        self.close_connection = True
        # echo text frames until a close frame arrives
        while True:
            opcode, payload = ws_read(self.rfile)
            if opcode == 8:
                self.wfile.write(ws_frame(b"", opcode=8))
                self.wfile.flush()
                return
            self.wfile.write(ws_frame(b"echo:" + payload))
            self.wfile.flush()


def ws_frame(payload: bytes, opcode: int = 1, mask: bool = False) -> bytes:
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([(0x80 if mask else 0) | n])
    else:
        head += bytes([(0x80 if mask else 0) | 126]) + struct.pack("!H", n)
    if mask:
        key = os.urandom(4)
        return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return head + payload


def ws_read(fp) -> tuple[int, bytes]:
    b0, b1 = fp.read(2)
    opcode, masked, n = b0 & 0x0F, b1 & 0x80, b1 & 0x7F
    if n == 126:
        n = struct.unpack("!H", fp.read(2))[0]
    key = fp.read(4) if masked else None
    data = fp.read(n)
    if masked:
        data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
    return opcode, data


class Upstream(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class RelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = Upstream(("127.0.0.1", 0), FakeOpencode)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.creds = ("cockpit", "secret")
        cfg = ar.RelayConfig(upstream=("127.0.0.1", cls.up.server_port),
                             credentials=lambda: cls.creds,
                             is_authed=lambda cookie: "cockpit=good" in (cookie or ""),
                             cockpit_port=30090)
        cls.cfg = cfg
        cls.relay = ar.serve(cfg, "127.0.0.1", 0)
        threading.Thread(target=cls.relay.serve_forever, daemon=True).start()
        cls.port = cls.relay.server_port
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.relay.shutdown(); cls.relay.server_close()
        cls.up.shutdown(); cls.up.server_close()

    def setUp(self):
        SEEN.clear()
        RELEASE.clear()
        type(self).creds = ("cockpit", "secret")

    def get(self, path, headers=None, method="GET", data=None):
        req = urllib.request.Request(self.base + path, method=method, data=data,
                                     headers={"Cookie": "cockpit=good", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers.items()), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers.items()), e.read()

    # ---- gate ----
    def test_no_session_is_401_json(self):
        code, hdr, body = self.get("/echo", headers={"Cookie": "cockpit=forged"})
        self.assertEqual(code, 401)
        self.assertEqual(json.loads(body)["error"], "cockpit session required")
        self.assertEqual(SEEN, [], "nothing reaches opencode without a session")

    def test_no_session_navigation_gets_the_sign_in_page(self):
        code, hdr, body = self.get("/", headers={"Cookie": "", "Accept": "text/html,*/*"})
        self.assertEqual(code, 401)
        self.assertIn("text/html", hdr["Content-Type"])
        self.assertIn(b"http://127.0.0.1:30090/", body)
        self.assertIn("frame-ancestors", hdr["Content-Security-Policy"])

    def test_foreign_origin_is_403_even_with_a_session(self):
        code, _, _ = self.get("/echo", headers={"Origin": "http://evil.example"})
        self.assertEqual(code, 403)
        code, _, _ = self.get("/echo", headers={"Origin": "null"})
        self.assertEqual(code, 403)
        code, _, _ = self.get("/echo", headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(code, 200)

    def test_manifest_passes_without_a_session_but_not_other_paths(self):
        code, _, _ = self.get("/site.webmanifest", headers={"Cookie": ""})
        self.assertEqual(code, 404, "reached the fake upstream (which has no manifest) with the credentials")
        self.assertEqual({k.lower(): v for k, v in SEEN[-1]["headers"].items()}["authorization"], GOOD_AUTH)
        code, _, _ = self.get("/site.webmanifest?x=1", headers={"Cookie": ""})
        self.assertEqual(code, 404)
        code, _, _ = self.get("/site.webmanifest", headers={"Cookie": ""}, method="POST", data=b"{}")
        self.assertEqual(code, 401, "only GET is public")
        code, _, _ = self.get("/site.webmanifest/../session", headers={"Cookie": ""})
        self.assertEqual(code, 401)

    def test_missing_credentials_is_503(self):
        type(self).creds = None
        code, _, body = self.get("/echo")
        self.assertEqual(code, 503)
        self.assertIn("credentials", json.loads(body)["error"])

    def test_upstream_down_is_502(self):
        with socket.socket() as dead:
            dead.bind(("127.0.0.1", 0))
            port = dead.getsockname()[1]
        cfg = ar.RelayConfig(upstream=("127.0.0.1", port), credentials=lambda: ("a", "b"),
                             is_authed=lambda c: True, cockpit_port=30090)
        srv = ar.serve(cfg, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/x")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(cm.exception.code, 502)
            self.assertIn("opencode-web.service", json.loads(cm.exception.read())["error"])
        finally:
            srv.shutdown(); srv.server_close()

    # ---- header hygiene ----
    def test_request_headers_are_rewritten(self):
        code, _, body = self.get("/echo?a=1&b=%2Fx", headers={
            "Authorization": "Bearer stolen", "Referer": "http://127.0.0.1:30090/#agent",
            "X-Custom": "kept", "Accept-Encoding": "gzip"})
        self.assertEqual(code, 200)
        seen = json.loads(body)
        self.assertEqual(seen["path"], "/echo?a=1&b=%2Fx", "path and query pass verbatim")
        h = {k.lower(): v for k, v in seen["headers"].items()}
        self.assertEqual(h["authorization"], GOOD_AUTH, "the relay's credentials, not the client's")
        self.assertNotIn("cookie", h, "the cockpit session never reaches opencode")
        self.assertNotIn("referer", h)
        self.assertEqual(h["host"], f"127.0.0.1:{self.up.server_port}")
        self.assertEqual(h["x-custom"], "kept")
        self.assertEqual(h["accept-encoding"], "gzip")
        self.assertEqual(h["connection"], "close")

    def test_response_headers_and_cookie_filter(self):
        code, hdr, _ = self.get("/cookie")
        self.assertEqual(code, 200)
        cookies = [v for k, v in hdr.items() if k.lower() == "set-cookie"]
        self.assertEqual(len(cookies), 1, "a cookie named like the session is dropped, others pass")
        self.assertTrue(cookies[0].startswith("theme=dark"))
        self.assertEqual(hdr["Content-Security-Policy"], "frame-ancestors 'self' http://127.0.0.1:30090")
        self.assertEqual(hdr["X-Content-Type-Options"], "nosniff")
        self.assertEqual(hdr["Referrer-Policy"], "no-referrer")
        self.assertTrue(hdr["Server"].startswith("SparkCockpitRelay/"))

    # ---- bodies and framing ----
    def test_post_body_is_forwarded_byte_exact(self):
        blob = os.urandom(1024 * 1024 + 17)
        code, _, body = self.get("/session", method="POST", data=blob,
                                 headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(code, 200)
        out = json.loads(body)
        self.assertEqual(out["len"], len(blob))
        self.assertEqual(out["sha256"], hashlib.sha256(blob).hexdigest())
        self.assertEqual(out["ctype"], "application/octet-stream")

    def test_oversize_body_is_413_before_reading_it(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/session")
        conn.putheader("Cookie", "cockpit=good")
        conn.putheader("Content-Length", str(ar.MAX_BODY + 1))
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 413)
        self.assertEqual(resp.getheader("Connection"), "close")
        conn.close()
        self.assertEqual(SEEN, [])

    def test_content_length_body_passes_whole(self):
        code, hdr, body = self.get("/len")
        self.assertEqual(code, 200)
        self.assertEqual(hdr["Content-Length"], "204800")
        self.assertEqual(body, bytes(range(256)) * 800)
        self.assertEqual(hdr["ETag"], '"abc"')

    def test_chunked_body_is_rechunked_whole(self):
        code, hdr, body = self.get("/chunked")
        self.assertEqual(code, 200)
        self.assertEqual(body, b"alpha-beta-gamma")
        self.assertEqual(hdr.get("Transfer-Encoding"), "chunked")
        self.assertNotIn("Content-Length", hdr)

    def test_204_and_head_carry_no_body(self):
        code, hdr, body = self.get("/nobody")
        self.assertEqual(code, 204)
        self.assertEqual(body, b"")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("HEAD", "/len", headers={"Cookie": "cockpit=good"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Length"), "204800")
        self.assertEqual(resp.read(), b"")
        # the connection is still usable: framing was exact
        conn.request("GET", "/nobody", headers={"Cookie": "cockpit=good"})
        self.assertEqual(conn.getresponse().status, 204)
        conn.close()

    def test_keep_alive_serves_several_requests_on_one_connection(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        for path, expect in (("/len", bytes(range(256)) * 800), ("/chunked", b"alpha-beta-gamma"),
                             ("/len", bytes(range(256)) * 800)):
            conn.request("GET", path, headers={"Cookie": "cockpit=good"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), expect)
        conn.close()

    def test_http10_client_gets_close_delimited_body(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(b"GET /chunked HTTP/1.0\r\nHost: x\r\nCookie: cockpit=good\r\n\r\n")
        raw = b""
        while True:
            piece = s.recv(65536)
            if not piece:
                break
            raw += piece
        s.close()
        head, body = raw.split(b"\r\n\r\n", 1)
        # the status line names the server's protocol (1.1, as any server does); what a
        # 1.0 client needs is no chunked framing and a body ended by the close
        self.assertTrue(head.startswith(b"HTTP/1.1 200") or head.startswith(b"HTTP/1.0 200"), head[:40])
        self.assertNotIn(b"Transfer-Encoding", head)
        self.assertEqual(body, b"alpha-beta-gamma")

    def test_server_sent_events_stream_as_they_are_emitted(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/sse", headers={"Cookie": "cockpit=good", "Accept": "text/event-stream"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        t0 = time.time()
        first = resp.read1(1024)
        self.assertEqual(first, b"data: one\n\n", "the first event arrives before the second exists")
        self.assertLess(time.time() - t0, 5.0)
        RELEASE.set()
        rest = b""
        while True:
            piece = resp.read1(1024)
            if not piece:
                break
            rest += piece
        self.assertEqual(rest, b"data: two\n\n")
        conn.close()

    # ---- WebSocket ----
    def test_websocket_is_tunneled_with_the_credentials(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET /pty/1/connect?ticket=t HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                   f"Origin: http://127.0.0.1:{self.port}\r\nCookie: cockpit=good\r\n"
                   f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += s.recv(4096)
        self.assertTrue(head.startswith(b"HTTP/1.1 101"), head)
        accept = base64.b64encode(hashlib.sha1(key.encode() + WS_GUID).digest()).decode()
        self.assertIn(b"Sec-WebSocket-Accept: " + accept.encode(), head)
        s.sendall(ws_frame(b"ping", mask=True))
        fp = s.makefile("rb")
        opcode, data = ws_read(fp)
        self.assertEqual((opcode, data), (1, b"echo:ping"))
        s.sendall(ws_frame(b"", opcode=8, mask=True))
        self.assertEqual(ws_read(fp)[0], 8, "the close frame comes back through the tunnel")
        fp.close()
        s.close()
        h = {k.lower(): v for k, v in SEEN[-1]["headers"].items()}
        self.assertEqual(h["authorization"], GOOD_AUTH)
        self.assertNotIn("cookie", h)
        self.assertEqual(h["upgrade"], "websocket")

    def test_websocket_without_session_is_refused_before_upstream(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall((f"GET /pty/1/connect HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: x\r\n\r\n").encode())
        head = s.recv(4096)
        s.close()
        self.assertTrue(head.startswith(b"HTTP/1.1 401"), head[:40])
        self.assertEqual(SEEN, [])

    # ---- health probe ----
    def test_health_uses_the_credentials(self):
        self.assertEqual(ar.health(self.cfg), {"healthy": True, "version": "9.9.9"})
        type(self).creds = ("cockpit", "wrong")
        out = ar.health(self.cfg)
        self.assertFalse(out["healthy"])
        self.assertIn("refused", out["error"])


class PureHelpers(unittest.TestCase):
    def test_host_name(self):
        self.assertEqual(ar.host_name("100.64.0.1:30090"), "100.64.0.1")
        self.assertEqual(ar.host_name("spark.tail.ts.net"), "spark.tail.ts.net")
        self.assertEqual(ar.host_name("[::1]:30090"), "[::1]")
        self.assertEqual(ar.host_name(None), "")

    def test_origin_allowed(self):
        self.assertTrue(ar.origin_allowed(None, "h:1"))
        self.assertTrue(ar.origin_allowed("http://H:1", "h:1"))
        self.assertTrue(ar.origin_allowed("https://h:1", "h:1"))
        self.assertFalse(ar.origin_allowed("http://h:2", "h:1"))
        self.assertFalse(ar.origin_allowed("null", "h:1"))
        self.assertFalse(ar.origin_allowed("http://h:1", None))

    def test_read_credentials(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "env"
            f.write_text('# comment\nOPENCODE_SERVER_USERNAME="cockpit"\nOPENCODE_SERVER_PASSWORD=\'p w\'\n')
            self.assertEqual(ar.read_credentials(f), ("cockpit", "p w"))
            f.write_text("OPENCODE_SERVER_PASSWORD=only\n")
            self.assertEqual(ar.read_credentials(f), ("opencode", "only"))
            f.write_text("OPENCODE_SERVER_USERNAME=u\n")
            self.assertIsNone(ar.read_credentials(f), "no password means no credentials")
            self.assertIsNone(ar.read_credentials(Path(d) / "missing"))

    def test_forward_request_headers(self):
        out = dict(ar.forward_request_headers(
            [("Host", "a"), ("Cookie", "c"), ("Authorization", "z"), ("Transfer-Encoding", "chunked"),
             ("Content-Length", "3"), ("Accept", "*/*"), ("Sec-WebSocket-Key", "k")], "127.0.0.1:4096", "Basic x"))
        self.assertEqual(out["Host"], "127.0.0.1:4096")
        self.assertEqual(out["Authorization"], "Basic x")
        self.assertEqual(out["Connection"], "close")
        self.assertEqual(out["Accept"], "*/*")
        self.assertEqual(out["Sec-WebSocket-Key"], "k")
        for gone in ("Cookie", "Transfer-Encoding", "Content-Length"):
            self.assertNotIn(gone, out)
        ws = dict(ar.forward_request_headers([], "u:1", "Basic x", websocket=True))
        self.assertEqual((ws["Connection"], ws["Upgrade"]), ("Upgrade", "websocket"))

    def test_frame_policy_and_upstream_split(self):
        self.assertEqual(ar.frame_policy("100.64.0.1:30091", 30090), "frame-ancestors 'self' http://100.64.0.1:30090")
        self.assertEqual(ar.frame_policy("[::1]:30091", 1), "frame-ancestors 'self' http://[::1]:1")
        self.assertEqual(ar.split_upstream("http://127.0.0.1:4096"), ("127.0.0.1", 4096))
        self.assertEqual(ar.split_upstream("127.0.0.1:5000"), ("127.0.0.1", 5000))

    def test_tailscale_ipv4_and_resolve_bind(self):
        class R:
            def __init__(self, out):
                self.stdout = out
        iface = "8: tailscale0    inet 100.114.54.60/32 scope global tailscale0\\       valid_lft forever\n"
        self.assertEqual(ar.tailscale_ipv4(lambda *a, **k: R(iface)), "100.114.54.60")
        calls = []
        def run(argv, **k):
            calls.append(argv[0])
            return R("" if argv[0] == "ip" else "100.99.1.2\nfd7a:115c::1\n")
        self.assertEqual(ar.tailscale_ipv4(run), "100.99.1.2")
        self.assertEqual(calls, ["ip", "tailscale"], "interface first, CLI as fallback")
        self.assertIsNone(ar.tailscale_ipv4(lambda *a, **k: R("")))
        def boom(*a, **k):
            raise OSError("no such binary")
        self.assertIsNone(ar.tailscale_ipv4(boom))
        self.assertIsNone(ar.tailscale_ipv4(lambda *a, **k: R("inet 192.168.1.5/24")), "a LAN address is not a tailnet one")
        self.assertEqual(ar.resolve_bind("127.0.0.1"), "127.0.0.1")
        self.assertEqual(ar.resolve_bind(" tailscale ", lambda *a, **k: R("100.64.0.9")), "100.64.0.9")
        self.assertIsNone(ar.resolve_bind(""))


if __name__ == "__main__":
    unittest.main(verbosity=1)
