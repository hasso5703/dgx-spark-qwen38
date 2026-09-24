"""A proxy that refuses the cockpit's key does not log the browser out.

The System One route relayed the proxy's status as it was, and the page reads a 401 as its
own session ending: a signed-in user who asked a question behind an identity wall that
does not list the cockpit's key was sent to the login (found in review, 2026-09-24). The
refusal reaches the page as a gateway error, with the proxy's status and detail."""
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]


def load(config_dir):
    os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(config_dir), COCKPIT_REPO_DIR=str(DASH.parent),
                      COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0")
    sys.path.insert(0, str(DASH))
    spec = importlib.util.spec_from_file_location("cockpit_so_refusal", DASH / "cockpit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TheRefusal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-so-"))
        (cls.tmp / "api-key").write_text("k-test\n")
        cls.cp = load(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call_with(self, status):
        def refuse(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, status, "no", {}, io.BytesIO(b'{"detail": "not a listed key"}'))
        saved = self.cp.urllib.request.urlopen
        self.cp.urllib.request.urlopen = refuse
        try:
            return self.cp.systemone_call({"state": "s", "questions": {"q": {"type": "noul", "instructions": "i"}}})
        finally:
            self.cp.urllib.request.urlopen = saved

    def test_a_refused_key_is_a_gateway_error(self):
        for status in (401, 403):
            code, out = self.call_with(status)
            self.assertEqual(code, 502, status)
            self.assertEqual(out["upstream_status"], status)
            self.assertEqual(out["refused"], {"detail": "not a listed key"})

    def test_other_refusals_keep_their_status(self):
        code, out = self.call_with(400)
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
