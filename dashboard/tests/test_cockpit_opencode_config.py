"""The Settings tab reads opencode's config the way opencode does.

collect_opencode() read it with json.loads: a config with a comment or a trailing comma,
which opencode reads (jsonc-parser), came out unreadable, and an unreadable one showed as
empty ("none", "not declared") on the page (found in review, 2026-09-24). This loads the
real cockpit with COCKPIT_DRY_RUN=1, a throwaway config dir and a throwaway HOME."""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
REPO = HERE.parents[2]

COMMENTED = """{
  // the lane the installer points at
  "model": "qwen38/qwen3.8-27b",
  "provider": {
    "qwen38": {"npm": "@ai-sdk/openai-compatible",
               "options": {"baseURL": "http://127.0.0.1:30001/v1"},
               "models": {"qwen3.8-27b": {"limit": {"context": 559000, "output": 186000},},},},
  },
}
"""

FOLDED = """{
  // both lanes behind one provider of the operator's own naming, plus a peer box
  "model": "GB_10/qwen3.8-flash-next",
  "provider": {
    "GB_10": {"npm": "@ai-sdk/openai-compatible",
              "options": {"baseURL": "http://127.0.0.1:30001/v1"},
              "models": {
                "qwen3.8-flash-next": {"limit": {"context": 205000, "input": 205000, "output": 32000}},
                "qwen3.8-27b": {"limit": {"context": 1010000, "input": 1010000, "output": 151000}}}},
    "RTX_3090": {"options": {"baseURL": "http://10.0.0.51:8013/v1"},
                 "models": {"qwen3.8-flash-next-iq3_s": {"limit": {"context": 262144}}}},
  },
}
"""


class TheConfigAsOpencodeReadsIt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env, path = dict(os.environ), list(sys.path)
        cls.addClassCleanup(lambda: (os.environ.clear(), os.environ.update(env), sys.path.__setitem__(slice(None), path)))
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-oc-"))
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)
        (cls.tmp / "cfg").mkdir()
        (cls.tmp / "cfg" / "api-key").write_text("k\n")
        os.environ.update(HOME=str(cls.tmp), COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.tmp / "cfg"),
                          COCKPIT_REPO_DIR=str(REPO), COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0")
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_oc_config_under_test", DASH / "cockpit.py")
        cls.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cp)
        cls.real = cls.tmp / ".config" / "opencode" / "opencode.json"
        cls.real.parent.mkdir(parents=True)

    def collect(self, text):
        self.real.write_text(text)
        out = self.cp.collect_opencode()
        return out.get("data", out)["real"]

    def test_comments_and_trailing_commas_are_read(self):
        real = self.collect(COMMENTED)
        self.assertNotIn("error", real)
        self.assertEqual(real["default"], "qwen38/qwen3.8-27b")
        self.assertEqual(real["limits"]["qwen38/qwen3.8-27b"], {"context": 559000, "output": 186000})

    def test_a_config_that_does_not_parse_says_so(self):
        real = self.collect('{"model": ')
        self.assertIn("error", real)
        self.assertIsNone(real["default"])

    def test_a_config_that_is_not_an_object_says_so(self):
        real = self.collect("[1, 2]")
        self.assertIn("not a JSON object", real["error"])

    def test_a_box_that_folded_both_lanes_into_one_provider(self):
        # GB_10 on the reference box, 2026-10-07: neither qwen38 nor flashnext is in its
        # config, and the panel showed no limits, no fit verdict, and a default model that
        # did follow the lane as though it did not.
        self.real.write_text(FOLDED)
        out = self.cp.collect_opencode()
        out = out.get("data", out)
        self.assertEqual(out["real"]["by_model"]["qwen3.8-flash-next"],
                         {"context": 205000, "output": 32000})
        self.assertEqual(out["real"]["by_model"]["qwen3.8-27b"],
                         {"context": 1010000, "output": 151000})
        self.assertEqual(out["lane_providers"].get("qwen3.8-flash-next"), ["GB_10"])
        self.assertEqual(out["lane_providers"].get("qwen3.8-27b"), ["GB_10"])
        # a provider pointing at another box is not one of this box's lanes
        self.assertNotIn("RTX_3090", out["real"]["limits"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
