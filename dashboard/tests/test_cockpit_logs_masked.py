"""The Logs tab never shows the serving key.

SGLang prints its ServerArgs at boot, 'api_key' included, and the Logs tab served the last
120 lines of the container and the unit's journal as they were: for the first minutes of a
boot the key was on screen, while the diagnostics bundle masked it (found in review,
2026-09-24). The same value is masked here."""
import http.client
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
KEY = "k-" + "Z" * 40


class TheLogs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-logs-"))
        (cls.tmp / "api-key").write_text(KEY + "\n")
        os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.tmp), COCKPIT_REPO_DIR=str(DASH.parent),
                          COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0")
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_logs_masked", DASH / "cockpit.py")
        cls.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cp)
        banner = f"[2026-09-24] server_args=ServerArgs(model_path='x', 'api_key': '{KEY}', port=30000)\nready\n"
        cls.cp.run = lambda argv, timeout=5.0, merge_err=False: banner
        cls.srv = cls.cp.Server(("127.0.0.1", 0), cls.cp.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        try:
            c.request("GET", path, headers={"Cookie": "cockpit=" + self.cp.make_token("sess")})
            r = c.getresponse()
            return r.status, r.read().decode()
        finally:
            c.close()

    def test_no_source_shows_the_key(self):
        sources = list(self.cp.CONTAINERS) + list(self.cp.JOURNAL_UNITS)
        self.assertTrue(sources)
        for name in sources:
            st, body = self.get(f"/api/logs/{name}")
            self.assertEqual(st, 200, name)
            self.assertNotIn(KEY, body, name)
            self.assertIn("<masked>", json.loads(body)["lines"][0], name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
