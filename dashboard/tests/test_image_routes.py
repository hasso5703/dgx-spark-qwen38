#!/usr/bin/env python3
"""The cockpit's image routes: the refusals it makes so the engine does not have to.

Three of these correspond to a live HTTP 500 from the lane with nothing in the body, and
one to a wrong answer with a 200. All four were measured against Qwen-Image 2.1 on a DGX
Spark on 2026-09-22:

  * a width or height that is not a multiple of 32 -> HTTP 500, reason only in the
    engine's own log ("must be divisible by 32");
  * no output_format -> HTTP 500, because the API falls back to JPEG when the background
    is not transparent and this model returns RGBA for everything it makes, which PIL
    refuses to write as JPEG. The plainest possible request fails for that alone;
  * an edit sent width and height -> HTTP 200 with the reference's size instead of the
    one asked for, because /edits takes `size` and /generations takes width and height;
  * /health answers 503 for the ~77 s the weights take to load, while systemd reads the
    unit `active` throughout.

The module is imported with its environment pointed at a throwaway box, like every other
test here, so importing it writes nothing into the developer's own HOME.
"""
import importlib.util
import json
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
    os.environ.update(
        COCKPIT_DRY_RUN="1",
        COCKPIT_CONFIG_DIR=str(config_dir),
        COCKPIT_REPO_DIR=str(REPO),
        COCKPIT_PORT="0",
        COCKPIT_AGENT_PORT="0",
        COCKPIT_AUTOHEAL="0",
    )
    sys.path.insert(0, str(DASH))
    spec = importlib.util.spec_from_file_location("cockpit_image_under_test",
                                                  DASH / "cockpit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Sent:
    """Whatever the cockpit put on the wire, captured instead of sent."""

    def __init__(self):
        self.body = None
        self.headers = {}
        self.url = ""
        self.status = 200
        self.payload = b'{"data":[{"b64_json":"AAAA"}]}'

    def urlopen(self, req, timeout=None):
        self.body, self.url = req.data, req.full_url
        self.headers = dict(req.headers)
        outer = self

        class R:
            def read(self_inner):
                return outer.payload

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        return R()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-image-"))
        (cls.tmp / "api-key").write_text("test-key-not-a-real-one\n")
        cls.ck = load_cockpit(cls.tmp)
        # Kept in a dict, not as a class attribute: a plain function assigned to a class
        # becomes a bound method and would be handed self. Two tests below call the real
        # one to check it reads the installed unit.
        cls.saved = {"image_base": cls.ck.image_base}
        cls.ck.image_base = lambda: "http://127.0.0.1:30020"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.spy = Sent()
        self._orig = self.ck.urllib.request.urlopen
        self.ck.urllib.request.urlopen = self.spy.urlopen

    def tearDown(self):
        self.ck.urllib.request.urlopen = self._orig

    def call(self, payload, editing=False):
        return self.ck.image_call(payload, editing=editing)


class TheRefusals(Base):
    def test_a_size_that_is_not_a_multiple_of_32_never_reaches_the_engine(self):
        for bad in (1328, 1000, 33, 100, 0, -32):
            code, out = self.call({"prompt": "x", "width": bad, "height": 1024})
            self.assertEqual(code, 400, bad)
            self.assertIn("multiple of 32", out["error"])
        code, _ = self.call({"prompt": "x", "width": 1024, "height": 1328})
        self.assertEqual(code, 400)
        self.assertIsNone(self.spy.body, "nothing should have been sent")

    def test_a_size_that_is_a_multiple_of_32_goes_through(self):
        code, _ = self.call({"prompt": "x", "width": 1184, "height": 896})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(self.spy.body)["width"], 1184)

    def test_jpeg_is_refused_with_the_reason_the_engine_withholds(self):
        for fmt in ("jpeg", "jpg", "JPEG", "Jpg"):
            code, out = self.call({"prompt": "x", "output_format": fmt})
            self.assertEqual(code, 400, fmt)
            self.assertIn("alpha", out["error"])

    def test_an_empty_prompt_is_refused(self):
        for bad in ("", "   ", None):
            code, out = self.call({"prompt": bad} if bad is not None else {})
            self.assertEqual(code, 400, repr(bad))

    def test_a_format_is_always_sent_even_when_the_caller_forgot(self):
        self.call({"prompt": "x"})
        body = json.loads(self.spy.body)
        self.assertEqual(body["output_format"], "png")
        self.assertEqual(body["response_format"], "b64_json")

    def test_the_callers_own_format_is_kept(self):
        self.call({"prompt": "x", "output_format": "webp"})
        self.assertEqual(json.loads(self.spy.body)["output_format"], "webp")


class WhatABrowserMayAsk(Base):
    def test_fields_that_would_reach_past_the_lane_are_dropped(self):
        """The request model accepts extras. A page must not be able to name an upscaler
        path, a LoRA, a perf dump target, a diffusers kwargs blob or another model."""
        self.call({"prompt": "x", "upscaling_model_path": "/etc/passwd",
                   "perf_dump_path": "/tmp/x", "diffusers_kwargs": {"a": 1},
                   "lora_path": "/x", "enable_upscaling": True, "model": "other"})
        body = json.loads(self.spy.body)
        for forbidden in ("upscaling_model_path", "perf_dump_path", "diffusers_kwargs",
                          "lora_path", "enable_upscaling", "model"):
            self.assertNotIn(forbidden, body, forbidden)

    def test_the_fields_the_tab_needs_do_get_through(self):
        self.call({"prompt": "x", "width": 512, "height": 512, "num_inference_steps": 8,
                   "n": 2, "background": "transparent", "seed": 7, "true_cfg_scale": 4.0,
                   "negative_prompt": "blurry", "flow_shift": 3.0,
                   "generator_device": "cpu"})
        body = json.loads(self.spy.body)
        for field in ("width", "height", "num_inference_steps", "n", "background", "seed",
                      "true_cfg_scale", "negative_prompt", "flow_shift", "generator_device"):
            self.assertIn(field, body, field)


class TheEditingPath(Base):
    PNG = "data:image/png;base64,iVBORw0KGgo="

    def test_it_sends_the_size_the_way_the_edits_endpoint_takes_it(self):
        """/edits has no width or height fields. Sent that way an edit ignores them and
        returns the reference's own size: a request for 512x512 came back 1024x1024
        against the live lane."""
        self.call({"prompt": "x", "width": 512, "height": 512, "images": [self.PNG]},
                  editing=True)
        self.assertIn(b'name="size"', self.spy.body)
        self.assertIn(b"512x512", self.spy.body)
        self.assertNotIn(b'name="width"', self.spy.body)
        self.assertNotIn(b'name="height"', self.spy.body)

    def test_it_posts_to_the_edits_endpoint(self):
        self.call({"prompt": "x", "images": [self.PNG]}, editing=True)
        self.assertTrue(self.spy.url.endswith("/v1/images/edits"), self.spy.url)
        self.assertIn("multipart/form-data", self.spy.headers.get("Content-type", ""))

    def test_every_reference_becomes_its_own_ordered_part(self):
        """The OpenAI SDK sends image[] for a list and so does this, or a second
        reference is silently dropped. Order is the model's Picture 1, Picture 2."""
        self.call({"prompt": "x", "images": [self.PNG] * 3}, editing=True)
        self.assertEqual(self.spy.body.count(b'name="image[]"'), 3)
        for i in (1, 2, 3):
            self.assertIn(f'filename="picture-{i}.png"'.encode(), self.spy.body)

    def test_it_refuses_anything_that_is_not_an_image_data_url(self):
        for bad in (["http://example.com/x.png"], ["data:text/html;base64,AAA"],
                    ["data:image/png,notbase64"], ["data:image/png;base64,"],
                    ["data:image/png;base64,!!!!"], [""], [None], "notalist", []):
            code, _ = self.call({"prompt": "x", "images": bad}, editing=True)
            self.assertEqual(code, 400, bad)

    def test_it_refuses_more_references_than_the_model_takes(self):
        code, out = self.call({"prompt": "x", "images": [self.PNG] * 11}, editing=True)
        self.assertEqual(code, 400)
        self.assertIn("10", out["error"])
        code, _ = self.call({"prompt": "x", "images": [self.PNG] * 10}, editing=True)
        self.assertEqual(code, 200)

    def test_no_bearer_is_sent_because_the_lane_cannot_check_one(self):
        """The diffusion runtime has no --api-key. A header that reads as a gate and is
        not one is worse than none."""
        self.call({"prompt": "x", "images": [self.PNG]}, editing=True)
        self.assertNotIn("Authorization", self.spy.headers)
        self.call({"prompt": "x"})
        self.assertNotIn("Authorization", self.spy.headers)


class WhatTheTabIsTold(Base):
    def test_a_lane_that_is_not_installed_says_so_rather_than_stopped(self):
        """"Stopped" invites a start button for a unit that does not exist."""
        self.ck.IMAGE_UNIT_PATH = Path("/nonexistent/qwen38-image.service")
        out = self.ck.image_status()
        self.assertFalse(out["installed"])
        self.assertFalse(out["available"])
        self.assertEqual(out["state"], "not installed")

    def test_the_port_is_read_from_the_installed_unit_not_assumed(self):
        unit = self.tmp / "qwen38-image.service"
        unit.write_text("[Service]\nExecStart=/x/sglang serve --model-path Q/M \\\n"
                        "  --host 127.0.0.1 --port 31234\n")
        self.ck.IMAGE_UNIT_PATH = unit
        self.assertEqual(self.ck.image_port(), 31234)
        self.assertEqual(self.ck.image_status()["model"], "Q/M")

    def test_a_missing_unit_falls_back_to_the_documented_port(self):
        self.ck.IMAGE_UNIT_PATH = Path("/nonexistent/qwen38-image.service")
        self.assertEqual(self.ck.image_port(), 30020)

    def test_a_lane_loading_its_weights_is_not_reported_as_ready(self):
        """/health answers 503 for the ~70 s it takes to load 31 GB, while systemd reads
        `active` throughout. The status takes the lifecycle's word, which derives ready
        from a 200 only, rather than probing again on its own."""
        unit = self.tmp / "qwen38-image.service"
        unit.write_text("[Service]\nExecStart=/x/sglang serve --model-path Q/M "
                        "--host 127.0.0.1 --port 30020\n")
        self.ck.IMAGE_UNIT_PATH = unit
        with self.ck.LIFE_LOCK:
            self.ck.LIFE["states"] = {"qwen38-image.service": "loading-weights"}
        out = self.ck.image_status()
        self.assertFalse(out["available"])
        self.assertEqual(out["state"], "loading-weights")

    def test_the_status_does_not_probe_the_lane_itself(self):
        """The lifecycle probes /health every 2 s; the tab polls this every 1.5 s through a
        generation. Two probes of one lane is two answers that can disagree for a tick."""
        unit = self.tmp / "qwen38-image.service"
        unit.write_text("[Service]\nExecStart=/x/sglang serve --model-path Q/M --port 30020\n")
        self.ck.IMAGE_UNIT_PATH = unit
        calls = []
        self.ck.urllib.request.urlopen = lambda req, timeout=None: calls.append(req.full_url)
        with self.ck.LIFE_LOCK:
            self.ck.LIFE["states"] = {"qwen38-image.service": "stopped"}
        self.ck.image_status()
        self.assertEqual(calls, [], "image_status sent a request of its own")

    def test_before_the_first_lifecycle_tick_the_state_is_unknown_not_stopped(self):
        unit = self.tmp / "qwen38-image.service"
        unit.write_text("[Service]\nExecStart=/x/sglang serve --model-path Q/M --port 30020\n")
        self.ck.IMAGE_UNIT_PATH = unit
        with self.ck.LIFE_LOCK:
            self.ck.LIFE["states"] = {}
        out = self.ck.image_status()
        self.assertEqual(out["state"], "unknown")
        self.assertFalse(out["available"])

class OneAtATime(Base):
    """The diffusion scheduler has no admission cap. Measured on the reference box on
    2026-09-22: one generation holds 31.2 GB and stays there across eight of them, two at
    once held 90.5 GB of its 121.6, and the engine then stopped answering and had to be
    restarted. Two browser tabs are enough to do that, so the refusal lives here rather
    than in the page."""

    PNG = "data:image/png;base64,iVBORw0KGgo="

    def test_a_second_request_is_refused_while_one_is_running(self):
        import threading
        started, release = threading.Event(), threading.Event()
        second = {}

        def slow(req, timeout=None):
            started.set()
            release.wait(10)
            raise self.ck.urllib.error.HTTPError(req.full_url, 500, "x", {}, None)

        self.ck.urllib.request.urlopen = slow
        first = threading.Thread(target=lambda: self.call({"prompt": "x"}))
        first.start()
        self.assertTrue(started.wait(5), "the first request never reached the engine")
        second["code"], second["out"] = self.call({"prompt": "y"})
        release.set(); first.join(10)
        self.assertEqual(second["code"], 409)
        self.assertIn("one image at a time", second["out"]["error"])

    def test_the_lock_is_given_back_when_a_request_fails(self):
        """A lane that refuses forever after one error is worse than one that overlaps."""
        def boom(req, timeout=None):
            raise ConnectionRefusedError("nothing listening")

        self.ck.urllib.request.urlopen = boom
        self.assertEqual(self.call({"prompt": "x"})[0], 502)
        self.assertEqual(self.call({"prompt": "x"})[0], 502)   # not 409
        self.assertFalse(self.ck.IMAGE_LOCK.locked())

    def test_the_lock_is_given_back_when_a_request_is_refused_by_shape(self):
        self.assertEqual(self.call({"prompt": "x", "width": 1000})[0], 400)
        self.assertFalse(self.ck.IMAGE_LOCK.locked())

    def _timeout_then(self, lives, ended):
        """A call that times out here while the lane goes on, with the lane's invocation and
        the journal's end marker driven by the test."""
        import socket
        saved = (self.ck._image_life, self.ck._image_request_ended_since, self.ck.IMAGE_END_POLL_S)
        self.addCleanup(lambda: (setattr(self.ck, "_image_life", saved[0]),
                                 setattr(self.ck, "_image_request_ended_since", saved[1]),
                                 setattr(self.ck, "IMAGE_END_POLL_S", saved[2])))

        def slow(req, timeout=None):
            raise socket.timeout("timed out")
        self.ck.urllib.request.urlopen = slow
        self.ck._image_life = lambda: lives[0]
        self.ck._image_request_ended_since = lambda invocation, since: ended[0]
        self.ck.IMAGE_END_POLL_S = 0.02

    def _released(self):
        import time
        for _ in range(200):
            if not self.ck.IMAGE_LOCK.locked():
                return True
            time.sleep(0.01)
        return False

    def test_a_request_that_timed_out_keeps_the_lane_busy_until_it_ends(self):
        """The lane has no abort: a call this process gave up on after IMAGE_TIMEOUT is still
        generating there, and releasing the lock then admitted a second generation beside it
        (two at once held 90.5 GB and stopped the engine; found in review, 2026-09-24). The
        lock now stays held until this invocation's journal shows a request end."""
        lives, ended = [("active", "run-1")], [False]
        self._timeout_then(lives, ended)
        code, out = self.call({"prompt": "x", "width": 512, "height": 512})
        self.assertEqual(code, 504, out)
        self.assertIn("still generating", out["error"])
        self.assertNotIn("start it", out["error"], "it is serving: no advice to start it")
        self.assertEqual(self.call({"prompt": "y", "width": 512, "height": 512})[0], 409)
        ended[0] = True
        self.assertTrue(self._released(), "released once the journal shows the end")

    def test_a_lane_that_restarts_while_the_lock_is_held_gives_it_back(self):
        lives, ended = [("active", "run-1")], [False]
        self._timeout_then(lives, ended)
        self.assertEqual(self.call({"prompt": "x", "width": 512, "height": 512})[0], 504)
        lives[0] = ("active", "run-2")                   # Cancel, a Stop and a Start, a crash
        self.assertTrue(self._released(), "a new invocation is not generating the old request")


class ThePixelBudget(Base):
    """The images of one call run as one batch: ten 2048x2048 images took 42 s per step on
    2026-09-23, ten times one image's 4.6, and nobody has measured where that batch's
    memory ends. On unified memory running out hangs the machine, so a call may not ask
    for more pixels, all its images together, than the largest call measured here: one
    2752x1536 image, 44.8 GB at its peak. Nothing past it reaches the lane."""

    def ask(self, **fields):
        return self.call({"prompt": "p", "output_format": "png", **fields})

    def test_the_call_that_started_this_is_refused_before_the_lane_sees_it(self):
        code, out = self.ask(width=2048, height=2048, n=10, num_inference_steps=60)
        self.assertEqual(code, 400)
        self.assertIn("41.9 megapixels", out["error"])
        self.assertIn("one 2752x1536 image, 44.8 GB", out["error"])
        self.assertIsNone(self.spy.body, "a refused call reached the lane")

    def test_the_largest_measured_call_still_goes_through(self):
        code, _ = self.ask(width=2752, height=1536, n=1)
        self.assertEqual(code, 200)
        self.assertIsNotNone(self.spy.body)

    def test_the_budget_is_the_total_of_the_call_not_the_size_of_one_image(self):
        self.assertEqual(self.ask(width=1024, height=1024, n=4)[0], 200)     # 4.2 MP, under
        self.spy.body = None
        self.assertEqual(self.ask(width=2048, height=2048, n=2)[0], 400)     # 8.4 MP, over
        self.assertIsNone(self.spy.body)

    def test_a_size_string_counts_too(self):
        code, out = self.ask(size="2048x2048", n=2)
        self.assertEqual(code, 400, out)

    def test_each_axis_is_read_the_way_the_lane_reads_it(self):
        """build_sampling_params in the lane's runtime: an explicit width or height first,
        axis by axis, then `size` lower-cased with its spaces dropped, then the pipeline's
        1024 default. The budget read `size` only when both axes were missing, and matched
        it case-sensitively, so these reached the lane (found in review, 2026-09-24)."""
        for fields in ({"width": 16384, "n": 4},                       # height 1024: 67 MP
                       {"width": 16384, "size": "32x4096", "n": 10},   # 16384 x 4096: 671 MP
                       {"size": "2048X2048", "n": 4},                  # 16.8 MP
                       {"size": "123456x123456"},                      # 15 GP
                       {"height": 8192, "size": "4096x32"}):           # 4096 x 8192
            with self.subTest(fields=fields):
                self.spy.body = None
                code, out = self.ask(**fields)
                self.assertEqual(code, 400, out)
                self.assertIsNone(self.spy.body, "a refused call reached the lane")

    def test_a_size_the_lane_would_refuse_or_500_on_is_refused_here(self):
        for size in ("big", "512x", "0x512", "1000x1000", "1024x1000"):
            with self.subTest(size=size):
                self.spy.body = None
                self.assertEqual(self.ask(size=size)[0], 400)
                self.assertIsNone(self.spy.body)
        self.assertEqual(self.ask(size=" 1024 X 768 ")[0], 200, "the lane reads this as 1024x768")

    def test_steps_are_a_number_from_1_to_100(self):
        """The page offers 1 to 100 and the server capped nothing: one call with a huge
        count outlived the 1800 s timeout and, with it, the lock."""
        for bad in (0, 101, -5, "30", 2.5, True):
            with self.subTest(steps=bad):
                self.assertEqual(self.ask(width=512, height=512, num_inference_steps=bad)[0], 400)
        for good in (1, 100):
            self.assertEqual(self.ask(width=512, height=512, num_inference_steps=good)[0], 200)

    def test_the_count_is_a_number_from_1_to_10(self):
        for bad in (0, 11, -1, "3", 2.0, True, [2]):
            code, out = self.ask(width=512, height=512, n=bad)
            self.assertEqual(code, 400, (bad, out))

    def test_the_page_and_the_server_hold_the_same_number(self):
        js = (REPO / "dashboard" / "static" / "app.js").read_text()
        self.assertEqual(self.ck.IMAGE_MAX_PIXELS, 2752 * 1536)
        self.assertIn("const IMG_MAX_PIXELS = 2752 * 1536;", js)
        problem = js[js.index("function imgProblem(){"):js.index("function imgEstimate(")]
        self.assertIn("n * w * h > IMG_MAX_PIXELS", problem)


class ARequestCutByAStopSaysSo(Base):
    """A Stop or a restart cancels the request in flight after 5 s (the shutdown patch), and
    uvicorn answers that with a bare HTTP 500, which the page showed as "Refused with HTTP
    500" unless the page itself had sent the stop. Whoever sent it, the cockpit checks, on
    the error path only, whether the lane is still the one the request started on."""

    def fail_with(self, exc):
        def boom(req, timeout=None):
            raise exc
        self.ck.urllib.request.urlopen = boom

    def lives(self, *seq):
        it = iter(seq)
        self.ck._image_life = lambda: next(it)

    def test_a_500_from_a_lane_that_changed_is_an_interruption(self):
        self.fail_with(self.ck.urllib.error.HTTPError("http://x", 500, "Internal Server Error", {}, None))
        self.lives(("active", "run-1"), ("deactivating", "run-1"))
        code, out = self.call({"prompt": "p", "width": 512, "height": 512})
        self.assertEqual((code, out.get("interrupted")), (503, True))
        self.assertIn("stopped or restarted", out["error"])

    def test_a_500_from_the_same_lane_is_the_engine_refusing(self):
        self.fail_with(self.ck.urllib.error.HTTPError("http://x", 500, "Internal Server Error", {}, None))
        self.lives(("active", "run-1"), ("active", "run-1"))
        code, out = self.call({"prompt": "p", "width": 512, "height": 512})
        self.assertEqual(code, 500)
        self.assertNotIn("interrupted", out)

    def test_a_dropped_connection_across_a_restart_is_an_interruption(self):
        self.fail_with(ConnectionResetError("reset"))
        self.lives(("active", "run-1"), ("active", "run-2"))          # a restart: a new invocation
        code, out = self.call({"prompt": "p", "width": 512, "height": 512})
        self.assertEqual((code, out.get("interrupted")), (503, True))

    def test_a_lane_that_was_never_there_still_says_to_start_it(self):
        self.fail_with(ConnectionRefusedError("refused"))
        self.lives(("inactive", ""), ("inactive", ""))
        code, out = self.call({"prompt": "p", "width": 512, "height": 512})
        self.assertEqual(code, 502)
        self.assertIn("start it", out["error"])


class TheAgentLimitsFollowABoot(Base):
    """A switch writes the target's nominal opencode pair (900,000 on the 1M lanes), since
    the pool is only known once the engine is up, and nothing fitted it after a switch
    and a Start from the page: the Agent tab asked for more than the 880,417-token pool
    it got on 2026-09-23 until someone pressed the button. The cockpit now does it once
    the engine is ready, once per activation, and never to raise a number."""
    U = "qwen38-sglang.service"

    def setUp(self):
        super().setUp()
        self.calls = []
        self.ck.start_action = lambda name, params, origin="ui": (self.calls.append((name, origin)) or (202, {}))
        self.ck.AUTOFIT_DONE.clear()
        with self.ck.LIFE_LOCK:
            self.ck.LIFE["enter"] = {self.U: "100"}

    def fit(self, ctx=700_000, out=200_000, pool=880_417, ok=False):
        return {"pool": pool, "context": ctx, "output": out, "worst": ctx + out, "ok": ok}

    def test_a_nominal_pair_over_the_pool_is_fitted_once_per_boot(self):
        ready = {self.U: "ready"}
        self.assertEqual(self.ck.maybe_autofit(self.fit(), ready, 0), "started")
        self.assertEqual(self.ck.maybe_autofit(self.fit(), ready, 0), "already handled in this activation")
        self.assertEqual(self.calls, [("fit_opencode", "autofit")])
        with self.ck.LIFE_LOCK:
            self.ck.LIFE["enter"] = {self.U: "200"}                   # the next boot
        self.assertEqual(self.ck.maybe_autofit(self.fit(), ready, 0), "started")
        self.assertEqual(len(self.calls), 2)

    def test_limits_that_fit_are_left_alone(self):
        self.assertEqual(self.ck.maybe_autofit(self.fit(ok=True), {self.U: "ready"}, 0), "not needed")
        self.assertEqual(self.calls, [])

    def test_nothing_is_fitted_while_no_text_engine_is_ready(self):
        states = {self.U: "capturing-graphs", "qwen38-image.service": "ready"}
        self.assertEqual(self.ck.maybe_autofit(self.fit(), states, 0), "no text engine is ready")
        self.assertEqual(self.calls, [])

    def test_a_number_somebody_set_lower_is_never_raised(self):
        # a context below what the pool fits, with an output that makes the pair too big
        out = self.ck.maybe_autofit(self.fit(ctx=300_000, out=600_000), {self.U: "ready"}, 0)
        self.assertEqual(out, "left alone: the fit would raise a declared limit")
        self.assertEqual(self.calls, [])

    def test_a_flash_boot_is_fitted_to_its_window_and_its_own_pair(self):
        """A big flash pool used to fit to 225,000/116,000: an output above the declared
        32,000, so the rule above left the lane on limits its engine refuses. The fit now
        holds the 262,144 window and never goes past the lane's own pair."""
        fit = {"pool": 564_352, "context": 225_000, "output": 32_000, "worst": 257_000, "ok": False,
               "window": 262_144, "served": "qwen3.8-flash-next"}
        states = {"qwen38-flash.service": "ready"}
        with self.ck.LIFE_LOCK:
            self.ck.LIFE["enter"] = {"qwen38-flash.service": "100"}
        self.assertEqual(self.ck.maybe_autofit(fit, states, 250_000), "started")
        self.assertEqual(self.calls, [("fit_opencode", "autofit")])

    def test_a_busy_job_lock_is_retried_on_the_next_tick(self):
        self.ck.start_action = lambda name, params, origin="ui": (409, {"error": "busy"})
        self.assertIn("not started yet", self.ck.maybe_autofit(self.fit(), {self.U: "ready"}, 0))
        self.assertNotIn(self.U, self.ck.AUTOFIT_DONE)


class TheImageLaneStateHoldsWhileItServes(Base):
    """A lane that has served in this activation does not go back to "starting" on one
    slow probe: its boot lines leave the 300-line journal tail within minutes (the
    cockpit's own /health probe writes one every 2 s), and a tail without them used to
    parse as stage None, which derives starting. Measured shape, not a guess: one probe
    over its 3 s timeout during a 190 s 2048px generation is enough."""
    U = "qwen38-image.service"

    def state(self, healthy, enter="100", prev="ready"):
        self.ck.image_healthy = lambda: healthy
        self.ck._image_journal = lambda invocation: ["[x] INFO: GET /health 200 OK"] * 300
        st, boot, running = self.ck.image_engine_state(self.U, active="active", sub="running",
                                                       prev_state=prev, enter_key=enter)
        return st["state"]

    def setUp(self):
        super().setUp()
        self.ck.UNHEALTHY_TICKS.pop(self.U, None)
        self.ck.IMAGE_READY_ENTER.pop(self.U, None)

    def test_one_missed_probe_keeps_a_serving_lane_ready(self):
        self.assertEqual(self.state(True), "ready")
        self.assertEqual(self.state(False), "ready")
        self.assertEqual(self.state(False), "ready")

    def test_three_misses_in_a_row_make_it_degraded_not_starting(self):
        self.assertEqual(self.state(True), "ready")
        for _ in range(2):
            self.state(False)
        self.assertEqual(self.state(False), "degraded")

    def test_a_restart_is_a_new_life_at_once(self):
        """Keyed on the activation: a restart inside one 2 s tick must not read as three
        more ticks of the old life's "ready"."""
        self.assertEqual(self.state(True, enter="100"), "ready")
        self.assertEqual(self.state(False, enter="200", prev="ready"), "starting")

    def test_a_lane_that_never_served_is_read_from_its_boot_log(self):
        self.ck.image_healthy = lambda: False
        self.ck._image_journal = lambda invocation: ["[09-22 17:20:37] Starting server...",
                                                    "[09-22 17:20:44] Loading pipeline modules...",
                                                    "... Loading transformer from /x"]
        st, boot, _ = self.ck.image_engine_state(self.U, active="active", sub="running",
                                                 prev_state="starting", enter_key="300")
        self.assertEqual(st["state"], "loading-weights")
        self.assertIn("DiT", boot["detail"])


class TheJournalIsThisRunsOnly(Base):
    """A new process takes about 8 s to print its first line, and until it does the last
    "Starting server" in the unit's journal is the previous run's, which had reached
    "fired up". Measured on the reference box: a lane started at 21:33:18 read "warming
    up, ready" until 21:33:26. So the journal is read by systemd invocation."""
    U = "qwen38-image.service"
    OLD_RUN = "\n".join(["[09-22 21:19:44] Starting server...",
                         "[09-22 21:20:52] The server is fired up and ready to roll!"])

    def setUp(self):
        super().setUp()
        self.ck.UNHEALTHY_TICKS.pop(self.U, None)
        self.ck.IMAGE_READY_ENTER.pop(self.U, None)
        self.argvs = []

        def fake_run(argv, **kw):
            self.argvs.append(argv)
            if argv[0] != "journalctl":
                return ""
            # the unit's journal holds the previous run; the new invocation has no line yet
            return "" if "_SYSTEMD_INVOCATION_ID=new-run" in argv else self.OLD_RUN

        self.ck.run = fake_run
        self.ck.image_healthy = lambda: False

    def test_a_run_that_has_printed_nothing_yet_is_starting_not_warming_up(self):
        st, boot, _ = self.ck.image_engine_state(self.U, active="active", sub="running",
                                                 prev_state="stopped", enter_key="500",
                                                 invocation="new-run")
        self.assertEqual(st["state"], "starting")
        self.assertFalse(boot["fired_up"])

    def test_the_journal_is_asked_for_the_invocation_not_the_unit(self):
        self.ck._image_journal("abc123")
        asked = [a for a in self.argvs if a[0] == "journalctl"]
        self.assertEqual(len(asked), 1)
        self.assertIn("_SYSTEMD_INVOCATION_ID=abc123", asked[0])
        self.assertNotIn("-u", asked[0])

    def test_no_invocation_reads_nothing(self):
        self.assertEqual(self.ck._image_journal(""), [])
        self.assertEqual([a for a in self.argvs if a[0] == "journalctl"], [])

    def test_a_request_in_flight_is_read_from_the_run_the_lifecycle_saw(self):
        self.ck.IMAGE_INVOCATION[self.U] = "new-run"
        self.ck.image_progress(serving=True)
        asked = [a for a in self.argvs if a[0] == "journalctl"]
        self.assertIn("_SYSTEMD_INVOCATION_ID=new-run", asked[-1])


class TheTextBeltsAreForTextEngines(Base):
    """The canary, the pool guard and the wedge autoheal all talk to ENGINE_BASE, the text
    port. With the image lane serving that port is closed."""

    def test_the_canary_skips_when_only_the_image_lane_is_ready(self):
        with self.ck.LIFE_LOCK:
            self.ck.LIFE["states"] = {"qwen38-image.service": "ready",
                                      "qwen38-sglang.service": "stopped",
                                      "qwen38-flash.service": "stopped"}
        self.ck.DRY_RUN = False
        try:
            out = self.ck.collect_canary()
        finally:
            self.ck.DRY_RUN = True
        data = out.get("data", out)
        self.assertTrue(data.get("skipped"), "the canary probed a text port nothing listens on")
        self.assertIsNone(self.spy.body)

    def test_text_units_exclude_the_image_lane(self):
        self.assertNotIn("qwen38-image.service", self.ck.lc.TEXT_UNITS)
        self.assertIn("qwen38-image.service", self.ck.lc.ENGINE_UNITS)

    def test_the_logs_tab_lists_each_unit_once(self):
        self.assertEqual(len(self.ck.JOURNAL_UNITS), len(set(self.ck.JOURNAL_UNITS)))


class TheBindIsReadNotAssumed(Base):
    """install-image.sh takes IMAGE_BIND. A cockpit that probes 127.0.0.1 regardless
    reads "loading weights" forever on a lane that is serving fine."""

    def test_the_host_comes_from_the_installed_unit(self):
        unit = self.tmp / "qwen38-image.service"
        unit.write_text("[Service]\nExecStart=/x/sglang serve --model-path Q/M \\\n"
                        "  --host 100.114.54.60 --port 31234\n")
        self.ck.IMAGE_UNIT_PATH = unit
        self.assertEqual(self.saved["image_base"](), "http://100.114.54.60:31234")
        self.assertEqual(self.ck.image_status()["host"], "100.114.54.60")

    def test_a_missing_unit_falls_back_to_loopback(self):
        self.ck.IMAGE_UNIT_PATH = Path("/nonexistent/qwen38-image.service")
        self.assertEqual(self.saved["image_base"](), "http://127.0.0.1:30020")


class TheCapOnTheseTwoRoutes(Base):
    def test_ten_references_do_not_fit_in_the_ordinary_post_cap(self):
        self.assertGreater(self.ck.IMAGE_MAX_POST, 10 * 1024 * 1024)

    def test_the_raised_cap_applies_to_the_image_routes_and_nothing_else(self):
        text = (DASH / "cockpit.py").read_text()
        self.assertIn('cap = IMAGE_MAX_POST if raised else 65536', text)
        self.assertIn('raised = path in ("/api/image/edit", "/api/image/generate")', text)

    def test_the_raised_cap_authenticates_before_it_buffers(self):
        """Otherwise an unauthenticated client makes this process hold 40 MB in a thread
        just by declaring a Content-Length, which the 64 KiB cap used to bound."""
        text = (DASH / "cockpit.py").read_text()
        i = text.index("raised = path in (")
        body = text[i:i + 1200]
        self.assertIn("if raised and not self.authed():", body)
        self.assertLess(body.index("if raised and not self.authed():"),
                        body.index("raw = self.rfile.read(length)"))


class WhenTheLaneIsNotThere(Base):
    def test_a_lane_that_does_not_answer_says_how_to_start_it(self):
        def refuse(req, timeout=None):
            raise ConnectionRefusedError("nothing listening")

        self.ck.urllib.request.urlopen = refuse
        code, out = self.call({"prompt": "x"})
        self.assertEqual(code, 502)
        self.assertIn("Switch to Qwen-Image 2.1", out["error"])

    def test_an_engine_refusal_is_relayed_with_its_own_status(self):
        def refuse(req, timeout=None):
            raise self.ck.urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

        self.ck.urllib.request.urlopen = refuse
        code, out = self.call({"prompt": "x"})
        self.assertEqual(code, 500)
        self.assertIn("refused", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
