"""What a client can hold of the cockpit and the Agent relay before it has a session.

Both servers started a thread per connection with no bound and no timeout, so a request
sent in part held its thread, and a descriptor, for good: 100 of them held 100 threads,
in the process whose 1,024 descriptors the collectors and the memory floor need. Every
POST route but the image ones read its body before looking at the session, and the
relay passed a GET of its one public path to opencode with its body and the Basic
credentials. And the session cookie was read with http.cookies.SimpleCookie, which gives
up at the first pair it has no grammar for: another app's cookie on the same host (a
JSON value, a space) hid the session, and a name like `a/b` raised, which ended the
connection with no answer (found in review, 2026-09-24). Everything here runs against
the real Handler, Relay and servers on ephemeral ports, with COCKPIT_DRY_RUN=1 and a
throwaway config dir, as test_cockpit_http.py does."""
import http.client
import http.server
import importlib.util
import os
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
REPO = HERE.parents[2]
API_KEY = "test-key-not-a-real-one"


def load_cockpit(config_dir: Path):
    os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(config_dir), COCKPIT_REPO_DIR=str(REPO),
                      COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0")
    sys.path.insert(0, str(DASH))
    spec = importlib.util.spec_from_file_location("cockpit_connections_under_test", DASH / "cockpit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def wait_until(cond, limit=5.0):
    end = time.time() + limit
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def half_sent(port, n):
    """n connections that each send part of a request and then nothing."""
    out = []
    for _ in range(n):
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"GET /api/health HTTP/1.1\r\nHost: x\r\n")
        out.append(s)
    return out


def closed_by_server(s, within=1.0):
    s.settimeout(within)
    try:
        return s.recv(1) == b""
    except socket.timeout:
        return False
    except OSError:
        return True


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-conn-"))
        (cls.tmp / "api-key").write_text(API_KEY + "\n")
        cls.cp = load_cockpit(cls.tmp)
        cls.ar = cls.cp.ar

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def serve(self, server_cls, handler, **limits):
        # closing a connection sent in part ends its request at EOF, and the answer then
        # meets a client that is gone: expected here, so not printed
        quiet = type("Quiet" + server_cls.__name__, (server_cls,), {"handle_error": lambda *a: None})
        srv = quiet(("127.0.0.1", 0), handler)
        for k, v in limits.items():
            setattr(srv, k, v)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def request(self, port, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            c.request(method, path, body, headers or {})
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()


class TheCookieHeaderIsReadAsBrowsersSendIt(Base):
    NEIGHBOURS = ('prefs={"theme":"dark"}', "greeting=hello world", "a/b=1", "x=é", '_ga=GA1.1.2',
                  "flag", "=orphan", 'q="a\\"b"')

    def test_the_session_survives_every_neighbour(self):
        tok = self.cp.make_token("sess")
        for n in self.NEIGHBOURS:
            for header in (f"{n}; cockpit={tok}", f"cockpit={tok}; {n}"):
                self.assertTrue(self.cp.cookie_authed(header), header)

    def test_a_stale_cookie_beside_the_fresh_one(self):
        self.assertTrue(self.cp.cookie_authed(f"cockpit=sess:1:{'0' * 32}; cockpit={self.cp.make_token('sess')}"))

    def test_no_value_raises(self):
        for v in ("", ";;;", "cockpit=", "cockpit=é", "cockpit=sess:1:éé", "cockpit=a:b:c:d",
                  "cockpit=csrf:" + str(int(time.time())) + ":" + "0" * 32, "a/b=1"):
            self.assertFalse(self.cp.cookie_authed(v), v)

    def test_over_http_a_neighbour_that_raised_is_answered(self):
        port = self.serve(self.cp.Server, self.cp.Handler)
        tok = self.cp.make_token("sess")
        for n in self.NEIGHBOURS[:3]:
            st, _ = self.request(port, "GET", "/api/actions", {"Cookie": f"{n}; cockpit={tok}"})
            self.assertEqual(st, 200, n)


class AClientThatSaysNothingIsLetGo(Base):
    def test_both_handlers_have_a_client_timeout(self):
        relay = self.ar.make_handler(self.ar.RelayConfig(upstream=("127.0.0.1", 9), credentials=lambda: None,
                                                         is_authed=lambda c: False, cockpit_port=1))
        for handler in (self.cp.Handler, relay):
            self.assertIsNotNone(handler.timeout, handler)
            self.assertLessEqual(handler.timeout, 60, handler)

    def test_half_sent_requests_are_released(self):
        live, peak = set(), [0]     # this server's requests in progress, and the most at once

        class Probe(self.cp.Handler):
            timeout = 1.5

            def setup(self):
                live.add(self)
                peak[0] = max(peak[0], len(live))
                super().setup()

            def finish(self):
                try:
                    super().finish()
                finally:
                    live.discard(self)
        port = self.serve(self.cp.Server, Probe)
        socks = half_sent(port, 8)
        self.addCleanup(lambda: [s.close() for s in socks])
        self.assertTrue(wait_until(lambda: peak[0] == 8, 5), f"{peak[0]} of 8 connections in progress at most")
        self.assertTrue(wait_until(lambda: not live, 10), f"{len(live)} requests still held")
        self.assertTrue(all(closed_by_server(s) for s in socks))

    def test_the_event_stream_outlives_the_timeout(self):
        handler = type("QuickHandler", (self.cp.Handler,), {"timeout": 0.5})
        port = self.serve(self.cp.Server, handler)
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(s.close)
        s.sendall(b"GET /api/stream HTTP/1.1\r\nHost: x\r\nCookie: cockpit=" + self.cp.make_token("sess").encode()
                  + b"\r\n\r\n")
        got, t0 = b"", time.time()
        while got.count(b"data: ") < 2 and time.time() - t0 < 8:
            got += s.recv(65536)
        self.assertGreaterEqual(got.count(b"data: "), 2, got[:200])
        self.assertGreater(time.time() - t0, 0.5, "two events came faster than the timeout: nothing proven")

    def test_the_relay_releases_them_too(self):
        cfg = self.ar.RelayConfig(upstream=("127.0.0.1", 9), credentials=lambda: None,
                                  is_authed=lambda c: False, cockpit_port=1)
        handler = type("QuickRelay", (self.ar.make_handler(cfg),), {"timeout": 0.5})
        port = self.serve(self.ar.RelayServer, handler)
        socks = half_sent(port, 4)
        self.addCleanup(lambda: [s.close() for s in socks])
        time.sleep(1.5)
        self.assertTrue(all(closed_by_server(s) for s in socks))


class ConnectionsAreBounded(Base):
    def test_both_servers_are_bounded(self):
        for srv in (self.cp.Server, self.ar.RelayServer):
            self.assertTrue(issubclass(srv, self.ar.BoundedThreadingMixIn), srv)
            self.assertLessEqual(srv.max_connections, 256)
            self.assertLessEqual(srv.max_per_address, srv.max_connections)

    def check_cap(self, server_cls, handler, per_address):
        port = self.serve(server_cls, handler, max_per_address=per_address, max_connections=100)
        socks = half_sent(port, per_address + 3)
        self.addCleanup(lambda: [s.close() for s in socks])
        # past the cap, closed at once; within it, held (the timeout is left long here)
        self.assertEqual([closed_by_server(s, 1.0) for s in socks[per_address:]], [True] * 3)
        self.assertEqual([closed_by_server(s, 0.2) for s in socks[:per_address]], [False] * per_address)
        # a slot comes back when its connection ends
        socks[0].close()
        time.sleep(0.3)
        late = half_sent(port, 1)[0]
        self.addCleanup(late.close)
        self.assertFalse(closed_by_server(late, 0.3), "a freed slot was not given back")

    def test_the_cockpit(self):
        self.check_cap(self.cp.Server, type("LongHandler", (self.cp.Handler,), {"timeout": 30}), 4)

    def test_the_relay(self):
        cfg = self.ar.RelayConfig(upstream=("127.0.0.1", 9), credentials=lambda: None,
                                  is_authed=lambda c: False, cockpit_port=1)
        self.check_cap(self.ar.RelayServer, self.ar.make_handler(cfg), 4)


class NoBodyIsReadBeforeTheSession(Base):
    def test_a_post_without_a_session_is_refused_before_its_body(self):
        """Its declared body never comes: an answer means the server did not wait for it."""
        port = self.serve(self.cp.Server, type("LongHandler", (self.cp.Handler,), {"timeout": 30}))
        for path, length in (("/api/action", 60000), ("/api/csrf", 60000), ("/api/systemone", 60000),
                             ("/nowhere", 60000), ("/api/image/generate", 30_000_000),
                             ("/api/image/edit", 30_000_000)):
            s = socket.create_connection(("127.0.0.1", port), timeout=3)
            self.addCleanup(s.close)
            s.sendall(f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {length}\r\n\r\n".encode())
            try:
                head = s.recv(4096)
            except socket.timeout:
                head = b""
            self.assertTrue(head.startswith(b"HTTP/1.1 401"), f"{path}: {head[:60]!r}")

    def test_a_login_is_a_small_body(self):
        port = self.serve(self.cp.Server, self.cp.Handler)
        st, _ = self.request(port, "POST", "/api/login", {"Content-Type": "application/json"},
                             b'{"key": "' + b"x" * 5000 + b'"}')
        self.assertEqual(st, 413)
        st, _ = self.request(port, "POST", "/api/login", {"Content-Type": "application/json"},
                             ('{"key": "%s"}' % API_KEY).encode())
        self.assertEqual(st, 200)


class InputsThatEndedTheHandler(Base):
    """Two inputs from before the login ended the handler thread with no answer: a key
    with non-ASCII in it (compare_digest raises on such a str), counted and audited by
    nobody, and a NUL in a static path (resolve() raises)."""

    def raw(self, port, data):
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            s.sendall(data)
            return s.recv(4096)
        finally:
            s.close()

    def test_a_non_ascii_key_is_a_counted_refusal(self):
        port = self.serve(self.cp.Server, self.cp.Handler)
        self.cp.LOGIN_FAILS.clear()
        body = '{"key": "\u00e9t\u00e9"}'.encode()
        head = self.raw(port, b"POST /api/login HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                        b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.assertTrue(head.startswith(b"HTTP/1.1 403"), head[:60])
        self.assertEqual(sum(len(v) for v in self.cp.LOGIN_FAILS.values()), 1, "the attempt was not counted")

    def test_a_nul_in_a_static_path_is_a_404(self):
        port = self.serve(self.cp.Server, self.cp.Handler)
        head = self.raw(port, b"GET /static/app\x00.js HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 404"), head[:60])


class TheLoginLimitHoldsUnderConcurrency(Base):
    """Counted after its reply, a burst of simultaneous attempts was judged far past the
    limit of five: 107 of 200 in the review's run (found in review, 2026-09-24)."""

    def test_a_burst_is_judged_five_times_at_most(self):
        port = self.serve(type("Wide", (self.cp.Server,), {"max_per_address": 96}), self.cp.Handler)
        self.cp.LOGIN_FAILS.clear()
        go, codes, lock = threading.Barrier(40), [], threading.Lock()

        def attempt():
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                c.connect()              # connected first: the listen backlog is not the subject
                go.wait()
                c.request("POST", "/api/login", b'{"key": "wrong"}', {"Content-Type": "application/json"})
                with lock:
                    codes.append(c.getresponse().status)
            finally:
                c.close()
        threads = [threading.Thread(target=attempt) for _ in range(40)]
        for t in threads:
            t.start()
            time.sleep(0.01)
        for t in threads:
            t.join(20)
        self.assertEqual(len(codes), 40)
        self.assertLessEqual(codes.count(403), 5, sorted(codes))
        self.assertEqual(codes.count(403) + codes.count(429), 40)

    def test_a_good_key_gives_its_slot_back(self):
        port = self.serve(self.cp.Server, self.cp.Handler)
        self.cp.LOGIN_FAILS.clear()
        for _ in range(4):
            self.request(port, "POST", "/api/login", {"Content-Type": "application/json"}, b'{"key": "wrong"}')
        st, _ = self.request(port, "POST", "/api/login", {"Content-Type": "application/json"},
                             ('{"key": "%s"}' % API_KEY).encode())
        self.assertEqual(st, 200)
        self.assertEqual(sum(len(v) for v in self.cp.LOGIN_FAILS.values()), 4, "the success was counted as a failure")


class Recorder(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    SEEN: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        n = int(self.headers.get("Content-Length") or 0)
        Recorder.SEEN.append((self.command, self.path, self.rfile.read(n) if n else b""))
        body = b'{"name": "opencode"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/manifest+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TheRelaysPublicPathCarriesNoBody(Base):
    def setUp(self):
        Recorder.SEEN.clear()
        up = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Recorder)
        up.daemon_threads = True
        threading.Thread(target=up.serve_forever, daemon=True).start()
        self.addCleanup(up.server_close)
        self.addCleanup(up.shutdown)
        cfg = self.ar.RelayConfig(upstream=("127.0.0.1", up.server_address[1]), credentials=lambda: ("u", "p"),
                                  is_authed=lambda c: False, cockpit_port=1)
        self.port = self.serve(self.ar.RelayServer, self.ar.make_handler(cfg))

    def test_the_manifest_passes_without_a_session(self):
        st, body = self.request(self.port, "GET", "/site.webmanifest")
        self.assertEqual((st, body), (200, b'{"name": "opencode"}'))

    def test_a_body_on_it_needs_the_session(self):
        st, _ = self.request(self.port, "GET", "/site.webmanifest", {"Content-Length": "5"}, b"12345")
        self.assertEqual(st, 401)
        self.assertEqual(Recorder.SEEN, [], "the body reached opencode, with the Basic credentials")


if __name__ == "__main__":
    unittest.main(verbosity=2)
