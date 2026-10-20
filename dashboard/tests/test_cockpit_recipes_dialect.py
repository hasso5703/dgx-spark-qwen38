"""The flash lane's recipe panel, where two configs speak two engines.

The lane has one lane key and two dialects: the SGLang configs and the
FP4-Local (b12x vLLM) one. The panel must compare each recipe with an
invocation of the SAME engine: comparing across dialects would report every
flag of the other engine as drift, which is the one thing the drift panel is
forbidden to do (docs/flash-lane.md, "The lane's other config").
"""
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


def load_cockpit(config_dir: Path):
    # nothing under COCKPIT_* is set from this module at import time: the suite's
    # isolation guard (test_env_isolation) restores only what a module added
    # after its own setUpModule ran
    os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(config_dir),
                      COCKPIT_REPO_DIR=str(REPO), COCKPIT_PORT="0",
                      COCKPIT_UNIT_DIR=str(config_dir / "units"), HOME=str(config_dir))
    sys.path.insert(0, str(DASH))
    spec = importlib.util.spec_from_file_location("cockpit_recipes_dialect_under_test",
                                                  DASH / "cockpit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def render(name: str) -> str:
    """The template the way install.sh renders it. recipes.render_template is
    exactly what builtin() derives the recipe from, so the rendered launcher
    here is byte-for-byte what a fresh install writes for that config."""
    import recipes as rc
    assigns = rc.parse_assignments((REPO / "install.sh").read_text())
    rid = "flash-fp4-local" if "fp4local" in name else "flash"
    return rc.render_template(name, rid, rc.load_templates(REPO), assigns)["text"]


class DialectPanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-recipes-dialect-"))
        (cls.tmp / "units").mkdir(exist_ok=True)   # no real unit: the launcher on disk decides
        # what is restored, never deleted: another module may have set HOME or a
        # COCKPIT_* knob before us, and popping it here would leave that module
        # running against the real box (found via test_kolibri_lane in the full
        # suite, 2026-10-19)
        keys = ("COCKPIT_DRY_RUN", "COCKPIT_CONFIG_DIR", "COCKPIT_REPO_DIR",
                "COCKPIT_PORT", "COCKPIT_UNIT_DIR", "HOME")
        cls.env_before = {k: os.environ.get(k) for k in keys}
        cls.cp = load_cockpit(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.env_before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        for n in ("launch-flash.sh", "launch-flash-fp4local.sh", "units/qwen38-flash.service"):
            (self.tmp / n).unlink(missing_ok=True)

    def unit(self, launcher: str):
        (self.tmp / "units" / "qwen38-flash.service").write_text(
            f"[Service]\nExecStart=/bin/bash {self.tmp / launcher}\n")

    def both_launchers(self):
        (self.tmp / "launch-flash.sh").write_text(render("qwen38-flash-launch.sh.template"))
        (self.tmp / "launch-flash-fp4local.sh").write_text(
            render("qwen38-flash-fp4local-launch.sh.template"))

    def test_a_leftover_fp4_launcher_is_not_installed_while_the_unit_runs_sglang(self):
        # install.sh never removes the other config's launcher: SGLang -> FP4 -> SGLang
        # leaves it, and it read "installed" (found in review, 2026-10-20)
        self.both_launchers()
        self.unit("launch-flash.sh")
        rows, snap = self.rows()
        self.assertIsNone(snap["installed"]["flash-fp4-local"])
        self.assertFalse(rows["flash-fp4-local"]["installed"])
        self.assertIsNone(rows["flash-fp4-local"]["drift"])
        self.assertTrue(rows["flash"]["installed"])
        self.assertEqual(rows["flash"]["drift"], [])

    def test_a_leftover_sglang_launcher_is_not_installed_while_the_unit_runs_fp4(self):
        self.both_launchers()
        self.unit("launch-flash-fp4local.sh")
        rows, snap = self.rows()
        self.assertIsNone(snap["installed"]["flash"])
        self.assertFalse(rows["flash"]["installed"])
        self.assertIsNone(rows["flash"]["drift"])
        self.assertTrue(rows["flash-fp4-local"]["installed"])
        self.assertEqual(rows["flash-fp4-local"]["drift"], [])

    def rows(self):
        snap = self.cp.recipes_snapshot(max_age=0)
        return {r["recipe"]["id"]: r for r in snap["builtin"]}, snap

    def test_the_vllm_launcher_feeds_its_own_recipe_and_no_other(self):
        (self.tmp / "launch-flash-fp4local.sh").write_text(
            render("qwen38-flash-fp4local-launch.sh.template"))
        rows, snap = self.rows()
        self.assertEqual(snap["installed"]["flash-fp4-local"]["engine"]["family"], "vllm")
        fp4 = rows["flash-fp4-local"]
        self.assertTrue(fp4["installed"])
        self.assertEqual(fp4["drift"], [])          # a fresh install drifts from nothing
        self.assertIsNone(rows["flash"]["drift"])   # the other dialect is not drift
        self.assertFalse(rows["flash"]["installed"])

    def test_the_sglang_launcher_still_answers_for_the_sglang_recipes(self):
        (self.tmp / "launch-flash.sh").write_text(render("qwen38-flash-launch.sh.template"))
        rows, snap = self.rows()
        self.assertEqual(snap["installed"]["flash"]["engine"]["family"], "sglang")
        self.assertTrue(rows["flash"]["installed"])
        self.assertEqual(rows["flash"]["drift"], [])
        self.assertIsNone(rows["flash-fp4-local"]["drift"])
        self.assertFalse(rows["flash-fp4-local"]["installed"])

    def test_a_missing_launcher_is_not_drift(self):
        rows, snap = self.rows()
        self.assertIsNone(snap["installed"]["flash"])
        self.assertIsNone(rows["flash"]["drift"])
        self.assertIsNone(rows["flash-fp4-local"]["drift"])
        for row in rows.values():          # eight builtins, all still reported
            self.assertIn("presence", row)


if __name__ == "__main__":
    unittest.main()
