"""The Agent relay the cockpit builds opens opencode to a cockpit session, and to nothing else.

The relay's own tests hand it a fake is_authed, and no test built the relay the way the
cockpit does: an agent_config() whose is_authed let every cookie through (every tailnet
peer on opencode, which runs its tools without asking in auto mode) left all the tests
green (found in review, 2026-09-24). This builds the relay with the cockpit's own
agent_config(), in front of a fake opencode that records what reaches it, with
COCKPIT_DRY_RUN=1 and a throwaway config dir."""
import base64
import http.client
import http.server
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
REPO = HERE.parents[2]


class FakeOpencode(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        FakeOpencode.seen.append((self.path, self.headers.get("Authorization")))
        out = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class TheCockpitsRelay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env, path = dict(os.environ), list(sys.path)
        cls.addClassCleanup(lambda: (os.environ.clear(), os.environ.update(env), sys.path.__setitem__(slice(None), path)))
        cls.tmp = Path(tempfile.mkdtemp(prefix="relay-session-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        (cls.tmp / "api-key").write_text("test-key-not-a-real-one\n")
        (cls.tmp / "opencode-web.env").write_text("OPENCODE_SERVER_USERNAME=opencode\nOPENCODE_SERVER_PASSWORD=pw-test\n")
        cls.upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOpencode)
        cls.upstream.daemon_threads = True
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.upstream.server_close)
        cls.addClassCleanup(cls.upstream.shutdown)
        os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.tmp), COCKPIT_REPO_DIR=str(REPO),
                          COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0",
                          COCKPIT_AGENT_UPSTREAM=f"http://127.0.0.1:{cls.upstream.server_address[1]}")
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_relay_session_under_test", DASH / "cockpit.py")
        cls.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cp)
        cls.relay = cls.cp.ar.serve(cls.cp.agent_config(), "127.0.0.1", 0)
        threading.Thread(target=cls.relay.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.relay.server_close)
        cls.addClassCleanup(cls.relay.shutdown)

    def setUp(self):
        FakeOpencode.seen.clear()

    def get(self, cookie=None, path="/session"):
        c = http.client.HTTPConnection("127.0.0.1", self.relay.server_address[1], timeout=10)
        try:
            c.request("GET", path, headers={"Cookie": cookie} if cookie else {})
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def test_no_session_reaches_nothing(self):
        st, body = self.get()
        self.assertEqual(st, 401, body[:200])
        self.assertEqual(FakeOpencode.seen, [])

    def test_a_cookie_that_is_not_a_session_reaches_nothing(self):
        for cookie in ("cockpit=sess:1:" + "0" * 32, "cockpit=anything", "other=" + self.cp.make_token("sess"),
                       "cockpit=" + self.cp.make_token("csrf")):
            st, body = self.get(cookie)
            self.assertEqual(st, 401, f"{cookie}: {body[:200]!r}")
        self.assertEqual(FakeOpencode.seen, [])

    def test_a_session_is_relayed_with_the_boxs_credentials(self):
        st, body = self.get("cockpit=" + self.cp.make_token("sess"))
        self.assertEqual(st, 200, body[:200])
        self.assertEqual(json.loads(body), {"ok": True})
        want = "Basic " + base64.b64encode(b"opencode:pw-test").decode()
        self.assertEqual(FakeOpencode.seen, [("/session", want)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
