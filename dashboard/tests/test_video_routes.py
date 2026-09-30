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

    def __init__(self, statuses=("completed",)):
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

            def read(self_inner, n=-1):
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
        the image lane budgets pixels. The memory ceiling refuses this call first
        since 2026-09-29, so the ceiling is lifted here to hold the budget on its own."""
        was = self.ck.VIDEO_LARGE_MAX_SECONDS
        self.ck.VIDEO_LARGE_MAX_SECONDS = 15
        try:
            code, out = self.call({"prompt": "a cat", "seconds": 15, "size": "1280x704",
                                   "num_inference_steps": 100})
        finally:
            self.ck.VIDEO_LARGE_MAX_SECONDS = was
        self.assertEqual(code, 400)
        self.assertIn("steps", out["error"])

    def test_the_budget_guard_leaves_the_measured_runs_alone(self):
        # 4 s at 720P on the 50-step default: the measured run, about 25 min
        code, out = self.call({"prompt": "a cat", "seconds": 4, "size": "1280x720"})
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
        # both orientations of the two measured ratios pass the gate, at the 4 s every
        # size was measured at (720P past 4 s is TheMemoryCeilingOfLongVideos's)
        for size in ("864x480", "480x864", "1280x720", "720x1280"):
            with self.subTest(size=size):
                code, out = self.call({"prompt": "a cat", "size": size, "seconds": 4})
                self.assertEqual(code, 200, f"{size}: {out}")


class TheMemoryCeilingOfLongVideos(Base):
    """A video's memory grows far faster than its length: 4 s at 480P peaked at 9.6 GB and
    15 s at 78.3 GB (measured 2026-09-29), and 4 s at 720P at 82 GB of this box's 121.6
    (2026-09-25). The time budget alone admitted 720P up to about 15 s, where the memory
    cannot fit, and on unified memory running out hangs the machine (a power cycle by
    hand). Only what was measured is admitted."""

    def test_past_4_s_at_720p_is_refused_before_anything_is_sent(self):
        for size in ("1280x720", "720x1280", "1280x704"):
            for secs in (5, 8, 15):
                with self.subTest(size=size, secs=secs):
                    code, out = self.call({"prompt": "a cat", "seconds": secs, "size": size})
                    self.assertEqual(code, 400, out)
                    self.assertIn("4 s", out["error"])
                    self.assertIn("hangs", out["error"])
        self.assertEqual(self.spy.calls, [], "a refused call reached the lane")

    def test_the_measured_shapes_are_admitted(self):
        for size, secs in (("1280x720", 4), ("720x1280", 4), ("864x480", 15), ("480x864", 15), ("864x480", 4)):
            with self.subTest(size=size, secs=secs):
                code, out = self.call({"prompt": "a cat", "seconds": secs, "size": size})
                self.assertEqual(code, 200, out)


class TheEstimateParity(Base):
    """The tab shows a cost and refuses at a bound; the server refuses at a bound too.
    When the two formulas drift, the button promises what the server refuses - the
    exact defect class this branch was reviewed for, twice. Same source of truth:
    the constants parsed out of the page's video.js must answer like video_call does."""

    JS_SIZES = ("864x480", "480x864", "1280x720", "720x1280")
    JS_COMBO = ((50, 4), (50, 15), (100, 15), (100, 4), (100, 9), (1, 15), (100, 7))

    def js_numbers(self):
        js = (Path(__file__).resolve().parents[1] / "static" / "js" / "video.js").read_text()
        m = re.search(r"perStep = sw \* sh <= (\d+) \* (\d+) \? ([\d.]+) : ([\d.]+)", js)
        self.assertIsNotNone(m, "video.js no longer carries the budget formula in the held shape")
        bound = int(m.group(1)) * int(m.group(2))
        per480, per720 = float(m.group(3)), float(m.group(4))
        cond = float(re.search(r"(?:vidMode|VS\.mode) === 'fl2v' \? ([\d.]+) : 1", js).group(1))
        budget = int(re.search(r"VID_BUDGET_S = (\d+) \* (\d+)", js).group(1)) * \
            int(re.search(r"VID_BUDGET_S = (\d+) \* (\d+)", js).group(2))
        # the tenth kept in hand: an accepted estimate must leave slack under the
        # lock's deadline, and both files have to keep the same one (review 2026-09-28)
        margin = float(re.search(r"if \(est \* ([\d.]+) > VID_BUDGET_S\)", js).group(1))
        m = re.search(r"const VID_LONG_MAX_PIXELS = (\d+) \* (\d+), VID_LARGE_MAX_SECONDS = (\d+);", js)
        self.assertIsNotNone(m, "video.js no longer carries the memory ceiling in the held shape")
        self.assertEqual(int(m.group(1)) * int(m.group(2)), self.ck.VIDEO_LONG_MAX_PIXELS, "page and server bound different sizes")
        self.assertEqual(int(m.group(3)), self.ck.VIDEO_LARGE_MAX_SECONDS, "page and server cap different lengths")
        srv = (Path(__file__).resolve().parents[1] / "cockpit.py").read_text()
        srv_margin = float(re.search(r"if est \* ([\d.]+) > 2 \* 3600\.0", srv).group(1))
        self.assertEqual(margin, srv_margin, "tab and server keep different slack")
        return bound, per480, per720, cond, budget, margin

    def test_the_tab_and_the_server_refuse_the_same_calls(self):
        bound, per480, per720, cond, budget, margin = self.js_numbers()
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
                    ceiling = sw * sh > self.ck.VIDEO_LONG_MAX_PIXELS and secs > self.ck.VIDEO_LARGE_MAX_SECONDS
                    self.assertEqual(code, 200 if est * margin <= budget and not ceiling else 400,
                                     f"{size} {steps}x{secs} frames={bool(frames)} est={round(est)}: {out}")

    def test_the_server_constants_are_the_measured_ones(self):
        """3.05 s per step-second is 592 s / (50 steps x 4 s) = 2.96 rounded up with
        the encode and decode folded in, and 7.65 rides the one measured 720P run
        (25 min, 7.43) with the same margin (docs/video-lane.md): the JS mirror reads
        these numbers, so pinning them here pins both files against a silent retune."""
        text = (Path(__file__).resolve().parents[1] / "cockpit.py").read_text()
        self.assertIn("3.05 if sw * sh <= 864 * 480 else 7.65", text)
        self.assertIn("est * 1.1 > 2 * 3600.0", text)

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
        self.spy.statuses = ["completed"] + ["queued"] * 50
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


class ASilenceIsNotAnAnswer(Base):
    """run() swallows a systemctl that takes longer than its five seconds as "",
    and two tuples read from silence look like the lane died. The branch's
    concurrency review (2026-09-28) found what that made of a request: a cut, a
    confident "the lane was stopped" over a lane that was only mute, and the lock
    given back beside a job still generating. Silence answers "do not know"."""

    def test_a_mute_systemd_mid_poll_keeps_the_wait_and_the_lock(self):
        silent = self.ck.Ran("")
        silent.ok = False
        seq = iter(["ActiveState=active\nInvocationID=aaa\n", silent,
                    "ActiveState=active\nInvocationID=aaa\n"])
        self.ck.run = lambda argv, timeout=5.0, merge_err=False: next(
            seq, "ActiveState=active\nInvocationID=aaa\n")
        self.addCleanup(setattr, self.ck, "run", lambda argv, timeout=5.0, merge_err=False: "")
        self.spy.statuses = ["completed", "queued", "completed"]
        code, out = self.call({"prompt": "a cat"})
        self.assertEqual(code, 200, out)
        self.assertFalse(self.ck.VIDEO_LOCK.locked())

    def test_a_watcher_blind_from_the_start_waits_on_deaths_not_on_tuples(self):
        """With no baseline read at all, only systemd's own verdicts (inactive,
        failed, a unit that answers as gone) end the wait: an "active" arriving
        after the silence must not read as a different run. Reverting the
        predicate to the bare tuple compare turns this call into a 503 over a
        job the spy still shows queued (the review's mutation proved it)."""
        silent = self.ck.Ran("")
        silent.ok = False
        seq = iter([silent])
        self.ck.run = lambda argv, timeout=5.0, merge_err=False: next(
            seq, "ActiveState=active\nInvocationID=aaa\n")
        self.addCleanup(setattr, self.ck, "run", lambda argv, timeout=5.0, merge_err=False: "")
        self.spy.statuses = ["completed", "queued", "completed"]
        code, out = self.call({"prompt": "a cat"})
        self.assertEqual(code, 200, out)


class TheKeyframesOfAHandOff(Base):
    """The lane reads the file:// keyframes when the job starts, not when the POST
    arrives (the runtime localizes URIs at the encoding stage): a 504 whose frames
    were unlinked with the call hands a still-queued job files that are gone."""

    def test_the_hand_off_parks_the_frames_and_the_watchers_removes_them(self):
        # the same VIDEO_TIMEOUT bounds the watcher: 0.3 s of waiting, so the frame
        # is provably parked when the call answers 504, and provably gone when the
        # watcher's own deadline passes (the spy never answers a terminal status)
        self.spy.statuses = ["completed"] + ["queued"] * 50
        self.ck.VIDEO_TIMEOUT = 0.3
        self.addCleanup(setattr, self.ck, "VIDEO_TIMEOUT", 30)
        code, out = self.call({"prompt": "a cat", "first_frame": PNG})
        self.assertEqual(code, 504, out)
        staged = [p for p in self.tmp.iterdir() if "-frame-" in p.name]
        self.assertEqual(1, len(staged), "the hand-off deleted a keyframe a queued job still names")
        for _ in range(80):
            if not staged[0].exists():
                break
            time.sleep(0.05)
        self.assertFalse(staged[0].exists(), "the watcher never removed the staged keyframe")


class AFullDiskStagesNothing(Base):
    """The keyframes are staged on this box before the lane is asked anything. An
    ENOSPC there used to die outside the call's error paths: no answer on the
    socket, and the half-written PNG left on the disk to be written again by the
    next retry (found in review, 2026-09-28)."""

    def test_enospc_answers_says_so_and_leaves_the_disk_as_it_was(self):
        def boom(*args, **kwargs):
            raise OSError(28, "No space left on device")
        saved = tempfile.mkstemp
        tempfile.mkstemp = boom
        self.addCleanup(setattr, tempfile, "mkstemp", saved)
        code, out = self.call({"prompt": "a cat", "first_frame": PNG})
        self.assertEqual(code, 503, out)
        self.assertIn("stage", out["error"])
        self.assertEqual([], self.spy.calls, "a call that could not stage reached the lane")
        self.assertEqual([], [p for p in self.tmp.iterdir() if "-frame-" in p.name])


class TheBootSweep(Base):
    """Frames staged by a cockpit that died keep their file names until a new
    cockpit, owning no calls, decides they belong to nothing."""

    def test_stale_frames_go_at_boot_and_the_rest_of_the_dir_stays(self):
        # An idle lane holds nothing: declare it, the sweep asks the lane now.
        self.spy.statuses = ["completed"]
        (self.tmp / "qwen38-first-frame-dead.png").write_bytes(b"x")
        (self.tmp / "qwen38-last-frame-dead.png").write_bytes(b"x")
        self.ck._sweep_staged_frames()
        self.assertEqual([], [p for p in self.tmp.iterdir() if "-frame-" in p.name])
        self.assertTrue((self.tmp / "api-key").exists())

    def test_a_live_lane_keeps_its_staged_frames(self):
        """The lane outlives a cockpit restart: sweeping blind would fail a job
        still queued there, so the sweep stands down while the lane shows one
        (found in review, 2026-09-29)."""
        self.spy.statuses = ["processing"]
        (self.tmp / "qwen38-first-frame-live.png").write_bytes(b"x")
        self.ck._sweep_staged_frames()
        self.assertTrue((self.tmp / "qwen38-first-frame-live.png").exists())
        self.spy.statuses = ["completed"]
        self.ck._sweep_staged_frames()
        self.assertFalse((self.tmp / "qwen38-first-frame-live.png").exists())


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

    def test_content_over_the_cap_is_refused_before_the_read(self):
        """A lane answering a gigabyte would park it whole in this process's
        RAM: past the cap it is abuse or corruption, refused with no body read
        (found in review, 2026-09-29)."""
        seen = {}

        class Big:
            headers = {"Content-Length": str(2 ** 30 + 1)}

            def read(self_inner, n=-1):
                seen["read"] = True
                return b"x"

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        orig = self.ck.urllib.request.urlopen
        self.ck.urllib.request.urlopen = lambda req, timeout=None: Big()
        try:
            code, body, _ = self.ck.video_content("vid-test-1")
        finally:
            self.ck.urllib.request.urlopen = orig
        self.assertEqual(code, 413)
        self.assertEqual(body, b"")
        self.assertNotIn("read", seen)

    def test_content_past_the_cap_is_cut_even_undeclared(self):
        orig = self.ck.VIDEO_CONTENT_MAX_BYTES
        self.ck.VIDEO_CONTENT_MAX_BYTES = 10
        try:
            code, body, _ = self.ck.video_content("vid-test-1")
        finally:
            self.ck.VIDEO_CONTENT_MAX_BYTES = orig
        self.assertEqual(code, 413)
        self.assertEqual(body, b"")


class TheLaneIsAskedBeforeItIsBooked(Base):
    """The lock is per-process: it cannot see a generation an older cockpit left
    behind. Admission asks the lane itself, and a 409 names the orphan instead
    of generating beside it (found in review, 2026-09-29)."""

    def test_a_live_lane_refuses_the_new_call(self):
        self.spy.statuses = ["processing", "completed"]
        code, out = self.call({"prompt": "a cat"})
        self.assertEqual(code, 409)
        self.assertIn("already generating", out["error"])
        posts = [u for u, b in self.spy.calls if u.endswith("/v1/videos") and b]
        self.assertEqual(posts, [], "nothing was created on a busy lane")
        self.assertFalse(self.ck.VIDEO_LOCK.locked())
        self.assertEqual([p for p in self.tmp.iterdir() if "-frame-" in p.name], [])

    def test_a_quiet_lane_admits(self):
        self.spy.statuses = ["completed"]
        code, out = self.call({"prompt": "a cat"})
        self.assertEqual(code, 200, out)

    def test_a_down_lane_still_admits_and_fails_its_own_way(self):
        def down(req, timeout=None):
            raise ConnectionError("lane down")
        orig = self.ck.urllib.request.urlopen
        self.ck.urllib.request.urlopen = down
        try:
            code, out = self.call({"prompt": "a cat"})
        finally:
            self.ck.urllib.request.urlopen = orig
        self.assertEqual(code, 502, out)


class ARunningLaneThatSaysNothingIsNotAStoppedOne(Base):
    """During its warm-up the lane holds a POST until the three warm-up requests are done:
    measured 2026-09-29, an 80 s wait and then a 502 telling the person to start the
    lane, which was running. A lane that is up and silent says so; a lane that is down
    still gets the advice to start it."""

    def silent_lane(self, active_state):
        def urlopen(req, timeout=None):
            raise TimeoutError("timed out")
        orig_open, orig_run = self.ck.urllib.request.urlopen, self.ck.run
        self.ck.urllib.request.urlopen = urlopen
        self.ck.run = lambda argv, timeout=5.0, merge_err=False: (
            f"ActiveState={active_state}\nInvocationID=0123456789abcdef\n" if argv[:2] == ["systemctl", "show"] else "")
        try:
            return self.call({"prompt": "a cat"})
        finally:
            self.ck.urllib.request.urlopen, self.ck.run = orig_open, orig_run

    def test_a_running_lane_that_holds_the_call_is_warming_up_or_busy(self):
        code, out = self.silent_lane("active")
        self.assertEqual(code, 503, out)
        self.assertIn("warming up", out["error"])
        self.assertNotIn("start it", out["error"])
        self.assertFalse(self.ck.VIDEO_LOCK.locked())

    def test_a_stopped_lane_is_still_told_to_start(self):
        code, out = self.silent_lane("inactive")
        self.assertEqual(code, 502, out)
        self.assertIn("start it", out["error"])


class TheWatcherKeepsReporting(Base):
    """After a hand-off the parked page reads VIDEO_LAST: the watcher keeps its
    status and progress moving until it clears them, or the badge freezes at
    the hand-off minute (found in review, 2026-09-29)."""

    def test_progress_moves_until_the_end(self):
        import threading
        self.ck.VIDEO_POLL_S = 0.05
        try:
            self.assertTrue(self.ck.VIDEO_LOCK.acquire(blocking=False))
            self.spy.statuses = [("processing", p) for p in range(0, 60, 5)] + ["completed"]
            life0 = self.ck._video_life()
            t = threading.Thread(target=self.ck._release_video_lock_when_done,
                                 args=(life0, "vid-test-1", 30, ()))
            t.start()
            seen = None
            for _ in range(100):
                seen = self.ck.VIDEO_LAST.get("progress")
                if isinstance(seen, (int, float)) and seen >= 10:
                    break
                time.sleep(0.02)
            t.join(timeout=10)
            self.assertFalse(t.is_alive(), "the watcher never came back")
            self.assertTrue(isinstance(seen, (int, float)) and seen >= 10,
                            "progress never moved: the badge would freeze")
            self.assertFalse(self.ck.VIDEO_LOCK.locked())
            self.assertEqual(self.ck.VIDEO_LAST, {})
        finally:
            self.ck.VIDEO_POLL_S = 0


class TheRefusalsGrow(Base):
    """Bounds the code enforces but no test exercised (found in review, 2026-09-29)."""

    def test_a_seed_is_a_whole_number_or_empty(self):
        for bad in ("abc", "-1", True, 4.5):
            with self.subTest(seed=bad):
                code, out = self.call({"prompt": "a cat", "seed": bad})
                self.assertEqual(code, 400, out)
                self.assertIn("seed", out["error"])
        for good in ("42", 0, "007", ""):
            with self.subTest(seed=good):
                self.spy.statuses = ["completed"]
                code, out = self.call({"prompt": "a cat", "seed": good})
                self.assertEqual(code, 200, out)
        bodies = [json.loads(b.decode()) for u, b in self.spy.calls
                  if u.endswith("/v1/videos") and b]
        self.assertNotIn("seed", bodies[-1], "an empty seed rides out omitted, like the page")

    def test_steps_stay_inside_1_to_100(self):
        for bad in (0, 101, -3, "x", True):
            with self.subTest(steps=bad):
                code, out = self.call({"prompt": "a cat", "num_inference_steps": bad})
                self.assertEqual(code, 400, out)
        self.spy.statuses = ["completed"]
        code, out = self.call({"prompt": "a cat", "num_inference_steps": 100})
        self.assertEqual(code, 200, out)

    def test_a_bare_flag_and_a_comment_fall_back(self):
        orig = self.ck._video_unit_text
        try:
            self.ck._video_unit_text = lambda: "ExecStart=/v/bin/sglang serve --port\n"
            self.assertEqual(self.ck._video_unit_flag("--port", "fb"), "fb")
            self.ck._video_unit_text = lambda: ("# default --port 30022\n"
                                                "ExecStart=/v/bin/sglang serve --port 30099")
            self.assertEqual(self.ck._video_unit_flag("--port", "fb"), "30099")
        finally:
            self.ck._video_unit_text = orig

    def test_a_mute_systemd_at_the_cut_reads_as_a_crash(self):
        """systemd answered the life questions and then went mute for the
        verdict: the clean stop the 503 names cannot be proven (review,
        2026-09-29)."""
        lives = [True]

        def run(argv, timeout=5.0, merge_err=False):
            if any("SubState" in a for a in argv):
                r = self.ck.Ran("")
                r.ok = False
                return r
            if lives:
                lives.pop()
                return "ActiveState=active\nInvocationID=aaa\n"
            return "ActiveState=inactive\nInvocationID=zzz\n"

        self.ck.run = run
        try:
            self.spy.statuses = ["completed"]
            code, out = self.call({"prompt": "a cat"})
        finally:
            self.ck.run = lambda argv, timeout=5.0, merge_err=False: ""
        self.assertEqual(code, 502, out)
        self.assertTrue(out.get("crashed"), out)
        self.assertIn("answered nothing", out["error"])


class TheLaneReportsItsOwnHealth(Base):
    """video_engine_state, video_healthy and _video_journal had zero references
    in any test of the repo (found in review, 2026-09-29): the lane's own
    health was the least exercised path of the branch."""

    KEY = "qwen38-video-test-health"

    def tearDown(self):
        for d in (self.ck.VIDEO_INVOCATION, self.ck.UNHEALTHY_TICKS,
                  self.ck.VIDEO_READY_ENTER):
            d.pop(self.KEY, None)
        super().tearDown()

    def test_an_inactive_lane_is_not_running(self):
        st, boot, running = self.ck.video_engine_state(
            self.KEY, active="inactive", sub="dead", prev_state=None, enter_key="1")
        self.assertFalse(running)
        self.assertNotEqual(st["state"], "ready")

    def test_a_healthy_lane_is_ready(self):
        st, boot, running = self.ck.video_engine_state(
            self.KEY, active="active", sub="running", prev_state=None, enter_key="1")
        self.assertTrue(running)
        self.assertEqual(st["state"], "ready", st)
        self.assertEqual(self.ck.VIDEO_READY_ENTER.get(self.KEY), "1")
        self.assertEqual(self.ck.UNHEALTHY_TICKS.get(self.KEY), 0)

    def test_an_empty_journal_counts_strikes(self):
        def fake(req, timeout=None):
            if req.full_url.endswith("/health"):
                raise ConnectionError("lane down")
            return self.spy.urlopen(req, timeout)
        orig = self.ck.urllib.request.urlopen
        self.ck.urllib.request.urlopen = fake
        try:
            self.assertEqual(self.ck._video_journal("abc"), [])
            st, boot, running = self.ck.video_engine_state(
                self.KEY, active="active", sub="running", prev_state=None, enter_key="2")
        finally:
            self.ck.urllib.request.urlopen = orig
        self.assertTrue(running)
        self.assertEqual(self.ck.UNHEALTHY_TICKS.get(self.KEY), 1)
        self.assertNotEqual(st["state"], "ready")



class TheBootBarNeverGoesBackOnTheVideoLane(Base):
    """The cockpit reads the last 300 lines of the run, and its own /health probe writes one
    every 2 s through the ten-minute warm-up: on the reference box on 2026-09-30 FastAPI's
    start sat at line 74 of 395 when the lane went from warming up back to starting. The
    lines below are that boot's, verbatim."""
    KEY = "qwen38-video-test-bootbar"
    BOOT = ["[09-30 18:11:49] Starting server...",
            "[09-30 18:11:55] Loading pipeline modules...",
            "[09-30 18:11:56] Loading MiniMaxH3DiTModel from 13 safetensors file(s) , param_dtype: torch.bfloat16",
            "[09-30 18:12:06] Pipeline instantiated",
            "[09-30 18:12:20] Starting FastAPI server."]
    PROBE = '[2026-09-30 18:16:55] INFO:     127.0.0.1:34130 - "GET /health HTTP/1.1" 503 Service Unavailable'

    def setUp(self):
        super().setUp()
        self.tail, self.head, self.head_reads = [], [], 0

        def run(argv, timeout=5.0, merge_err=False):
            if argv and argv[0] == "journalctl":
                if "-n" in argv:
                    return "\n".join(self.tail)
                self.head_reads += 1
                return "\n".join(self.head)
            return ""

        def down(req, timeout=None):
            raise ConnectionError("503 while it warms up")
        self._run, self._open = self.ck.run, self.ck.urllib.request.urlopen
        self.ck.run, self.ck.urllib.request.urlopen = run, down

    def tearDown(self):
        self.ck.run, self.ck.urllib.request.urlopen = self._run, self._open
        for d in (self.ck.VIDEO_INVOCATION, self.ck.UNHEALTHY_TICKS, self.ck.VIDEO_READY_ENTER,
                  self.ck.VIDEO_BOOT_SEEN, self.ck.VIDEO_HEAD_READ):
            d.pop(self.KEY, None)
        super().tearDown()

    def tick(self, invocation):
        st, boot, running = self.ck.video_engine_state(
            self.KEY, active="active", sub="running", prev_state=None, enter_key="7", invocation=invocation)
        return st["state"]

    def test_the_warm_up_stays_when_its_markers_leave_the_tail(self):
        self.tail = self.BOOT + [self.PROBE] * 20
        self.assertEqual(self.tick("inv-a"), "warming-up")
        self.tail = [self.PROBE] * 300
        self.assertEqual(self.tick("inv-a"), "warming-up")
        self.assertEqual(self.tick("inv-a"), "warming-up")

    def test_a_new_run_starts_from_its_own_lines(self):
        self.tail = self.BOOT + [self.PROBE] * 20
        self.assertEqual(self.tick("inv-a"), "warming-up")
        self.tail, self.head = [self.PROBE] * 300, ["[09-30 19:00:01] Starting server..."]
        self.assertEqual(self.tick("inv-b"), "starting")


    def test_a_run_that_proves_nothing_yet_never_borrows_the_last_runs_stage(self):
        self.tail = self.BOOT + [self.PROBE] * 20
        self.assertEqual(self.tick("inv-a"), "warming-up")
        self.tail, self.head = [self.PROBE] * 300, [self.PROBE] * 300
        self.assertEqual(self.tick("inv-e"), "starting")

    def test_a_cockpit_that_came_up_late_reads_the_start_once(self):
        self.tail, self.head = [self.PROBE] * 300, self.BOOT + [self.PROBE] * 50
        self.assertEqual([self.tick("inv-c") for _ in range(3)], ["warming-up"] * 3)
        self.assertEqual(self.head_reads, 1, "the run's first lines are read once per activation")

    def test_a_run_with_no_marker_yet_is_not_read_again_every_tick(self):
        self.tail, self.head = [self.PROBE] * 300, [self.PROBE] * 300
        self.assertEqual([self.tick("inv-d") for _ in range(3)], ["starting"] * 3)
        self.assertEqual(self.head_reads, 1)

if __name__ == "__main__":
    unittest.main()
