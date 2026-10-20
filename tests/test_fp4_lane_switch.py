#!/usr/bin/env python3
"""The flash lane's FP4-Local config names its checkpoint the way vLLM serves it:
`vllm serve <repo> --revision <sha>`, a positional argument, not a --model-path flag.
Every reader of an installed checkpoint that knew only the flag shapes read this box
as serving no model and no image, which sent a plain ./install.sh re-run back to the
SGLang engine on an uncached target and stranded the cockpit readers (found in three
independent reviews, 2026-10-18). These are the shared read-back's tests, plus the
switch contract that came out of the same review: a full `flash-fp4-local` switch is
the lane-level bring-back where the unit already names the FP4-Local launcher, a
refusal naming the exact install.sh command where it does not, and --fetch in every
config, even on a box that never installed the lane."""
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))   # the in-test `import conformity` needs the root,
                                # the way every sibling conformity test does it

SWITCH = (REPO / "switch-model.sh").read_text()
LAUNCHER_TPL = (REPO / "qwen38-flash-fp4local-launch.sh.template").read_text()
PIN_TEXT = (REPO / "install.sh").read_text()


def pin(name):
    m = re.search(rf'^{name}="([^"]+)"', PIN_TEXT, re.M)
    assert m, f"install.sh no longer defines {name} that way"
    return m.group(1)


FP4_REPO, FP4_REV = pin("FLASH_LOCAL_REPO"), pin("FLASH_LOCAL_REV")
FP4_IMAGE = pin("FLASH_LOCAL_IMAGE")


def render_launcher(hf="/home/tester/.cache/huggingface"):
    """The FP4-Local launcher as install.sh renders it: the same placeholders its
    render_tpl substitutes, with this test box's values."""
    return (LAUNCHER_TPL
            .replace("__HOME__", "/home/tester")
            .replace("__HF_CACHE__", hf)
            .replace("__IMAGE__", FP4_IMAGE)
            .replace("__MODEL__", FP4_REPO)
            .replace("__MODEL_REV_ARGS__", f"--revision {FP4_REV}")
            .replace("__FLASH_QUANT_ARGS__", "--quantization modelopt_mixed ")
            .replace("__ENGINE_BIND__", "0.0.0.0")
            .replace("__PORT__", "8000"))


def lanes_call(fn, target_file):
    """One lanes.sh helper, run the way the scripts run it: sourced into bash."""
    # bash -c takes the script's first operand as $0, not $1: passing lanes.sh as $1
    # sources whatever $1 names instead, and sourced the rendered launcher once.
    r = subprocess.run(["bash", "-c", 'source "$0" && ' + fn + ' "$1"',
                        str(REPO / "lanes.sh"), str(target_file)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


class TheReadBack(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="fp4-readback-"))

    def test_the_serve_argument_is_the_checkpoint(self):
        f = self.t / "launch-flash-fp4local.sh"
        f.write_text(render_launcher())
        self.assertEqual(lanes_call("launcher_model", f), FP4_REPO)
        self.assertEqual(lanes_call("engine_image", f), FP4_IMAGE)

    def test_the_sglang_spellings_still_read(self):
        f = self.t / "launch-flash.sh"
        f.write_text("exec docker run --rm \\\n  lmsysorg/sglang@sha256:" + "a" * 64 +
                     " \\\n  python3 -m sglang.launch_server \\\n  --model-path "
                     "RadixArk/Qwen3.8-Flash-Next-NVFP4 \\\n  --port 30000\n")
        self.assertEqual(lanes_call("launcher_model", f), "RadixArk/Qwen3.8-Flash-Next-NVFP4")
        self.assertEqual(lanes_call("engine_image", f), "lmsysorg/sglang@sha256:" + "a" * 64)
        g = self.t / "bare.sh"
        g.write_text("vllm --model someone/Custom-Model --port 1\n")
        self.assertEqual(lanes_call("launcher_model", g), "someone/Custom-Model")

    def test_conformity_names_the_fp4_target_from_the_serve_argument(self):
        import conformity
        t = pathlib.Path(tempfile.mkdtemp(prefix="fp4-observe-"))
        units, config = t / "units", t / "config"
        units.mkdir()
        config.mkdir()
        (units / "qwen38-flash.service").write_text(
            "[Service]\nExecStart=/bin/bash /home/tester/.config/qwen38/launch-flash-fp4local.sh\n")
        (config / "launch-flash-fp4local.sh").write_text(render_launcher())
        out = conformity.observe(units_dir=units, config_dir=config, lane="flash", repo_dir=REPO)
        self.assertEqual(out.get("target.flash"), "flash-fp4-local")
        self.assertEqual(out.get("PORT"), "8000")


FAKE_DOCKER = """#!/bin/sh
case "$1" in
  # every image is present, or only those DOCKER_IMAGES names when it is set
  image) [ -z "${DOCKER_IMAGES+x}" ] && exit 0
         case " $DOCKER_IMAGES " in *" $3 "*) exit 0 ;; esac
         exit 1 ;;
  run) printf '%s\\n' "$*" >> "$DOCKER_LOG"; exit 0 ;;   # the download succeeds
esac
exit 0
"""

# df answers 500G, or 1G under any path holding DF_SMALL: a room check on the wrong disk shows
FAKE_DF = """#!/bin/sh
echo Avail
if [ -n "${DF_SMALL:-}" ]; then
  case "$*" in *"$DF_SMALL"*) echo " 1G"; exit 0 ;; esac
fi
echo " 500G"
"""


class Box:
    """switch-model.sh as written, on a copy whose /etc paths live in a temporary
    home: a fake docker that succeeds, a fake systemctl and sudo that record and
    refuse (the switch's first sudo enable is the wall these tests stop at)."""

    def __init__(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="fp4-switch-"))
        self.home = self.t / "home"
        self.config = self.home / ".config/qwen38"
        self.config.mkdir(parents=True)
        self.units = self.t / "units"
        self.units.mkdir()
        repo = self.t / "repo"
        repo.mkdir()
        for f in ("install.sh", "lanes.sh", "ckpt-fetch.py", "hf-progress.py", "patch-template.py"):
            (repo / f).write_text((REPO / f).read_text())
        (repo / "switch-model.sh").write_text(SWITCH.replace("/etc/systemd/system/", str(self.units) + "/"))
        (repo / "switch-model.sh").chmod(0o755)
        (repo / "prepare-lane.sh").write_text(
            (REPO / "prepare-lane.sh").read_text().replace("/etc/systemd/system/", str(self.units) + "/"))
        (repo / "prepare-lane.sh").chmod(0o755)
        self.prepare = repo / "prepare-lane.sh"
        self.bin = self.t / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "systemctl").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SYSTEMCTL_LOG"\nexit 1\n')
        (self.bin / "sudo").write_text('#!/bin/sh\nprintf "sudo %s\\n" "$*" >> "$SYSTEMCTL_LOG"\nexit 1\n')
        (self.bin / "df").write_text(FAKE_DF)
        for n in ("docker", "systemctl", "sudo", "df"):
            (self.bin / n).chmod(0o755)
        self.docker_log = self.t / "docker.log"
        self.sysctl_log = self.t / "systemctl.log"
        self.script = repo / "switch-model.sh"

    def serve_fp4(self, template_fixture=False, hf=None, image=None):

        (self.units / "qwen38-flash.service").write_text(
            "[Service]\nExecStart=/bin/bash " + str(self.config / "launch-flash-fp4local.sh") + "\n")
        launch = self.config / "launch-flash-fp4local.sh"
        text = render_launcher(hf or "/home/tester/.cache/huggingface").replace("/home/tester", str(self.home))
        if image:
            text = text.replace(FP4_IMAGE, image)
        launch.write_text(text)
        launch.chmod(0o755)
        if template_fixture:
            import importlib.util
            pt_spec = importlib.util.spec_from_file_location("pt", str(REPO / "patch-template.py"))
            pt = importlib.util.module_from_spec(pt_spec)
            pt_spec.loader.exec_module(pt)
            snap = (self.home / ".cache/huggingface/hub" /
                    f"models--{FP4_REPO.replace('/', '--')}" / "snapshots" / FP4_REV)
            snap.mkdir(parents=True)
            (snap / "chat_template.jinja").write_text(
                "HEAD\n" + pt.EFFORT_ANCHOR + "\n" + pt.MSG_ANCHOR + "\nMID\n" +
                pt.LEAN_ANCHOR + pt.SYSTEM_ANCHOR + "\nTAIL\n")

    def serve_sglang_flash(self, hf=None):
        (self.units / "qwen38-flash.service").write_text(
            "[Service]\nExecStart=/bin/bash " + str(self.config / "launch-flash.sh") + "\n")
        (self.config / "launch-flash.sh").write_text(
            "exec docker run --rm \\\n" + (f"  -v {hf}:/root/.cache/huggingface \\\n" if hf else "") +
            "  lmsysorg/sglang@sha256:" + "a" * 64 +
            " \\\n  python3 -m sglang.launch_server \\\n  --model-path RadixArk/Qwen3.8-Flash-Next-NVFP4 \\\n"
            "  --revision " + "0" * 40 + " \\\n  --port 30000\n")

    def run(self, *args, script=None, **env):
        e = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home),
             "DOCKER_LOG": str(self.docker_log), "SYSTEMCTL_LOG": str(self.sysctl_log), **env}
        r = subprocess.run([str(script or self.script), *args], capture_output=True, text=True, env=e, timeout=120)
        runs = self.docker_log.read_text().splitlines() if self.docker_log.exists() else []
        calls = self.sysctl_log.read_text().splitlines() if self.sysctl_log.exists() else []
        return r.returncode, r.stdout + r.stderr, runs, calls


class TheSwitch(unittest.TestCase):
    def test_full_switch_refused_on_a_box_without_the_fp4_launcher(self):
        # no flash unit at all: the refusal names the install command, nothing runs
        rc, out, runs, calls = Box().run("flash-fp4-local")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("MODEL_CHOICE=flash-fp4-local ./install.sh", out)
        self.assertEqual(runs, [])
        self.assertTrue(all(c.startswith("is-enabled") for c in calls), calls)

    def test_full_switch_refused_while_the_lane_serves_sglang(self):
        box = Box()
        box.serve_sglang_flash()
        rc, out, runs, calls = box.run("flash-fp4-local")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("MODEL_CHOICE=flash-fp4-local ./install.sh", out)
        self.assertEqual(runs, [])
        self.assertTrue(all(c.startswith("is-enabled") for c in calls), calls)

    def test_sglang_target_still_refused_while_fp4_serves(self):
        box = Box()
        box.serve_fp4()
        rc, out, runs, _ = box.run("flash")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("MODEL_CHOICE=flash ./install.sh", out)
        self.assertEqual(runs, [])

    def test_fetch_works_on_a_box_serving_the_fp4_config(self):
        # into the cache its launcher mounts, not the default: the run read launch-flash.sh
        # and the 27B unit for it and fetched ~102 GB beside the engine's cache (found in
        # review, 2026-10-20)
        box = Box()
        hf = box.t / "data-hf"
        box.serve_fp4(hf=str(hf))
        rc, out, runs, calls = box.run("flash-fp4-local", "--fetch")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(runs), 1, runs)
        self.assertIn("spark-vllm-b12x", runs[0])   # its own image, not the SGLang one
        self.assertIn(f"-v {hf}:/hf ", runs[0])
        self.assertEqual([c for c in calls if "enable" in c], [])

    def test_fetch_without_its_image_names_its_own_preparation(self):
        # a bare ./prepare-lane.sh flash prepares the SGLang config (found in review, 2026-10-20)
        rc, out, runs, _ = Box().run("flash-fp4-local", "--fetch", DOCKER_IMAGES="")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("./prepare-lane.sh flash flash-fp4-local)", out)
        self.assertEqual(runs, [])

    def test_the_full_switch_fetches_in_the_image_its_launcher_runs(self):
        # not the repo's pin: a pin bump pulled but not installed yet refused the Load with
        # "run install.sh once" (found in review, 2026-10-20)
        box = Box()
        own = "eugr/spark-vllm-b12x:20261012"
        box.serve_fp4(template_fixture=True, image=own)
        rc, out, runs, _ = box.run("flash-fp4-local", DOCKER_IMAGES=own)
        self.assertNotIn("is not present", out)
        self.assertEqual(len(runs), 1, out)
        self.assertIn(f" {own} ", runs[0])

    def test_prepare_measures_and_fetches_in_the_cache_the_serving_launcher_mounts(self):
        # a stale fp4 launcher beside the serving SGLang one: its cache, on a disk with no
        # room, was the one prepare-lane measured, while the switch fetched into the other
        # (found in review, 2026-10-20)
        box = Box()
        box.serve_fp4(hf=str(box.t / "stale-hf"))
        hf = box.t / "data-hf"
        box.serve_sglang_flash(hf=str(hf))
        rc, out, runs, _ = box.run("flash", "flash-fp4-local", script=box.prepare, DF_SMALL="stale-hf")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(runs), 1, runs)
        self.assertIn(f"-v {hf}:/hf ", runs[0])

    def test_prepare_on_a_27b_only_box_uses_the_27b_unit_s_cache(self):
        box = Box()
        hf = box.t / "data-hf"
        (box.units / "qwen38-sglang.service").write_text(
            "[Service]\nExecStart=/usr/bin/docker run --rm -v " + str(hf) +
            ":/root/.cache/huggingface lmsysorg/sglang:v1 python3 -m sglang.launch_server\n")
        rc, out, runs, _ = box.run("flash", "flash-fp4-local", script=box.prepare, DF_SMALL=str(box.home))
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(runs), 1, runs)
        self.assertIn(f"-v {hf}:/hf ", runs[0])

    def test_fetch_works_on_a_box_that_never_installed_the_lane(self):
        # the cockpit's Library Download path: no unit, no launcher, still fetches
        rc, out, runs, calls = Box().run("flash-fp4-local", "--fetch")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(runs), 1, runs)
        self.assertIn("spark-vllm-b12x", runs[0])

    def test_the_lane_level_switch_brings_the_fp4_config_back(self):
        box = Box()
        box.serve_fp4(template_fixture=True)
        rc, out, runs, calls = box.run("flash-fp4-local")
        # the wall this test stops at is the first sudo (the fake refuses it): by then
        # the run must have fetched, regenerated the fp4 template, left the launcher
        # alone and reached the enable of the flash unit
        self.assertEqual(len(runs), 1, runs)
        self.assertIn("spark-vllm-b12x", runs[0])
        self.assertIn("already loads flash-fp4-local", out)
        tpl = box.config / "chat-template-flashnext-local.jinja"
        self.assertTrue(tpl.is_file(), out)
        self.assertIn("sudo systemctl enable qwen38-flash.service", calls)
        # and the template is the fp4 one: the SGLang file was never written
        self.assertFalse((box.config / "chat-template-flashnext.jinja").exists())


class TheLimits(unittest.TestCase):
    def test_fp4_local_always_gets_the_concurrency_pair(self):
        # its launcher pins 8 requests whatever tier install.sh read back (found in review,
        # 2026-10-20)
        for tier in ("throughput", "context", "concurrency"):
            r = subprocess.run([str(REPO / "oc-limits.sh"), "flash-fp4-local", tier],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual((r.returncode, r.stdout.split()), (0, ["205000", "32000", "local"]), tier)


class TheLauncher(unittest.TestCase):
    """What the FP4-Local launcher hands the engine, and what it must not: this
    build's own guard hardens /metrics behind the server key (read in the pinned
    image, 2026-10-19), so the engine gets no key of its own and the repo's
    middleware is the only gate, keeping /health and /metrics open as on every lane."""

    def test_the_engine_gets_no_key_of_its_own(self):
        # on the command lines (the comment above them says why there is none), and
        # as a whole flag: --speculative-config and --compilation-config may keep it
        serving = "\n".join(ln for ln in LAUNCHER_TPL.splitlines()
                            if not ln.lstrip().startswith("#"))
        self.assertIsNone(re.search(r"(^| )--config( |$)", serving, re.M))
        self.assertNotIn("engine-secrets", serving)

    def test_the_repo_middleware_is_the_gate(self):
        self.assertIn("--middleware vllm_engine_auth.RequireKey", LAUNCHER_TPL)
        self.assertIn("QWEN38_KEY_FILE=/out/api-key", LAUNCHER_TPL)
        # the key itself reaches no command line
        self.assertNotIn("--api-key", LAUNCHER_TPL)

    def test_dev_mode_stays_off_so_the_engine_has_no_server_info(self):
        # VLLM_SERVER_DEV_MODE mounts vLLM's own GET /server_info. The proxy reads
        # a 200 there as SGLang's info route, finds no max_total_num_tokens, and
        # never reaches the /metrics pool: every big prompt was refused "unknown
        # pool, engine warming up" (found on the box, 2026-10-19)
        serving = "\n".join(ln for ln in LAUNCHER_TPL.splitlines()
                            if not ln.lstrip().startswith("#"))
        self.assertNotIn("VLLM_SERVER_DEV_MODE", serving)


if __name__ == "__main__":
    unittest.main()
