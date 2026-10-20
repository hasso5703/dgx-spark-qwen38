#!/usr/bin/env python3
"""The cockpit follows the Nemotron lane on its own facts, as it does the Kolibri lane.

The lane runs a vLLM nightly (77154ca6) with b12x: none of the routes the SGLang belts read, a
/health that generates nothing, and a /metrics that also counts its drafter's work. The fixtures
are that engine's own answers, captured on the reference box on 2026-10-15 (the final settings,
eight requests running): its /metrics, its /v1/models and the log of one boot. The module is
imported with its environment pointed at a throwaway box.
"""
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
METRICS = (HERE.parent / "fixtures" / "vllm-metrics-nemotron.txt").read_text()
MODELS = json.loads((HERE.parent / "fixtures" / "vllm-models-nemotron.json").read_text())
BOOT = (HERE.parent / "fixtures" / "nemotron-boot.log").read_text().splitlines()
POOL = 4712014
U = "qwen38-nemotron.service"
ENV_BEFORE = {}


def setUpModule():
    ENV_BEFORE.update(os.environ)


def tearDownModule():
    for name in set(os.environ) - set(ENV_BEFORE):
        del os.environ[name]
    os.environ.update(ENV_BEFORE)


def counters(accepted, drafts):
    """The fixture's /metrics with the drafter's two counters set."""
    out = []
    for ln in METRICS.splitlines():
        if ln.startswith("vllm:spec_decode_num_accepted_tokens_total{"):
            ln = ln.rsplit(" ", 1)[0] + f" {accepted}"
        elif ln.startswith("vllm:spec_decode_num_drafts_total{"):
            ln = ln.rsplit(" ", 1)[0] + f" {drafts}"
        out.append(ln)
    return "\n".join(out) + "\n"


class Engine(http.server.BaseHTTPRequestHandler):
    """vLLM as the cockpit meets it on the Nemotron lane."""
    models = MODELS
    metrics = METRICS

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        out = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/metrics":
            self._send(200, Engine.metrics, "text/plain")
        elif path == "/v1/models":
            self._send(200, json.dumps(Engine.models))
        elif path == "/version":
            self._send(200, '{"version":"0.32.0rc1.dev100+g77154ca61"}')
        elif path == "/health":
            self._send(200, "")
        else:
            self._send(404, '{"detail":"Not Found"}')


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = Path(tempfile.mkdtemp(prefix="cockpit-nemotron-"))
        cls.engine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        cls.engine.daemon_threads = True
        threading.Thread(target=cls.engine.serve_forever, daemon=True).start()
        os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.box), COCKPIT_REPO_DIR=str(REPO),
                          COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0",
                          COCKPIT_ENGINE=f"http://127.0.0.1:{cls.engine.server_port}")
        (cls.box / "api-key").write_text("test-key\n")
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_nemotron_under_test", DASH / "cockpit.py")
        cls.ck = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ck)
        # the unit the box would have: the draft length is read from it
        cls.unit = cls.box / U
        cls.unit.write_text("ExecStart=... nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB --revision " + "c" * 40
                            + " \\\n    --speculative-config.num_speculative_tokens 7 \\\n")
        cls.ck.NEMOTRON_UNIT_PATH = cls.unit

    @classmethod
    def tearDownClass(cls):
        cls.engine.shutdown()
        cls.engine.server_close()
        shutil.rmtree(cls.box, ignore_errors=True)

    def serving(self, state="ready"):
        with self.ck.LIFE_LOCK:
            self.ck.LIFE["states"] = {U: state} if state else {}


class ItsMetrics(Base):
    def test_the_engines_own_page_gives_load_cache_and_drafter(self):
        f = self.ck.vllm_metrics_facts(METRICS)
        self.assertEqual((f["running"], f["waiting"], f["pool"]), (8, 0, POOL))
        self.assertGreater(f["usage"], 0)
        self.assertGreater(f["spec_drafts"], 0)
        self.assertGreater(f["spec_accepted"], f["spec_drafts"])     # several tokens per draft

    def test_the_acceptance_is_the_drafters_over_its_last_seconds(self):
        self.ck._SPEC_READS.clear()
        Engine.metrics = counters(1000, 400)
        try:
            first = self.ck.nemotron_load()[0]
            Engine.metrics = counters(1600, 600)            # 200 drafts, 600 accepted: 4.0 per pass
            second = self.ck.nemotron_load()[0]
            Engine.metrics = counters(1600, 600)            # nothing drafted since: still the window's
            third = self.ck.nemotron_load()[0]
        finally:
            Engine.metrics = METRICS
        self.assertIsNone(first["accept_len"], "one read is no rate")
        self.assertAlmostEqual(second["accept_len"], 4.0)
        self.assertAlmostEqual(third["accept_len"], 4.0)
        self.assertEqual((second["num_tokens"], second["num_reqs"]), (POOL, 8))

    def test_with_no_draft_in_the_window_it_is_idle(self):
        self.ck._SPEC_READS.clear()
        t = 1000.0
        self.assertIsNone(self.ck.spec_accept_len(50, 10, t))
        self.assertIsNone(self.ck.spec_accept_len(50, 10, t + 5))
        self.assertAlmostEqual(self.ck.spec_accept_len(80, 20, t + 6), 4.0)
        # past the window, what came before it no longer counts
        self.assertIsNone(self.ck.spec_accept_len(80, 20, t + 6 + self.ck.SPEC_WINDOW_S + 1))

    def test_the_window_is_twenty_seconds_and_counts_the_rejected_drafts(self):
        t = 1000.0
        self.ck._SPEC_READS.clear()
        self.ck.spec_accept_len(0, 0, t)
        self.ck.spec_accept_len(100, 50, t + 10)
        # at t + 30 the read of t is 30 s old, out of the window: 300 accepted in 50 drafts since t + 10
        self.assertAlmostEqual(self.ck.spec_accept_len(400, 100, t + 30), 7.0)
        self.ck._SPEC_READS.clear()
        self.ck.spec_accept_len(0, 0, t)
        self.assertAlmostEqual(self.ck.spec_accept_len(300, 100, t + 20), 4.0, msg="a read 20 s old still counts")
        # drafts with none of their tokens accepted: one token per pass, the target's own
        self.ck._SPEC_READS.clear()
        self.ck.spec_accept_len(500, 100, t)
        self.assertAlmostEqual(self.ck.spec_accept_len(500, 150, t + 5), 1.0)

    def test_a_counter_that_went_back_is_no_rate(self):
        # a restart of the engine starts its counters again
        self.ck._SPEC_READS.clear()
        self.ck.spec_accept_len(5000, 1000, 10.0)
        self.assertIsNone(self.ck.spec_accept_len(10, 2, 11.0))


class ItsEnginePanel(Base):
    def test_what_it_serves_comes_from_the_engine_and_its_unit(self):
        out = self.ck.nemotron_engine_info()
        info = out["info"]
        self.assertEqual(info["served_model_name"], "nemotron-3.5-bolt")
        self.assertEqual(info["model_path"], "nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB")
        self.assertEqual(info["context_length"], 262144)
        self.assertEqual(info["max_total_num_tokens"], POOL)
        self.assertEqual(info["kv_cache_dtype"], "fp8")
        self.assertEqual(info["mem_fraction_static"], 0.7)
        self.assertEqual(info["version"], "vLLM 0.32.0rc1.dev100+g77154ca61")
        self.assertEqual((out["served_target"], info["quantization"]), ("nemotron", "iq2_xxs"))
        self.assertEqual((info["speculative_algorithm"], info["speculative_num_draft_tokens"]), ("LiLiCorr (DFlash)", 7))
        self.assertNotIn("speculative_num_steps", info)    # the page says the draft length alone
        self.assertEqual(out["prompt_ceiling_tokens"], 0)

    def test_the_collectors_ask_it_while_it_serves(self):
        self.serving("ready")
        try:
            self.assertEqual(self.ck.collect_engine_info()["info"]["served_model_name"], "nemotron-3.5-bolt")
            fast = self.ck.collect_engine_fast()
            self.assertEqual(fast["load"][0]["num_tokens"], POOL)
            self.assertIn("accept_len", fast["load"][0])
        finally:
            self.serving(None)

    def test_before_the_lifecycle_says_it_serves_its_facts_are_read_by_its_name(self):
        self.serving(None)
        self.ck.ENGINE_ROUTES.update(self.ck.CURRENT_ROUTES)
        out = self.ck.collect_engine_info()
        self.assertEqual((out.get("served_target"), out.get("info", {}).get("served_model_name")),
                         ("nemotron", "nemotron-3.5-bolt"), out)


class ItsState(Base):
    def setUp(self):
        self.ck.UNHEALTHY_TICKS.pop(U, None)
        self.ck.NEMOTRON_READY_ENTER.pop(U, None)
        self.saved = (self.ck.run, self.ck.kolibri_healthy, self.ck._nemotron_log)
        self.ck.run = lambda argv, **kw: "abc\n" if argv[:2] == ["docker", "ps"] and "name=^qwen38-nemotron$" in argv else ""

    def tearDown(self):
        self.ck.run, self.ck.kolibri_healthy, self.ck._nemotron_log = self.saved

    def state(self, healthy, enter="100", prev="ready", log=()):
        self.ck.kolibri_healthy = lambda: healthy
        self.ck._nemotron_log = lambda *container: list(log)
        st, boot, running = self.ck.nemotron_engine_state(U, active="active", sub="running",
                                                          prev_state=prev, enter_key=enter)
        return st["state"], boot, running

    def test_one_missed_probe_keeps_it_ready_three_make_it_degraded(self):
        self.assertEqual(self.state(True)[0], "ready")
        self.assertEqual(self.state(False)[0], "ready")
        self.assertEqual(self.state(False)[0], "ready")
        self.assertEqual(self.state(False)[0], "degraded")

    def test_a_boot_is_read_from_its_own_log(self):
        upto = next(i for i, ln in enumerate(BOOT) if "Loading weights took" in ln)
        st, boot, running = self.state(False, prev="starting", log=BOOT[:upto])
        self.assertEqual(st, "loading-weights")
        self.assertIn("shard", boot["detail"])
        self.assertTrue(running)

    def test_its_boot_never_goes_back_and_names_the_drafters_load(self):
        # read with Kolibri's markers, the drafter's one shard after the model's 90 put the boot
        # back from allocating-kv to loading-weights, "shard 1 of 1" (found in review, 2026-10-16)
        stages, seen = list(self.ck.lc.NEMOTRON_STAGES), []
        start = next(i for i, ln in enumerate(BOOT) if "Initializing a V1 LLM engine" in ln)
        for k in range(start + 1, len(BOOT) + 1):
            b = self.ck.lc.parse_nemotron_boot_log(BOOT[:k])
            seen.append((stages.index(b["stage"]), b["detail"], BOOT[k - 1]))
        for (a, _, _), (z, detail, line) in zip(seen, seen[1:]):
            self.assertGreaterEqual(z, a, f"the boot went back at: {line[:120]!r} ({detail})")
        model_done = next(i for i, (_, _, ln) in enumerate(seen) if "Loading weights took" in ln)
        drafter = [d for _, d, ln in seen[model_done:] if "Loading safetensors checkpoint shards" in ln]
        self.assertTrue(drafter and all(d.startswith("the drafter") for d in drafter), drafter)
        self.assertEqual(seen[-1][0], stages.index("warming-up"))

    def test_its_whole_boot_reads_as_its_warmup_until_it_answers(self):
        self.assertEqual(self.state(False, prev="capturing-graphs", log=BOOT)[0], "warming-up")

    def test_its_container_is_its_own(self):
        self.ck.run = lambda argv, **kw: "abc\n" if argv[:2] == ["docker", "ps"] and "name=^qwen38-kolibri$" in argv else ""
        self.assertFalse(self.state(False, prev="stopped")[2], "the Kolibri container is not this lane's")


class WhatItDoesNotHave(Base):
    def test_the_canary_stands_down_and_says_why(self):
        self.serving("ready")
        try:
            out = self.ck.collect_canary()
        finally:
            self.serving(None)
        self.assertTrue(out["skipped"])
        self.assertIn("the Nemotron lane is checked by vLLM's own /health", out["why"])

    def test_a_flush_and_an_abort_say_it_has_none(self):
        self.serving("ready")
        try:
            ok, lines, data = self.ck.job_flush_cache(None)
            with self.assertRaises(RuntimeError) as e:
                self.ck.engine_abort_all()
        finally:
            self.serving(None)
        self.assertFalse(ok)
        self.assertEqual(data["reason"], "unsupported")
        self.assertIn("The Nemotron lane has no cache flush route", lines[0])
        self.assertIn("The Nemotron lane has no route that aborts its generations", str(e.exception))

    def test_system_one_points_at_the_qwen_lanes(self):
        self.serving("ready")
        try:
            out = self.ck.systemone_available()
        finally:
            self.serving(None)
        self.assertFalse(out["available"])
        self.assertIn("a Nemotron lane is serving", out["reason"])

    def test_its_served_name_is_its_own(self):
        with self.ck.STATE_LOCK:
            self.ck.STATE.pop("engine_info", None)
        self.serving("ready")
        try:
            self.assertEqual(self.ck.served_model_name(), "nemotron-3.5-bolt")
        finally:
            self.serving(None)

    def test_flush_abort_and_fit_are_refused_by_the_server_with_why(self):
        def act(name):
            assert self.ck.JOB_LOCK.acquire(blocking=False)
            try:
                return self.ck.start_action(name, {})
            finally:
                self.ck.JOB_LOCK.release()
        self.serving("ready")
        try:
            out = {name: act(name) for name in ("flush_cache", "abort_all", "fit_opencode")}
        finally:
            self.serving(None)
        for name, why in (("flush_cache", "no cache flush route"), ("abort_all", "no route that aborts"),
                          ("fit_opencode", "nothing to fit")):
            code, body = out[name]
            self.assertEqual((code, body.get("error")), (409, "blocked"), name)
            reasons = " ".join(body.get("reasons") or [])
            self.assertIn(why, reasons, name)
            self.assertIn("Nemotron", reasons, name)


class ItsUnit(Base):
    def test_the_target_is_read_from_the_unit(self):
        self.assertEqual(self.ck.unit_target(U), {"model": "nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB", "target": "nemotron"})
        saved = self.ck.NEMOTRON_UNIT_PATH
        try:
            self.ck.NEMOTRON_UNIT_PATH = self.box / "absent.service"
            self.assertEqual(self.ck.unit_target(U), {"model": None, "target": None})
        finally:
            self.ck.NEMOTRON_UNIT_PATH = saved

    def test_it_is_a_switch_target_a_unit_the_page_may_drive_and_a_lane_it_prepares(self):
        self.assertIn(U, self.ck.SERVING_UNITS)
        self.assertEqual(self.ck.ACTIONS["switch"]["params"]["target"][-2:], ["nemotron", "nemotron-super"])
        self.assertEqual(self.ck.target_lane("nemotron"), "nemotron")
        self.assertEqual(self.ck.TARGET_PIN["nemotron"], "NEMOTRON_BOLT_REV")
        self.assertIn("NEMOTRON_BOLT_REV", self.ck.rg.PIN_MODELS)
        self.assertEqual(self.ck.ACTIONS["prepare"]["argv"]({"target": "nemotron"})[-1], "nemotron")
        self.assertIn("qwen38-nemotron", self.ck.CONTAINERS)
        self.assertNotIn("qwen38-nemotron", self.ck.SGL_CONTAINERS)
        self.assertIn(U, self.ck.JOURNAL_UNITS)
        self.assertEqual(self.ck.UNIT2CONT[U], "qwen38-nemotron")

    def test_it_is_prepared_once_the_image_its_unit_names_is_built(self):
        unit = self.box / "prepared.service"
        unit.write_text("ExecStart=/bin/bash -c 'exec /usr/bin/docker run qwen38-nemotron:vllm77154ca6-b12x1.5.1-1ee85ca80e0f \\\n")
        saved_path, saved_run = self.ck.NEMOTRON_UNIT_PATH, self.ck.run
        try:
            self.ck.NEMOTRON_UNIT_PATH = unit
            self.ck.run = lambda argv, **kw: "sha256:abc\n" if argv[:3] == ["docker", "image", "inspect"] \
                and argv[-1] == "qwen38-nemotron:vllm77154ca6-b12x1.5.1-1ee85ca80e0f" else ""
            self.assertTrue(self.ck.lane_prepared("nemotron"))
            self.ck.run = lambda argv, **kw: ""
            self.assertFalse(self.ck.lane_prepared("nemotron"))
            self.ck.NEMOTRON_UNIT_PATH = self.box / "absent.service"
            self.assertIsNone(self.ck.lane_prepared("nemotron"))
        finally:
            self.ck.NEMOTRON_UNIT_PATH, self.ck.run = saved_path, saved_run


if __name__ == "__main__":
    unittest.main()
