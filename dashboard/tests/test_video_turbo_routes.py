#!/usr/bin/env python3
"""The Video view's Turbo switch, on the cockpit's side: the cookbook's Turbo adapter for
MiniMax-H3 (larryvrh's LoRA, nine steps) put on for a call that asks for it, in SGLang's
dynamic mode, and taken off after, so that the cockpit's next call meets the base.

Measured on the reference box on 2026-10-10 (docs/video-lane.md): on in about 5 s the first
time and 0.02 s after, off in 0.01 s, a 4 s 480P video in 127 s against 623 s for the base at
its 50 steps (the same prompt), and the base's next video identical to the byte to one made
before the adapter went on. Here the lane is a fake that keeps the adapter's state the way the runtime reports
it (/v1/list_loras), and the adapter file sits in a throwaway cache.
"""
import json
import os
import re
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# setUpModule and tearDownModule too: the cockpit is loaded with its environment pointed at a
# throwaway box, and the next module must find the environment this one found
from test_video_routes import Base, VideoSent, REPO, setUpModule, tearDownModule  # noqa: E402,F401

PINS = dict(re.findall(r'^(VIDEO_TURBO_REPO|VIDEO_TURBO_REV|VIDEO_TURBO_FILE)="([^"]+)"$',
                       (REPO / "install-video.sh").read_text(), re.M))


class TurboLane(VideoSent):
    """The video lane's adapter routes on top of VideoSent's: list_loras says what is
    active, set_lora puts one on (dynamic), unmerge_lora_weights takes it off."""

    def __init__(self, active=None, lists=True):
        super().__init__()
        self.active = active          # None, or the list_loras entry of the active adapter
        self.lists = lists
        self.unmerges = True          # False: the unmerge fails, as one stuck behind a queued job
        self.sets = True              # False: set_lora fails
        self.on_create = None         # called as the video is created (the call is under way)

    def urlopen(self, req, timeout=None):
        url = req.full_url
        if url.endswith("/v1/list_loras"):
            self.calls.append((url, None))
            if not self.lists:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return self._answer({"loaded_adapters": [], "active": {"transformer": [self.active]} if self.active else {}})
        if url.endswith("/v1/set_lora"):
            self.calls.append((url, req.data))
            if not self.sets:
                self.lists = False    # and the lane says nothing more after that
                raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None)
            body = json.loads(req.data.decode())
            self.active = {"nickname": body["lora_nickname"], "path": body["lora_path"], "merged": False, "mode": "unmerged"}
            return self._answer({"status": "ok"})
        if url.endswith("/v1/unmerge_lora_weights"):
            self.calls.append((url, req.data))
            if not self.unmerges:
                raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None)
            self.active = None
            return self._answer({"status": "ok"})
        if url.endswith("/v1/videos") and req.data and self.on_create:
            self.on_create()
        return super().urlopen(req, timeout)

    @staticmethod
    def _answer(obj):
        class R:
            headers = {}

            def read(self_inner, n=-1):
                return json.dumps(obj).encode()

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False
        return R()


class TheTurbo(Base):
    def setUp(self):
        super().setUp()
        self.spy = TurboLane()
        self.ck.urllib.request.urlopen = self.spy.urlopen
        hf = self.tmp / "hf"
        self.adapter = (hf / "hub" / ("models--" + PINS["VIDEO_TURBO_REPO"].replace("/", "--")) / "snapshots"
                        / PINS["VIDEO_TURBO_REV"] / PINS["VIDEO_TURBO_FILE"])
        self.adapter.parent.mkdir(parents=True, exist_ok=True)
        self.adapter.write_bytes(b"lora")
        self.unit = self.tmp / "qwen38-video.service"
        self.write_unit("fl2va")
        self._unit_path = self.ck.VIDEO_UNIT_PATH
        self.ck.VIDEO_UNIT_PATH = self.unit
        self.ck.VIDEO_TURBO_LEFT["run"] = None

    def tearDown(self):
        self.ck.VIDEO_UNIT_PATH = self._unit_path
        self.ck.VIDEO_UNIT_CACHE.clear()
        super().tearDown()

    def write_unit(self, variant):
        self.unit.write_text(f"[Service]\nEnvironment=HF_HOME={self.tmp / 'hf'}\n"
                             f"ExecStart=/x/sglang serve --model-path MiniMaxAI/MiniMax-H3 \\\n"
                             f"  --model-variant {variant} --host 127.0.0.1 --port 30022\n")
        self.ck.VIDEO_UNIT_CACHE.clear()

    def urls(self):
        """The lane's routes in the order asked, the video's creation named "create" (the
        GETs of /v1/videos before and after it are the busy check and the polls)."""
        return ["create" if u.endswith("/v1/videos") and b else u.rsplit("/v1/", 1)[-1]
                for u, b in self.spy.calls if "/v1/" in u]

    def test_a_turbo_call_puts_the_adapter_on_then_takes_it_off(self):
        code, out = self.call({"prompt": "a cat", "turbo": True, "seconds": 4})
        self.assertEqual(code, 200, out)
        urls = self.urls()
        create = urls.index("create")
        self.assertEqual(urls[create - 2:create], ["list_loras", "set_lora"], urls)
        self.assertEqual(urls[-2:], ["list_loras", "unmerge_lora_weights"], urls)
        put = json.loads(next(b for u, b in self.spy.calls if u.endswith("/v1/set_lora")).decode())
        self.assertEqual(put, {"lora_nickname": "larry-v4", "lora_path": str(self.adapter),
                               "target": "all", "strength": 1.0, "merge_mode": "dynamic"})
        body = self.created_body()
        self.assertEqual(body["num_inference_steps"], 9)
        self.assertNotIn("turbo", body, "the lane was sent a field it does not have")
        self.assertIsNone(self.spy.active, "the adapter was left on")

    def test_a_base_call_takes_off_what_another_left_on(self):
        self.spy.active = {"nickname": "larry-v4", "merged": False}
        code, out = self.call({"prompt": "a cat", "num_inference_steps": 50})
        self.assertEqual(code, 200, out)
        urls = self.urls()
        create = urls.index("create")
        self.assertEqual(urls[create - 2:create], ["list_loras", "unmerge_lora_weights"], urls)
        self.assertNotIn("set_lora", urls)
        self.assertEqual(self.created_body()["num_inference_steps"], 50)

    def test_a_base_call_on_the_base_changes_nothing(self):
        code, out = self.call({"prompt": "a cat"})
        self.assertEqual(code, 200, out)
        urls = self.urls()
        self.assertEqual(urls[urls.index("create") - 1], "list_loras", urls)
        self.assertNotIn("unmerge_lora_weights", urls)

    def test_a_lane_that_cannot_say_still_serves_the_base(self):
        self.spy.lists = False
        code, out = self.call({"prompt": "a cat"})
        self.assertEqual(code, 200, out)

    def test_a_lane_that_cannot_say_gets_no_turbo_call(self):
        self.spy.lists = False
        code, out = self.call({"prompt": "a cat", "turbo": True, "seconds": 4})
        self.assertEqual(code, 502, out)
        self.assertIn("did not say which adapter", out["error"])
        self.assertNotIn("create", self.urls())
        self.assertFalse(self.ck.VIDEO_LOCK.locked())

    def test_no_adapter_on_the_box_is_said_before_the_lane_is_asked_for_a_video(self):
        self.adapter.unlink()
        code, out = self.call({"prompt": "a cat", "turbo": True, "seconds": 4})
        self.assertEqual(code, 502, out)
        self.assertIn("./install-video.sh fetches it", out["error"])
        self.assertNotIn("create", self.urls())
        self.assertFalse(self.ck.video_status()["turbo"])

    def test_an_adapter_merged_into_the_weights_is_left_alone(self):
        """Taking a merged one out restores 62 GiB of weights in memory, and this cockpit never
        merges one: a lane in that state is restarted, not unmerged from here."""
        self.spy.active = {"nickname": "someone", "merged": True, "mode": "merged"}
        for turbo in (False, True):
            code, out = self.call({"prompt": "a cat", "turbo": turbo, "seconds": 4})
            self.assertEqual(code, 502, out)
            self.assertIn("merged into its weights", out["error"])
        self.assertNotIn("unmerge_lora_weights", self.urls())
        self.assertNotIn("set_lora", self.urls())

    def test_the_turbo_runs_its_own_steps(self):
        code, out = self.call({"prompt": "a cat", "turbo": True, "seconds": 4, "num_inference_steps": 20})
        self.assertEqual(code, 400)
        self.assertIn("9 steps", out["error"])
        code, out = self.call({"prompt": "a cat", "turbo": True, "seconds": 4, "num_inference_steps": 9})
        self.assertEqual(code, 200, out)

    def test_refusals_said_before_anything_is_switched(self):
        for payload, why in (({"turbo": "yes"}, "true or false"),
                             ({"turbo": True, "seconds": 4, "quality": "high"}, "quality"),
                             ({"turbo": True, "size": "1280x720", "seconds": 4}, "480P only"),
                             ({"turbo": True, "seconds": 5}, "4 s only"),
                             ({"turbo": True}, "4 s only")):
            with self.subTest(payload=payload):
                code, out = self.call({"prompt": "a cat", **payload})
                self.assertEqual(code, 400, out)
                self.assertIn(why, out["error"])
        self.assertEqual([u for u in self.urls() if u != "videos"], [], "a refused call switched the lane")

    def test_a_lane_on_the_ref2va_weights_has_no_turbo(self):
        self.write_unit("ref2va")
        code, out = self.call({"prompt": "a cat", "turbo": True, "seconds": 4})
        self.assertEqual(code, 400)
        self.assertIn("FL2VA", out["error"])
        self.assertFalse(self.ck.video_status()["turbo"])

    def test_the_status_offers_it_when_the_adapter_is_here(self):
        status = self.ck.video_status()
        self.assertTrue(status["turbo"])
        self.assertEqual(status["turbo_path"], str(self.adapter), "the view's copied command needs it")

    def a_run(self, life=("active", "run-1")):
        """The lane's run as systemd says it (ActiveState, InvocationID)."""
        before = self.ck._video_life
        self.ck._video_life = lambda: life
        self.addCleanup(setattr, self.ck, "_video_life", before)

    def turbo_left_on(self):
        """A Turbo video whose adapter did not come off after it: the unmerge failed (behind a
        request sent straight to the lane, its list_loras waits out its timeout)."""
        self.a_run()
        self.spy.unmerges = False
        code, out = self.call({"prompt": "a cat", "turbo": True, "seconds": 4})
        self.assertEqual(code, 200, out)
        self.assertIsNotNone(self.spy.active)
        self.spy.calls.clear()

    def test_a_base_call_waits_while_the_lane_cannot_say_the_turbo_is_off(self):
        self.turbo_left_on()
        self.spy.lists = False
        code, out = self.call({"prompt": "a cat"})
        self.assertEqual(code, 502, out)
        self.assertIn("whether the Turbo adapter of an earlier video is off", out["error"])
        self.assertNotIn("create", self.urls(), "the base video went to a lane that may run the Turbo")
        # once the lane answers, the adapter comes off and the base call goes through
        self.spy.lists, self.spy.unmerges = True, True
        code, out = self.call({"prompt": "a cat"})
        self.assertEqual(code, 200, out)
        self.assertIn("unmerge_lora_weights", self.urls())
        self.assertIsNone(self.ck.VIDEO_TURBO_LEFT["run"])

    def test_a_lane_that_restarted_since_runs_the_base(self):
        """A new run of the lane starts without the adapter, whatever the last one held."""
        self.turbo_left_on()
        self.spy.lists = False
        self.a_run(("active", "a-new-run"))
        code, out = self.call({"prompt": "a cat"})
        self.assertEqual(code, 200, out)

    def test_the_adapter_comes_off_after_the_video_though_the_check_now_fails(self):
        """The check passed when the adapter went on; by the end of the video an update may
        have rewritten a note, or systemd may not answer. The lane is the run the adapter
        went on: it is taken off all the same (review, 2026-10-10)."""
        for how in ("a note rewritten", "a mute systemd"):
            with self.subTest(how=how):
                self.lane_since(1_000_100)
                self.a_run()
                note, silent = self.tmp / "lane" / "sglang-source", self.ck.Ran("")
                silent.ok = False
                run = self.ck.run

                def later():
                    if how == "a note rewritten":
                        os.utime(note, (1_000_500, 1_000_500))
                    else:
                        self.ck.run = lambda argv, timeout=5.0, merge_err=False: silent
                self.spy.on_create = later
                code, out = self.call({"prompt": "a cat", "turbo": True, "seconds": 4})
                self.ck.run = run
                self.assertEqual(code, 200, out)
                self.assertEqual(self.urls()[-2:], ["list_loras", "unmerge_lora_weights"], self.urls())
                self.assertIsNone(self.spy.active, "the adapter was left on")
                self.assertIsNone(self.ck.VIDEO_TURBO_LEFT["run"])
                self.spy.calls.clear()

    def test_a_stale_lane_that_restarted_is_asked_nothing(self):
        """An adapter this cockpit left on went with its run: the lane that runs now, here on
        another commit as after a rollback, is not asked to take anything off."""
        self.turbo_left_on()
        self.lane_since(2_000_000, source="ddebc52f237a1dbb56533469ab2ec2a7b856c4ab")
        self.a_run(("active", "run-2"))
        code, out = self.call({"prompt": "a cat"})
        self.assertEqual(code, 200, out)
        self.assertEqual([u for u in self.urls() if u not in ("videos", "create")], [], self.urls())
        self.assertIsNone(self.ck.VIDEO_TURBO_LEFT["run"])

    def test_a_failed_set_lora_is_kept_in_mind_only_on_a_lane_still_up(self):
        """It may have gone on in part on a lane that is up and then said nothing more; a lane
        that stopped took it with it, and a base call to that lane gets the usual answer, not
        "try again in a minute" (review, 2026-10-10)."""
        for life, kept in ((("active", "run-1"), True), (("inactive", ""), False)):
            with self.subTest(life=life):
                self.ck.VIDEO_TURBO_LEFT["run"] = None
                self.spy.sets, self.spy.lists = False, True
                self.a_run(life)
                code, out = self.call({"prompt": "a cat", "turbo": True, "seconds": 4})
                self.assertEqual(code, 502, out)
                self.assertIn("could not put the Turbo adapter on", out["error"])
                self.assertEqual(self.ck.VIDEO_TURBO_LEFT["run"] is not None, kept)
                code, out = self.call({"prompt": "a cat"})
                self.assertEqual("try again in a minute" in str(out.get("error", "")), kept, out)

    def lane_since(self, start, source=None, wheel_at=1_000_000, source_at=1_000_000, silent=False):
        """The lane as an update leaves it: its process started at `start` (systemd's
        ExecMainStartTimestamp), beside the notes install-video.sh writes: the commit its source
        holds (by default the one the Turbo was measured on; '' for no note) and its wheel."""
        lane = self.tmp / "lane"
        lane.mkdir(exist_ok=True)
        notes = {"sglang-source": (self.ck.video_turbo_runtime() if source is None else source, source_at),
                 "sglang-wheel": ("0.5.21", wheel_at)}
        for name, (text, at) in notes.items():
            note = lane / name
            if text:
                note.write_text(text + "\n")
                os.utime(note, (at, at))
            else:
                note.unlink(missing_ok=True)        # the class's box is shared by its tests
        self.unit.write_text(self.unit.read_text().replace("[Service]\n", f"[Service]\nWorkingDirectory={lane}\n"))
        self.ck.VIDEO_UNIT_CACHE.clear()
        run = self.ck.run

        def systemd(argv, timeout=5.0, merge_err=False):
            if "ActiveState,ExecMainStartTimestamp" not in argv:
                return ""
            if silent:                              # what run() gives back on a timeout
                out = self.ck.Ran("")
                out.ok = False
                return out
            return f"ActiveState=active\nExecMainStartTimestamp=@{start}\n"
        self.ck.run = systemd
        self.addCleanup(setattr, self.ck, "run", run)

    def test_a_lane_started_before_its_runtime_was_updated_gets_no_turbo(self):
        """An update does not restart a serving lane, and the runtime it ran before v1.25
        loads the whole DiT into memory to put an adapter on (cockpit.py
        video_runtime_stale): nothing is asked of that lane, and the status says why."""
        self.lane_since(999_900)
        code, out = self.call({"prompt": "a cat", "turbo": True, "seconds": 4})
        self.assertEqual(code, 409, out)
        self.assertIn("next start", out["error"])
        self.assertEqual(self.urls(), [], "the lane was asked something")
        status = self.ck.video_status()
        self.assertFalse(status["turbo"])
        self.assertEqual(status["turbo_why"], self.ck.VIDEO_STALE_RUNTIME)

    def test_the_newer_note_is_the_one_that_counts(self):
        """A new source under the same wheel (a pin that moves, a rollback undone) is a new
        runtime as much as a new wheel is."""
        self.lane_since(1_000_100, source_at=1_000_200)
        code, out = self.call({"prompt": "a cat", "turbo": True, "seconds": 4})
        self.assertEqual(code, 409, out)
        self.assertIn("next start", out["error"])

    def test_a_base_call_on_such_a_lane_touches_no_adapter(self):
        self.lane_since(999_900)
        self.spy.active = {"nickname": "larry-v4", "merged": False}
        code, out = self.call({"prompt": "a cat"})
        self.assertEqual(code, 200, out)
        self.assertEqual([u for u in self.urls() if u not in ("videos", "create")], [], self.urls())

    def test_a_lane_without_its_notes_runs_an_unknown_runtime(self):
        for source, wheel_note in (("", True), (None, False)):
            with self.subTest(source=source, wheel=wheel_note):
                self.lane_since(2_000_000, source=source)
                if not wheel_note:
                    (self.tmp / "lane" / "sglang-wheel").unlink()
                code, out = self.call({"prompt": "a cat", "turbo": True, "seconds": 4})
                self.assertEqual(code, 409, out)
                self.assertIn("no note", out["error"])
                self.assertEqual(self.urls(), [])

    def test_a_lane_on_another_commit_gets_no_turbo(self):
        """SGLANG_DIFFUSION_PIN, or a rollback to v1.24: a runtime the Turbo was not measured on."""
        self.lane_since(2_000_000, source="ddebc52f237a1dbb56533469ab2ec2a7b856c4ab")
        code, out = self.call({"prompt": "a cat", "turbo": True, "seconds": 4})
        self.assertEqual(code, 409, out)
        self.assertIn("SGLang at ddebc52f237a", out["error"])
        self.assertEqual(self.urls(), [])

    def test_a_systemd_that_does_not_answer_gets_no_turbo(self):
        """run() answers a timeout with an empty reading marked unanswered: silence is not a
        lane at rest."""
        self.lane_since(2_000_000, silent=True)
        code, out = self.call({"prompt": "a cat", "turbo": True, "seconds": 4})
        self.assertEqual(code, 409, out)
        self.assertIn("systemd did not say", out["error"])
        self.assertEqual(self.urls(), [])
        self.assertFalse(self.ck.video_status()["turbo"])

    def test_a_lane_started_after_its_update_gets_the_turbo(self):
        self.lane_since(1_000_100)
        self.assertTrue(self.ck.video_status()["turbo"])
        code, out = self.call({"prompt": "a cat", "turbo": True, "seconds": 4})
        self.assertEqual(code, 200, out)
        self.assertIn("set_lora", self.urls())
        self.assertIsNone(self.spy.active, "the adapter was left on")

    def test_a_handed_over_turbo_call_takes_the_adapter_off_when_it_ends(self):
        self.spy.active = {"nickname": "larry-v4", "merged": False}
        self.assertTrue(self.ck.VIDEO_LOCK.acquire(blocking=False))
        self.ck._release_video_lock_when_done(self.ck._video_life(), "vid-test-1", 5, [], True)
        self.assertIn("unmerge_lora_weights", self.urls())
        self.assertIsNone(self.spy.active)
        self.assertFalse(self.ck.VIDEO_LOCK.locked())

    def test_the_page_and_the_server_count_the_turbo_alike(self):
        js = (REPO / "dashboard/static/js/video.js").read_text()
        page = re.search(r"const VID_TURBO_STEPS = (\d+), VID_TURBO_PER_STEP = ([\d.]+), VID_TURBO_MAX_SECONDS = (\d+);", js)
        server = (REPO / "dashboard/cockpit.py").read_text()
        self.assertEqual(int(page.group(1)), self.ck.VIDEO_TURBO_STEPS)
        self.assertIn(f"per_step = {page.group(2)}\n", server)
        self.assertEqual(int(page.group(3)), self.ck.VIDEO_TURBO_MAX_SECONDS)


if __name__ == "__main__":
    unittest.main()
