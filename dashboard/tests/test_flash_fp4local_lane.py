#!/usr/bin/env python3
"""The flash lane's FP4-Local config runs vLLM, not SGLang, on the flash lane's own unit
and served name. The 2026-10-19 review found the cockpit asking it SGLang questions: the
zombie belt read "no scheduler" from a serving vLLM and would restart it, the sampler
404'd /v1/loads once a second, auto-fit started a doomed job, and the eighth target was
missing from the action lists. These tests pin the fixed contract: the config is followed
with vLLM facts (the Nemotron pattern), SGLang facts never touch it, its engine image is
read from its launcher, and every target list names it."""
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
REPO = HERE.parents[2]
sys.path.insert(0, str(DASH))
ENV_BEFORE: dict = {}
TMP = Path(tempfile.mkdtemp(prefix="cockpit-fp4lane-"))
cp = None


def setUpModule():
    # every cockpit suite chains the environment it hands on (test_env_isolation.py): set
    # the throwaway box here, never at import, or every module collected after this one
    # snapshots it as its own baseline and reads a config directory that is gone by the
    # time it runs. The snapshot too is taken here, not at import: pytest keeps a note
    # about the running test in os.environ and pops it after the last test's teardown
    global cp
    ENV_BEFORE.update(os.environ)
    (TMP / "config").mkdir()
    (TMP / "config" / "api-key").write_text("k\n")
    (TMP / "units").mkdir()
    # cockpit reads its config/unit directories at import, so the module loads here,
    # with the throwaway box already in place
    os.environ.update(HOME=str(TMP), COCKPIT_DRY_RUN="1",
                      COCKPIT_CONFIG_DIR=str(TMP / "config"),
                      COCKPIT_UNIT_DIR=str(TMP / "units"),
                      COCKPIT_ENGINE="http://127.0.0.1:1")
    spec = importlib.util.spec_from_file_location("cockpit_fp4lane", str(DASH / "cockpit.py"))
    cp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cp)


def tearDownModule():
    global cp
    cp = None
    for name in set(os.environ) - set(ENV_BEFORE):
        del os.environ[name]
    os.environ.update(ENV_BEFORE)
    shutil.rmtree(TMP, True)


FP4 = "launch-flash-fp4local.sh"
SGL = "launch-flash.sh"


def write_files(launchers):
    for name in (SGL, FP4):
        p = TMP / "config" / name
        if name in launchers:
            p.write_text(f"exec docker run --rm img {name}\n")
        elif p.exists():
            p.unlink()


def set_life(**states):
    with cp.LIFE_LOCK:
        cp.LIFE["states"] = dict(states)


class WhichConfigRuns(unittest.TestCase):
    """flash_fp4_local() is the one fact every vLLM gate asks; it reads the unit, and the
    on-disk names only when there is no unit."""

    def tearDown(self):
        write_files(())
        for f in (TMP / "units").glob("*.service"):
            f.unlink(missing_ok=True)
        set_life()

    def unit(self, exec_start):
        (TMP / "units" / "qwen38-flash.service").write_text(
            f"[Service]\nExecStart=/bin/bash {exec_start}\n")

    def test_the_unit_names_the_config(self):
        write_files((SGL, FP4))
        self.unit("launch-flash-fp4local.sh")
        self.assertTrue(cp.flash_fp4_local())
        self.unit("launch-flash.sh")
        self.assertFalse(cp.flash_fp4_local())

    def test_with_no_unit_a_name_on_disk_beats_a_name_that_does_not_exist(self):
        write_files((FP4,))
        self.assertTrue(cp.flash_fp4_local())
        write_files((SGL,))
        self.assertFalse(cp.flash_fp4_local())
        write_files((SGL, FP4))
        self.assertFalse(cp.flash_fp4_local())          # the SGLang box stays the default

    def test_serving_means_busy_and_the_fp4_launcher(self):
        write_files((SGL, FP4))
        self.unit("launch-flash-fp4local.sh")
        set_life(**{"qwen38-flash.service": "ready"})
        self.assertTrue(cp.flash_fp4_local_serving())   # (it takes LIFE_LOCK: never hold it)
        set_life(**{"qwen38-flash.service": "stopped"})
        self.assertFalse(cp.flash_fp4_local_serving())
        set_life(**{"qwen38-flash.service": "ready"})
        self.unit("launch-flash.sh")
        self.assertFalse(cp.flash_fp4_local_serving())


class NoSGLangFactsAgainstIt(unittest.TestCase):
    """The sampler reads vLLM's facts while the fp4 config serves, and the actions that
    SGLang's routes could not serve answer with their refusal sentence."""

    def tearDown(self):
        write_files(())
        for f in (TMP / "units").glob("*.service"):
            f.unlink(missing_ok=True)
        set_life()

    def serving(self, fp4):
        write_files((SGL, FP4))
        (TMP / "units" / "qwen38-flash.service").write_text(
            "[Service]\nExecStart=/bin/bash "
            + ("launch-flash-fp4local.sh" if fp4 else "launch-flash.sh") + "\n")
        set_life(**{"qwen38-flash.service": "ready"})

    def test_the_fast_sampler_reads_vLLMs_metrics_not_sglangs_load_routes(self):
        seen = []
        real_n, real_l = cp.nemotron_load, cp.engine_load
        self.addCleanup(setattr, cp, "nemotron_load", real_n)
        self.addCleanup(setattr, cp, "engine_load", real_l)
        cp.nemotron_load = lambda **k: seen.append("vllm") or []
        cp.engine_load = lambda **k: seen.append("sglang") or []
        self.serving(True)
        cp.collect_engine_fast()
        self.assertEqual(seen, ["vllm"])                # no /v1/loads, no /get_load
        seen.clear()
        self.serving(False)
        cp.collect_engine_fast()
        self.assertEqual(seen, ["sglang"])              # the SGLang configs keep their read

    def test_abort_refuses_with_the_lanes_sentence(self):
        self.serving(True)
        with self.assertRaises(RuntimeError) as e:
            cp.engine_abort_all()
        self.assertIn(cp.FLASH_FP4LOCAL_CANNOT["abort_all"], str(e.exception))

    def test_flush_abort_and_fit_are_refused_by_the_server_with_why(self):
        # the Library/Settings actions: the server answers 409 with the config's own
        # sentence, as for the Kolibri and Nemotron lanes, and only on this config. The job
        # lock is held so a gate that let one through answers "busy", never runs a job.
        def act(name):
            assert cp.JOB_LOCK.acquire(blocking=False)
            try:
                return cp.start_action(name, {})
            finally:
                cp.JOB_LOCK.release()
        self.serving(True)
        for name, why in cp.FLASH_FP4LOCAL_CANNOT.items():
            code, body = act(name)
            self.assertEqual((code, body.get("error"), body.get("reasons")), (409, "blocked", [why]), name)
        self.serving(False)
        for name in cp.FLASH_FP4LOCAL_CANNOT:
            self.assertNotEqual(act(name)[1].get("error"), "blocked", name)

    def test_the_sglang_log_readers_skip_its_container(self):
        # qwen38-flash runs vLLM here: read as SGLang, decode was always None and the zombie
        # chip said the override was off (found in review, 2026-10-20)
        real = cp.run
        self.addCleanup(setattr, cp, "run", real)
        cp.run = lambda argv, **kw: cp.Ran("c0ffee\n" if argv[:2] == ["docker", "ps"] and "name=^qwen38-flash$" in argv else "")
        self.serving(True)
        self.assertIsNone(cp.sgl_active_container())
        self.assertIsNone(cp.collect_decode_telemetry()["lane"])
        self.assertIsNone(cp.collect_guard()["lane"])
        self.serving(False)
        self.assertEqual(cp.sgl_active_container(), "qwen38-flash")

    def test_scheduler_alive_still_says_no_for_a_vllm_container(self):
        # unchanged on purpose: vLLM truly has no sglang::scheduler, and that is exactly
        # why the zombie belt must never ask it about the fp4 config
        real = cp.run
        self.addCleanup(setattr, cp, "run", real)

        def fake(argv, **kw):
            return cp.Ran("PID COMM\n7 python3 -m vllm.entrypoints.openai.api_server\n")
        cp.run = fake
        self.assertIs(cp.scheduler_alive("qwen38-flash"), False)

        def fake2(argv, **kw):
            return cp.Ran("PID COMM\n7 sglang::scheduler\n")
        cp.run = fake2
        self.assertIs(cp.scheduler_alive("qwen38-flash"), True)

        def fake3(argv, **kw):
            return cp.Ran("PID COMM\n")
        cp.run = fake3
        self.assertIsNone(cp.scheduler_alive("qwen38-flash"))


class TheBeltsSkipIt(unittest.TestCase):
    """The SGLang belts and probes stand down on the fp4 config, called as the samplers call
    them; the boot budget is exercised by the boot-budget test in test_cockpit_collectors.py."""

    def tearDown(self):
        write_files(())
        for f in (TMP / "units").glob("*.service"):
            f.unlink(missing_ok=True)
        set_life()

    def unit(self, launcher):
        write_files((SGL, FP4))
        (TMP / "units" / "qwen38-flash.service").write_text(f"[Service]\nExecStart=/bin/bash {launcher}\n")

    def test_the_autofit_refuses_with_the_configs_sentence(self):
        fit = {"ok": False, "pool": 1000, "context": 2000, "output": 100, "worst": 2100}
        ready = {"qwen38-flash.service": "ready"}
        self.unit(FP4)
        self.assertEqual(cp.maybe_autofit(fit, ready, 0), cp.FLASH_FP4LOCAL_CANNOT["fit_opencode"])
        self.unit(SGL)                      # the SGLang config goes on to the activation check
        self.assertNotEqual(cp.maybe_autofit(fit, ready, 0), cp.FLASH_FP4LOCAL_CANNOT["fit_opencode"])

    def test_the_canary_stands_down_and_says_why(self):
        self.unit(FP4)
        set_life(**{"qwen38-flash.service": "ready"})
        out = cp.collect_canary()
        self.assertTrue(out["skipped"])
        self.assertIn("FP4-Local config is checked by vLLM's own /health", out["why"])
        self.unit(SGL)
        self.assertNotIn("FP4-Local", cp.collect_canary()["why"])

    def test_the_slow_read_asks_vllms_routes(self):
        real = cp.flash_fp4_local_engine_info
        self.addCleanup(setattr, cp, "flash_fp4_local_engine_info", real)
        cp.flash_fp4_local_engine_info = lambda models=None: {"served_target": "flash-fp4-local"}
        self.unit(FP4)
        set_life(**{"qwen38-flash.service": "ready"})
        self.assertEqual(cp.collect_engine_info(), {"served_target": "flash-fp4-local"})


class ItsEnginePanel(unittest.TestCase):
    """The panel's facts: the proxy's flash ceiling applies to the name this config serves,
    and its MTP drafter's length is its launcher's (found in review, 2026-10-20)."""
    METRICS = ('vllm:cache_config_info{cache_dtype="fp8",engine="0",gpu_memory_utilization="0.8"} 1.0\n'
               'vllm:num_requests_running{engine="0",model_name="qwen3.8-flash-next"} 0.0\n')

    def setUp(self):
        (TMP / "units" / "qwen38-flash.service").write_text(f"[Service]\nExecStart=/bin/bash {FP4}\n")
        (TMP / "config" / FP4).write_text(
            "exec docker run --rm img vllm serve m \\\n"
            "    --speculative-config '{\"method\":\"mtp\",\"num_speculative_tokens\":4}' \\\n")
        root = cp.rg.PIN_MODELS["FLASH_LOCAL_REV"]
        saved = (cp.http_json, cp.run, cp.urllib.request.urlopen)
        self.addCleanup(lambda: (setattr(cp, "http_json", saved[0]), setattr(cp, "run", saved[1]),
                                 setattr(cp.urllib.request, "urlopen", saved[2])))
        cp.http_json = lambda url, timeout=4.0: ({"data": [{"id": "qwen3.8-flash-next", "root": root,
                                                            "max_model_len": 262144}]}
                                                  if url.endswith("/v1/models") else {"version": "0.1"})
        cp.run = lambda argv, **kw: cp.Ran(
            "Environment=PROMPT_CEILING_TOKENS=0 FLASH_PROMPT_CEILING_TOKENS=250000\n"
            if "qwen38-keepalive.service" in argv else "")
        cp.urllib.request.urlopen = lambda url, timeout=None: io.BytesIO(self.METRICS.encode())

    def tearDown(self):
        write_files(())
        for f in (TMP / "units").glob("*.service"):
            f.unlink(missing_ok=True)

    def test_the_proxy_ceiling_and_the_drafter_are_reported(self):
        out = cp.flash_fp4_local_engine_info()
        self.assertEqual(out["served_target"], "flash-fp4-local")
        self.assertEqual(out["prompt_ceiling_tokens"], 250000)
        self.assertEqual((out["info"]["speculative_algorithm"], out["info"]["speculative_num_draft_tokens"]),
                         ("MTP", 4))

    def test_the_ceiling_follows_the_proxys_rule_for_the_served_name(self):
        # the panel's ceiling is the proxy's own rule, per served name (proxy v6.25): the
        # SGLang panel and this one both read it through proxy_prompt_ceiling
        for prompt, flash, served, want in ((0, 250000, "qwen3.8-27b", 0),
                                            (0, 250000, "qwen3.8-flash-next", 250000),
                                            (100000, 250000, "qwen3.8-flash-next", 100000),
                                            (250000, 0, "qwen3.8-27b", 250000)):
            env = f"Environment=PROMPT_CEILING_TOKENS={prompt} FLASH_PROMPT_CEILING_TOKENS={flash}\n"
            cp.run = lambda argv, env=env, **kw: cp.Ran(env if "qwen38-keepalive.service" in argv else "")
            with self.subTest(prompt=prompt, flash=flash, served=served):
                self.assertEqual(cp.proxy_prompt_ceiling(served), want)


class TheEngineImage(unittest.TestCase):
    IMAGE = "b12x/vllm:nightly-2026-10-17"

    def test_the_vllm_serve_shape(self):
        text = (f"#!/usr/bin/env bash\nexec docker run -d --name qwen38-flash \\\n"
                f"  {self.IMAGE} \\\n  vllm serve nvidia/Qwen3.8-Flash-Next-FP4-Local"
                f" --revision {'a' * 40} \\\n    --port 8000\n")
        self.assertEqual(cp.engine_image(text), self.IMAGE)

    def test_the_sglang_shape_still_works(self):
        text = ("exec docker run --rm \\\n"
                "  sglangai/sglang:v0.5.19 \\\n"
                "  python3 -m sglang.launch_server --model x\n")
        self.assertEqual(cp.engine_image(text), "sglangai/sglang:v0.5.19")

    def test_prose_about_serving_is_not_the_serve_line(self):
        text = (f"#!/usr/bin/env bash\n# the serve line below is NVIDIA's: vllm serve\n"
                f"{self.IMAGE} \\\nvllm serve x\n")
        self.assertEqual(cp.engine_image(text), self.IMAGE)

    def test_the_rendered_launcher_of_the_feature(self):
        text = (REPO / "qwen38-flash-fp4local-launch.sh.template").read_text()
        text = text.replace("__IMAGE__", self.IMAGE).replace("__MODEL__", "nvidia/x")
        self.assertEqual(cp.engine_image(text), self.IMAGE)


class TheEighthTarget(unittest.TestCase):
    def test_the_action_lists_name_it(self):
        targets = cp.ACTIONS["switch"]["params"]["target"]
        self.assertIn("flash-fp4-local", targets)
        self.assertIs(cp.ACTIONS["fetch"]["params"]["target"], targets)
        self.assertIs(cp.ACTIONS["prepare"]["params"]["target"], targets)

    def test_its_download_and_its_lane_run_prepare_lane(self):
        # its engine is a container the SGLang configs never pull: Download is the lane's
        # prepare, which takes image and checkpoint together; Load is the switch itself
        argv = cp.ACTIONS["fetch"]["argv"]({"target": "flash-fp4-local"})
        self.assertEqual(argv[1:], [str(cp.REPO_DIR / "prepare-lane.sh"), "flash",
                                    "flash-fp4-local"])
        argv = cp.ACTIONS["prepare"]["argv"]({"target": "flash-fp4-local"})
        self.assertEqual(argv[1:], [str(cp.REPO_DIR / "prepare-lane.sh"), "flash",
                                    "flash-fp4-local"])
        argv = cp.ACTIONS["switch"]["argv"]({"target": "flash-fp4-local"})
        self.assertEqual(argv[1:], [str(cp.REPO_DIR / "switch-model.sh"), "flash-fp4-local"])

    def test_the_page_lists_it_under_the_flash_lane(self):
        js = (DASH / "static" / "js" / "base.js").read_text()
        block = js.split("LANE_TARGETS", 1)[1].split("};", 1)[0]
        self.assertIn("'flash-fp4-local'", block)       # the flash lane card lists it
        self.assertIn("'flash-nvda'", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
