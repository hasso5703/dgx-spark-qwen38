"""The two image routes' HTTP guards, driven over HTTP instead of read in the source.

Their guards were checked by a grep of cockpit.py: a CSRF check that skipped the image
routes, or a 40 MB cap raised on every route, left the tests green (found in review,
2026-09-24; that the session comes before the body is driven in
test_cockpit_connections.py). This runs the real Handler with COCKPIT_DRY_RUN=1 and a
throwaway config dir, and image_call replaced by a recorder, so nothing here can reach
an image lane."""
import http.client
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
REPO = HERE.parents[2]
API_KEY = "test-key-not-a-real-one"
ROUTES = ("/api/image/generate", "/api/image/edit")


class TheImageRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env, path = dict(os.environ), list(sys.path)
        cls.addClassCleanup(lambda: (os.environ.clear(), os.environ.update(env), sys.path.__setitem__(slice(None), path)))
        cls.tmp = Path(tempfile.mkdtemp(prefix="image-http-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        (cls.tmp / "api-key").write_text(API_KEY + "\n")
        os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.tmp), COCKPIT_REPO_DIR=str(REPO),
                          COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0")
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_image_http_under_test", DASH / "cockpit.py")
        cls.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cp)
        cls.calls = []
        cls.cp.image_call = lambda payload, editing=False: (cls.calls.append((editing, payload)) or
                                                           (200, {"ok": True, "editing": editing}))
        cls.srv = cls.cp.Server(("127.0.0.1", 0), cls.cp.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.srv.server_close)
        cls.addClassCleanup(cls.srv.shutdown)

    def setUp(self):
        self.cp.LOGIN_FAILS.clear()
        self.calls.clear()

    def post(self, path, body, cookie=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=30)
        try:
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            c.request("POST", path, raw, {"Content-Type": "application/json", **({"Cookie": cookie} if cookie else {})})
            r = c.getresponse()
            return r.status, r.getheader("Set-Cookie") or "", r.read()
        finally:
            c.close()

    def session(self):
        st, raw, _ = self.post("/api/login", {"key": API_KEY})
        self.assertEqual(st, 200)
        cookie = raw.split(";")[0]
        st, _, data = self.post("/api/csrf", {}, cookie)
        self.assertEqual(st, 200)
        return cookie, json.loads(data)["token"]

    def declared_only(self, path, length):
        """The status a POST gets for its declared length alone: its body never comes."""
        s = socket.create_connection(("127.0.0.1", self.srv.server_address[1]), timeout=5)
        try:
            s.sendall(f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {length}\r\n\r\n".encode())
            head = s.recv(4096)
        except socket.timeout:
            head = b""
        finally:
            s.close()
        return int(head.split()[1]) if head.startswith(b"HTTP/1.1 ") else None

    def test_they_need_the_csrf_token_this_server_issued(self):
        cookie, token = self.session()
        for path in ROUTES:
            for csrf in (None, "", "csrf:1:" + "0" * 32, cookie.split("=", 1)[1]):
                body = {"prompt": "a cat"} if csrf is None else {"prompt": "a cat", "csrf": csrf}
                st, _, data = self.post(path, body, cookie)
                self.assertEqual((st, json.loads(data)), (403, {"error": "csrf"}), f"{path} csrf={csrf!r}")
        self.assertEqual(self.calls, [], "a request without the token reached the image lane")
        for path in ROUTES:
            st, _, data = self.post(path, {"prompt": "a cat", "csrf": token}, cookie)
            self.assertEqual(st, 200, data[:200])
        self.assertEqual([editing for editing, _ in self.calls], [False, True])

    def test_the_raised_cap_is_theirs_alone(self):
        for path in ("/api/action", "/api/systemone", "/api/csrf", "/nowhere"):
            self.assertEqual(self.declared_only(path, 70_000), 413, path)
        for path in ROUTES:
            self.assertEqual(self.declared_only(path, 70_000), 401, path)
            self.assertEqual(self.declared_only(path, self.cp.IMAGE_MAX_POST + 1), 413, path)

    def test_ten_references_arrive_whole(self):
        cookie, token = self.session()
        refs = ["data:image/png;base64," + "A" * 1_200_000 for _ in range(10)]
        st, _, data = self.post("/api/image/edit", {"prompt": "x", "images": refs, "csrf": token}, cookie)
        self.assertEqual(st, 200, data[:200])
        self.assertEqual(self.calls[-1][1]["images"], refs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
