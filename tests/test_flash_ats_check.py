#!/usr/bin/env python3
"""The flash lane starts only on a GPU in its ATS addressing mode (issue #54, 2026-10-19).

The flash lane serves its 47.7 GiB N-gram table from a file on NVMe, which the GPU reads through
the host page tables: a GB10 in its ATS addressing mode. On the box of issue #54, whose mode reads
None, SGLang refused the file backend when it loaded the weights, after the image and the
checkpoint had been fetched, and systemd started the lane again every 15 s. flash-ats-check.sh
asks nvidia-smi for the mode and says whether the lane can start. The launcher asks it before the
table is written or the image runs, says why in the line the cockpit reads, and exits with the code
its unit does not restart on. install.sh, the first Load (tests/test_lane_on_demand.py) and
switch-model.sh ask it before the first byte, and an update gives the flash lane its units only,
beside the 27B lane or as the box's own text lane, so the new launcher reaches a box where it was
prepared (tests/test_install_units_only_run.py runs both to their end). Only the check's status 1
says "cannot": anything else, a mode nvidia-smi does not name or a check that cannot run, leaves
the engine to decide, as before.
"""
import importlib.util
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
CHECK = REPO / "flash-ats-check.sh"
QUERY = "--query-gpu=addressing_mode --format=csv,noheader"
# what nvidia-smi 580.178.04 answered on the reference box (2026-10-19): the mode, and for a field
# it does not know (an older driver) this line and an empty one, with exit code 2
NO_FIELD = 'Field "addressing_mode" is not a valid field to query.\n\n'


def load_lifecycle():
    spec = importlib.util.spec_from_file_location("lifecycle_ats", REPO / "dashboard" / "lifecycle.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lifecycle_ats"] = mod
    spec.loader.exec_module(mod)
    return mod


class Box(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="flash-ats-"))
        self.addCleanup(shutil.rmtree, self.t, True)

    def smi(self, out, rc=0, sleep=0):
        """An nvidia-smi that answers the one query the check asks, and nothing else."""
        p = self.t / "nvidia-smi"
        (self.t / "smi-out").write_text(out)
        p.write_text(f'#!/bin/sh\n[ "$*" = "{QUERY}" ] || exit 99\nsleep {sleep}\n'
                     f'cat "{self.t}/smi-out"\nexit {rc}\n')
        p.chmod(0o755)
        return str(p)

    def check(self, smi, **env):
        r = subprocess.run(["bash", str(CHECK)], capture_output=True, text=True, timeout=30,
                           env={"PATH": "/usr/bin:/bin", "QWEN38_PREFLIGHT_SMI": smi, **env})
        return r.returncode, r.stdout.strip()


class TheCheck(Box):
    def test_ats_starts(self):
        self.assertEqual(self.check(self.smi("ATS\n")), (0, "addressing mode ATS"))

    def test_hmm_and_none_are_refused_with_the_reason(self):
        for mode in ("HMM", "None", " None "):
            rc, out = self.check(self.smi(mode + "\n"))
            self.assertEqual(rc, 1, mode)
            self.assertIn(f"addressing mode is {mode.strip()} (nvidia-smi {QUERY.split()[0]}) and the flash lane needs ATS", out)
            self.assertIn("docs/flash-lane.md", out)

    def test_what_nvidia_smi_does_not_name_leaves_the_engine_to_decide(self):
        """An older driver, a field it cannot read, an empty answer, no nvidia-smi at all: as
        before, the engine decides. Refused, a lane would stay down for good on a misreading."""
        for out, rc in (("[N/A]\n", 0), ("[Not Supported]\n", 0), ("\n", 0), (NO_FIELD, 2), ("ATS\n", 2)):
            got = self.check(self.smi(out, rc))
            self.assertEqual(got[0], 0, (out, rc))
            self.assertTrue(got[1].startswith("addressing mode not reported by nvidia-smi"), got)
        self.assertEqual(self.check(str(self.t / "no-such-smi"))[0], 0)

    def test_an_nvidia_smi_that_does_not_answer_leaves_the_engine_to_decide_in_time(self):
        t0 = time.monotonic()
        rc, out = self.check(self.smi("None\n", sleep=30), QWEN38_PREFLIGHT_SMI_TIMEOUT_S="1")
        self.assertEqual(rc, 0, out)
        self.assertLess(time.monotonic() - t0, 10)

    def test_no_variable_skips_it(self):
        """QWEN38_FLASH_SKIP_ATS_CHECK was documented and never reached the launcher (review,
        2026-10-19): there is no way around the question, and nothing says there is."""
        rc, _ = self.check(self.smi("None\n"), QWEN38_FLASH_SKIP_ATS_CHECK="1")
        self.assertEqual(rc, 1)
        for f in ("flash-ats-check.sh", "qwen38-flash-launch.sh.template", "docs/flash-lane.md", "CHANGELOG.md"):
            self.assertFalse("SKIP_ATS" in (REPO / f).read_text(), f)


class TheSharedQuestion(Box):
    """lanes.sh's flash_cannot_start, which install.sh, prepare-lane.sh and switch-model.sh ask:
    each read any status but 0 as "cannot", and a check that was not there stopped the lane for
    good with no reason (found in review, 2026-10-19)."""

    def ask(self, body):
        repo = self.t / "repo"
        repo.mkdir(exist_ok=True)
        if body is not None:
            (repo / "flash-ats-check.sh").write_text(body)
        r = subprocess.run(["bash", "-c", f'CONFIG_DIR="{self.t}/cfg"; REPO_DIR="{repo}"; . "{REPO}/lanes.sh"\n'
                            'if why="$(flash_cannot_start)"; then echo "CANNOT:$why"; else echo "CAN"; fi'],
                           capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
        return r.stdout.strip()

    def test_only_status_1_with_a_reason_is_cannot(self):
        self.assertEqual(self.ask('echo "the GPU\'s addressing mode is None"; exit 1\n'),
                         "CANNOT:the GPU's addressing mode is None")
        for body in ('echo "addressing mode ATS"\n', 'echo oops; exit 2\n', "exit 1\n", "exit 127\n", None):
            self.assertEqual(self.ask(body), "CAN", body)


class TheLauncher(Box):
    TEMPLATE = (REPO / "qwen38-flash-launch.sh.template").read_text()

    def test_it_asks_before_the_table_is_written_or_the_image_runs(self):
        t = self.TEMPLATE
        ask = t.index("flash-ats-check.sh")
        self.assertLess(ask, t.index("PLE_DIR=__PLE_DIR__"))
        self.assertLess(ask, t.index("docker run"))

    def run_head(self, smi):
        home = self.t / "home"
        cfg = home / ".config" / "qwen38"
        cfg.mkdir(parents=True, exist_ok=True)
        shutil.copy(CHECK, cfg / "flash-ats-check.sh")
        head = self.TEMPLATE[:self.TEMPLATE.index("PLE_DIR=__PLE_DIR__")].replace("__HOME__", str(home))
        (self.t / "launch.sh").write_text(head + "echo REACHED\n")
        return subprocess.run(["bash", str(self.t / "launch.sh")], capture_output=True, text=True, timeout=30,
                              env={"PATH": "/usr/bin:/bin", "QWEN38_PREFLIGHT_SMI": smi})

    def test_a_gpu_that_cannot_start_it_is_said_as_such_and_ends_without_a_restart(self):
        """The launcher's head, run as rendered, on a GPU in None: one line the cockpit reads as
        "cannot start" with the reason, never as GPU memory held, and the exit code the unit does
        not start it again on."""
        r = self.run_head(self.smi("None\n"))
        code = int(re.search(r"^RestartPreventExitStatus=(\d+)$", (REPO / "qwen38-flash.service.template").read_text(), re.M).group(1))
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        self.assertNotIn("REACHED", r.stdout)
        lc = load_lifecycle()
        said = lc.parse_preflight(r.stdout.splitlines())
        self.assertEqual(said["verdict"], "cannot start", r.stdout)
        self.assertIn("addressing mode is None", said["reason"])
        flags = lc.preflight_flags({"state": "failed"}, unit_sub="failed", preflight=said)
        self.assertEqual(flags.get("cannot"), said["reason"])
        self.assertNotIn("refused", flags)

    def test_ats_and_a_mode_not_named_go_on(self):
        for out in ("ATS\n", "[N/A]\n"):
            r = self.run_head(self.smi(out))
            self.assertEqual((r.returncode, r.stdout.strip().splitlines()[-1]), (0, "REACHED"), out)

    def test_a_check_that_cannot_answer_leaves_the_engine_to_decide(self):
        """Not there, or ending on another status: a start as before, never exit 78 with an
        empty reason the cockpit cannot read (found in review, 2026-10-19)."""
        r = self.run_head(self.smi("None\n"))
        cfg = self.t / "home" / ".config" / "qwen38"
        (cfg / "flash-ats-check.sh").unlink()
        r = subprocess.run(["bash", str(self.t / "launch.sh")], capture_output=True, text=True, timeout=30,
                           env={"PATH": "/usr/bin:/bin"})
        self.assertEqual((r.returncode, r.stdout.strip().splitlines()[-1]), (0, "REACHED"), r.stdout + r.stderr)
        (cfg / "flash-ats-check.sh").write_text("echo half an answer; exit 2\n")
        r = subprocess.run(["bash", str(self.t / "launch.sh")], capture_output=True, text=True, timeout=30,
                           env={"PATH": "/usr/bin:/bin"})
        self.assertEqual((r.returncode, r.stdout.strip().splitlines()[-1]), (0, "REACHED"), r.stdout + r.stderr)


class TheFP4Launcher(Box):
    """The FP4-Local config asks the same question at its start: install.sh, prepare-lane.sh
    and switch-model.sh refused it on such a GPU while its launcher started anyway, every 15 s
    (found in review, 2026-10-20). Whether its engine needs ATS is not measured; the lane keeps
    one rule."""
    TEMPLATE = (REPO / "qwen38-flash-fp4local-launch.sh.template").read_text()

    def run_launcher(self, smi):
        """The whole launcher as rendered, its docker a stub that says it ran."""
        home = self.t / "home"
        cfg = home / ".config" / "qwen38"
        cfg.mkdir(parents=True, exist_ok=True)
        shutil.copy(CHECK, cfg / "flash-ats-check.sh")
        docker = self.t / "docker"
        docker.write_text('#!/bin/sh\necho DOCKER "$@"\n')
        docker.chmod(0o755)
        text = (self.TEMPLATE.replace("__HOME__", str(home)).replace("/usr/bin/docker", str(docker)))
        text = re.sub(r"__[A-Z_]+__", "x", text)
        (self.t / "launch.sh").write_text(text)
        return subprocess.run(["bash", str(self.t / "launch.sh")], capture_output=True, text=True, timeout=30,
                              env={"PATH": "/usr/bin:/bin", "QWEN38_PREFLIGHT_SMI": smi,
                                   "QWEN38_FLASH_MEM_FLOOR_GIB": "0"})

    def test_a_gpu_in_hmm_ends_the_start_before_any_docker_call(self):
        r = self.run_launcher(self.smi("HMM\n"))
        unit = (REPO / "qwen38-flash-fp4local.service.template").read_text()
        code = int(re.search(r"^RestartPreventExitStatus=(\d+)$", unit, re.M).group(1))
        self.assertEqual((r.returncode, code), (78, 78), r.stdout + r.stderr)
        self.assertNotIn("DOCKER", r.stdout)
        self.assertEqual(r.stdout.splitlines()[0][:46], "preflight: qwen38-flash.service cannot start: ")
        said = load_lifecycle().parse_preflight(r.stdout.splitlines())
        self.assertEqual(said["verdict"], "cannot start", r.stdout)
        self.assertIn("addressing mode is HMM", said["reason"])
        # the reason claims no table file for this config
        self.assertIn("whose need for ATS is not measured", said["reason"])

    def test_ats_goes_on_to_the_engine(self):
        r = self.run_launcher(self.smi("ATS\n"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DOCKER run --rm --name qwen38-flash", r.stdout)


class TheInstaller(Box):
    TEXT = (REPO / "install.sh").read_text()

    def test_every_box_gets_the_check_as_it_gets_the_engine_preflight(self):
        self.assertIn('cmp -s "$REPO_DIR/flash-ats-check.sh" "$CONFIG_DIR/flash-ats-check.sh"', self.TEXT)
        self.assertIn('install -m 755 "$REPO_DIR/flash-ats-check.sh" "$CONFIG_DIR/flash-ats-check.sh"', self.TEXT)

    def test_a_flash_install_asks_before_its_first_download(self):
        line = 'if [ "$LANE" = "flash" ] && ATS_CANNOT="$(flash_cannot_start)"; then'
        self.assertEqual(self.TEXT.count(line), 1)
        self.assertLess(self.TEXT.index(line), self.TEXT.index('docker pull "$PULL_TARGET"'))
        self.assertLess(self.TEXT.index(line), self.TEXT.index("dl_fetch(){"))

    def step_one(self, mode, installed, secondary="", units_only=0):
        """The block of step 1 that asks the GPU, run as written."""
        start = self.TEXT.index('ATS_CANNOT=""\nif [ "$LANE" = "flash" ] && ATS_CANNOT=')
        end = self.TEXT.index("\nfi\n", start) + len("\nfi\n")
        repo = self.t / "repo"
        repo.mkdir(exist_ok=True)
        shutil.copy(CHECK, repo / "flash-ats-check.sh")
        script = ("set -euo pipefail\ndie(){ echo \"DIE $*\"; exit 1; }\n"
                  f'CONFIG_DIR="{self.t}/cfg"; REPO_DIR="{repo}"; . "{REPO}/lanes.sh"\n'
                  f'LANE=flash; INSTALLED_CHOICE="{installed}"; SECONDARY="{secondary}"; UNITS_ONLY={units_only}\n'
                  + self.TEXT[start:end] + 'echo "UNITS_ONLY=$UNITS_ONLY CANNOT=${ATS_CANNOT:0:30}"\n')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": "/usr/bin:/bin", "QWEN38_PREFLIGHT_SMI": self.smi(mode + "\n")})
        return r.returncode, r.stdout

    def test_step_one_stops_a_new_flash_install_and_lets_an_update_go_on_with_units_only(self):
        """A box whose text lane is the flash lane (at boot, or the one a side lane at boot came
        from) was stopped here, kept the launcher that starts again every 15 s, and a box booting
        a side lane was never updated again (found in review, 2026-10-19)."""
        rc, out = self.step_one("None", "flash")
        self.assertEqual(rc, 0, out)
        self.assertIn("NOTE: the flash lane cannot start on this box: the GPU's addressing mode is None", out)
        self.assertIn("this run starts nothing", out)
        self.assertIn("UNITS_ONLY=1 CANNOT=the GPU's addressing mode is", out)
        for installed in ("", "27b"):
            rc, out = self.step_one("None", installed)
            self.assertEqual(rc, 1, out)
            self.assertIn("DIE The flash lane cannot start on this box: the GPU's addressing mode is None", out)
        self.assertIn("The 27B lane can: MODEL_CHOICE=stock ./install.sh", self.step_one("None", "")[1])
        # a box serving the 27B keeps its target with a plain run: MODEL_CHOICE=stock moved one
        # serving fp8 or an abliterated target to stock, 22 GB to fetch (found in review, 2026-10-19)
        out = self.step_one("None", "27b")[1]
        self.assertIn("Its 27B lane can: ./install.sh updates it and keeps its target", out)
        self.assertNotIn("MODEL_CHOICE=", out)
        rc, out = self.step_one("None", "flash", secondary="flash", units_only=1)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("NOTE", out)
        self.assertIn("UNITS_ONLY=1 CANNOT=the GPU's", out)
        for installed in ("flash", "", "27b"):
            self.assertEqual(self.step_one("ATS", installed), (0, "UNITS_ONLY=0 CANNOT=\n"), installed)

    def ending(self, second_state, cockpit=1, unit=True):
        """The end of the path that starts nothing, on a GPU outside ATS, run as written."""
        start = self.TEXT.index('  if [ -n "$ATS_CANNOT" ]; then\n    step "Done: this box is up to date')
        end = self.TEXT.index("\n  else\n", start)
        sgl = self.t / "qwen38-sglang.service"
        if unit:
            sgl.write_text("[Unit]\n")
        elif sgl.exists():
            sgl.unlink()
        script = ('step(){ echo "STEP $*"; }\n'
                  f'ATS_CANNOT="the GPU\'s addressing mode is None"; SERVING_LANE=""; SECOND_STATE="{second_state}"\n'
                  f'COCKPIT={cockpit}; SGL_UNIT_PATH="{sgl}"\n' + self.TEXT[start:end] + "\n  fi\n")
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_its_end_says_what_the_27b_lane_beside_it_can_do(self):
        """It told every box to Load the 27B lane or to switch to it: one with the 27B's units
        only (whose switch refuses, the image is not there), one where it did not install, one
        without it or without a cockpit (found in review, 2026-10-19)."""
        self.assertIn("Load it from the cockpit's Lanes view (or ./switch-model.sh stock)", self.ending("installed"))
        out = self.ending("installed", cockpit=0)
        self.assertIn("./switch-model.sh stock switches to it", out)
        self.assertNotIn("cockpit", out)
        out = self.ending("units")
        self.assertIn("its first Load from the cockpit's Lanes view builds it and fetches its weights", out)
        self.assertNotIn("switch-model.sh", out)
        self.assertIn("./prepare-lane.sh 27b builds it", self.ending("units", cockpit=0))
        out = self.ending("failed")
        self.assertIn("it did not install", out)
        self.assertIn("./install.sh --with-27b installs it whole", out)
        self.assertIn("./install.sh --with-27b brings it back", self.ending("off"))
        self.assertIn("./install.sh --with-27b adds it", self.ending("off", unit=False))
        for state in ("installed", "units", "failed", "off"):
            out = self.ending(state)
            self.assertIn("STEP Done: this box is up to date, and its flash lane, which this GPU cannot start", out)
            self.assertNotIn("serves on this GPU", out, state)

    def test_the_box_s_own_flash_lane_is_neither_enabled_nor_started_by_the_run(self):
        """Outside ATS the run takes the path that starts nothing (a side lane at boot takes it
        too) and says why at its end."""
        t = self.TEXT
        self.assertIn('if [ -z "$ATS_CANNOT" ] && [ "$IMAGE_BOOT" -eq 0 ]', t)
        end = t.index('  if [ -n "$ATS_CANNOT" ]; then\n    step "Done: this box is up to date, and its flash lane')
        self.assertLess(t.index('if [ -z "$ATS_CANNOT" ] && [ "$IMAGE_BOOT" -eq 0 ]'), end)
        self.assertLess(end, t.index('step "9/10'))

    def beside(self, mode, prepared, named=0):
        """The block of install.sh that installs the flash lane beside the 27B lane, run as
        written, with a nested install.sh that records what it was asked."""
        start = self.TEXT.index('    SECOND_CANNOT=""\n')
        end = self.TEXT.index("  else\n    SECOND_STATE=off", start)
        repo = self.t / "repo"
        repo.mkdir(exist_ok=True)
        shutil.copy(CHECK, repo / "flash-ats-check.sh")
        (repo / "install.sh").write_text(f'env | grep "^QWEN38_" | sort > "{self.t}/asked"\n')
        script = (
            "set -euo pipefail\nstep(){ echo \"STEP $*\"; }\n"
            f'CONFIG_DIR="{self.t}/cfg"; . "{REPO}/lanes.sh"; SECOND_FRESH=0; SECOND_TARGET=flash\n'
            f'REPO_DIR="{repo}"; LANE=27b; SECOND_LANE=flash; SECOND_PREPARED={prepared}; ALL_WEIGHTS=0\n'
            f'WITH_FLASH={named}; WITH_27B=0; RECOMMENDED=0; PORT=30000; HF_CACHE=/hf; ENGINE_BIND=127.0.0.1\n'
            'PROXY_BIND=0.0.0.0; PROXY_PORT=30001; SECOND_STATE=""; SECOND_CANNOT=""; SECOND_ENV=()\n'
            "if true; then\n" + self.TEXT[start:end] + "fi\n"
            'echo "STATE=$SECOND_STATE"\n')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": "/usr/bin:/bin", "QWEN38_PREFLIGHT_SMI": self.smi(mode + "\n")})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout, (self.t / "asked").read_text().split()

    def test_an_update_of_a_box_outside_ats_writes_the_flash_units_and_fetches_nothing(self):
        """Issue #54's box: the 27B serves, the flash lane was prepared by its first Load. Run
        whole, the flash lane's run stopped at the question before it wrote its new launcher, so
        the box kept the one that starts every 15 s (review, 2026-10-19). Prepared, named or
        neither, it now gets its units only."""
        for prepared, named in ((1, 0), (0, 1), (0, 0)):
            out, asked = self.beside("None", prepared, named)
            self.assertIn("QWEN38_UNITS_ONLY=1", asked, (prepared, named))
            self.assertIn("QWEN38_SECONDARY=flash", asked)
            self.assertEqual(asked.count("QWEN38_UNITS_ONLY=1"), 1, asked)
            self.assertIn("STEP The flash lane, installed beside the 27B lane: its units only, as this GPU cannot start it", out)
            self.assertIn("why: the GPU's addressing mode is None", out)
            self.assertNotIn("it serves when it is loaded", out)
            self.assertIn("STATE=cannot", out)

    def test_a_box_in_ats_installs_a_prepared_flash_lane_whole_as_before(self):
        out, asked = self.beside("ATS", 1)
        self.assertNotIn("QWEN38_UNITS_ONLY=1", asked)
        self.assertIn("STATE=installed", out)
        out, asked = self.beside("ATS", 0)
        self.assertEqual(asked.count("QWEN38_UNITS_ONLY=1"), 1)
        self.assertIn("STATE=units", out)
        self.assertIn("(it serves when it is loaded)", out)
        self.assertNotIn("cannot start", out)

    def test_its_summary_says_it_cannot_start_here_not_ready_to_load(self):
        start = self.TEXT.index("    lane_line(){")
        end = self.TEXT.index("\n    }\n", start) + len("\n    }\n")
        script = ("set -u\nCOCKPIT=1\n"
                  'SECOND_CANNOT="the GPU\'s addressing mode is None (nvidia-smi) and the flash lane needs ATS"\n'
                  + self.TEXT[start:end] + 'lane_line "Flash 176B " cannot flash flash "223 GB" /nonexistent\n')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), "Flash 176B : installed, and it cannot start on this box: "
                         "the GPU's addressing mode is None (nvidia-smi) and the flash lane needs ATS")
        # its units did not install either: --with-flash would only give it its units again
        r = subprocess.run(["bash", "-c", script.replace(' cannot flash flash ', ' failed flash flash ')],
                           capture_output=True, text=True, timeout=30)
        self.assertIn("NOT ready, see its NOTE above (it did not install), and this GPU cannot start it", r.stdout)
        self.assertNotIn("--with-flash", r.stdout)


class TheSwitch(unittest.TestCase):
    TEXT = (REPO / "switch-model.sh").read_text()

    def test_a_flash_checkpoint_is_neither_fetched_nor_booted_on_a_gpu_that_cannot_start_it(self):
        """A Load of another flash checkpoint, or the Library's Download, fetched 124 to 136 GB
        for a lane that cannot start (review, 2026-10-19): the switch asks first."""
        ask = self.TEXT.index('if [ "$TARGET_LANE" = flash ] && ATS_WHY="$(flash_cannot_start)"; then')
        self.assertLess(self.TEXT.index('TARGET_LANE=flash ;;'), ask)
        for later in ('docker image inspect "$DL_IMAGE"', 'fetching $TARGET_REPO @ $TARGET_REV',
                      'sudo systemctl enable "$TARGET_UNIT_NAME"'):
            self.assertLess(ask, self.TEXT.index(later), later)


if __name__ == "__main__":
    unittest.main()
