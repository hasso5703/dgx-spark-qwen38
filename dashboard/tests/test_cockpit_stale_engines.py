"""An update that changed what a serving engine starts with says so, and what to do.

An update rewrites a lane's unit, a script it runs or the image it names, and leaves the engine
that serves running: what it brought applies at the lane's next start. Only a NOTE line of the
installer's output said a restart was owed, and the cockpit said nothing (found 2026-10-16: the
prefix cache settings v1.35.0 gives Bolt's unit waited for a restart nobody was told about).
The update collector now names each engine an update left behind, with engine-inputs.py's rule,
the one the installers go by."""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
ENV_BEFORE = {}


def setUpModule():
    ENV_BEFORE.update(os.environ)


def tearDownModule():
    for name in set(os.environ) - set(ENV_BEFORE):
        del os.environ[name]
    os.environ.update(ENV_BEFORE)


class AnUpdateOwedToAServingEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-stale-"))
        (cls.tmp / "api-key").write_text("k\n")
        cls.units = cls.tmp / "units"
        cls.units.mkdir()
        for u in ("qwen38-sglang.service", "qwen38-nemotron.service", "qwen38-image.service", "qwen38-keepalive.service"):
            (cls.units / u).write_text("[Service]\nExecStart=/bin/true\n")
        os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.tmp), COCKPIT_REPO_DIR=str(DASH.parent),
                          COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0",
                          COCKPIT_UPDATE_CHECK="0", COCKPIT_UNIT_DIR=str(cls.units))
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_stale_engines", DASH / "cockpit.py")
        cls.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def fake(self, answers):
        """engine-inputs.py, answering per unit file as its running() does."""
        calls = []

        self.kws = {}

        class EI:
            @staticmethod
            def running(unit, config_dir, hf_cache, ckpts, **kw):
                calls.append(os.path.basename(unit))
                self.kws[os.path.basename(unit)] = kw
                return answers.get(os.path.basename(unit), "no: x is inactive")
        self.cp._ENGINE_INPUTS["mod"] = EI
        return calls

    def tearDown(self):
        self.cp._ENGINE_INPUTS.pop("mod", None)

    def test_a_unit_rewritten_after_its_engine_started_is_owed_a_restart(self):
        u = str(self.units / "qwen38-nemotron.service")
        self.fake({"qwen38-nemotron.service": f"no: {u} changed after qwen38-nemotron.service started"})
        self.assertEqual(self.cp.stale_engines(), {"qwen38-nemotron.service": "Its unit changed (what it runs and how)"})

    def test_a_script_or_an_image_the_engine_starts_with_names_itself(self):
        self.fake({"qwen38-sglang.service": f"no: {self.tmp}/launch-flash.sh changed after qwen38-sglang.service started",
                   "qwen38-nemotron.service": "no: container qwen38-nemotron runs another image than the one its unit names"})
        self.assertEqual(self.cp.stale_engines(), {
            "qwen38-sglang.service": "launch-flash.sh changed (a file it reads when it starts)",
            "qwen38-nemotron.service": "Its image changed"})

    def test_a_drop_in_counts_as_the_unit(self):
        d = "/etc/systemd/system/qwen38-nemotron.service.d/override.conf"
        self.fake({"qwen38-nemotron.service": f"no: {d} changed after qwen38-nemotron.service started"})
        self.assertEqual(self.cp.stale_engines(), {"qwen38-nemotron.service": "Its unit changed (what it runs and how)"})

    def test_nothing_an_update_did_owes_nothing(self):
        # a stopped unit, an engine up to date, a lane with no container to compare (the image
        # lane's venv): none of these is an update left unapplied
        self.fake({"qwen38-sglang.service": "no: qwen38-sglang.service is inactive",
                   "qwen38-nemotron.service": "yes",
                   "qwen38-image.service": "no: found no container name to check the image against"})
        self.assertEqual(self.cp.stale_engines(), {})

    def test_only_installed_engine_units_are_read_and_never_the_proxy(self):
        calls = self.fake({})
        self.cp.stale_engines()
        self.assertEqual(sorted(calls), ["qwen38-image.service", "qwen38-nemotron.service", "qwen38-sglang.service"])

    def test_the_update_collector_carries_it_with_the_release_check_off(self):
        u = str(self.units / "qwen38-nemotron.service")
        self.fake({"qwen38-nemotron.service": f"no: {u} changed after qwen38-nemotron.service started"})
        out = self.cp.collect_update()
        self.assertEqual(out.get("stale_engines"), {"qwen38-nemotron.service": "Its unit changed (what it runs and how)"})
        self.fake({"qwen38-nemotron.service": "yes"})
        self.assertNotIn("stale_engines", self.cp.collect_update())

    def test_every_call_is_bounded(self):
        # a docker that no longer answers held the whole 30 s tier (found in review, 2026-10-16)
        self.fake({})
        self.cp.stale_engines()
        self.assertTrue(self.kws)
        self.assertEqual({u: kw.get("timeout") for u, kw in self.kws.items()},
                         {u: self.cp.ENGINE_INPUTS_TIMEOUT_S for u in self.kws})

    def test_a_check_that_does_not_answer_ends_the_round_with_what_it_had(self):
        u = str(self.units / "qwen38-sglang.service")
        calls = []

        class EI:
            @staticmethod
            def running(unit, config_dir, hf_cache, ckpts, **kw):
                name = os.path.basename(unit)
                calls.append(name)
                if name == "qwen38-image.service":
                    raise subprocess.TimeoutExpired(["docker"], kw.get("timeout"))
                if name == "qwen38-sglang.service":
                    return f"no: {u} changed after qwen38-sglang.service started"
                return "yes"
        self.cp._ENGINE_INPUTS["mod"] = EI
        self.assertEqual(self.cp.stale_engines(), {"qwen38-sglang.service": "Its unit changed (what it runs and how)"})
        # the units after the one that did not answer are not asked this round
        self.assertEqual(calls, ["qwen38-sglang.service", "qwen38-image.service"])

    def test_the_vllm_lanes_count_the_files_their_installer_counts(self):
        self.fake({})
        self.cp.stale_engines()
        cfg = self.cp.CONFIG_DIR
        for unit in ("qwen38-kolibri.service", "qwen38-nemotron.service", "qwen38-nemotron-super.service"):
            self.assertIn(unit, self.cp.VLLM_UNITS)
        self.assertEqual(self.kws["qwen38-nemotron.service"]["extra"], [str(cfg / "vllm-auth/vllm_engine_auth.py")])
        self.assertEqual(self.kws["qwen38-sglang.service"]["extra"], [])
        self.assertEqual(self.kws["qwen38-image.service"]["extra"], [])

    def test_the_flash_lanes_fp4_config_counts_the_middleware_and_the_sglang_one_does_not(self):
        # the FP4-Local launcher mounts the same middleware, its engine's only key gate: an
        # update of it was never flagged on that config (found in review, 2026-10-20)
        flash = self.units / "qwen38-flash.service"
        self.addCleanup(flash.unlink, missing_ok=True)
        mw = str(self.cp.CONFIG_DIR / "vllm-auth/vllm_engine_auth.py")
        for launcher, extra in (("launch-flash-fp4local.sh", [mw]), ("launch-flash.sh", [])):
            flash.write_text(f"[Service]\nExecStart=/bin/bash {self.tmp / launcher}\n")
            self.fake({})
            self.cp.stale_engines()
            self.assertEqual(self.kws["qwen38-flash.service"]["extra"], extra, launcher)

    def test_those_files_are_the_vllm_installers_own_list(self):
        # vllm-lane.sh's LANE_FILES: the unit and engine-secrets.sh (which every vLLM unit runs,
        # so engine-inputs.py finds them), the start guard (no input of the engine, which
        # engine-inputs.py leaves out by design), and the file the cockpit adds; nothing else
        text = (DASH.parent / "vllm-lane.sh").read_text()
        line = next(ln for ln in text.splitlines() if ln.strip().startswith("LANE_FILES=("))
        named = set(re.findall(r'\$CONFIG_DIR/([A-Za-z0-9._/-]+)', line))
        self.assertEqual(named - {"engine-secrets.sh", "engine-preflight.sh"}, set(self.cp.VLLM_LANE_FILES))
        for tpl in ("qwen38-kolibri.service.template", "qwen38-nemotron.service.template",
                    "qwen38-nemotron-super.service.template"):
            self.assertIn("engine-secrets.sh", (DASH.parent / tpl).read_text(), tpl)
        ei = (DASH.parent / "engine-inputs.py").read_text()
        self.assertRegex(ei, r'NOT_INPUTS = \{[^}]*"engine-preflight\.sh"')

    def test_the_vllm_units_are_the_installers_that_use_the_vllm_base(self):
        # every installer that sources vllm-lane.sh names its unit in UNIT=; a lane added on
        # that base and missing here would be checked without its middleware
        units = set()
        for inst in sorted(DASH.parent.glob("install-*.sh")):
            text = inst.read_text()
            if re.search(r'^\. "\$HERE/vllm-lane\.sh"', text, re.M):
                units.add(re.search(r"^UNIT=(\S+)", text, re.M).group(1))
        self.assertEqual(units, set(self.cp.VLLM_UNITS))

    def test_a_restart_or_a_switch_reads_what_is_owed_again(self):
        # the tier's answer from before the restart said owed for up to 30 s, and the card offered
        # the restart again over the boot it had started (found in review, 2026-10-17)
        for action in ("unit", "switch"):
            with self.subTest(action=action):
                with self.cp.STATE_LOCK:
                    self.cp.STATE["update"] = {"data": {"installed": "v1.35.0",
                                                        "stale_engines": {"qwen38-nemotron.service": "Its unit changed (what it runs and how)"}},
                                               "ts": 0.0}
                self.fake({"qwen38-nemotron.service": "yes"})
                self.assertTrue(self.cp.JOB_LOCK.acquire(blocking=False))   # run_job releases it
                job = self.cp.Job(action, None, 20, fn=lambda j: (True, [], None))
                self.cp.JOBS[job.id] = job
                self.cp.JOB_CURRENT["id"] = job.id
                self.cp.run_job(job)
                data = self.cp.STATE["update"]["data"]
                self.assertNotIn("stale_engines", data)
                self.assertEqual(data.get("installed"), "v1.35.0")

    def test_the_repo_modules_it_loads_are_part_of_the_code_the_cockpit_watches(self):
        fp = self.cp.code_fingerprint()
        dash = (DASH / "install-dashboard.sh").read_text()
        for name in ("engine-inputs.py", "oc-fit-limits.py", "oc-merge-limits.py"):
            with self.subTest(name=name):
                self.assertIn(name, fp)
                self.assertNotEqual(fp[name], "missing")
                self.assertRegex(dash, r'stale_since "\$UNIT"[^\n]*"\$REPO_DIR/' + re.escape(name) + '"')

    def test_with_no_update_answer_yet_a_finished_job_says_nothing(self):
        with self.cp.STATE_LOCK:
            self.cp.STATE.pop("update", None)
        self.fake({"qwen38-nemotron.service": "yes"})
        self.cp.refresh_owed()
        self.assertNotIn("update", self.cp.STATE)

    def test_an_unreadable_rule_says_nothing_rather_than_fail(self):
        def boom():
            raise OSError("no engine-inputs.py")
        real = self.cp._engine_inputs
        self.cp._engine_inputs = boom
        try:
            self.assertEqual(self.cp.stale_engines(), {})
        finally:
            self.cp._engine_inputs = real

    def test_the_rule_is_the_repository_engine_inputs(self):
        # the real file, as the installers run it: the cockpit reads the same rule
        self.cp._ENGINE_INPUTS.pop("mod", None)
        mod = self.cp._engine_inputs()
        self.assertTrue(callable(getattr(mod, "running", None)))
        self.assertEqual(Path(mod.__file__).resolve(), (DASH.parent / "engine-inputs.py").resolve())


if __name__ == "__main__":
    unittest.main(verbosity=2)
