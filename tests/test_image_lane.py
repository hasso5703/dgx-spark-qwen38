#!/usr/bin/env python3
"""The Qwen-Image 2.1 lane: the refusals that were measured, held in place.

Every assertion here corresponds to a request that was actually made against the lane on
the reference box on 2026-09-22, and to the answer it gave. Three of them cost a live 500
to discover, which is the whole reason they are gates rather than documentation:

  * a size that is not a multiple of 32 comes back HTTP 500 with nothing in the body;
    the reason is only in the engine's own log ("must be divisible by 32").
  * output_format left out comes back HTTP 500 too. The API falls back to JPEG when the
    background is not transparent, this model returns RGBA for everything it makes, and
    PIL refuses to write RGBA as JPEG. The plainest possible request, a bare prompt,
    fails for that reason alone.
  * a CFG scale without a negative prompt is ignored byte for byte (schedule_batch.py
    needs both), so a tab that offers one without the other offers a dead control.

The rest holds the shape of the install: one engine at a time is a systemd Conflicts=
rather than a convention, the runtime pin is a full commit because Qwen-Image 2.1 is in
no SGLang release, and the sizes the cockpit offers are the seven ratios Qwen publishes.
"""
import pathlib
import re
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
UNIT_TPL = REPO / "qwen38-image.service.template"
INSTALLER = REPO / "install-image.sh"
COCKPIT = REPO / "dashboard" / "cockpit.py"
APP_JS = REPO / "dashboard" / "static" / "app.js"
INDEX = REPO / "dashboard" / "static" / "index.html"

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
        """A placeholder the installer does not know about lands in /etc verbatim and the
        unit fails to start minutes later, on a box whose engine has just been stopped.
        This is the same gate the engine templates have, for the same reason."""
        tpl = UNIT_TPL.read_text()
        used = set(re.findall(r"__[A-Z_]+__", tpl))
        sub = set(re.findall(r'-e "s\|(__[A-Z_]+__)\|', INSTALLER.read_text()))
        self.assertEqual(used - sub, set(), "the installer does not substitute these")

    def test_one_engine_at_a_time_is_declared_not_documented(self):
        """31 GB of image weights do not fit beside a serving LLM, and a wrapper script
        that stops the other lane can be bypassed by typing systemctl start."""
        tpl = UNIT_TPL.read_text()
        self.assertRegex(tpl, r"(?m)^Conflicts=.*qwen38-sglang\.service")
        self.assertRegex(tpl, r"(?m)^Conflicts=.*qwen38-flash\.service")

    def test_the_other_lane_is_stopped_before_this_one_starts(self):
        """Conflicts= orders nothing (systemd.unit(5)): the stop of the other lane and the
        start of this one ran side by side, 5 s, 6.7 s and 60 s on the reference box on
        2026-09-23. Measured with two throwaway units on its systemd 255 on 2026-09-24:
        without After= the new lane started 3.0 s before the old one had stopped, in both
        directions; with After= on this unit only, 0.02 s after, in both directions. So
        every unit this one conflicts with must also be one it is ordered after."""
        tpl = UNIT_TPL.read_text()
        unit = tpl.split("[Service]")[0]
        def listed(key):
            return {u for line in re.findall(rf"(?m)^{key}=(.*)$", unit) for u in line.split()}
        self.assertTrue(listed("Conflicts"), "the template still declares its conflicts")
        self.assertEqual(listed("Conflicts") - listed("After"), set(),
                         "conflicting units this lane is not ordered after")

    def test_it_serves_the_cookbook_recipe_and_does_not_force_what_the_runtime_picks(self):
        """--performance-mode speed is the DGX Spark recipe. The attention backend is NOT
        forced: the runtime logs "Defaulting to Torch SDPA backend on SM12.x" on its own,
        and naming it in the unit is how a verified recipe silently stops being one."""
        tpl = UNIT_TPL.read_text()
        self.assertIn("--performance-mode speed", tpl)
        self.assertNotIn("--attention-backend", tpl)
        self.assertNotIn("--enable-torch-compile", tpl)

    def test_it_passes_no_flag_the_diffusion_parser_does_not_have(self):
        """The diffusion runtime is a different parser from the LLM one and has neither
        --api-key nor --sleep-on-idle. Either kills the unit at startup with
        "unrecognized arguments", which is how the first install of this lane failed on
        the reference box. `sglang serve --help` lists both, because without a resolvable
        diffusion model it prints the LLM parser, so the help text is not the evidence."""
        for absent in ("--api-key", "--sleep-on-idle"):
            self.assertNotIn(absent, exec_start(), absent)

    def test_the_lane_is_loopback_because_it_cannot_authenticate_anyone(self):
        """No --api-key means no key. The bind is the only thing in front of it, and it
        does not follow ENGINE_BIND, which belongs to the lane that does have one."""
        self.assertIn("__IMAGE_BIND__", UNIT_TPL.read_text())
        text = INSTALLER.read_text()
        self.assertIn('IMAGE_BIND="${IMAGE_BIND:-$(unit_flag --host)}"', text)
        self.assertIn('IMAGE_BIND="${IMAGE_BIND:-127.0.0.1}"', text)
        self.assertIn("anyone who can reach that address can generate on your GPU", text)

    def test_a_lane_still_loading_its_weights_is_not_reported_as_ready(self):
        """/health answers 503 for the ~70 s it takes to load 31 GB, while systemd reads
        `active` throughout. Only a 200 is ready, and there is ONE probe: the lifecycle's.
        The status the tab polls takes its word instead of asking the lane a second time."""
        text = COCKPIT.read_text()
        healthy = text[text.index("def image_healthy("):text.index("def image_status(")]
        self.assertIn('urllib.request.urlopen(urllib.request.Request(image_base() + "/health")', healthy)
        self.assertIn("return False", healthy)          # any non-200 answer is "not yet"
        st = text[text.index("def image_status("):text.index("def _image_decoded_refs(")]
        self.assertIn('out["available"] = out["state"] in ("ready", "degraded")', st)
        self.assertNotIn("/health", st.split('"""', 2)[2], "image_status probes the lane itself again")

    def test_the_cockpit_does_not_send_a_key_the_lane_cannot_check(self):
        """A Bearer header on a server with no --api-key is noise that reads as a gate.
        What it actually puts on the wire is asserted in dashboard/tests/test_image_routes.py;
        this holds the source so a future edit cannot quietly put one back."""
        text = COCKPIT.read_text()
        img = text[text.index("def image_call("):text.index("def _multipart(")]
        self.assertNotIn("api_key()", img)

    def test_the_server_is_told_not_to_keep_a_copy_of_everything(self):
        """Left alone this server writes every image it makes and every reference anyone
        uploads, forever: 103 MB in one afternoon on the reference box. Empty string is
        read back as None, which server_args.py does explicitly."""
        tpl = UNIT_TPL.read_text()
        self.assertIn('--output-path ""', tpl)
        self.assertIn('--input-save-path ""', tpl)

    def test_it_is_not_enabled_at_boot_by_the_installer(self):
        """Starting it stops the LLM lane. A box that reboots comes back the way its
        owner left it, not holding 31 GB of image weights nobody asked for."""
        self.assertNotRegex(INSTALLER.read_text(), r"systemctl\s+enable\s+(--now\s+)?\"?\$UNIT")


class TheInstaller(unittest.TestCase):
    def run_it(self, *args, **env):
        e = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": tempfile.mkdtemp(prefix="img-home-")}
        e.update(env)
        r = subprocess.run([str(INSTALLER), *args], capture_output=True, text=True,
                           env=e, cwd=str(REPO), timeout=60)
        return r.returncode, r.stdout + r.stderr

    def test_it_refuses_root(self):
        """Under sudo the venv, the weights and the key all move to /root, and the unit
        points at paths the real user cannot read. get.sh and install.sh refuse the same
        way; this one is reachable on its own, so it refuses on its own.

        Run under a faked uid 0 (an `id` on PATH, as test_install_root_refusal.py does),
        with an unknown option: past the wall the run stops at that option, before anything
        is written, so a wall that no longer refuses shows here. The message alone was
        checked, and a disabled check that kept it passed (found in review, 2026-09-24)."""
        fake = pathlib.Path(tempfile.mkdtemp(prefix="img-fake-root-"))
        (fake / "id").write_text('#!/bin/sh\ncase "$*" in -u) echo 0 ;; -un) echo root ;; '
                                 '*) exec /usr/bin/id "$@" ;; esac\n')
        (fake / "id").chmod(0o755)
        path = f"{fake}:/usr/local/bin:/usr/bin:/bin"
        rc, out = self.run_it("--wat", PATH=path)
        self.assertEqual(rc, 1, out)
        self.assertIn("not as root", out)
        self.assertNotIn("unknown option", out)
        # the way out is real, and reaches the next refusal down
        rc, out = self.run_it("--wat", PATH=path, ALLOW_ROOT="1")
        self.assertEqual(rc, 1, out)
        self.assertIn("unknown option: --wat", out)
        self.assertNotIn("not as root", out)

    def test_the_source_overlay_needs_no_rust_toolchain(self):
        """DGX OS ships no cargo, and the pinned source declares Rust extensions that
        only the LLM runtime uses: built with them, the overlay failed on every box
        without a Rust toolchain (reference box in a clean login, 2026-09-23)."""
        text = INSTALLER.read_text()
        pip = [ln for ln in text.splitlines() if '"$VENV/bin/pip" install' in ln and '-e "$SRC/python"' in ln]
        self.assertEqual(len(pip), 1, pip)
        self.assertIn("SGLANG_BUILD_RUST_EXTS=none", pip[0])

    def test_the_runtime_pin_is_a_full_commit(self):
        """Qwen-Image 2.1 is in no SGLang release, so the lane runs a source checkout. A
        branch name would make two boxes install two different runtimes from one command."""
        m = re.search(r'PIN="\$\{SGLANG_DIFFUSION_PIN:-([^}]+)\}"', INSTALLER.read_text())
        self.assertIsNotNone(m, "the pin is no longer assigned the way this test reads it")
        self.assertRegex(m.group(1), r"^[0-9a-f]{40}$")

    def test_the_wheel_goes_in_before_the_source(self):
        """Order is not cosmetic: the released wheel carries sglang-kernel built for
        aarch64, which a source tree does not build. Source first leaves a runtime with
        no native kernels that dies on the first request."""
        text = INSTALLER.read_text()
        self.assertLess(text.index('sglang[diffusion]=='), text.index('-e "$SRC/python"'))
        self.assertIn("--no-deps", text, "the editable overlay must not re-resolve dependencies")

    def test_it_checks_for_room_before_downloading_31_gb(self):
        # where the checkpoint lands, before the download step (the behaviour itself is
        # in test_image_lane_install_paths.py)
        text = INSTALLER.read_text()
        self.assertIn("df -BG", text)
        check = text.index('FREE_W="$(free_gb "$HF_CACHE")"')
        self.assertLess(check, text.index('step "4/6 Checkpoint'))

    def test_an_unknown_option_is_refused_rather_than_ignored(self):
        code, out = self.run_it("--wat")
        self.assertNotEqual(code, 0)
        self.assertIn("unknown option", out)

    def test_the_text_lane_comes_back_however_the_smoke_test_ends(self):
        """The smoke test stops the serving LLM lane. Every failure below that point used
        to exit through die(), leaving the box serving nothing with the image unit holding
        31 GB, until somebody noticed. Putting it back belongs in a trap, not on the
        happy path."""
        text = INSTALLER.read_text()
        self.assertIn("trap restore_text_lane EXIT", text)
        body = text[text.index("restore_text_lane() {"):text.index("trap restore_text_lane EXIT")]
        self.assertIn('systemctl stop "$UNIT"', body)
        self.assertIn('systemctl start "$WAS_LLM"', body)
        # and nothing may restart it on the happy path instead
        after = text[text.index("trap restore_text_lane EXIT"):]
        self.assertNotIn('if [ -n "$WAS_LLM" ]; then sudo systemctl start', after)

    def test_a_routine_rerun_does_not_stop_the_engine_to_redo_the_smoke_test(self):
        """An upgrade of a box that happens to have the lane must not cost it minutes of
        unserved traffic. The smoke test runs on the install that asked for the lane."""
        text = (REPO / "install.sh").read_text()
        self.assertIn('[ "$WITH_IMAGE" -eq 0 ] && IMAGE_ARGS=(--no-smoke)', text)

    def test_flags_that_would_silently_do_nothing_are_refused_at_parse_time(self):
        """--no-start and --no-service both return before the image step, so the flag
        would be accepted and quietly ignored at the end of a long install."""
        for flag in ("--no-start", "--no-service"):
            e = {"PATH": "/usr/local/bin:/usr/bin:/bin",
                 "HOME": tempfile.mkdtemp(prefix="img-home-"), "MODEL_CHOICE": "stock"}
            r = subprocess.run([str(REPO / "install.sh"), "--with-image", flag],
                               capture_output=True, text=True, env=e, cwd=str(REPO), timeout=60)
            self.assertNotEqual(r.returncode, 0, flag)
            self.assertIn("--with-image needs the full install", r.stdout + r.stderr, flag)

    def test_uninstall_reads_the_lane_directory_from_the_unit(self):
        """install-image.sh takes IMAGE_LANE_DIR. A lane installed elsewhere would be
        reported clean and left on disk."""
        text = (REPO / "uninstall.sh").read_text()
        self.assertIn("^WorkingDirectory=", text)

    def test_a_serving_image_lane_is_left_serving_by_the_smoke_test(self):
        """Re-running the installer on a box whose image lane is serving must not end
        with it stopped: the trap used to stop the unit unconditionally on the way out."""
        text = INSTALLER.read_text()
        self.assertIn('systemctl is-active --quiet "$UNIT" 2>/dev/null && WAS_IMAGE=1', text)
        body = text[text.index("restore_text_lane() {"):text.index("trap restore_text_lane EXIT")]
        self.assertIn('[ "$WAS_IMAGE" -eq 1 ] || sudo systemctl stop "$UNIT"', body)

    def test_the_smoke_test_proves_a_picture_not_a_status_code(self):
        """An HTTP 200 from this lane can carry an image of the wrong size, or no image.
        The install ends by reading the PNG header it got back."""
        text = INSTALLER.read_text()
        self.assertIn("\\x89PNG", text)
        self.assertIn("(512, 512)", text)

    def test_the_smoke_test_measures_what_the_lane_costs_at_rest(self):
        """1.047 cores at rest is how the idle-loop defect showed itself. Every install
        now reads that number off the unit's own cgroup and says it."""
        text = INSTALLER.read_text()
        smoke = text[text.index('step "6/6'):]
        self.assertIn("systemctl show -p ControlGroup --value", smoke)
        self.assertIn("usage_usec", smoke)
        self.assertIn("at rest:", smoke)


PATCH = REPO / "image-sglang" / "scheduler-idle-poll.patch"
PATCHED = "python/sglang/multimodal_gen/runtime/managers/scheduler.py"
HTTP_PATCH = REPO / "image-sglang" / "http-graceful-timeout.patch"
HTTP_PATCHED = "python/sglang/multimodal_gen/runtime/launch_server.py"
# Each hunk's context as it stands at the pin, three lines either side of the insertion.
CONTEXT_AT_PIN = (
    "                        self._poller.poll(timeout=remaining_ms)\n"
    "                    elif remaining_ms > 0:\n"
    "                        time.sleep(remaining_ms / 1000.0)\n"
    "                continue\n"
    "\n"
    "            if self.metrics is not None:\n"
)
HTTP_CONTEXT_AT_PIN = (
    "        port=server_args.port,\n"
    "        reload=False,\n"
    "        ws_per_message_deflate=False,\n"
    "    )\n"
    "\n"
    "\n"
)


def added_code(patch: pathlib.Path) -> list:
    diff = patch.read_text()
    return [ln[1:].strip() for ln in diff.splitlines()
            if ln.startswith("+") and not ln.startswith("+++") and not ln[1:].strip().startswith("#")]


class TheLocalPatches(unittest.TestCase):
    """Two local changes to the pinned SGLang source, both measured on the reference box.
    The idle loop: an idle lane held one CPU core at 100% (1.047 cores, against 0.029 for
    the 27B lane and 0.045 with the patch). The HTTP shutdown: a Stop during a 2048x2048
    request waited 60 s for its connection until systemd killed the lane and marked the
    unit failed (2026-09-23); bounded at 5 s, an isolated uvicorn with a request in flight
    stopped in 5.2 s with Result=success. Whether they still apply to the pin is a CI step
    that fetches the real files; these hold what they do and how the installer carries them."""

    def test_each_patch_touches_one_file_and_removes_nothing(self):
        for patch, target in ((PATCH, PATCHED), (HTTP_PATCH, HTTP_PATCHED)):
            diff = patch.read_text()
            self.assertEqual(re.findall(r"^\+\+\+ b/(.+)$", diff, re.M), [target], patch.name)
            removed = [ln for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---")]
            self.assertEqual(removed, [], f"{patch.name} changes upstream lines instead of adding")

    def test_the_idle_patch_only_waits_on_the_socket_when_nothing_is_queued(self):
        self.assertEqual(added_code(PATCH), ["elif not self.waiting_queue and self.receiver is not None:",
                                             "self._poller.poll(timeout=1000)"])

    def test_the_http_patch_only_bounds_the_graceful_shutdown(self):
        self.assertEqual(added_code(HTTP_PATCH), ["timeout_graceful_shutdown=5,"])

    def test_the_installer_applies_exactly_the_patches_in_the_repo(self):
        listed = re.search(r"^PATCHES=\((.*)\)$", INSTALLER.read_text(), re.M).group(1).split()
        on_disk = sorted(f.stem for f in (REPO / "image-sglang").glob("*.patch"))
        self.assertEqual(sorted(listed), on_disk)

    def _block(self):
        """The installer's own lines for the patches, run as they are written."""
        t = INSTALLER.read_text()
        end = 'that part runs as upstream wrote it."\n  fi\ndone\n'
        return t[t.index("PATCHES=("):t.index(end) + len(end)]

    def _git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                              text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(repo),
                                              "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                              "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()

    def _repo(self, sched=CONTEXT_AT_PIN, http=HTTP_CONTEXT_AT_PIN):
        d = pathlib.Path(tempfile.mkdtemp(prefix="img-src-"))
        self._git(d, "init", "-q")
        for rel, text in ((PATCHED, sched), (HTTP_PATCHED, http)):
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(text)
        self._git(d, "add", "-A")
        self._git(d, "commit", "-qm", "pin")
        self._git(d, "remote", "add", "origin", str(d))
        return d

    def _install(self, src, pin):
        script = ("set -euo pipefail\ndie(){ echo \"DIE: $*\"; exit 1; }\n"
                  f'HERE="{REPO}"; SRC="{src}"; PIN="{pin}"\n' + self._block())
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout + r.stderr

    def test_applied_once_then_recognised(self):
        src = self._repo()
        pin = self._git(src, "rev-parse", "HEAD")
        rc, out = self._install(src, pin)
        self.assertEqual(rc, 0, out)
        self.assertIn("scheduler-idle-poll: applied", out)
        self.assertIn("http-graceful-timeout: applied", out)
        rc, out = self._install(src, pin)
        self.assertEqual(rc, 0, out)
        self.assertIn("scheduler-idle-poll: already applied", out)
        self.assertIn("http-graceful-timeout: already applied", out)
        self.assertEqual((src / PATCHED).read_text().count("self._poller.poll(timeout=1000)"), 1)
        self.assertEqual((src / HTTP_PATCHED).read_text().count("timeout_graceful_shutdown=5"), 1)

    def test_a_new_pin_checks_out_over_a_patched_tree(self):
        """The patches are local edits, and git refuses to check out over a local edit of a
        file the new commit changes. Without taking them off first, the next pin bump would
        die at the checkout on every box that ever installed the lane."""
        src = self._repo()
        old = self._git(src, "rev-parse", "HEAD")
        self._install(src, old)                                  # a box patched at the old pin
        self._git(src, "stash", "-q")
        (src / PATCHED).write_text(CONTEXT_AT_PIN + "# a later upstream commit\n")
        (src / HTTP_PATCHED).write_text(HTTP_CONTEXT_AT_PIN + "# a later upstream commit\n")
        self._git(src, "commit", "-qam", "new pin")
        new = self._git(src, "rev-parse", "HEAD")
        self._git(src, "checkout", "-q", old)
        self._git(src, "stash", "pop", "-q")                     # back to: old pin, patched
        rc, out = self._install(src, new)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._git(src, "rev-parse", "HEAD"), new)
        self.assertIn("scheduler-idle-poll: applied", out)
        self.assertIn("http-graceful-timeout: applied", out)

    def test_a_pin_one_no_longer_fits_is_a_note_not_a_failure(self):
        src = self._repo(http=HTTP_CONTEXT_AT_PIN.replace("reload=False", "reload=True"))
        rc, out = self._install(src, self._git(src, "rev-parse", "HEAD"))
        self.assertEqual(rc, 0, out)
        self.assertIn("NOTE: http-graceful-timeout does not apply", out)
        self.assertIn("scheduler-idle-poll: applied", out)       # the other one still goes in

    def test_they_go_in_before_the_runtime_is_checked(self):
        text = INSTALLER.read_text()
        self.assertLess(text.index('git -C "$SRC" checkout --quiet "$PIN"'),
                        text.index('git -C "$SRC" apply "$PF"'))
        self.assertLess(text.index('git -C "$SRC" apply "$PF"'),
                        text.index("the runtime does not know Qwen-Image 2.1"))


class TheOneLinerReachesIt(unittest.TestCase):
    """The lane has to be installable the way everything else on this box is: one
    command, from nothing. These read install.sh's source rather than run it, because
    the decision is made at the very end, after an engine is up and answering."""

    def setUp(self):
        self.text = (REPO / "install.sh").read_text()

    def test_the_flag_exists_and_is_parsed(self):
        self.assertIn("--with-image) WITH_IMAGE=1 ;;", self.text)
        self.assertIn("--no-image) NO_IMAGE=1 ;;", self.text)

    def test_get_sh_passes_it_through(self):
        """curl ... | bash -s -- --with-image has to reach install.sh untouched."""
        get = (REPO / "get.sh").read_text()
        self.assertIn('"$@"', get)

    def test_an_installed_lane_survives_a_plain_rerun(self):
        """An upgrade must not silently drop a lane someone installed on purpose, which
        is the same promise the cockpit, opencode and the bind choices already make."""
        self.assertIn("[ -f /etc/systemd/system/qwen38-image.service ] && IMAGE_ON=1", self.text)
        self.assertIn('[ "$WITH_IMAGE" -eq 1 ] && IMAGE_ON=1', self.text)
        self.assertIn('[ "$NO_IMAGE" -eq 1 ] && IMAGE_ON=0', self.text)
        # and the order matters: an explicit --no-image has to win over the installed unit
        self.assertLess(self.text.index("[ -f /etc/systemd/system/qwen38-image.service ] && IMAGE_ON=1"),
                        self.text.index('[ "$NO_IMAGE" -eq 1 ] && IMAGE_ON=0'))

    def test_it_runs_after_the_engine_is_up_and_cannot_fail_the_install(self):
        """It is the only step that stops the engine that is already serving, and the
        only one that costs 38 GB. A lane that will not install must not take down an
        install that is otherwise finished."""
        # The normal path's call, the one after a text engine proved itself. The other
        # call belongs to the image-is-the-boot-lane exit, which ends before step 9 on
        # purpose and is held by AnUpdateKeepsTheImageLaneAsTheBootLane.
        cockpit = self.text.index("10/10 Spark Cockpit")
        i = self.text.index('"$REPO_DIR/install-image.sh"', cockpit)
        self.assertLess(cockpit, i)
        tail = self.text[i:i + 500]
        self.assertIn("Everything above is up and serving", tail)
        self.assertEqual(self.text.count('"$REPO_DIR/install-image.sh"'), 2,
                         "one call per path, the normal one and the image-boot one")

    def test_the_help_says_what_it_costs_before_someone_spends_it(self):
        self.assertIn("--with-image", self.text)
        self.assertIn("38 GB", self.text)


class AnUpdateKeepsTheImageLaneAsTheBootLane(unittest.TestCase):
    """install.sh keeps the operator's choices across a re-run: the target, the context
    mode, the port. Once the image lane is a lane, which lane the box boots is one of
    those choices. Before this, a box switched to images fell through to "27b", got the
    27B unit enabled beside the image one, and was left with two engines set to start at
    the next reboot."""

    def setUp(self):
        self.text = (REPO / "install.sh").read_text()

    def test_it_is_detected_where_the_other_two_lanes_are(self):
        self.assertIn("systemctl is-enabled --quiet qwen38-image.service", self.text)
        self.assertIn('[ "$SGL_ENABLED" -eq 0 ] && [ "$FLASH_ENABLED" -eq 0 ]', self.text)

    def test_the_text_unit_is_not_enabled_on_that_path(self):
        i = self.text.index('if [ "$IMAGE_BOOT" -eq 0 ]; then')
        self.assertIn('sudo systemctl enable "$UNIT_NAME"', self.text[i:i + 120])
        # and that is the ONLY enable of the serving unit: an unguarded one would undo it
        self.assertEqual(self.text.count('sudo systemctl enable "$UNIT_NAME"'), 1)

    def test_that_path_ends_before_any_text_engine_is_started(self):
        i = self.text.index('if [ "$IMAGE_BOOT" -eq 0 ]; then')
        early = self.text[i:self.text.index("exit 0", i)]
        self.assertNotIn("systemctl start", early)
        self.assertIn("--no-smoke", early, "the smoke test stops and starts the serving lane")
        self.assertLess(i, self.text.index('step "9/10 Starting'))


class TheBootLaneConvergenceHoldsEveryWay(unittest.TestCase):
    """What an adversarial review found in the first version of the boot-lane logic."""

    def setUp(self):
        self.text = (REPO / "install.sh").read_text()
        self.sw = (REPO / "switch-model.sh").read_text()

    def test_an_explicit_model_choice_is_honoured(self):
        """MODEL_CHOICE=flash on a box booting images asks for flash. Keeping the image
        lane answered "flash updated, not served" and exited 0."""
        i = self.text.index("IMAGE_BOOT=0\nif systemctl is-enabled --quiet qwen38-image.service")
        block = self.text[i:i + 900]
        self.assertIn('if [ -n "$_ENV_MODEL_CHOICE" ]; then', block)
        self.assertLess(block.index('if [ -n "$_ENV_MODEL_CHOICE" ]; then'), block.index("IMAGE_BOOT=1"))

    def test_the_normal_path_takes_the_image_lane_off_the_boot(self):
        """Enabling a text lane beside an enabled image lane is two engines at the next boot."""
        self.assertIn('if [ "$IMAGE_BOOT" -eq 0 ] && systemctl is-enabled --quiet qwen38-image.service', self.text)
        self.assertIn("sudo systemctl disable qwen38-image.service", self.text)

    def test_the_image_boot_path_restarts_the_proxy_it_rewrote(self):
        i = self.text.index('if [ "$IMAGE_BOOT" -eq 0 ]; then')
        early = self.text[i:self.text.index("exit 0", i)]
        self.assertIn('sudo systemctl restart "$KEEPALIVE_UNIT"', early)

    def test_the_image_boot_path_respects_no_image(self):
        i = self.text.index('if [ "$IMAGE_BOOT" -eq 0 ]; then')
        early = self.text[i:self.text.index("exit 0", i)]
        call = early.index('"$REPO_DIR/install-image.sh" --no-smoke')
        self.assertIn('if [ "$NO_IMAGE" -eq 0 ]; then', early[:call])

    def test_the_text_lane_used_before_images_is_the_one_brought_up_to_date(self):
        """A switch to images disables both text units, so enablement cannot say which one
        was in use: the switch writes it down, and install.sh reads it."""
        self.assertIn('> "$CONFIG_DIR/lane-before-image"', self.sw)
        # written BEFORE the loop that disables them, or it would record nothing
        self.assertLess(self.sw.index('> "$CONFIG_DIR/lane-before-image"'),
                        self.sw.index('sudo systemctl disable "$TEXT_UNIT_NAME"'))
        self.assertIn('[ "$LANE_BEFORE_IMAGE" = "qwen38-flash.service" ]', self.text)

    def test_it_never_points_at_a_switch_target_that_does_not_exist(self):
        """A kept custom model is MODEL_CHOICE=custom, which switch-model.sh rejects."""
        i = self.text.index('if [ "$IMAGE_BOOT" -eq 0 ]; then')
        early = self.text[i:self.text.index("exit 0", i)]
        self.assertIn('if [ "$MODEL_CHOICE" = "custom" ]; then', early)


class ThePageNeverShowsAStaleOrRacingState(unittest.TestCase):
    """The frontend findings of the same review. All three pass in a page opened fresh and
    fail in one left open, which is how the cockpit is actually used."""

    def setUp(self):
        self.js = APP_JS.read_text()

    def test_the_text_engines_target_never_labels_the_image_lane(self):
        self.assertIn("unit !== IMAGE_UNIT && F.target", self.js)
        self.assertIn("s[0] !== IMAGE_UNIT && F.target", self.js)
        down = self.js[self.js.index("function rEngineInfoDown("):self.js.index("function showEngineFacts(")]
        self.assertIn("F.target = null", down)

    def test_this_pages_own_request_keeps_generate_off(self):
        for fn in ("function imgSync(){", "function imgRenderLane(){"):
            body = self.js[self.js.index(fn):]
            line = [ln for ln in body.splitlines() if "$('imgrun').disabled" in ln][0]
            self.assertIn("imgInflight", line, fn)

    def test_nothing_is_claimed_before_the_first_lifecycle_snapshot(self):
        render = self.js[self.js.index("function imgRenderLane(){"):]
        self.assertLess(render.index("if (!F.life){"), render.index("} else if (!e){"))


class AGenerationCanBeCancelled(unittest.TestCase):
    """SGLang Diffusion cannot abort a request, and nothing in the page could end one: a
    47-minute call (ten 2048x2048 images at 60 steps) on 2026-09-23 had no way out but the
    lane's Stop, which timed out. Cancel restarts the lane, and whatever cuts a request
    this page is waiting on (Cancel, the lane's Stop, a restart from the Engines card)
    makes that request read as cancelled, not as a lane that failed to answer."""

    def setUp(self):
        self.js = APP_JS.read_text()

    def test_the_button_is_hidden_until_something_generates(self):
        html = INDEX.read_text()
        self.assertRegex(html, r'<button class="btn mini danger" id="imgcancel" hidden')
        # beside the run's clock, where the eye is while it waits, not under the settings
        head = html[html.index("<h3>The image "):]
        self.assertLess(head.index('id="imgtime"'), head.index('id="imgcancel"'))
        self.assertLess(head.index('id="imgcancel"'), head.index("</h3>"))
        self.assertIn("$('imgcancel').addEventListener('click', imgCancel);", self.js)

    def test_only_a_stop_or_restart_of_the_image_lane_marks_a_request_cancelled(self):
        self.assertIn("if (name === 'unit' && params.unit === IMAGE_UNIT && params.verb !== 'start') "
                      "IMG_INTERRUPTED = Date.now();", self.js)

    def test_a_cut_request_reads_as_cancelled_before_it_reads_as_refused(self):
        run = self.js[self.js.index("async function imgRun(){"):self.js.index("let imgPoll = null;")]
        self.assertLess(run.index("if (!r.ok && (IMG_INTERRUPTED > t0 || out.interrupted)){"), run.index("if (!r.ok){"))
        self.assertIn("setChip('imgtime', 'cancelled', 'warn')", run)


class TheImageLaneIsALaneLikeTheOthers(unittest.TestCase):
    """Switched to from the one switcher at the top, started and stopped by the action
    bar's lane button, gated by the same "never two engines at once" rule, drawn by the
    same Engines card. The first version put a Start button inside the Image tab, which
    started the lane by a path none of the other lanes use and, through the unit's
    Conflicts=, stopped the text lane silently where the cockpit's rule for every other
    lane is to refuse and say "stop it first"."""

    def test_it_is_in_the_switcher(self):
        html = INDEX.read_text()
        sel = re.search(r'<select id="switchsel".*?</select>', html, re.S).group(0)
        self.assertIn('<option value="image">', sel)
        # and grouped apart: it is not one more LLM checkpoint
        self.assertIn('<optgroup label="Images">', sel)

    def test_the_tab_says_switch_before_stop_like_the_readme(self):
        """Stopped first, the lane button offers "Start 27B", the unit still enabled: one
        click from a 7-minute boot nobody asked for. The README, the installer and the
        tab give the same order, switch first."""
        js = APP_JS.read_text()
        render = js[js.index("function imgRenderLane(){"):]
        self.assertLess(render.index("then press Switch"), render.index("two engines never run at once"))
        readme = (REPO / "README.md").read_text()
        self.assertIn("switcher, **Switch**, stop the serving lane, **Start Qwen-Image**", readme)
        self.assertIn("pick Qwen-Image 2.1 in the switcher, Switch, stop the serving lane, Start",
                      INSTALLER.read_text())

    def test_the_switch_accepts_it_everywhere_it_is_checked(self):
        cock = COCKPIT.read_text()
        enum = re.search(r'"switch":\s*\{.*?"params":\s*\{"target":\s*\[(.*?)\]\}', cock, re.S)
        self.assertIn('"image"', enum.group(1))
        sw = (REPO / "switch-model.sh").read_text()
        guard = re.search(r'case "\$CHOICE" in ([a-z0-9|-]+)\)', sw).group(1)
        self.assertIn("image", guard.split("|"))

    def test_it_is_an_engine_to_the_lifecycle_and_a_unit_to_the_collector(self):
        lc = (REPO / "dashboard" / "lifecycle.py").read_text()
        self.assertRegex(lc, r'ENGINE_UNITS = \([^)]*"qwen38-image\.service"')
        cock = COCKPIT.read_text()
        units = cock[cock.index("UNITS = ("):cock.index(")", cock.index("UNITS = ("))]
        self.assertIn("qwen38-image.service", units)

    def test_the_image_tab_has_no_lane_control_of_its_own(self):
        """One place starts and stops lanes. A second one is how the image lane came to
        behave differently from the two others in the first place."""
        js = APP_JS.read_text()
        tab = js[js.index("async function imgLane(){"):js.index("function imgInit(){")]
        # The one unit action the tab sends is the restart that cancels a generation:
        # SGLang Diffusion cannot abort a request, so that is the only way to end one, and
        # a 47-minute call (ten 2048x2048 images at 60 steps, 2026-09-23) had no way out
        # but the lane's Stop. It starts and stops nothing: no Start, no Stop, here.
        calls = re.findall(r"askAction\('unit', \{verb: '(\w+)'", tab)
        self.assertEqual(calls, ["restart"], "the Image tab starts or stops a lane again")
        cancel = tab[tab.index("function imgCancelSync(){"):tab.index("function imgCancel(){")]
        self.assertIn("imgInflight || IMG_STATE.busy", cancel, "Cancel must only show while a generation runs")
        self.assertNotIn("function imgUnitButton", js)
        # and it points at the controls that do exist, named as they read on screen
        self.assertIn("Start Qwen-Image", tab)
        self.assertIn("press Switch", tab)

    def test_switching_to_a_text_target_takes_the_image_lane_off_the_boot(self):
        """Exactly one serving unit enabled at boot. Leaving the image lane enabled when a
        text lane is made the boot lane would bring two engines up at the next reboot."""
        sw = (REPO / "switch-model.sh").read_text()
        self.assertIn('sudo systemctl disable "$IMAGE_UNIT_NAME"', sw)

    def test_switching_to_image_leaves_the_text_clients_alone(self):
        """The proxy is a text door and opencode a text client: pointing opencode's default
        model at an image lane would break every session it opened."""
        sw = (REPO / "switch-model.sh").read_text()
        branch = sw[sw.index('if [ "$CHOICE" = "image" ]; then'):sw.index("exit 0\nfi")]
        self.assertNotIn("oc-point-default", branch)
        self.assertNotIn("PROMPT_CEILING_TOKENS", branch)
        # it runs from its own venv: no serving image to inspect, no container to download with
        code = "\n".join(ln for ln in branch.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotIn("docker run", code)
        self.assertNotIn("docker image inspect", code)
        self.assertIn('"$IMG_PY" -', code)

    def test_every_privileged_call_the_image_switch_makes_is_allowlisted(self):
        sudoers = (REPO / "dashboard" / "sudoers-cockpit.template").read_text()
        for verb in ("start", "stop", "restart", "enable", "disable"):
            self.assertIn(f"/usr/bin/systemctl {verb} qwen38-image.service", sudoers, verb)

    def test_the_confirmation_says_how_this_lane_starts(self):
        js = APP_JS.read_text()
        self.assertIn("IMAGE_EXPLAIN", js)
        self.assertIn("only offered once no other engine is running", js)
        self.assertNotIn("stops whichever text lane", js)


class TheTabTellsTheTruth(unittest.TestCase):
    """The tab's defaults and its size list are facts about the model, so they are gated
    like facts: a drift here is a page that promises what the engine refuses."""

    def js_object(self, name):
        text = APP_JS.read_text()
        m = re.search(r"const %s = (\{.*?\n\}|\[[\s\S]*?\n\]);" % name, text, re.S)
        self.assertIsNotNone(m, name)
        return m.group(1)

    def test_the_defaults_are_the_models_own(self):
        """1024x1024, 40 steps, one image, CFG off, RNG on the CPU: read out of
        configs/sample/qwenimage21.py and configs/pipeline_configs/qwen_image21.py,
        not chosen by the page."""
        d = self.js_object("IMG_DEFAULTS")
        for fact in ("size: '1024x1024'", "steps: 40", "n: 1", "cfg: ''", "dev: 'cpu'",
                     "bg: 'auto'", "fmt: 'png'"):
            self.assertIn(fact, d, fact)

    def test_every_size_the_page_offers_is_a_multiple_of_32(self):
        sizes = re.findall(r"\['(\d+)x(\d+)'", self.js_object("IMG_SIZES"))
        self.assertGreaterEqual(len(sizes), 7)
        for w, h in sizes:
            self.assertEqual(int(w) % 32, 0, f"{w}x{h}")
            self.assertEqual(int(h) % 32, 0, f"{w}x{h}")

    def test_the_seven_ratios_qwen_publishes_are_all_reachable(self):
        """The model card names 1:1, 4:3, 3:4, 3:2, 2:3, 16:9 and 9:16. Offering five of
        them would quietly drop the ones a user came for."""
        sizes = [(int(w), int(h)) for w, h in re.findall(r"\['(\d+)x(\d+)'", self.js_object("IMG_SIZES"))]
        want = {1.0: "1:1", 4 / 3: "4:3", 3 / 4: "3:4", 3 / 2: "3:2",
                2 / 3: "2:3", 16 / 9: "16:9", 9 / 16: "9:16"}
        for ratio, name in want.items():
            self.assertTrue(any(abs(w / h - ratio) < 0.05 for w, h in sizes), name)

    def test_the_generate_button_stays_off_when_the_lane_cannot_take_it(self):
        """imgSync runs on every keystroke, so every reason the button should be off has
        to be in that one expression. Without the availability term, typing a character
        re-enabled it on a stopped lane; without the busy term it offers a request the
        cockpit will refuse with 409, because this lane takes one at a time."""
        js = APP_JS.read_text()
        sync = js[js.index("function imgSync(){"):js.index("function imgReset(){")]
        line = [ln for ln in sync.splitlines() if "$('imgrun').disabled" in ln]
        self.assertEqual(len(line), 1, "imgSync must decide that in one place")
        for term in ("IMG_STATE.available", "IMG_STATE.busy", "problem", "imgprompt"):
            self.assertIn(term, line[0], term)

    def test_the_copyable_curl_carries_no_key_the_lane_cannot_check(self):
        """The cockpit's own call had that header removed and gated; the snippet next to
        it must not keep offering one."""
        js = APP_JS.read_text()
        curl = js[js.index("function imgCurl(){"):js.index("function imgSync(){")]
        self.assertNotIn("Authorization", curl)
        self.assertNotIn("api-key", curl)
        # and it addresses the lane's own bind, not whatever host this browser is on
        self.assertNotIn("location.hostname", curl)
        self.assertIn("IMG_STATE.host", curl)

    def test_a_prompt_with_an_apostrophe_does_not_break_the_snippet(self):
        js = APP_JS.read_text()
        self.assertIn("const shq =", js)

    def test_jpeg_is_not_offered_at_all(self):
        """Offering a format the engine cannot produce is offering a 500."""
        fmt = re.search(r'<select id="imgfmt">(.*?)</select>', INDEX.read_text(), re.S)
        self.assertIsNotNone(fmt)
        self.assertNotIn("jpeg", fmt.group(1).lower())
        self.assertIn('value="png"', fmt.group(1))

    def test_the_page_says_cfg_needs_both_halves(self):
        js = APP_JS.read_text()
        self.assertIn("does nothing without a negative prompt", js)
        self.assertIn("does nothing without a CFG scale above 1", js)

    def test_the_reset_button_exists_and_restores_every_field(self):
        """Every control the page offers has to come back, or Reset is a lie about the
        two it forgot."""
        js = APP_JS.read_text()
        reset = re.search(r"function imgReset\(\)\{(.*?)\n\}", js, re.S)
        self.assertIsNotNone(reset)
        body = reset.group(1)
        for field in ("imgsize", "imgw", "imgh", "imgsteps", "imgn", "imgbg", "imgfmt",
                      "imgdev", "imgseed", "imgcfg", "imgshift", "imgneg", "imgprompt"):
            self.assertIn(field, body, field)
        self.assertIn('id="imgreset"', INDEX.read_text())

    def test_the_licence_is_on_the_page(self):
        """Qwen Research License: research and evaluation, not commercial use. A box that
        serves this to a team has to be told once, where they will read it."""
        self.assertIn("Qwen Research License", INDEX.read_text())
        self.assertIn("not commercial use", INDEX.read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
