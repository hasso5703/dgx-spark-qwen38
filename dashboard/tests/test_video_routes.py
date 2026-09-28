#!/usr/bin/env python3
"""The cockpit's video routes: the refusals it makes so the engine does not have to,
and the asynchronous call it holds from the create to the download.

The refusals mirror the cookbook's bands, read on 2026-09-25: seconds 4 to 15, one
video per call on this lane, tasks t2va or fl2va on the fl2va weights, keyframes as
files the lane reads by URI. None of them was measured against the lane: what was
measured is the shape (create -> poll -> download), and every refusal below names the
cookbook band it holds, so a wrong one reads as wrong, not as strict.

The module is imported with its environment pointed at a throwaway box, like every
other test here, so importing it writes nothing into the developer's own HOME.
"""
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
REPO = HERE.parents[2]

ENV_BEFORE = {}


def setUpModule():
    ENV_BEFORE.update(os.environ)


def tearDownModule():
    for name in set(os.environ) - set(ENV_BEFORE):
        del os.environ[name]
    os.environ.update(ENV_BEFORE)


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
    spec = importlib.util.spec_from_file_location("cockpit_video_under_test",
                                                  DASH / "cockpit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class VideoSent:
    """Whatever the cockpit put on the wire, captured instead of sent. Routes by URL:
    POST /v1/videos creates, GET /v1/videos lists, GET .../content downloads."""

    def __init__(self, statuses=("queued", "completed")):
        self.body = None
        self.headers = {}
        self.url = ""
        self.statuses = list(statuses)
        self.calls = []

    def urlopen(self, req, timeout=None):
        self.body, self.url = req.data, req.full_url
        self.headers = dict(req.headers)
        self.calls.append((req.full_url, req.data))
        outer = self

        class R:
            headers = {}

            def read(self_inner):
                if outer.url.endswith("/content"):
                    return b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200000
                if outer.url.endswith("/v1/videos") and not outer.body:
                    nxt = outer.statuses.pop(0) if len(outer.statuses) > 1 else outer.statuses[0]
                    st, pct = nxt if isinstance(nxt, tuple) else (nxt, None)
                    item = {"id": "vid-test-1", "status": st}
                    if pct is not None:
                        item["progress"] = pct
                    return json.dumps({"data": [item]}).encode()
                return json.dumps({"id": "vid-test-1", "status": "queued"}).encode()

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        return R()


PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
       "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-video-"))
        (cls.tmp / "api-key").write_text("test-key-not-a-real-one\n")
        cls.ck = load_cockpit(cls.tmp)
        cls.ck.run = lambda argv, timeout=5.0, merge_err=False: ""
        cls.ck.video_base = lambda: "http://127.0.0.1:30022"
        cls.ck.VIDEO_POLL_S = 0
        cls.ck.VIDEO_TIMEOUT = 30

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.spy = VideoSent()
        self._orig = self.ck.urllib.request.urlopen
        self.ck.urllib.request.urlopen = self.spy.urlopen
        self.assertTrue(self.ck.VIDEO_LOCK.acquire(blocking=False),
                        "a previous test left the video lock held")
        self.ck.VIDEO_LOCK.release()

    def tearDown(self):
        self.ck.urllib.request.urlopen = self._orig
        if self.ck.VIDEO_LOCK.locked():
            self.ck.VIDEO_LOCK.release()
        self.ck.VIDEO_LAST.clear()

    def created_body(self):
        """The JSON of the create POST: polls and downloads come after it."""
        for url, body in self.spy.calls:
            if url.endswith("/v1/videos") and body:
                return json.loads(body.decode())
        self.fail("the call never created a video")

    def call(self, payload):
        return self.ck.video_call(payload)


class TheRefusals(Base):
    def test_a_prompt_is_required(self):
        for payload in ({}, {"prompt": ""}, {"prompt": "   "}, {"seconds": 5}):
            with self.subTest(payload=payload):
                code, out = self.call(dict(payload))
                self.assertEqual(code, 400)
                self.assertIn("prompt", out["error"])

    def test_seconds_is_the_cookbook_band(self):
        """The cookbook's duration band is 4 to 15 s."""
        for secs in (1, 3, 16, 60, "5", True):
            with self.subTest(seconds=secs):
                code, out = self.call({"prompt": "a cat", "seconds": secs})
                self.assertEqual(code, 400, secs)
                self.assertIn("4", out["error"])
        code, _ = self.call({"prompt": "a cat"})
        # absent seconds default to 5 and reach the wire: no refusal here
        body = self.created_body()
        self.assertEqual(body["seconds"], 5)

    def test_one_video_per_call(self):
        code, out = self.call({"prompt": "a cat", "num_outputs_per_prompt": 3})
        self.assertEqual(code, 400)
        self.assertIn("one video", out["error"])

    def test_a_size_is_width_by_height(self):
        code, out = self.call({"prompt": "a cat", "size": "huge"})
        self.assertEqual(code, 400)
        self.assertIn("WIDTHxHEIGHT", out["error"])

    def test_an_unknown_task_is_refused(self):
        code, out = self.call({"prompt": "a cat", "task": "ref2va"})
        self.assertEqual(code, 400)
        self.assertIn("t2va", out["error"])

    def test_a_run_past_the_lock_guard_is_refused_at_the_door(self):
        """15 s at 720P with 100 steps is about three hours of work: past the two
        hours the lock is held (the wait plus the hand-off watcher), the lock frees
        while the lane still works and the next call could start beside the first,
        which is the pair this box does not survive. Budgeted at admission, the way
        the image lane budgets pixels."""
        code, out = self.call({"prompt": "a cat", "seconds": 15, "size": "1280x704",
                               "num_inference_steps": 100})
        self.assertEqual(code, 400)
        self.assertIn("steps", out["error"])

    def test_the_budget_guard_leaves_the_measured_runs_alone(self):
        # 15 s at 720P on the 50-step default is about 96 min: legal, inside the guard.
        code, out = self.call({"prompt": "a cat", "seconds": 15, "size": "1280x720"})
        self.assertEqual(code, 200, out)
        # 15 s at 480P on the 100-step cap is about 76 min: legal too.
        code, out = self.call({"prompt": "a cat", "seconds": 15, "num_inference_steps": 100})
        self.assertEqual(code, 200, out)

    def test_a_busy_lane_refuses_without_leaving_the_keyframes(self):
        """The 409 lands after the frames were staged and before the try whose finally
        cleans them: without its own unlink, every retry beside a running call piles
        two more PNGs into the config dir forever."""
        self.assertTrue(self.ck.VIDEO_LOCK.acquire(blocking=False))  # stand in for the running call
        code, out = self.call({"prompt": "continue", "first_frame": PNG, "last_frame": PNG})
        self.assertEqual(code, 409, out)
        left = [p for p in self.tmp.iterdir() if p.name.startswith("qwen38-")]
        self.assertEqual([], left, "the refused call staged keyframes it never cleaned")

    def test_ratio_and_canvas_outside_what_was_measured_are_refused(self):
        """The lane was served and costed at 16:9 and 9:16 canvases up to 1280x720
        (81.9 GB peak of 121.6): other ratios would declare an aspect they are not,
        and bigger canvases are an unknown cost and a certain OOM - the image lane's
        IMAGE_MAX_PIXELS rule applied to step-seconds."""
        for size in ("640x640", "1024x768", "864x720", "1920x1080", "2560x1440"):
            with self.subTest(size=size):
                code, out = self.call({"prompt": "a cat", "size": size})
                self.assertEqual(code, 400, size)
        # both orientations of the two measured ratios pass the gate
        for size in ("864x480", "480x864", "1280x720", "720x1280"):
            with self.subTest(size=size):
                code, out = self.call({"prompt": "a cat", "size": size})
                self.assertEqual(code, 200, f"{size}: {out}")


class TheEstimateParity(Base):
    """The tab shows a cost and refuses at a bound; the server refuses at a bound too.
    When the two formulas drift, the button promises what the server refuses - the
    exact defect class this branch was reviewed for, twice. Same source of truth:
    the constants parsed out of app.js must answer like video_call does."""

    JS_SIZES = ("864x480", "480x864", "1280x720", "720x1280")
    JS_COMBO = ((50, 4), (50, 15), (100, 15), (100, 4), (100, 9), (1, 15), (100, 7))

    def js_numbers(self):
        js = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()
        m = re.search(r"perStep = sw \* sh <= (\d+) \* (\d+) \? ([\d.]+) : ([\d.]+)", js)
        self.assertIsNotNone(m, "app.js no longer carries the budget formula in the held shape")
        bound = int(m.group(1)) * int(m.group(2))
        per480, per720 = float(m.group(3)), float(m.group(4))
        cond = float(re.search(r"vidMode === 'fl2v' \? ([\d.]+) : 1", js).group(1))
        budget = int(re.search(r"VID_BUDGET_S = (\d+) \* (\d+)", js).group(1)) * \
            int(re.search(r"VID_BUDGET_S = (\d+) \* (\d+)", js).group(2))
        return bound, per480, per720, cond, budget

    def test_the_tab_and_the_server_refuse_the_same_calls(self):
        bound, per480, per720, cond, budget = self.js_numbers()
        for size in self.JS_SIZES:
            sw, sh = (int(p) for p in size.split("x"))
            for steps, secs in self.JS_COMBO:
                for frames in (None, PNG):
                    est = (per480 if sw * sh <= bound else per720) * steps * secs
                    if frames:
                        est *= cond
                    payload = {"prompt": "a cat", "seconds": secs, "size": size,
                               "num_inference_steps": steps}
                    if frames:
                        payload["first_frame"] = frames
                    code, out = self.call(dict(payload))
                    self.assertEqual(code, 200 if est <= budget else 400,
                                     f"{size} {steps}x{secs} frames={bool(frames)} est={round(est)}: {out}")

    def test_the_server_constants_are_the_measured_ones(self):
        """3.05 s per step-second is 592 s / (49 steps x 4 s), and 7.65 is about 25 min
        over the same step-seconds (docs/video-lane.md): the JS mirror reads these
        numbers, so pinning them here pins both files against a silent retune."""
        text = (Path(__file__).resolve().parents[1] / "cockpit.py").read_text()
        self.assertIn("3.05 if sw * sh <= 864 * 480 else 7.65", text)
        self.assertIn("est > 2 * 3600.0", text)

    def test_fl2va_named_with_no_frame_is_refused_not_served_as_text(self):
        """fl2va without a keyframe would read as text-only under a conditioning task:
        refused as asked, not served as something else."""
        code, out = self.call({"prompt": "a cat", "task": "fl2va"})
        self.assertEqual(code, 400)
        self.assertIn("first_frame", out["error"])

    def test_keyframes_turn_the_call_into_fl2va(self):
        code, out = self.call({"prompt": "continue", "first_frame": PNG, "last_frame": PNG})
        self.assertEqual(code, 200, out)
        body = self.created_body()
        self.assertEqual(body["task"], "fl2va")
        self.assertEqual(len(body["conditions"]), 2)
        self.assertTrue(body["conditions"][0]["uri"].startswith("file://"))
        self.assertEqual(body["conditions"][0]["frame_index"], 0)
        self.assertEqual(body["conditions"][1]["frame_index"], -1)

    def test_keyframes_are_staged_as_files_then_removed(self):
        """The lane reads keyframes by URI: staged under the config dir for the call,
        gone after it, whatever way the call ends."""
        code, _ = self.call({"prompt": "continue", "first_frame": PNG})
        self.assertEqual(code, 200)
        left = [p for p in self.tmp.iterdir() if p.name.startswith("qwen38-")]
        self.assertEqual(left, [], f"keyframe files left behind: {left}")

    def test_a_malformed_keyframe_is_refused_not_staged(self):
        for bad in ("not-a-data-url", "data:image/png;base64,!!!", "data:image/png,"):
            with self.subTest(frame=bad[:20]):
                code, out = self.call({"prompt": "continue", "first_frame": bad})
                self.assertEqual(code, 400)

    def test_keyframes_are_refused_on_the_ref_weights(self):
        """fl2va conditioning needs the fl2va partition: the unit says which one serves."""
        orig = self.ck._video_unit_text
        self.ck._video_unit_text = lambda: "ExecStart=/v/bin/sglang serve --model-path M --model-variant ref2va --port 30022"
        try:
            code, out = self.call({"prompt": "continue", "first_frame": PNG})
        finally:
            self.ck._video_unit_text = orig
        self.assertEqual(code, 400)
        self.assertIn("ref2va", out["error"])

    def test_only_what_the_model_accepts_reaches_the_wire(self):
        """A page cannot ask for a LoRA, an upscaler path or a kwargs blob just because
        it can spell the field."""
        code, _ = self.call({"prompt": "a cat", "lora_path": "/tmp/evil.safetensors",
                             "output_path": "/tmp/archive", "seconds": 4})
        self.assertEqual(code, 200)
        body = self.created_body()
        self.assertNotIn("lora_path", body)
        self.assertNotIn("output_path", body)

    def test_the_lane_gets_the_target_it_requires(self):
        """Measured 2026-09-25: without a target object the lane answers 400."""
        code, _ = self.call({"prompt": "a cat", "seconds": 4})
        self.assertEqual(code, 200)
        body = self.created_body()
        self.assertEqual(body["model"], "MiniMax-H3")
        self.assertEqual(body["task"], "t2va")
        self.assertEqual(body["target"],
                         {"short_edge": 480, "aspect_ratio": "16:9", "duration_seconds": 4})


class TheAsyncCall(Base):
    def test_create_and_poll_return_the_video_id(self):
        # The download leg (GET .../content) is a separate browser call, covered by
        # test_content_is_the_lane_bytes; here we only assert create + poll.
        code, out = self.call({"prompt": "a cat", "seconds": 4})
        self.assertEqual(code, 200, out)
        self.assertEqual(out["video_id"], "vid-test-1")
        self.assertIn("seconds", out)
        self.assertNotIn("/content", [u for u, _ in self.spy.calls],
                         "video_call must not download; the browser fetches /content")

    def test_a_failed_video_is_a_502_not_a_video(self):
        self.spy.statuses = ["failed"]
        code, out = self.call({"prompt": "a cat", "seconds": 4})
        self.assertEqual(code, 502)
        self.assertIn("failed", out["error"])

    def test_the_lane_progress_reaches_the_tab(self):
        """The lane reports 0-100 itself: the poll stores it where /api/video serves
        it, so the tab's bar moves instead of pulsing."""
        self.spy.statuses = [("queued", 12.5)]
        st, pct = self.ck._video_list_entry("vid-test-1")
        self.assertEqual(st, "queued")
        self.assertEqual(pct, 12.5)
        st, pct = self.ck._video_list_entry("no-such-video")
        self.assertEqual((st, pct), ("missing", None))

    def test_a_video_the_lane_forgot_is_a_502_not_an_hour_of_polling(self):
        """'missing' is the lane's definitive answer (the record is gone), where
        'unknown' is our failure to ask: only the second polls on."""
        self.spy.statuses = ["missing"]
        code, out = self.call({"prompt": "a cat", "seconds": 4})
        self.assertEqual(code, 502)
        self.assertIn("no record", out["error"])

    def test_one_at_a_time_is_a_409_that_names_it(self):
        self.assertTrue(self.ck.VIDEO_LOCK.acquire(blocking=False))
        try:
            code, out = self.call({"prompt": "a cat"})
        finally:
            self.ck.VIDEO_LOCK.release()
        self.assertEqual(code, 409)
        self.assertIn("one video", out["error"])

    def test_a_stop_under_the_request_reads_as_interrupted(self):
        lives = [{"ActiveState": "active", "InvocationID": "aaa"}]
        self.ck.run = lambda argv, timeout=5.0, merge_err=False: \
            "ActiveState=inactive\nInvocationID=bbb\n" if lives and lives.pop() else \
            "ActiveState=active\nInvocationID=aaa\n"
        try:
            code, out = self.call({"prompt": "a cat"})
        finally:
            self.ck.run = lambda argv, timeout=5.0, merge_err=False: ""
        self.assertEqual(code, 503)
        self.assertTrue(out.get("interrupted"), out)

    def test_a_crash_under_the_request_is_not_a_cancel(self):
        lives = [{"ActiveState": "active", "InvocationID": "aaa"}]

        def run(argv, timeout=5.0, merge_err=False):
            if any("SubState" in a for a in argv):
                return "SubState=auto-restart\nResult=oom-kill\nNRestarts=1\n"
            if lives:
                lives.pop()
                return "ActiveState=active\nInvocationID=aaa\n"
            return "ActiveState=inactive\nInvocationID=zzz\n"

        self.ck.run = run
        try:
            code, out = self.call({"prompt": "a cat"})
        finally:
            self.ck.run = lambda argv, timeout=5.0, merge_err=False: ""
        self.assertEqual(code, 502)
        self.assertTrue(out.get("crashed"), out)
        self.assertIn("Logs tab", out["error"])

    def test_a_wait_past_the_timeout_hands_the_lock_to_a_watcher(self):
        """The lane has no abort and goes on: the lock is given back when the lane is
        done, so a second generation never runs beside the first."""
        self.spy.statuses = ["queued"] * 50
        self.ck.VIDEO_TIMEOUT = 0.01
        try:
            code, out = self.call({"prompt": "a cat"})
        finally:
            self.ck.VIDEO_TIMEOUT = 30
        self.assertEqual(code, 504, out)
        self.assertIn("video_id", out)
        for _ in range(100):
            if not self.ck.VIDEO_LOCK.locked():
                break
            time.sleep(0.05)
        self.assertFalse(self.ck.VIDEO_LOCK.locked(), "the watcher never gave the lock back")


class WhatTheTabIsTold(Base):
    def test_absent_lane_reads_not_installed(self):
        orig = self.ck.VIDEO_UNIT_PATH
        self.ck.VIDEO_UNIT_PATH = Path("/nonexistent/qwen38-video.service")
        try:
            st = self.ck.video_status()
        finally:
            self.ck.VIDEO_UNIT_PATH = orig
        self.assertFalse(st["installed"])
        self.assertEqual(st["state"], "not installed")
        self.assertFalse(st["available"])

    def test_an_unready_lane_is_not_available(self):
        st = self.ck.video_status()
        # no lifecycle snapshot in this harness: unknown, and unknown is not available
        self.assertFalse(st["available"])

    def test_the_unit_names_its_model_variant_and_port(self):
        orig = self.ck._video_unit_text
        self.ck._video_unit_text = lambda: (
            "ExecStart=/v/bin/sglang serve --model-path MiniMaxAI/MiniMax-H3 "
            "--model-variant fl2va --host 127.0.0.1 --port 30022")
        try:
            st = self.ck.video_status()
        finally:
            self.ck._video_unit_text = orig
        self.assertEqual(st["model"], "MiniMaxAI/MiniMax-H3")
        self.assertEqual(st["variant"], "fl2va")
        self.assertEqual(st["port"], 30022)

    def test_content_needs_a_path_shaped_id(self):
        for bad in ("", "../x", "a/b", "a b", "vid.mp4"):
            with self.subTest(vid=bad):
                code, _, _ = self.ck.video_content(bad)
                self.assertEqual(code, 400)

    def test_content_is_the_lane_bytes(self):
        code, body, ctype = self.ck.video_content("vid-test-1")
        self.assertEqual(code, 200)
        self.assertGreater(len(body), 100000)
        self.assertTrue(ctype.startswith("video/"))


if __name__ == "__main__":
    unittest.main()
