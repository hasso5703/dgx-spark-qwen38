#!/usr/bin/env python3
"""Every lane but the one that serves is installed on demand: its units at install, what it
runs at its first Load from the cockpit, as the user, by prepare-lane.sh. The cockpit says
which are built (the venv and its wheel's note, written once the whole runtime is in; the image
the Kolibri unit names; the image the flash launcher or the 27B unit runs, with the vocabulary or
the draft that lane keeps beside it, whole and set for its window), which can start as installed
(built, and switched to: the checkpoint it serves whole, its chat template, the 27B's window),
and runs the preparation of the target a Load loads as a job of its own, with no sudo."""
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
sys.path.insert(0, str(DASH))
ENV_BEFORE = dict(os.environ)


def tearDownModule():
    os.environ.clear()
    os.environ.update(ENV_BEFORE)


class Prepare(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-prepare-"))
        (cls.tmp / "config").mkdir()
        (cls.tmp / "config" / "api-key").write_text("k\n")
        closed = "http://127.0.0.1:1"
        os.environ.update(HOME=str(cls.tmp), COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.tmp / "config"),
                          COCKPIT_REPO_DIR=str(REPO), COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0",
                          COCKPIT_AUTOHEAL="0", COCKPIT_ENGINE=closed, COCKPIT_PROXY=closed, COCKPIT_IMAGE=closed)
        spec = importlib.util.spec_from_file_location("cockpit_prepare_under_test", DASH / "cockpit.py")
        cls.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cp)
        cls.units = cls.tmp / "units"
        cls.units.mkdir()
        cls.cp.IMAGE_UNIT_PATH = cls.units / "qwen38-image.service"
        cls.cp.VIDEO_UNIT_PATH = cls.units / "qwen38-video.service"
        cls.cp.KOLIBRI_UNIT_PATH = cls.units / "qwen38-kolibri.service"
        # the box's own Nemotron unit, once installed, read as this test's (found on the reference box, 2026-10-16)
        cls.cp.NEMOTRON_UNIT_PATH = cls.units / "qwen38-nemotron.service"
        cls.cp.NEMOTRON_SUPER_UNIT_PATH = cls.units / "qwen38-nemotron-super.service"
        cls.cp.UNIT_PATHS = {"qwen38-sglang.service": cls.units / "qwen38-sglang.service",
                             "qwen38-flash.service": cls.units / "launch-flash.sh"}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        for f in self.units.iterdir():
            f.unlink()
        self.cp.UNIT_TARGET_CACHE.clear()
        self.images = set()
        self.cp.run = lambda argv, timeout=5.0, merge_err=False: (
            "sha256:abc\n" if argv[:3] == ["docker", "image", "inspect"] and argv[-1] in self.images else "")

    def lane(self, name, built):
        d = self.tmp / f"lane-{name}"
        shutil.rmtree(d, ignore_errors=True)
        (self.units / f"qwen38-{name}.service").write_text(f"[Service]\nWorkingDirectory={d}\n")
        if built:
            (d / "venv" / "bin").mkdir(parents=True)
            (d / "venv" / "bin" / "python").write_text("#!/bin/sh\n")
            (d / "sglang-wheel").write_text("0.5.21\n")

    def test_which_lanes_are_prepared(self):
        self.lane("image", built=True)
        self.lane("video", built=False)
        self.assertEqual(self.cp.collect_lanes_ready(), {"image": True, "video": False, "kolibri": None, "nemotron": None,
                                                          "nemotron-super": None, "flash": None, "27b": None})
        (self.units / "qwen38-kolibri.service").write_text("ExecStart=/usr/bin/docker run qwen38-kolibri:1.0\n")
        self.assertIs(self.cp.collect_lanes_ready()["kolibri"], False)
        self.images.add("qwen38-kolibri:1.0")
        self.assertIs(self.cp.collect_lanes_ready()["kolibri"], True)

    @staticmethod
    def runs(image, *rest):
        """A launcher or unit as their templates write it: the image alone on the line before
        the server command."""
        return ("exec /usr/bin/docker run --rm \\\n  -e HF_HUB_OFFLINE=1 \\\n"
                f"  {image} \\\n  python3 -m sglang.launch_server \\\n    --model-path x \\\n" + "".join(rest))

    def test_the_flash_lane_beside_the_serving_one(self):
        # its image, whatever its form, and the draft vocabulary its launcher waits for when it waits for one
        vocab = self.tmp / "token-map-65536.pt"
        for img in ("lmsysorg/sglang@sha256:" + "d" * 64, "lmsysorg/sglang:dev-qwen38-next-local"):
            with self.subTest(img=img):
                self.images.clear()
                vocab.unlink(missing_ok=True)
                launch = self.units / "launch-flash.sh"
                launch.write_text(self.runs(img, f'[ -s "{vocab}" ] && TIER+=(--speculative-token-map /out/token-map-65536.pt)\n'))
                self.assertIs(self.cp.lane_prepared("flash"), False)          # no image
                self.images.add(img)
                self.assertIs(self.cp.lane_prepared("flash"), False)          # the vocabulary it waits for
                vocab.write_text("map")
                self.assertIs(self.cp.lane_prepared("flash"), True)
                launch.write_text(self.runs(img, "TIER+=(--speculative-token-map /out/token-map-65536.pt)\n"))
                vocab.unlink()
                self.assertIs(self.cp.lane_prepared("flash"), True)           # built before: the line it always had

    def test_the_27b_beside_a_serving_flash(self):
        # its image and the DFlash2 draft its unit names, in the cache it mounts, at its revision
        img, hf, rev = "lmsysorg/sglang@sha256:" + "f" * 64, self.tmp / "hf27", "a" * 40
        shutil.rmtree(hf, ignore_errors=True)
        unit = self.units / "qwen38-sglang.service"
        unit.write_text(self.runs(img, f"  -v {hf}:/root/.cache/huggingface \\\n",
                                  "    --speculative-draft-model-path org/draft \\\n",
                                  f"    --speculative-draft-model-revision {rev} \\\n"))
        self.assertIs(self.cp.lane_prepared("27b"), False)            # no image
        self.images.add(img)
        self.assertIs(self.cp.lane_prepared("27b"), False)            # no draft
        snap = hf / "hub" / "models--org--draft" / "snapshots" / rev
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}")
        self.assertIs(self.cp.lane_prepared("27b"), False)            # its config, not its weights yet
        (snap / "model.safetensors").write_text("w")
        self.assertIs(self.cp.lane_prepared("27b"), True)
        unit.write_text(unit.read_text().replace(rev, "main"))        # a ref, read through refs/
        self.assertIs(self.cp.lane_prepared("27b"), False)
        (hf / "hub" / "models--org--draft" / "refs").mkdir()
        (hf / "hub" / "models--org--draft" / "refs" / "main").write_text(rev)
        self.assertIs(self.cp.lane_prepared("27b"), True)
        self.assertIs(self.cp.collect_lanes_ready()["27b"], True)

    def test_an_empty_vocabulary_is_none(self):
        # [ -s ]: the launcher skips an empty one, and so does this (found in review, 2026-10-15)
        img, vocab = "lmsysorg/sglang@sha256:" + "d" * 64, self.tmp / "token-map-65536.pt"
        self.images.add(img)
        (self.units / "launch-flash.sh").write_text(
            self.runs(img, f'[ -s "{vocab}" ] && TIER+=(--speculative-token-map /out/token-map-65536.pt)\n'))
        vocab.write_text("")
        self.assertIs(self.cp.lane_prepared("flash"), False)
        vocab.write_text("map")
        self.assertIs(self.cp.lane_prepared("flash"), True)

    def test_a_draft_half_there_or_set_for_another_window_is_not_prepared(self):
        # one shard of two, a link to a blob gone, a config set for another window than its unit
        # serves: each read as prepared, and the next Load started the 1M unit on a native draft
        # (found in review, 2026-10-15)
        img, hf, rev = "lmsysorg/sglang@sha256:" + "f" * 64, self.tmp / "hf-draft", "a" * 40
        shutil.rmtree(hf, ignore_errors=True)
        self.images.add(img)
        unit = self.units / "qwen38-sglang.service"

        def write(window):
            unit.write_text(self.runs(img, f"  -v {hf}:/root/.cache/huggingface \\\n",
                                      "    --context-length 1010000 \\\n" if window == "1m" else "",
                                      "    --speculative-draft-model-path org/draft \\\n",
                                      f"    --speculative-draft-model-revision {rev} \\\n"))
        repo = hf / "hub" / "models--org--draft"
        snap, blobs = repo / "snapshots" / rev, repo / "blobs"
        snap.mkdir(parents=True)
        blobs.mkdir()
        (snap / "config.json").write_text('{"max_position_embeddings": 262144}')
        (snap / "model.safetensors.index.json").write_text(
            '{"weight_map": {"a": "model-1.safetensors", "b": "model-2.safetensors"}}')
        (snap / "model-1.safetensors").write_text("w")
        write("native")
        self.assertIs(self.cp.lane_prepared("27b"), False)              # one shard of two
        (blobs / "b2").write_text("w")
        (snap / "model-2.safetensors").symlink_to(blobs / "b2")
        self.assertIs(self.cp.lane_prepared("27b"), True)
        (blobs / "b2").unlink()
        self.assertIs(self.cp.lane_prepared("27b"), False)              # a link to a blob gone
        (blobs / "b2").write_text("w")
        write("1m")
        self.assertIs(self.cp.lane_prepared("27b"), False)              # native config, 1M unit
        (snap / "config.json").write_text('{"max_position_embeddings": 1010000}')
        self.assertIs(self.cp.lane_prepared("27b"), True)
        write("native")
        self.assertIs(self.cp.lane_prepared("27b"), False)              # 1M config, native unit

    def test_a_venv_without_its_wheels_note_is_not_prepared(self):
        # the venv is made first and the wheel's note written once the wheel is in
        self.lane("image", built=True)
        (self.tmp / "lane-image" / "sglang-wheel").unlink()
        self.assertIs(self.cp.collect_lanes_ready()["image"], False)

    def wait(self, job):
        for _ in range(200):
            if self.cp.JOBS[job].status != "running":
                return
            __import__("time").sleep(0.05)

    def test_the_action_prepares_the_target_a_load_loads_and_takes_nothing_else(self):
        # the lane from the target, and the target too for the two lanes whose preparation fetches
        # a checkpoint: it fetched the default, then the switch the one loaded (2026-10-15)
        for target, args in (("kolibri", ["kolibri"]), ("image-turbo", ["image"]), ("video", ["video"]),
                             ("flash-nvda", ["flash", "flash-nvda"]), ("fp8", ["27b", "fp8"])):
            with self.subTest(target=target):
                code, body = self.cp.start_action("prepare", {"target": target})
                self.assertEqual(code, 202, body)
                self.assertEqual(body["argv"], ["bash", str(REPO / "prepare-lane.sh"), *args])
                self.wait(body["job"])
        for params in ({"target": "27B"}, {"target": "../install"}, {"target": ""}, {"target": "flash "},
                       {"lane": "kolibri"}, {}):
            with self.subTest(params=params):
                self.assertEqual(self.cp.start_action("prepare", params)[0], 400)
        self.assertGreaterEqual(self.cp.ACTIONS["prepare"]["timeout"], self.cp.ACTIONS["switch"]["timeout"])


    def test_a_text_lane_starts_as_installed_once_its_load_has_switched_to_it(self):
        # built is its runtime; its Start needs what its switch writes too: the checkpoint it
        # serves whole, its chat template, and for the 27B its config set for its window (found
        # in review, 2026-10-15: a Load stopped after its preparation offered a Start that
        # crash-looped, the unit stopping the lane that served on its way)
        img, cfg, hf = "lmsysorg/sglang@sha256:" + "b" * 64, self.tmp / "out", self.tmp / "hf-start"
        shutil.rmtree(hf, ignore_errors=True)
        shutil.rmtree(cfg, ignore_errors=True)
        cfg.mkdir()
        (hf / "hub").mkdir(parents=True)
        self.images.add(img)
        model, rev = self.cp.rg.PIN_MODELS["FLASH_NVDA_REV"], "c" * 40
        (self.units / "launch-flash.sh").write_text(
            f"exec /usr/bin/docker run --rm \\\n  -v {hf}:/root/.cache/huggingface \\\n  -v {cfg}:/out \\\n"
            f"  {img} \\\n  python3 -m sglang.launch_server \\\n    --model-path {model} --revision {rev} \\\n"
            "    --chat-template /out/chat-template-flashnext.jinja \\\n")
        self.assertIs(self.cp.lane_prepared("flash"), True)
        self.assertIs(self.cp.lane_startable("flash"), False)            # no checkpoint, no template
        snap = hf / "hub" / ("models--" + model.replace("/", "--")) / "snapshots" / rev
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}")
        (cfg / "chat-template-flashnext.jinja").write_text("{{ x }}")
        self.assertIs(self.cp.lane_startable("flash"), False)            # its config, no weights yet
        (snap / "model.safetensors").write_text("w")
        (cfg / "chat-template-flashnext.jinja").unlink()
        self.assertIs(self.cp.lane_startable("flash"), False)            # its checkpoint whole, no template
        (cfg / "chat-template-flashnext.jinja").write_text("")
        self.assertIs(self.cp.lane_startable("flash"), False)            # an empty one
        (cfg / "chat-template-flashnext.jinja").write_text("{{ x }}")
        self.assertIs(self.cp.lane_startable("flash"), True)
        self.assertIs(self.cp.collect_lanes_startable()["flash"], True)
        # the mount read-only, as the engines have it: the template is still looked for there
        launch = self.units / "launch-flash.sh"
        launch.write_text(launch.read_text().replace(f"-v {cfg}:/out \\", f"-v {cfg}:/out:ro \\"))
        self.assertIn(":/out:ro", launch.read_text())
        (cfg / "chat-template-flashnext.jinja").write_text("")
        self.assertIs(self.cp.lane_startable("flash"), False)            # an empty one, read-only mount
        (cfg / "chat-template-flashnext.jinja").write_text("{{ x }}")
        self.assertIs(self.cp.lane_startable("flash"), True)
        self.images.clear()
        self.assertIs(self.cp.lane_startable("flash"), False)            # not built: no Start either
        self.assertIsNone(self.cp.lane_startable("video"))               # not installed

        # the 27B in 1M: its checkpoint's config too
        self.images.add(img)
        model27, rev27 = self.cp.rg.PIN_MODELS["STOCK_REV"], "e" * 40
        (self.units / "qwen38-sglang.service").write_text(
            f"[Service]\nExecStart=/usr/bin/docker run --rm \\\n  -v {hf}:/root/.cache/huggingface \\\n  -v {cfg}:/out \\\n"
            f"  {img} \\\n  python3 -m sglang.launch_server \\\n"
            f"    --trust-remote-code --context-length 1010000 --model-path {model27} --revision {rev27} \\\n"
            "    --chat-template /out/chat-template-sglang.jinja \\\n")
        (cfg / "chat-template-sglang.jinja").write_text("{{ x }}")
        snap27 = hf / "hub" / ("models--" + model27.replace("/", "--")) / "snapshots" / rev27
        snap27.mkdir(parents=True)
        (snap27 / "model.safetensors").write_text("w")
        (snap27 / "config.json").write_text('{"text_config": {"max_position_embeddings": 262144}}')
        self.assertIs(self.cp.lane_prepared("27b"), True)                # no draft named
        self.assertIs(self.cp.lane_startable("27b"), False)              # native config, 1M unit
        (snap27 / "config.json").write_text('{"text_config": {"max_position_embeddings": 1010000}}')
        self.assertIs(self.cp.lane_startable("27b"), True)

    def test_an_image_lane_starts_once_the_checkpoint_it_serves_is_whole(self):
        # its runtime built, its checkpoint the one refs/main names, as the unit serves it offline
        hf = self.tmp / "hf-image"
        shutil.rmtree(hf, ignore_errors=True)
        (hf / "hub").mkdir(parents=True)
        self.lane("image", built=True)
        unit = self.units / "qwen38-image.service"
        unit.write_text(unit.read_text() + f"Environment=HF_HOME={hf}\nExecStart=/x/sglang serve --model-path Qwen/Qwen-Image-2.1\n")
        self.assertIs(self.cp.lane_startable("image"), False)
        repo = hf / "hub" / "models--Qwen--Qwen-Image-2.1"
        (repo / "refs").mkdir(parents=True)
        (repo / "refs" / "main").write_text("f" * 40)
        tr = repo / "snapshots" / ("f" * 40) / "transformer"
        tr.mkdir(parents=True)
        (tr / "diffusion_pytorch_model.safetensors").write_text("w")
        self.assertIs(self.cp.lane_startable("image"), True)
        # whole in another lane's cache and not in the one this unit reads: not startable
        other = self.tmp / "hf-other"
        shutil.rmtree(other, ignore_errors=True)
        (other / "hub").mkdir(parents=True)
        unit.write_text(unit.read_text().replace(f"HF_HOME={hf}", f"HF_HOME={other}"))
        (self.units / "qwen38-video.service").write_text(f"[Service]\nEnvironment=HF_HOME={hf}\n")
        self.assertIs(self.cp.lane_startable("image"), False)

    def test_a_video_lane_on_another_model_is_judged_on_its_whole_repo(self):
        # the partition patterns are MiniMax-H3's: a lane installed on another model, whole in its
        # cache, had no Start for ever (found in review, 2026-10-15)
        hf = self.tmp / "hf-video"
        shutil.rmtree(hf, ignore_errors=True)
        self.lane("video", built=True)
        unit = self.units / "qwen38-video.service"
        unit.write_text(unit.read_text() + f"Environment=HF_HOME={hf}\nExecStart=/x/sglang serve --model-path org/other-video\n")
        repo = hf / "hub" / "models--org--other-video"
        (repo / "refs").mkdir(parents=True)
        (repo / "refs" / "main").write_text("9" * 40)
        (repo / "snapshots" / ("9" * 40)).mkdir(parents=True)
        (repo / "snapshots" / ("9" * 40) / "model.safetensors").write_text("w")
        self.assertIs(self.cp.lane_startable("video"), True)

    def test_a_template_and_a_mount_in_quotes_are_read(self):
        img, cfg, hf = "lmsysorg/sglang@sha256:" + "a" * 64, self.tmp / "out-q", self.tmp / "hf-q"
        for d in (cfg, hf):
            shutil.rmtree(d, ignore_errors=True)
        cfg.mkdir()
        (hf / "hub").mkdir(parents=True)
        self.images.add(img)
        model, rev = self.cp.rg.PIN_MODELS["FLASH_NVDA_REV"], "d" * 40
        (self.units / "launch-flash.sh").write_text(
            f'exec /usr/bin/docker run --rm \\\n  -v {hf}:/root/.cache/huggingface \\\n  -v "{cfg}":/out \\\n'
            f"  {img} \\\n  python3 -m sglang.launch_server \\\n    --model-path {model} --revision {rev} \\\n"
            "    --chat-template '/out/chat-template-flashnext.jinja'\n")
        snap = hf / "hub" / ("models--" + model.replace("/", "--")) / "snapshots" / rev
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}")
        (snap / "model.safetensors").write_text("w")
        (cfg / "chat-template-flashnext.jinja").write_text("{{ x }}")
        self.assertIs(self.cp.lane_startable("flash"), True)
        (cfg / "chat-template-flashnext.jinja").unlink()               # read in its quotes, and missing
        self.assertIs(self.cp.lane_startable("flash"), False)

    def test_a_job_that_changes_a_lane_has_the_lanes_and_library_read_again(self):
        # the next Load and Library read what it left, not what the last 30 s tier saw (2026-10-15)
        self.cp.REGISTRY_CACHE.update(ts=__import__("time").time(), data={"stale": True})
        with self.cp.STATE_LOCK:
            self.cp.STATE.pop("lanes_startable", None)
        code, body = self.cp.start_action("prepare", {"target": "kolibri"})
        self.assertEqual(code, 202, body)
        self.wait(body["job"])
        for _ in range(100):                      # read again just after the job's end
            with self.cp.STATE_LOCK:
                if "lanes_startable" in self.cp.STATE:
                    break
            __import__("time").sleep(0.05)
        self.assertEqual(self.cp.REGISTRY_CACHE["ts"], 0.0)
        with self.cp.STATE_LOCK:
            self.assertIn("lanes_startable", self.cp.STATE)
            self.assertIn("lanes_ready", self.cp.STATE)

    def test_a_preparation_waits_for_a_lane_that_is_starting(self):
        # a first Load's pip and docker pull beside a lane loading its weights, on the same unified
        # memory: refused as a switch is (found in review, 2026-10-15)
        with self.cp.LIFE_LOCK:
            before = dict(self.cp.LIFE.get("states", {}))
            self.cp.LIFE["states"] = {**before, "qwen38-sglang.service": "starting"}
        try:
            code, body = self.cp.start_action("prepare", {"target": "kolibri"})
            self.assertEqual(code, 409, body)
            self.assertEqual(body["error"], "blocked")
        finally:
            with self.cp.LIFE_LOCK:
                self.cp.LIFE["states"] = before

    def test_the_lanes_read_again_does_not_hold_the_next_step(self):
        # read while the job lock was held, the page that saw the job end started its next step
        # on a lock still taken, and was told busy (found in review, 2026-10-15)
        slow = self.cp.collect_lanes_startable
        self.cp.collect_lanes_startable = lambda: (__import__("time").sleep(0.8), {})[1]
        try:
            code, body = self.cp.start_action("prepare", {"target": "kolibri"})
            self.assertEqual(code, 202, body)
            self.wait(body["job"])
            t0 = __import__("time").time()
            while True:
                code, nxt = self.cp.start_action("prepare", {"target": "kolibri"})
                if code != 409 or __import__("time").time() - t0 > 0.4:
                    break
                __import__("time").sleep(0.02)
            self.assertEqual(code, 202, nxt)
            self.wait(nxt["job"])
        finally:
            self.cp.collect_lanes_startable = slow

    def test_a_preparation_goes_beside_a_lane_that_is_steady(self):
        # only a lane starting or stopping holds it back: ready, degraded, wedged, orphan or failed,
        # a Load's first step goes (a gate on any of them refused every Load)
        for state in ("ready", "degraded", "wedged", "orphan", "failed"):
            with self.subTest(state=state):
                with self.cp.LIFE_LOCK:
                    before = dict(self.cp.LIFE.get("states", {}))
                    self.cp.LIFE["states"] = {**before, "qwen38-sglang.service": state}
                try:
                    code, body = self.cp.start_action("prepare", {"target": "kolibri"})
                    self.assertEqual(code, 202, body)
                    self.wait(body["job"])
                finally:
                    with self.cp.LIFE_LOCK:
                        self.cp.LIFE["states"] = before

    def test_the_video_lane_on_minimax_h3_is_judged_on_the_partition_it_serves(self):
        # FL2VA whole and an index of Ref2VA begun (a full pull that stopped, as on the reference
        # box): the lane serves FL2VA and starts
        hf = self.tmp / "hf-h3"
        shutil.rmtree(hf, ignore_errors=True)
        self.lane("video", built=True)
        unit = self.units / "qwen38-video.service"
        unit.write_text(unit.read_text() + f"Environment=HF_HOME={hf}\nExecStart=/x/sglang serve --model-path MiniMaxAI/MiniMax-H3\n")
        repo = hf / "hub" / "models--MiniMaxAI--MiniMax-H3"
        (repo / "refs").mkdir(parents=True)
        (repo / "refs" / "main").write_text("8" * 40)
        snap = repo / "snapshots" / ("8" * 40)
        (snap / "FL2VA").mkdir(parents=True)
        (snap / "FL2VA" / "model.safetensors").write_text("w")
        (snap / "Ref2VA").mkdir()
        (snap / "Ref2VA" / "model.safetensors.index.json").write_text('{"weight_map": {"a": "model-00001.safetensors"}}')
        self.assertIs(self.cp.lane_startable("video"), True)
        (snap / "FL2VA" / "model.safetensors").unlink()                # its own partition not there
        self.assertIs(self.cp.lane_startable("video"), False)

    def test_library_is_read_again_after_a_scan_that_was_running(self):
        # its date set to 0 under the scan's lock: a scan running as the job ended wrote its own
        # date over it after, and Library kept what it had read before the job
        lock = self.cp.REGISTRY_LOCK
        lock.acquire()
        try:
            code, body = self.cp.start_action("prepare", {"target": "kolibri"})
            self.assertEqual(code, 202, body)
            self.wait(body["job"])
            __import__("time").sleep(0.2)                              # the job waits on the scan
            self.cp.REGISTRY_CACHE["ts"] = 12345.0                     # the scan ends, with its date
        finally:
            lock.release()
        for _ in range(100):
            if self.cp.REGISTRY_CACHE["ts"] == 0.0:
                break
            __import__("time").sleep(0.02)
        self.assertEqual(self.cp.REGISTRY_CACHE["ts"], 0.0)

    def test_each_job_that_changes_a_lane_has_them_read_again(self):
        for action, params in (("switch", {"target": "kolibri"}), ("fetch", {"target": "kolibri"}), ("delete_strays", {})):
            with self.subTest(action=action):
                self.cp.REGISTRY_CACHE.update(ts=__import__("time").time(), data={"stale": True})
                with self.cp.STATE_LOCK:
                    self.cp.STATE.pop("lanes_startable", None)
                code, body = self.cp.start_action(action, params)
                self.assertEqual(code, 202, body)
                self.wait(body["job"])
                for _ in range(100):
                    with self.cp.STATE_LOCK:
                        if "lanes_startable" in self.cp.STATE:
                            break
                    __import__("time").sleep(0.05)
                self.assertEqual(self.cp.REGISTRY_CACHE["ts"], 0.0)
                with self.cp.STATE_LOCK:
                    self.assertIn("lanes_startable", self.cp.STATE)

    def test_a_mount_quoted_whole_is_read_too(self):
        img, cfg, hf = "lmsysorg/sglang@sha256:" + "a" * 64, self.tmp / "out-w", self.tmp / "hf-w"
        for d in (cfg, hf):
            shutil.rmtree(d, ignore_errors=True)
        cfg.mkdir()
        self.images.add(img)
        model, rev = self.cp.rg.PIN_MODELS["FLASH_NVDA_REV"], "d" * 40
        (self.units / "launch-flash.sh").write_text(
            f'exec /usr/bin/docker run --rm \\\n  -v {hf}:/root/.cache/huggingface \\\n  -v "{cfg}:/out" \\\n'
            f"  {img} \\\n  python3 -m sglang.launch_server \\\n    --model-path {model} --revision {rev} \\\n"
            "    --chat-template /out/chat-template-flashnext.jinja\n")
        snap = hf / "hub" / ("models--" + model.replace("/", "--")) / "snapshots" / rev
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}")
        (snap / "model.safetensors").write_text("w")
        self.assertIs(self.cp.lane_startable("flash"), False)          # its template not there
        (cfg / "chat-template-flashnext.jinja").write_text("{{ x }}")
        self.assertIs(self.cp.lane_startable("flash"), True)

    def test_a_unit_with_no_cache_of_its_own_reads_the_default_one_and_one_with_a_cache_reads_it_only(self):
        default = self.tmp / ".cache" / "huggingface"
        shutil.rmtree(default, ignore_errors=True)
        repo = default / "hub" / "models--Qwen--Qwen-Image-2.1"
        (repo / "refs").mkdir(parents=True)
        (repo / "refs" / "main").write_text("e" * 40)
        tr = repo / "snapshots" / ("e" * 40) / "transformer"
        tr.mkdir(parents=True)
        (tr / "diffusion_pytorch_model.safetensors").write_text("w")
        self.lane("image", built=True)
        unit = self.units / "qwen38-image.service"
        base = unit.read_text()
        unit.write_text(base + "ExecStart=/x/sglang serve --model-path Qwen/Qwen-Image-2.1\n")
        self.assertIs(self.cp.lane_startable("image"), True)           # HOME's cache, as the lanes run
        own = self.tmp / "hf-own"
        shutil.rmtree(own, ignore_errors=True)
        (own / "hub").mkdir(parents=True)
        unit.write_text(base + f"Environment=HF_HOME={own}\nExecStart=/x/sglang serve --model-path Qwen/Qwen-Image-2.1\n")
        self.assertIs(self.cp.lane_startable("image"), False)          # not the default beside its own

    def test_a_job_is_seen_ended_once_its_lock_is_free(self):
        # seen ended before (its audit and its event in between), a client that chained the next
        # job at once was told busy, 138 times in 300 (found in review, 2026-10-15)
        prune = self.cp._prune_jobs

        def slow_prune():                    # the last step before the lock goes
            __import__("time").sleep(0.3)
            prune()
        self.cp._prune_jobs = slow_prune
        try:
            code, body = self.cp.start_action("prepare", {"target": "kolibri"})
            self.assertEqual(code, 202, body)
            while self.cp.JOBS[body["job"]].status == "running":
                __import__("time").sleep(0.005)
            code, nxt = self.cp.start_action("prepare", {"target": "kolibri"})
            self.assertEqual(code, 202, nxt)
            self.wait(nxt["job"])
        finally:
            self.cp._prune_jobs = prune

if __name__ == "__main__":
    unittest.main()
