#!/usr/bin/env python3
"""A plain install installs every lane (v1.20).

Both text lanes (the one MODEL_CHOICE names serves; the other is installed beside it by
this same script, started again for that lane with QWEN38_SECONDARY), the image lane and
the video lane. Each one a run installs for the first time proves it serves, all of them
together at the end, with the served lane stopped once and proved again after. Each can
be left out with --no-<lane>, remembered in a marker file like --no-cockpit.

These run install.sh's own lines under its own shell options, against stubs and from a
throwaway HOME, or a copy of the whole script that ends before step 1: nothing reaches
the box the suite runs on.
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import installer_wall as wall  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()


def between(start, end):
    s = TEXT.index(start)
    return TEXT[s:TEXT.index(end, s)]


def tmp(prefix):
    return pathlib.Path(tempfile.mkdtemp(prefix=prefix))


def stub(path, body):
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


class EachLaneIsWantedUnlessLeftOut(unittest.TestCase):
    """lane_wanted, run as written: on by default, --no-<lane> writes the marker and turns
    it off, the marker keeps it off on the next run, --with-<lane> removes it."""
    FUNC = between("lane_wanted(){", "\n}\n") + "\n}\n"

    def want(self, no, with_, marker=False):
        cfg = tmp("lane-want-") / ".config" / "qwen38"
        if marker:
            cfg.mkdir(parents=True)
            (cfg / "image.off").write_text("left out\n")
        script = (f'set -euo pipefail\nCONFIG_DIR="{cfg}"\n{self.FUNC}\n'
                  f'lane_wanted image {no} {with_}\necho "WANT=$WANT"\n')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout, (cfg / "image.off")

    def test_a_plain_run_wants_it(self):
        out, mark = self.want(0, 0)
        self.assertIn("WANT=1", out)
        self.assertFalse(mark.exists())

    def test_no_writes_the_marker(self):
        out, mark = self.want(1, 0)
        self.assertIn("WANT=0", out)
        self.assertIn("--no-image", mark.read_text())

    def test_the_marker_keeps_it_out_and_says_so(self):
        out, mark = self.want(0, 0, marker=True)
        self.assertIn("WANT=0", out)
        self.assertIn("Pass --with-image to bring it back", out)
        self.assertTrue(mark.exists())

    def test_with_brings_it_back(self):
        out, mark = self.want(0, 1, marker=True)
        self.assertIn("WANT=1", out)
        self.assertFalse(mark.exists())


class TheFlags(unittest.TestCase):
    """The flags reach lane_wanted before anything is downloaded: these copies stop at the
    refusal of --no-service with CONTEXT_MODE=1m, which comes right after it."""

    def run_install(self, *args):
        home = tmp("lane-flags-home-")
        env, record = wall.fenced_env(home=home, CONTEXT_MODE="1m")
        r = subprocess.run([wall.walled(), *args, "--no-service"], capture_output=True, text=True,
                           env=env, cwd=wall.cwd(), timeout=60)
        out = r.stdout + r.stderr
        self.assertNotIn(wall.WALL, out)
        self.assertEqual(wall.reached(record), [])
        return r.returncode, out, home / ".config" / "qwen38"

    def test_each_no_flag_is_remembered(self):
        for lane in ("image", "video", "flash", "27b"):
            rc, out, cfg = self.run_install(f"--no-{lane}")
            self.assertEqual(rc, 1, out)
            self.assertIn("CONTEXT_MODE=1m", out, "stopped at the refusal after the flags")
            self.assertTrue((cfg / f"{lane}.off").exists(), lane)

    def test_contradictions_are_refused(self):
        for lane in ("flash", "27b"):
            rc, out, cfg = self.run_install(f"--no-{lane}", f"--with-{lane}")
            self.assertEqual(rc, 1)
            self.assertIn(f"--no-{lane} and --with-{lane} contradict each other", out)
            self.assertFalse((cfg / f"{lane}.off").exists())


def converge(units, enabled, env, home=None):
    """A copy of install.sh that prints what it resolved and exits before step 1, with its
    units read from a directory the test owns and the box's commands fenced."""
    unit_dir = tmp("lane-units-")
    for name, text in units.items():
        (unit_dir / name).write_text(text)
    probe = 'echo "RESOLVED LANE=$LANE MODEL_CHOICE=$MODEL_CHOICE INSTALLED=$INSTALLED_CHOICE CONTEXT=$CONTEXT_MODE"; exit 97\n'
    at = 'step "1/10 Preflight checks"\n'
    text = TEXT.replace(at, probe + at, 1).replace(wall.UNITS, f"{unit_dir}/")
    assert "/etc/systemd" not in text[:text.index(probe)]
    copy = tmp("lane-walled-") / "install.sh"
    copy.write_text(text)
    copy.chmod(0o755)
    home = home or tmp("lane-home-")
    fenv, record = wall.fenced_env(home=home, enabled=enabled, **env)
    r = subprocess.run([str(copy)], capture_output=True, text=True, env=fenv, cwd=wall.cwd(), timeout=60)
    return r.returncode, r.stdout + r.stderr, wall.reached(record)


UNC = "edp1096/Huihui-RadixArk-Qwen3.8-27B-abliterated-NVFP4"
SGL_UNIT = ("[Service]\nExecStart=/bin/bash -c 'exec /usr/bin/docker run --rm --name qwen38-sglang "
            "-v /home/x/.cache/huggingface:/root/.cache/huggingface lmsysorg/sglang@sha256:" + "a" * 64 +
            f" python3 -m sglang.launch_server --model-path {UNC} --revision 21565d389fe573a32c1c425e0c7ade204ddb2263"
            " --context-length 1010000 --host 127.0.0.1 --port 30000'\n")
SHARED = {"PORT": "30000", "HF_CACHE": "/home/x/.cache/huggingface", "ENGINE_BIND": "127.0.0.1",
          "PROXY_BIND": "0.0.0.0", "PROXY_PORT": "30001"}


class TheOtherTextLaneConvergesOnItsOwnUnit(unittest.TestCase):
    """The run for the other lane follows that lane's unit, not the enabled one, which is
    the lane its parent serves: on the reference box, the 27B beside a serving flash."""

    def test_the_27b_beside_a_serving_flash_keeps_its_target_and_mode(self):
        rc, out, reached = converge({"qwen38-sglang.service": SGL_UNIT,
                                     "qwen38-flash.service": "[Service]\nExecStart=/bin/bash /x/launch-flash.sh\n"},
                                    enabled=("qwen38-flash.service",),
                                    env={"QWEN38_SECONDARY": "27b", **SHARED})
        self.assertEqual(rc, 97, out)
        self.assertIn("RESOLVED LANE=27b MODEL_CHOICE=uncensored INSTALLED=27b CONTEXT=1m", out)
        self.assertEqual(reached, [])

    def test_a_new_flash_lane_beside_a_serving_27b_takes_the_target_it_is_given(self):
        rc, out, _ = converge({"qwen38-sglang.service": SGL_UNIT}, enabled=("qwen38-sglang.service",),
                              env={"QWEN38_SECONDARY": "flash", "MODEL_CHOICE": "flash", **SHARED})
        self.assertEqual(rc, 97, out)
        self.assertIn("RESOLVED LANE=flash MODEL_CHOICE=flash INSTALLED= CONTEXT=native", out)

    def test_it_never_installs_the_lane_its_parent_serves(self):
        rc, out, _ = converge({"qwen38-sglang.service": SGL_UNIT}, enabled=("qwen38-sglang.service",),
                              env={"QWEN38_SECONDARY": "flash", "MODEL_CHOICE": "stock", **SHARED})
        self.assertEqual(rc, 1)
        self.assertIn("a run for the flash lane resolved the 27b lane", out)

    def test_a_box_booting_images_is_its_parents_business(self):
        rc, out, _ = converge({"qwen38-sglang.service": SGL_UNIT, "qwen38-image.service": "[Service]\n"},
                              enabled=("qwen38-image.service",),
                              env={"QWEN38_SECONDARY": "flash", "MODEL_CHOICE": "flash", **SHARED})
        self.assertEqual(rc, 97, out)
        self.assertNotIn("boot lane", out)

    def test_an_unknown_lane_is_refused(self):
        rc, out, _ = converge({}, enabled=(), env={"QWEN38_SECONDARY": "image"})
        self.assertEqual(rc, 1)
        self.assertIn("QWEN38_SECONDARY names a text lane", out)


class ATextLaneBeforeBothSideLanes(unittest.TestCase):
    """A switch to video writes down the lane it leaves, the image lane when images served
    (install-video.sh goes back to it), so the text lane is the one written before the
    images. The reference box went flash, image, video, and its update took the 27B for
    its text lane and pointed opencode at it (2026-10-06)."""

    FLASH_UNIT = "[Service]\nExecStart=/bin/bash /x/launch-flash.sh\n"
    LAUNCHER = ("docker run --rm -v /home/x/.cache/huggingface:/root/.cache/huggingface img "
                "python3 -m sglang.launch_server --model-path dealignai/Qwen3.8-Flash-Next-ABLITERATED-NVFP4 "
                "--revision be794b990578ef3031eccf9f28e675a289a09ee9 --port 30000 "
                "--max-running-requests 8 --context-length 262144\n")

    def resolve(self, before_image, before_video, boot):
        home = tmp("lane-sides-home-")
        cfg = home / ".config" / "qwen38"
        cfg.mkdir(parents=True)
        (cfg / "lane-before-image").write_text(before_image + "\n")
        (cfg / "lane-before-video").write_text(before_video + "\n")
        (cfg / "launch-flash.sh").write_text(self.LAUNCHER)
        units = {"qwen38-sglang.service": SGL_UNIT, "qwen38-flash.service": self.FLASH_UNIT,
                 "qwen38-image.service": "[Service]\n", "qwen38-video.service": "[Service]\n"}
        rc, out, _ = converge(units, enabled=(boot,), env=SHARED, home=home)
        self.assertEqual(rc, 97, out)
        return out

    def test_video_after_images_updates_the_text_lane_before_them(self):
        out = self.resolve("qwen38-flash.service", "qwen38-image.service", "qwen38-video.service")
        self.assertIn("RESOLVED LANE=flash", out)
        self.assertIn("INSTALLED=flash", out)

    def test_images_after_video_do_the_same(self):
        out = self.resolve("qwen38-video.service", "qwen38-flash.service", "qwen38-image.service")
        self.assertIn("INSTALLED=flash", out)

    def test_a_27b_before_them_stays_the_27b(self):
        out = self.resolve("qwen38-sglang.service", "qwen38-image.service", "qwen38-video.service")
        self.assertIn("INSTALLED=27b", out)


class ItLeavesWhatIsSharedToItsParent(unittest.TestCase):
    """What the run for the other lane must not touch, read where it would."""

    def test_step_7_is_not_its(self):
        i = TEXT.index('if [ -n "$SECONDARY" ]; then\n  step "7/10 opencode: left to the run that serves')
        self.assertLess(i, TEXT.index('elif [ "$OPENCODE" -eq 0 ]; then\n  step "7/10 opencode integration: off"'))

    def test_the_boot_lane_and_the_proxy_are_not_its(self):
        for guarded in ('if [ -z "$SECONDARY" ] && [ -n "$OTHER_UNIT" ] && systemctl is-enabled',
                        'if [ -z "$SECONDARY" ] && [ "$IMAGE_BOOT" -eq 0 ] && systemctl is-enabled --quiet qwen38-image.service',
                        'if [ -z "$SECONDARY" ] && [ "$VIDEO_BOOT" -eq 0 ] && systemctl is-enabled --quiet qwen38-video.service'):
            self.assertIn(guarded, TEXT)
        ka = TEXT.index('KEEPALIVE_UNIT="qwen38-keepalive.service"\n')
        gate = TEXT.index('if [ -z "$SECONDARY" ]; then\n', ka)
        self.assertLess(gate, TEXT.index('install -m 755 "$REPO_DIR/keepalive-proxy.py"', ka))
        self.assertLess(gate, TEXT.index('sudo systemctl enable "$KEEPALIVE_UNIT"', ka))

    def test_it_ends_before_enabling_or_starting_anything(self):
        end = TEXT.index('if [ -n "$SECONDARY" ]; then\n  # Installed, not enabled and not started')
        self.assertLess(TEXT.rindex("sudo systemctl daemon-reload", 0, end), end)
        self.assertLess(end, TEXT.index('  sudo systemctl enable "$UNIT_NAME"'))
        self.assertIn("exit 0", TEXT[end:end + 300])


class TheParentStartsIt(unittest.TestCase):
    """The block before step 7 that starts the run for the other lane, against a stub
    install.sh that prints what it was given."""
    BLOCK = between('SECOND_LANE=""; SECOND_UNIT=""; SECOND_FRESH=0; SECOND_STATE=""\n',
                    '\nif [ -n "$SECONDARY" ]; then\n  step "7/10')

    def parent(self, lane, want, unit_there, child_rc=0, no_service=0):
        t = tmp("lane-parent-")
        units = t / "units"
        units.mkdir()
        if unit_there:
            (units / ("qwen38-flash.service" if lane == "27b" else "qwen38-sglang.service")).write_text("[Unit]\n")
        repo = t / "repo"
        repo.mkdir()
        stub(repo / "install.sh",
             'echo "CHILD SECONDARY=${QWEN38_SECONDARY:-} MODEL_CHOICE=${MODEL_CHOICE:-unset} '
             'MODEL_REV=${MODEL_REV:-unset} CONTEXT_MODE=${CONTEXT_MODE:-unset} PORT=${PORT:-} '
             'HF_CACHE=${HF_CACHE:-} ENGINE_BIND=${ENGINE_BIND:-} PROXY_BIND=${PROXY_BIND:-} PROXY_PORT=${PROXY_PORT:-}"\n'
             f"exit {child_rc}")
        block = self.BLOCK.replace("/etc/systemd/system/", f"{units}/")
        script = ("set -euo pipefail\nstep(){ echo \"STEP $*\"; }\n"
                  f'SECONDARY=""; NO_SERVICE={no_service}; LANE={lane}; REPO_DIR="{repo}"\n'
                  f"WANT_FLASH={want}; WANT_27B={want}\n"
                  'PORT=31000; HF_CACHE=/data/hf; ENGINE_BIND=127.0.0.1; PROXY_BIND=0.0.0.0; PROXY_PORT=31001\n'
                  + block + '\necho "AFTER FRESH=$SECOND_FRESH STATE=$SECOND_STATE"\n')
        env = {"PATH": "/usr/bin:/bin", "MODEL_CHOICE": "stock", "MODEL_REV": "main", "CONTEXT_MODE": "1m"}
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_a_new_flash_lane_gets_its_default_target_and_the_shared_settings_only(self):
        out = self.parent("27b", 1, unit_there=False)
        self.assertIn("CHILD SECONDARY=flash MODEL_CHOICE=flash MODEL_REV=unset CONTEXT_MODE=unset "
                      "PORT=31000 HF_CACHE=/data/hf ENGINE_BIND=127.0.0.1 PROXY_BIND=0.0.0.0 PROXY_PORT=31001", out)
        self.assertIn("AFTER FRESH=1 STATE=installed", out)

    def test_an_installed_27b_beside_flash_keeps_its_own_target(self):
        out = self.parent("flash", 1, unit_there=True)
        self.assertIn("CHILD SECONDARY=27b MODEL_CHOICE=unset", out)
        self.assertIn("AFTER FRESH=0 STATE=installed", out)

    def test_a_lane_left_out_is_not_started(self):
        out = self.parent("27b", 0, unit_there=False)
        self.assertNotIn("CHILD", out)
        self.assertIn("STATE=off", out)

    def test_a_lane_that_fails_does_not_fail_the_run(self):
        out = self.parent("27b", 1, unit_there=False, child_rc=1)
        self.assertIn("AFTER FRESH=1 STATE=failed", out)
        self.assertIn("the 27B lane goes on", out)

    def test_no_service_installs_one_lane(self):
        out = self.parent("27b", 1, unit_there=False, no_service=1)
        self.assertNotIn("CHILD", out)


class TheOtherLanesAndTheirProofs(unittest.TestCase):
    """The end of the run as written, against stub installers and a stub prove_text_lane:
    what is prepared, what is proved, in which order, and that the served lane comes back."""
    BLOCK = between("    # ── Every other lane", "    printf '\\n\\033[1;32m✅ Installed")

    def end(self, units=(), want_image=1, want_video=1, with_image=0, with_video=0, with_flash=0,
            second_state="installed", second_fresh=1, image_rc=0, video_rc=0, prove_fail=""):
        t = tmp("lane-end-")
        unit_dir = t / "units"
        unit_dir.mkdir()
        for u in units:
            (unit_dir / u).write_text("[Unit]\n")
        repo = t / "repo"
        repo.mkdir()
        log = t / "calls"
        log.write_text("")
        for name, rc in (("install-image.sh", image_rc), ("install-video.sh", video_rc)):
            stub(repo / name, f'echo "{name} $*" >> "{log}"\n'
                              f'case "$*" in *--no-smoke*) exit {rc} ;; esac\nexit 0')
        (repo / "oc-fit-limits.py").write_text(f"open({str(log)!r}, 'a').write('oc-fit-limits\\n')\n")
        bin_ = t / "bin"
        bin_.mkdir()
        stub(bin_ / "sudo", f'echo "sudo $*" >> "{log}"')
        stub(bin_ / "curl", "exit 0")
        block = self.BLOCK.replace("/etc/systemd/system/", f"{unit_dir}/")
        script = ("set -euo pipefail\nstep(){ echo \"STEP $*\"; }\ndie(){ echo \"DIE $*\"; exit 1; }\n"
                  "stale_since(){ return 1; }\n"
                  f'prove_text_lane(){{ echo "prove $*" >> "{log}"; case "$1" in {prove_fail or "__none__"}) return 1 ;; esac; }}\n'
                  f'REPO_DIR="{repo}"; CONFIG_DIR="{t}"; LANE=27b; UNIT_NAME=qwen38-sglang.service; SMOKE_MODEL=qwen3.8-27b\n'
                  'KEEPALIVE_UNIT=qwen38-keepalive.service; PORT=30000; PROXY_PORT=30001; CONTEXT_MODE=1m; OPENCODE=1; KA_CHANGED=0\n'
                  f"WANT_IMAGE={want_image}; WANT_VIDEO={want_video}; WITH_IMAGE={with_image}; WITH_VIDEO={with_video}\n"
                  f"WITH_FLASH={with_flash}; WITH_27B=0\n"
                  f"SECOND_LANE=flash; SECOND_UNIT=qwen38-flash.service; SECOND_STATE={second_state}; SECOND_FRESH={second_fresh}\n"
                  + block + '\necho "STATES second=$SECOND_STATE image=$IMAGE_STATE video=$VIDEO_STATE"\n')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": f"{bin_}:/usr/bin:/bin"})
        return r.returncode, r.stdout + r.stderr, log.read_text().splitlines()

    def test_a_new_box_prepares_then_proves_each_lane_and_its_own_last(self):
        rc, out, calls = self.end()
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls, [
            "install-image.sh --no-smoke", "install-video.sh --no-smoke",
            "sudo systemctl stop qwen38-sglang.service",
            "prove qwen38-flash.service qwen3.8-flash-next stop",
            "install-image.sh ", "install-video.sh ",
            "prove qwen38-sglang.service qwen3.8-27b",
            "sudo systemctl restart qwen38-keepalive.service", "oc-fit-limits"])
        self.assertIn("STATES second=proved image=proved video=proved", out)

    def test_a_routine_update_proves_nothing_and_keeps_the_lane_serving(self):
        rc, out, calls = self.end(units=("qwen38-image.service", "qwen38-video.service"), second_fresh=0)
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls, ["install-image.sh --no-smoke", "install-video.sh --no-smoke"])
        self.assertIn("STATES second=installed image=installed video=installed", out)

    def test_a_lane_left_out_is_not_touched(self):
        rc, out, calls = self.end(units=("qwen38-video.service",), want_image=0, second_fresh=0)
        self.assertEqual(calls, ["install-video.sh --no-smoke"])
        self.assertIn("image=off", out)

    def test_a_lane_that_does_not_install_is_not_proved_and_the_rest_goes_on(self):
        rc, out, calls = self.end(image_rc=1)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("install-image.sh ", calls)
        self.assertIn("install-video.sh ", calls)
        self.assertIn("image=failed", out)

    def test_naming_a_lane_proves_it_on_an_update(self):
        rc, out, calls = self.end(units=("qwen38-image.service", "qwen38-video.service"), second_fresh=0,
                                  with_video=1)
        self.assertEqual(calls[2:], ["sudo systemctl stop qwen38-sglang.service", "install-video.sh ",
                                     "prove qwen38-sglang.service qwen3.8-27b",
                                     "sudo systemctl restart qwen38-keepalive.service", "oc-fit-limits"])
        self.assertIn("video=proved", out)
        rc, out, calls = self.end(units=("qwen38-image.service", "qwen38-video.service"), second_fresh=0,
                                  with_flash=1)
        self.assertIn("prove qwen38-flash.service qwen3.8-flash-next stop", calls)

    def test_a_proof_that_fails_is_said_and_the_served_lane_still_comes_back(self):
        rc, out, calls = self.end(prove_fail="qwen38-flash.service")
        self.assertEqual(rc, 0, out)
        self.assertIn("second=failed", out)
        self.assertEqual(calls[-3], "prove qwen38-sglang.service qwen3.8-27b")

    def test_a_served_lane_that_does_not_come_back_ends_the_run_and_says_how(self):
        rc, out, calls = self.end(prove_fail="qwen38-sglang.service")
        self.assertEqual(rc, 1)
        self.assertIn("DIE the 27B lane did not come back", out)
        self.assertIn("sudo systemctl start qwen38-sglang.service", out)


class ProvingATextLane(unittest.TestCase):
    """prove_text_lane as written, against stub systemctl, sudo, curl and journalctl."""
    FUNC = between("prove_text_lane(){", "\n}\n") + "\n}\n"

    def prove(self, answer='{"choices":[{"message":{"content":"READY"}}]}', health=0, active="activating",
              restarts_after=0, after="stop"):
        t = tmp("prove-")
        log = t / "calls"
        log.write_text("")
        bin_ = t / "bin"
        bin_.mkdir()
        (t / "answer.json").write_text(answer)
        stub(bin_ / "sudo", f'echo "sudo $*" >> "{log}"')
        stub(bin_ / "systemctl", f'case "$*" in *NRestarts*) n=$(cat "{t}/n" 2>/dev/null || echo 0); echo $n; '
                                 f'echo {restarts_after} > "{t}/n" ;; is-active*) echo {active} ;; esac')
        stub(bin_ / "curl", f'case "$*" in *"/health"*) exit {health} ;; *) cat "{t}/answer.json" ;; esac')
        stub(bin_ / "journalctl", "echo JOURNAL")
        stub(bin_ / "sleep", "exit 0")
        script = ("set -euo pipefail\nPORT=30000; KEY=k\n" + self.FUNC +
                  f'\nif prove_text_lane qwen38-flash.service qwen3.8-flash-next {after}; then echo RC0; else echo RC1; fi\n')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                           env={"PATH": f"{bin_}:/usr/bin:/bin"})
        return r.stdout + r.stderr, log.read_text().splitlines()

    def test_an_answer_is_a_proof_and_the_lane_is_stopped_after(self):
        out, calls = self.prove()
        self.assertIn("RC0", out)
        self.assertEqual(calls, ["sudo systemctl start qwen38-flash.service", "sudo systemctl stop qwen38-flash.service"])

    def test_the_served_lane_is_left_running(self):
        out, calls = self.prove(after="")
        self.assertIn("RC0", out)
        self.assertEqual(calls, ["sudo systemctl start qwen38-flash.service"])

    def test_a_run_of_bangs_is_the_known_corruption_not_an_answer(self):
        out, _ = self.prove(answer='{"choices":[{"message":{"content":"' + "!" * 40 + '"}}]}')
        self.assertIn("RC1", out)
        self.assertIn("CORRUPT", out)

    def test_a_unit_that_fails_or_relaunches_is_a_death(self):
        out, calls = self.prove(health=7, active="failed")
        self.assertIn("RC1", out)
        self.assertIn("died during startup", out)
        out, _ = self.prove(health=7, restarts_after=1)
        self.assertIn("RC1", out)
        self.assertIn("died during startup", out, "a relaunch, not the 20 minutes running out")

    def test_twenty_minutes_of_silence_is_a_failure(self):
        out, calls = self.prove(health=7)
        self.assertIn("RC1", out)
        self.assertIn("did not answer within 20 min", out)
        self.assertEqual(calls[-1], "sudo systemctl stop qwen38-flash.service")


if __name__ == "__main__":
    unittest.main()
