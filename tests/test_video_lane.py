#!/usr/bin/env python3
"""The MiniMax-H3 video lane: the cookbook's recipe, held in place.

Every serving assertion here corresponds to the SGLang cookbook's MiniMax-H3 page as
read on 2026-09-25: the DGX Spark figures (about 12.1 s per denoise step, about 40 s
of decode, about 12 min per warm 4 s 480P request, with no flags at all), the two
checkpoint partitions (fl2va and ref2va, chosen with --model-variant), the request
shape (model, prompt, seconds 4 to 15, task, conditions with keyframe roles, target,
quality, num_outputs_per_prompt 1 to 10, num_inference_steps, flow_shift,
audio_flow_shift, seed) and the OpenAI videos endpoints (POST /v1/videos, GET
/v1/videos, GET /v1/videos/{id}/content).

The lane serves that recipe exactly as upstream wrote it at the pinned commit: no
local source patch. The image lane carries two, and this lane deliberately does not
(Hasan, 2026-09-25: the official SGLang and MiniMax recommendations, nothing added).
What that costs is stated, not patched around: a stop cancels the generation in
flight (measured under a second), TimeoutStopSec is a ceiling, because the runtime
has no abort.
"""
import json
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
UNIT_TPL = REPO / "qwen38-video.service.template"
INSTALLER = REPO / "install-video.sh"
COCKPIT = REPO / "dashboard" / "cockpit.py"
APP_JS = REPO / "dashboard" / "static" / "app.js"
INDEX = REPO / "dashboard" / "static" / "index.html"
SWITCH = REPO / "switch-model.sh"
INSTALL = REPO / "install.sh"


def exec_start():
    """The flags the unit actually passes, with the comments that explain them stripped.
    A comment naming a flag it deliberately does NOT pass must not read as passing it."""
    lines, taking = [], False
    for line in UNIT_TPL.read_text().splitlines():
        if line.startswith("ExecStart="):
            taking = True
        elif taking and not lines[-1].rstrip().endswith("\\"):
            break
        if taking:
            lines.append(line)
    return " ".join(lines)


class TheUnit(unittest.TestCase):
    def test_every_placeholder_is_one_the_installer_substitutes(self):
        rendered = UNIT_TPL.read_text()
        for ph in sorted(set(re.findall(r"__[A-Z][A-Z0-9_]*__", rendered))):
            self.assertIn(ph.replace("__", ""), INSTALLER.read_text(),
                          f"{ph} is in the template and no sed line names it")

    def test_one_engine_at_a_time_is_declared_not_documented(self):
        text = UNIT_TPL.read_text()
        after = [ln for ln in text.splitlines() if ln.startswith("After=")][0]
        for other in ("qwen38-sglang.service", "qwen38-flash.service",
                      "qwen38-image.service", "qwen38-llamacpp.service"):
            self.assertIn(other, after,
                          f"{other} is not ordered after in After=")
        conflicts = [ln for ln in text.splitlines() if ln.startswith("Conflicts=")][0]
        for other in ("qwen38-sglang.service", "qwen38-flash.service",
                      "qwen38-image.service", "qwen38-llamacpp.service"):
            self.assertIn(other, conflicts)
        # and the other three lanes name this one back, both directions
        for tpl in ("qwen38-sglang.service.template", "qwen38-sglang-1m.service.template",
                    "qwen38-flash.service.template", "qwen38-image.service.template"):
            body = (REPO / tpl).read_text()
            self.assertIn("qwen38-video.service", body, tpl)

    def test_it_serves_the_cookbook_recipe_and_nothing_else(self):
        """The cookbook verifies this model on the DGX Spark with no flags at all, and
        adding the discrete-GPU offload flags measured 2.1x slower on the same box.
        Forcing any placement is how a verified recipe stops being the verified recipe."""
        flags = exec_start()
        self.assertIn("--model-path", flags)
        self.assertIn("--model-variant", flags)
        for forced in ("--performance-mode", "--quantization", "--attention-backend",
                       "--use-fsdp-inference", "--dit-layerwise", "--layerwise-offload",
                       "--text-encoder-cpu-offload", "--vae-cpu-offload", "--tp-size",
                       "--sp-degree", "--num-gpus", "--enable-cfg-parallel"):
            self.assertNotIn(forced, flags, f"{forced} is not the cookbook's DGX Spark recipe")

    def test_it_passes_no_flag_the_diffusion_parser_does_not_have(self):
        """--api-key and --sleep-on-idle kill the unit at startup with "unrecognized
        arguments" (measured on the image lane's identical runtime, 2026-09-22)."""
        flags = exec_start()
        self.assertNotIn("--api-key", flags)
        self.assertNotIn("--sleep-on-idle", flags)

    def test_the_lane_is_loopback_because_it_cannot_authenticate_anyone(self):
        text = UNIT_TPL.read_text()
        self.assertIn("__VIDEO_BIND__", text)
        self.assertIn("127.0.0.1", INSTALLER.read_text())

    def test_a_stop_is_a_ceiling_not_a_wait(self):
        """A stop cancels the generation in under a second (measured on all seven
        logged stops), so TimeoutStopSec only needs to stand above any wedged
        scheduler, not above a draining generation."""
        text = UNIT_TPL.read_text()
        m = re.search(r"TimeoutStopSec=(\d+)", text)
        self.assertIsNotNone(m, "no TimeoutStopSec in the unit")
        self.assertGreaterEqual(int(m.group(1)), 3600)

    def test_the_decode_sits_on_expandable_segments(self):
        """The cookbook: the decode sits close enough to the cap that fragmentation
        otherwise tips it over."""
        self.assertIn("PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True", UNIT_TPL.read_text())

    def test_it_is_not_enabled_at_boot_by_the_installer(self):
        self.assertNotIn("systemctl enable", INSTALLER.read_text().split("step \"5/6 Service\"")[1].split('if [ "$SMOKE"')[0])


class TheInstaller(unittest.TestCase):
    def setUp(self):
        self.text = INSTALLER.read_text()

    def test_it_refuses_root(self):
        self.assertIn('if [ "$(id -u)" = "0"', self.text)

    def test_the_checkpoint_pin_is_a_full_commit(self):
        m = re.search(r'VIDEO_MODEL_PIN_REV="([0-9a-f]+)"', self.text)
        self.assertIsNotNone(m)
        self.assertEqual(len(m.group(1)), 40)

    def test_the_smoke_says_what_died_in_the_journal(self):
        """Each smoke death points at "the journal above": the line must print that
        journal first and survive its own failure (|| true), or the message sends the
        reader to an empty screen - or set -e silences the message itself."""
        for why in ("did not answer /health", "did not take the smoke video", "did not complete in 40 min"):
            i = self.text.index(why)
            window = self.text[max(0, i - 220):i]
            self.assertIn("journalctl", window, why)
            self.assertIn("|| true", window, why)

    def test_the_smoke_fits_the_weights_it_will_meet(self):
        """The smoke body is t2va on fl2va weights: a ref2va or custom-model install
        boots fifteen minutes for a body known wrong here, so the smoke is skipped
        with a reason instead of run into a 400."""
        i = self.text.index('"$SMOKE" -eq 1 ] &&')
        block = self.text[i:i + 400]
        self.assertIn('!= "fl2va"', block)
        self.assertIn('!= "MiniMaxAI/MiniMax-H3"', block)
        self.assertIn("SMOKE=0", block)

    def test_the_pipeline_prerequisites_are_checked_first(self):
        """MiniMax-H3 refuses to start without ffmpeg and ffprobe (its own words in
        minimax_h3_pipeline.py): the installer refuses before downloading 100 GB."""
        self.assertIn('command -v ffmpeg', self.text)
        self.assertIn('command -v ffprobe', self.text)

    def test_only_the_served_partition_is_fetched(self):
        """The repo holds FL2VA/ and Ref2VA/ (81 files each) plus a root native layout,
        and --model-variant fl2va roots the pipeline in FL2VA/ alone: fetching the rest
        filled the reference box's disk at 98% and killed the pull."""
        self.assertIn("allow_patterns", self.text)
        self.assertIn("FL2VA/*", self.text)
        self.assertIn("Ref2VA/*", self.text)
        self.assertIn("fetching with allow:", self.text)

    def test_a_generation_is_never_archived_on_disk(self):
        self.assertIn('mkdir -p "$LANE_DIR/outputs"', self.text)

    def test_a_completed_video_survives_its_request(self):
        """Left empty, --output-path answers into a temp dir the server deletes when the
        request ends: a video completed in 592 s 404d on download (measured 2026-09-25).
        The lane writes every MP4 to its own outputs dir instead."""
        tpl = (REPO / "qwen38-video.service.template").read_text()
        self.assertIn("--output-path __VIDEO_LANE_DIR__/outputs", tpl)
        self.assertIn('--input-save-path ""', tpl)

    def test_the_wheel_goes_in_before_the_source(self):
        """The wheel ships the prebuilt aarch64 kernels the source tree reuses; reversed,
        the server dies on its first request."""
        self.assertLess(self.text.index("sglang[diffusion]=="), self.text.index("pip\" install --quiet --no-deps -e"))

    def test_the_source_overlay_needs_no_rust_toolchain(self):
        self.assertIn("SGLANG_BUILD_RUST_EXTS=none", self.text)

    def test_there_is_no_local_patch_on_this_lane(self):
        """Hasan, 2026-09-25: the official recommendations, nothing added on top. A
        PATCHES= line or a video-sglang directory here is the custom method coming back."""
        self.assertNotIn("PATCHES=", self.text)
        self.assertFalse((REPO / "video-sglang").exists())
        self.assertNotIn("apply --check", self.text)

    def test_it_checks_for_room_before_downloading_145_gb(self):
        self.assertIn("WEIGHTS_GB=145", self.text)
        self.assertIn("WEIGHTS_NEED", self.text)

    def test_an_unknown_option_is_refused_rather_than_ignored(self):
        self.assertIn('die "unknown option: $a"', self.text)

    def test_the_variant_is_closed_to_the_two_partitions(self):
        """fl2va and ref2va are checkpoint partitions: a third string serves neither."""
        self.assertIn('case "$VARIANT" in fl2va|ref2va)', self.text)

    def test_the_other_lane_comes_back_however_the_smoke_test_ends(self):
        self.assertIn("trap restore_other_lane EXIT", self.text)

    def test_a_routine_rerun_does_not_stop_the_engine_to_redo_the_smoke_test(self):
        self.assertIn("--no-smoke", self.text)

    def test_a_serving_video_lane_is_left_serving_by_the_smoke_test(self):
        self.assertIn("WAS_VIDEO=0", self.text)

    def test_the_smoke_test_proves_a_video_not_a_status_code(self):
        """Creation is asynchronous: the smoke test polls the list to completed and
        downloads bytes it measures, the way the lane is actually used."""
        self.assertIn("/v1/videos", self.text)
        self.assertIn("completed", self.text)
        self.assertIn("/content", self.text)

    def test_the_smoke_body_sends_the_lanes_required_fields(self):
        """The lane 400s a request without task and without target (measured
        2026-09-25, docs/video-lane.md). This body is what a 15-minute boot is
        judged on, and commit 22a59dd fixed the task everywhere but here."""
        body = json.loads(re.search(r"-d '(\{[^']*\})'", self.text).group(1))
        self.assertEqual(body["task"], "t2va")
        self.assertEqual(sorted(body["target"]),
                         ["aspect_ratio", "duration_seconds", "short_edge"])
        self.assertEqual(body["seconds"], body["target"]["duration_seconds"])

    def test_uninstall_reads_the_lane_directory_from_the_unit(self):
        self.assertIn("installed WorkingDirectory", self.text)

    def test_uninstall_restores_the_lane_it_replaced_at_boot(self):
        self.assertIn("lane-before-video", self.text)


class TheSwitch(unittest.TestCase):
    def setUp(self):
        self.text = SWITCH.read_text()

    def test_video_is_a_switch_target(self):
        self.assertIn("|video)", self.text)
        self.assertIn("./switch-model.sh video", self.text)

    def test_the_switch_verifies_only_the_served_partition(self):
        """The switch re-downloaded the whole repo (Ref2VA included) onto a full disk:
        it carries the installer's allowlist, so a complete fl2va cache answers without
        touching the network. And it fetches the revision the unit serves, not whatever
        main became upstream: a no-revision download repoints refs/main at the tip
        before its first byte, stranding the offline unit on a half-fetched snapshot."""
        i = SWITCH.read_text().index('if [ "$CHOICE" = "video" ]; then')
        block = SWITCH.read_text()[i:SWITCH.read_text().index("exit 0", i)]
        self.assertIn("allow_patterns", block)
        self.assertIn("FL2VA/*", block)
        self.assertIn("revision=rev, local_files_only=True, allow_patterns=allow", block)
        self.assertIn("revision=rev, allow_patterns=allow", block)
        self.assertIn("refs/main", block)
        self.assertIn('VIDEO_MODEL_PIN_REV', block)

    def test_the_switch_allowlist_is_the_installers_allowlist(self):
        """Two hand-copied literals (ALLOW in install-video.sh, VID_ALLOW in
        switch-model.sh) drifted once: b7ce5a4 re-downloaded 280 files. Presence-only
        assertions let a one-sided edit pass; the switch's copy must be the
        installer's, character for character, per partition."""
        inst = INSTALLER.read_text()
        for prefix, variant in (("then ", "fl2va"), ("else ", "ref2va")):
            a = re.search(prefix + r'ALLOW="([^"]+)"', inst)
            b = re.search(prefix + r'VID_ALLOW="([^"]+)"', self.text)
            self.assertIsNotNone(a, f"no {variant} branch in the installer")
            self.assertIsNotNone(b, f"no {variant} branch in the switch")
            self.assertEqual(a.group(1), b.group(1), f"the {variant} allowlists drifted")

    def test_the_lane_before_video_is_written_down_before_anything_is_disabled(self):
        i = self.text.index('if [ "$CHOICE" = "video" ]; then')
        block = self.text[i:self.text.index("exit 0", i)]
        self.assertIn('> "$CONFIG_DIR/lane-before-video"', block)
        self.assertLess(block.index('> "$CONFIG_DIR/lane-before-video"'),
                        block.index("sudo systemctl disable"))

    def test_it_disables_every_other_engine_and_enables_this_one(self):
        i = self.text.index('if [ "$CHOICE" = "video" ]; then')
        block = self.text[i:self.text.index("exit 0", i)]
        for u in ("qwen38-sglang.service", "qwen38-flash.service", "qwen38-image.service"):
            self.assertIn(u, block)
        self.assertIn('sudo systemctl enable "$VIDEO_UNIT_NAME"', block)

    def test_it_never_starts_stops_or_restarts_an_engine(self):
        i = self.text.index('if [ "$CHOICE" = "video" ]; then')
        block = self.text[i:self.text.index("exit 0", i)]
        cmds = [ln.strip() for ln in block.splitlines()
                if ln.strip().startswith("sudo systemctl")]
        for cmd in cmds:
            self.assertTrue(cmd.startswith("sudo systemctl enable")
                            or cmd.startswith("sudo systemctl disable")
                            or cmd.startswith("sudo systemctl daemon-reload"),
                            f"the switch runs an engine: {cmd}")

    def test_a_text_switch_takes_the_video_lane_off_the_boot(self):
        self.assertIn('systemctl disable "$VIDEO_UNIT_NAME"', self.text)

    def test_the_proxy_and_opencode_are_left_alone(self):
        """Both are text clients: the proxy relays :30000 and opencode generates text."""
        i = self.text.index('if [ "$CHOICE" = "video" ]; then')
        block = self.text[i:self.text.index("exit 0", i)]
        self.assertIn("left as they are", block)
        self.assertNotIn("oc-merge-limits", block)
        self.assertNotIn("ceiling", block)


class TheLockHoldsWhatItPromises(unittest.TestCase):
    """One overlapping pair is what this box does not survive (measured with images
    on this runtime: 90.5 GB held of 121.6, engine wedged). Two facts are pinned at
    text level here; the refusal behavior itself runs in
    dashboard/tests/test_video_routes.py."""

    def test_the_watcher_treats_a_gone_record_as_done(self):
        """The synchronous poll reads "missing" as definitive; a watcher still
        waiting on it would spend its clock, free the lock and let the next call
        start beside a lane that may or may not still be working."""
        text = COCKPIT.read_text()
        i = text.index("def _release_video_lock_when_done")
        self.assertIn('"completed", "failed", "error", "cancelled", "missing"',
                      text[i:text.index("def video_call", i)])

    def test_the_longest_run_is_budgeted_at_admission(self):
        """A call estimated past the lock's guard is refused before it is made, so
        the escape into overlap is not reachable by a legal request. The tenth of
        slack is the point: an estimate inside 7200 but past 7200/1.1 would have the
        lock hit its deadline while the lane still works (review, 2026-09-28)."""
        self.assertIn("est * 1.1 > 2 * 3600.0", COCKPIT.read_text())


class TheBootLaneConvergence(unittest.TestCase):
    def setUp(self):
        self.text = INSTALL.read_text()

    def test_a_box_booting_video_is_detected(self):
        self.assertIn("VIDEO_BOOT=0", self.text)
        self.assertIn("systemctl is-enabled --quiet qwen38-video.service", self.text)

    def test_the_text_unit_is_not_enabled_on_that_path(self):
        i = self.text.index('if [ "$IMAGE_BOOT" -eq 0 ] && [ "$VIDEO_BOOT" -eq 0 ]; then')
        self.assertIn('sudo systemctl enable "$UNIT_NAME"', self.text[i:i + 120])
        self.assertEqual(self.text.count('sudo systemctl enable "$UNIT_NAME"'), 1)

    def test_that_path_updates_without_starting_anything(self):
        i = self.text.index('if [ "$IMAGE_BOOT" -eq 0 ] && [ "$VIDEO_BOOT" -eq 0 ]; then')
        early = self.text[i:self.text.index("exit 0", i)]
        self.assertNotIn("systemctl start", early)
        self.assertIn("--no-smoke", early)

    def test_that_path_respects_no_video(self):
        i = self.text.index('if [ "$IMAGE_BOOT" -eq 0 ] && [ "$VIDEO_BOOT" -eq 0 ]; then')
        early = self.text[i:self.text.index("exit 0", i)]
        call = early.index('"$REPO_DIR/install-video.sh" --no-smoke')
        self.assertIn('if [ "$NO_VIDEO" -eq 0 ]', early[:call])

    def test_an_explicit_model_choice_is_honoured(self):
        i = self.text.index("VIDEO_BOOT=0\nif systemctl is-enabled --quiet qwen38-video.service")
        block = self.text[i:i + 900]
        self.assertIn('if [ -n "$_ENV_MODEL_CHOICE" ]; then', block)
        self.assertLess(block.index('if [ -n "$_ENV_MODEL_CHOICE" ]; then'), block.index("VIDEO_BOOT=1"))

    def test_the_text_lane_used_before_video_is_the_one_brought_up_to_date(self):
        self.assertIn('> "$CONFIG_DIR/lane-before-video"', SWITCH.read_text())
        self.assertIn('[ "$LANE_BEFORE_VIDEO" = "qwen38-flash.service" ]', self.text)

    def test_with_video_and_no_video_contradict_each_other(self):
        self.assertIn("'--no-video and --with-video contradict each other", self.text)

    def test_with_video_needs_the_full_install(self):
        self.assertIn("'--with-video needs the full install", self.text)


class TheProxyLeavesVideoAlone(unittest.TestCase):
    def setUp(self):
        self.text = (REPO / "keepalive-proxy.py").read_text()

    def test_a_serving_video_lane_is_named_on_the_error_path(self):
        """With video serving, :30000 is closed: the client is told where the engine
        went, not that it is loading."""
        self.assertIn("def video_lane_serving", self.text)
        self.assertIn("qwen38-video.service", self.text[
            self.text.index("def engine_gone_reason"):self.text.index("def engine_gone_reason") + 1200])

    def test_nothing_video_is_relayed_through_the_text_proxy(self):
        """The proxy relays the text engine on :30000; the video lane listens on its
        own port and the cockpit calls it directly."""
        self.assertNotIn("/v1/videos", self.text)


class ThePageShowsVideoTruthfully(unittest.TestCase):
    def test_the_switcher_offers_minimax_h3(self):
        self.assertIn('<option value="video">MiniMax-H3</option>', INDEX.read_text())

    def test_the_video_tab_is_a_panel_not_a_promise(self):
        body = INDEX.read_text()
        self.assertNotIn("Coming soon", body)
        self.assertIn('id="tab-video"', body)
        self.assertIn('id="vidrun"', body)
        self.assertIn('<option value="qwen38-video.service">video lane (journal)</option>', body)

    def test_the_target_unit_is_the_video_unit(self):
        self.assertIn("t === 'video' ? VIDEO_UNIT", APP_JS.read_text())

    def test_cancel_restarts_the_lane(self):
        """The runtime has no abort: Cancel restarts the lane, like the image lane."""
        js = APP_JS.read_text()
        i = js.index("function vidCancel(){")
        self.assertIn("verb: 'restart', unit: VIDEO_UNIT", js[i:i + 400])

    def test_generate_stays_off_until_the_lane_answers(self):
        js = APP_JS.read_text()
        i = js.index("function vidRenderLane(){")
        self.assertIn("$('vidrun').disabled = !ready", js[i:i + 2500])

    def test_keyframes_are_capped_before_they_leave_the_browser(self):
        """A phone photo is megabytes against the 10 MiB the cockpit reads: capped on
        the long side and re-encoded to PNG, like the image lane's references."""
        js = APP_JS.read_text()
        i = js.index("function vidReadFile(")
        body = js[i:js.index("function vidRun(){")]
        self.assertIn("1280", body)
        self.assertIn("toDataURL('image/png')", body)

    def test_the_preview_shows_the_frames_the_button_sends(self):
        """One condition per attached frame: a placeholder slot in the preview would
        advertise conditioning on an image nobody attached, and the cockpit sends
        only attached frames."""
        js = APP_JS.read_text()
        block = js[js.index("function vidWireBody()"):js.index("function vidSync()")]
        self.assertNotIn("|| tag === 'first'", block)
        self.assertIn("filter(([tag]) => vidFrames[tag])", block)

    def test_the_tab_refuses_what_the_server_refuses(self):
        js = APP_JS.read_text()
        self.assertIn("const VID_BUDGET_S = 2 * 3600", js)
        block = js[js.index("function vidProblem()"):js.index("function vidWireBody()")]
        self.assertIn("VID_BUDGET_S", block)
        self.assertIn("1 to 100", block)

    def test_a_run_too_long_to_hold_keeps_its_id(self):
        """A 504 means the page stopped waiting, not that the video failed: the id
        must survive into something the reader can fetch when the lane is done."""
        js = APP_JS.read_text()
        i = js.index("r.status === 504", js.index("function vidRun("))
        self.assertIn("out.video_id", js[i - 40:i + 120])
        self.assertIn("vidParked", js)
        j = js.index("} finally {", i)
        self.assertIn("vidParked", js[j:j + 300])


class TheUninstallKnowsVideo(unittest.TestCase):
    def setUp(self):
        self.text = (REPO / "uninstall.sh").read_text()

    def test_the_unit_is_inventoried_and_removed(self):
        self.assertIn("qwen38-video.service", self.text)

    def test_the_runtime_is_read_from_the_unit_not_assumed(self):
        self.assertIn("qwen38-video.service", self.text[
            :self.text.index('VIDEO_LANE_DIR="${VIDEO_LANE_DIR:-$HOME/.local/share/qwen38-video}"') + 200])

    def test_the_checkpoint_pin_is_inventoried(self):
        self.assertIn("MiniMaxAI/MiniMax-H3", self.text)

    def test_only_what_the_installer_put_there_is_removed(self):
        """VIDEO_LANE_DIR can be shared: removing it whole took the rest with it on the
        image lane, so only venv/ and sglang/ go."""
        i = self.text.index('rm -rf "$VIDEO_LANE_DIR/venv"')
        self.assertIn("rmdir", self.text[i:i + 300])


if __name__ == "__main__":
    unittest.main()
