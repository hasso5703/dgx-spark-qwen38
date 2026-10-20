#!/usr/bin/env python3
"""install.sh restarts the engine only when it has to.

It restarted it on every run, so an update that changed nothing the engine reads still
cost a full boot: 8 min on the 27B, 12 on flash (reference box, 2026-09-23). The engine
is now kept when it started after every file it reads was last written, the run changed
none of them by content, and it answers. These pin engine-inputs.py (what the engine
reads, found from its unit) and the installer's own decision lines, run as written,
against fake systemctl, docker, python3 and curl."""
import atexit
import importlib.util
import inspect
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TOOL = REPO / "engine-inputs.py"
DIGEST = "lmsysorg/sglang@sha256:" + "a" * 64

FAKE_DOCKER = """#!/bin/sh
# docker image inspect --format {{.Id}} REF | docker inspect --format {{.Image}} NAME
if [ "$1" = image ]; then v=$(grep -F "$5=" "$FAKE_IMAGES" | head -1 | cut -d= -f2-); else v=$(grep -F "container:$4=" "$FAKE_IMAGES" | head -1 | cut -d= -f2-); fi
[ -n "$v" ] || exit 1
echo "$v"
"""
# systemd prints the epoch only when asked for it (--timestamp=unix, systemd >= 248), and a
# local date otherwise: a fake that always printed the epoch let the flag go unnoticed
# (found in review, 2026-09-24)
FAKE_SYSTEMCTL = """#!/bin/sh
echo "ActiveState=$FAKE_ACTIVE"
case " $* " in
  *" --timestamp=unix "*) echo "ExecMainStartTimestamp=@$FAKE_STARTED" ;;
  *) echo "ExecMainStartTimestamp=$(date -d "@$FAKE_STARTED" '+%a %Y-%m-%d %H:%M:%S %Z')" ;;
esac
"""


class Box:
    """A config dir, an HF cache and a unit, the way the installer lays them out."""

    def __init__(self, flash=False):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="engine-in-"))
        # gone when the run ends: every run left its boxes in /tmp (found in review, 2026-10-17)
        atexit.register(shutil.rmtree, self.t, True)
        self.cfg = self.t / "cfg"
        self.cfg.mkdir()
        self.hf = self.t / "hf"
        (self.cfg / "api-key").write_text("k\n")
        snap = self.hf / "hub" / "models--org--model" / "snapshots" / "rev1"
        snap.mkdir(parents=True)
        (snap / "config.json").write_text('{"rope_scaling": null}\n')
        self.config_json = snap / "config.json"
        self.unit = self.t / ("qwen38-flash.service" if flash else "qwen38-sglang.service")
        if flash:
            (self.cfg / "chat-template-flashnext.jinja").write_text("{{ x }}\n")
            (self.cfg / "token-map-65536.pt").write_bytes(b"\x00\x01")
            (self.cfg / "launch-flash.sh").write_text(
                f"docker run --name qwen38-flash -v {self.cfg}:/out {DIGEST} "
                f"--chat-template /out/chat-template-flashnext.jinja "
                f"--speculative-token-map /out/token-map-65536.pt --api-key $(cat {self.cfg}/api-key)\n")
            self.unit.write_text(f"[Service]\nExecStart=/bin/bash {self.cfg}/launch-flash.sh\n")
            self.template = self.cfg / "chat-template-flashnext.jinja"
        else:
            (self.cfg / "chat-template-sglang.jinja").write_text("{{ x }}\n")
            self.unit.write_text(
                f"[Service]\nExecStart=/bin/bash -c 'exec docker run --name qwen38-sglang -v {self.cfg}:/out "
                f"{DIGEST} --chat-template /out/chat-template-sglang.jinja "
                f"--api-key \"$(cat {self.cfg}/api-key)\"'\n")
            self.template = self.cfg / "chat-template-sglang.jinja"
        self.bin = self.t / "bin"
        self.bin.mkdir()
        for name, body in (("docker", FAKE_DOCKER), ("systemctl", FAKE_SYSTEMCTL)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.images = self.t / "images"
        container = "qwen38-flash" if flash else "qwen38-sglang"
        self.images.write_text(f"{DIGEST}=sha256:img1\ncontainer:{container}=sha256:img1\n")
        self.active, self.started = "active", int(time.time()) + 5

    def run(self, mode, ckpt="org/model@rev1"):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "FAKE_IMAGES": str(self.images),
               "FAKE_ACTIVE": self.active, "FAKE_STARTED": str(self.started)}
        r = subprocess.run([sys.executable, str(TOOL), mode, str(self.unit), str(self.cfg), str(self.hf),
                            ckpt], capture_output=True, text=True, env=env, timeout=30)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()


class WhatTheEngineReads(unittest.TestCase):
    def test_the_27b_unit_names_its_template_key_checkpoint_and_image(self):
        box = Box()
        out = subprocess.run([sys.executable, "-c",
                              "import importlib.util,sys; s=importlib.util.spec_from_file_location('e', sys.argv[1]);"
                              "m=importlib.util.module_from_spec(s); s.loader.exec_module(m);"
                              "f,i,c=m.inputs(*sys.argv[2:5], ['org/model@rev1']); print(f); print(i); print(c)",
                              str(TOOL), str(box.unit), str(box.cfg), str(box.hf)], capture_output=True, text=True)
        files = out.stdout.splitlines()[0]
        for p in (box.unit, box.template, box.cfg / "api-key", box.config_json):
            self.assertIn(str(p), files)
        self.assertIn(DIGEST, out.stdout.splitlines()[1])
        self.assertIn("qwen38-sglang", out.stdout.splitlines()[2])

    def test_the_guard_the_unit_runs_first_is_no_input(self):
        """Every engine unit runs engine-preflight.sh before it starts the engine, which never
        reads it: a new guard, or one rewritten after the start, must not restart a healthy
        engine. Both lookups found it, the scripts the unit runs and the config files it names."""
        for flash in (False, True):
            with self.subTest(flash=flash):
                box = Box(flash=flash)
                guard = box.cfg / "engine-preflight.sh"
                guard.write_text("#!/usr/bin/env bash\necho guard one\n")
                box.unit.write_text(box.unit.read_text().replace(
                    "[Service]\n", f"[Service]\nExecStartPre=/bin/bash {guard} {box.unit.name}\n"))
                fp = box.run("fingerprint")
                guard.write_text("#!/usr/bin/env bash\necho guard two\n")
                self.assertEqual(box.run("fingerprint"), fp)
                later = time.time() + 600
                os.utime(guard, (later, later))
                self.assertTrue(box.run("running").startswith("yes"), box.run("running"))

    def test_the_ats_question_the_flash_launcher_asks_first_is_no_input(self):
        """The flash launcher asks flash-ats-check.sh before it runs the image (issue #54), which
        the engine never reads: a new wording of it must not restart a healthy flash lane."""
        box = Box(flash=True)
        check = box.cfg / "flash-ats-check.sh"
        check.write_text("#!/usr/bin/env bash\necho addressing mode ATS\n")
        launcher = box.cfg / "launch-flash.sh"
        launcher.write_text(f'why="$(bash {check})" || exit 78\n' + launcher.read_text())
        fp = box.run("fingerprint")
        check.write_text("#!/usr/bin/env bash\necho addressing mode ATS, said otherwise\n")
        self.assertEqual(box.run("fingerprint"), fp)

    def test_the_flash_unit_brings_in_its_launcher_and_what_it_names(self):
        box = Box(flash=True)
        fp = box.run("fingerprint")
        (box.cfg / "token-map-65536.pt").write_bytes(b"\x00\x02")
        self.assertNotEqual(box.run("fingerprint"), fp, "the token map named by the launcher is an input")
        fp = box.run("fingerprint")
        (box.cfg / "launch-flash.sh").write_text((box.cfg / "launch-flash.sh").read_text() + "# a flag\n")
        self.assertNotEqual(box.run("fingerprint"), fp, "the launcher itself is an input")


class RefsAndMovingTags(unittest.TestCase):
    """Two documented settings kept a stale engine under "nothing it reads changed" (found in
    review, 2026-09-24): a revision given as a branch (MODEL_REV=main), whose snapshot is
    named by the commit its ref holds, so neither snapshots/main/config.json (it never
    exists) nor the ref counted; and an image given by a tag that moves, which neither of
    the image patterns matched."""

    def test_a_branch_revision_reads_the_config_of_the_commit_it_names(self):
        box = Box()
        refs = box.hf / "hub" / "models--org--model" / "refs"
        refs.mkdir()
        (refs / "main").write_text("rev1")
        fp = box.run("fingerprint", "org/model@main")
        box.config_json.write_text('{"rope_scaling": {"type": "yarn"}}\n')     # a YaRN patch
        self.assertNotEqual(box.run("fingerprint", "org/model@main"), fp)

    def test_a_branch_that_moves_is_a_change(self):
        box = Box()
        refs = box.hf / "hub" / "models--org--model" / "refs"
        refs.mkdir()
        (refs / "main").write_text("rev1")
        fp = box.run("fingerprint", "org/model@main")
        snap2 = box.hf / "hub" / "models--org--model" / "snapshots" / "rev2"
        snap2.mkdir()
        (snap2 / "config.json").write_text('{"rope_scaling": null, "new": 1}\n')
        (refs / "main").write_text("rev2")                                       # a download moved main
        self.assertNotEqual(box.run("fingerprint", "org/model@main"), fp)

    def test_an_image_named_by_a_tag_that_moves_is_a_change(self):
        box = Box()
        box.unit.write_text("[Service]\nExecStart=/bin/bash -c 'exec docker run --name qwen38-sglang "
                            f"-v {box.cfg}:/out \\\n  lmsysorg/sglang:v0.5.19 \\\n  python3 -m sglang.launch_server "
                            "--chat-template /out/chat-template-sglang.jinja'\n")
        box.images.write_text("lmsysorg/sglang:v0.5.19=sha256:img1\ncontainer:qwen38-sglang=sha256:img1\n")
        fp = box.run("fingerprint")
        self.assertEqual(box.run("running"), "yes")
        box.images.write_text("lmsysorg/sglang:v0.5.19=sha256:img2\ncontainer:qwen38-sglang=sha256:img1\n")
        self.assertNotEqual(box.run("fingerprint"), fp, "a pull that moved the tag is a change")
        self.assertIn("another image", box.run("running"))

    def fp4_box(self, image):
        """The FP4-Local unit and its launcher rendered from the repo's template, with image as
        the serving image (FLASH_LOCAL_IMAGE=eugr/spark-vllm-b12x:latest, the override the
        step-2 die message recommends)."""
        box = Box()
        launcher = box.cfg / "launch-flash-fp4local.sh"
        subs = {"__HOME__/.config/qwen38": str(box.cfg), "__HOME__": str(box.t), "__HF_CACHE__": str(box.hf),
                "__IMAGE__": image, "__MODEL__": "nvidia/Qwen3.8-Flash-Next-FP4-Local",
                "__MODEL_REV_ARGS__": "--revision rev1", "__FLASH_QUANT_ARGS__": "--quantization modelopt_mixed ",
                "__FLASH_GPU_UTIL__": "0.8", "__ENGINE_BIND__": "127.0.0.1", "__PORT__": "30001"}
        text = (REPO / "qwen38-flash-fp4local-launch.sh.template").read_text()
        for k, v in subs.items():
            text = text.replace(k, v)
        launcher.write_text(text)
        box.unit.write_text(f"[Service]\nExecStart=/bin/bash {launcher}\n")
        return box

    def test_the_fp4_local_launcher_names_its_image_by_a_tag_too(self):
        # only SGLang's server command was known, and the mount /opt/qwen38-auth:ro matched the
        # qwen38 tag pattern instead (found in review, 2026-10-20)
        box = self.fp4_box("eugr/spark-vllm-b12x:latest")
        spec = importlib.util.spec_from_file_location("engine_inputs_fp4", TOOL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _, images, _ = mod.inputs(str(box.unit), str(box.cfg), str(box.hf), [])
        self.assertEqual(images, ["eugr/spark-vllm-b12x:latest"])

    def test_the_fp4_local_middleware_is_an_input_the_installer_names(self):
        # the launcher mounts its folder only, so the installer passes the file (found in
        # review, 2026-10-20)
        box = self.fp4_box("eugr/spark-vllm-b12x:latest")
        auth = box.cfg / "vllm-auth" / "vllm_engine_auth.py"
        auth.parent.mkdir()
        auth.write_text("v1\n")
        fp = box.run("fingerprint", f"--extra={auth}")
        auth.write_text("v2\n")
        self.assertNotEqual(box.run("fingerprint", f"--extra={auth}"), fp)
        self.assertIn('ENGINE_CKPTS+=("--extra=$CONFIG_DIR/vllm-auth/vllm_engine_auth.py")',
                      (REPO / "install.sh").read_text())


class TheFingerprintIsByContent(unittest.TestCase):
    def test_a_changed_template_changes_it(self):
        box = Box()
        fp = box.run("fingerprint")
        box.template.write_text("{{ y }}\n")
        self.assertNotEqual(box.run("fingerprint"), fp)

    def test_a_file_rewritten_identically_does_not(self):
        box = Box()
        fp = box.run("fingerprint")
        box.template.write_text(box.template.read_text())
        os.utime(box.config_json, None)
        self.assertEqual(box.run("fingerprint"), fp)

    def test_a_yarn_patch_in_the_checkpoint_changes_it(self):
        box = Box()
        fp = box.run("fingerprint")
        box.config_json.write_text('{"rope_scaling": {"type": "yarn", "factor": 4.0}}\n')
        self.assertNotEqual(box.run("fingerprint"), fp)

    def test_the_lines_naming_the_other_lanes_do_not(self):
        """A lane added to the box changes the Conflicts= and After= of every other unit, which
        systemd applies at its daemon-reload and the engine never reads: the update that brought
        the Nemotron lane restarted the text lane that served for them (found in review,
        2026-10-15). What the engine runs still counts, in the unit and in a drop-in."""
        box = Box()
        head = "[Unit]\nDescription=27B\nConflicts=qwen38-flash.service\nAfter=network-online.target qwen38-flash.service\n"
        box.unit.write_text(head + box.unit.read_text())
        dropin = pathlib.Path(str(box.unit) + ".d")
        dropin.mkdir()
        (dropin / "10-x.conf").write_text("[Unit]\nWants=docker.service\n[Service]\nEnvironment=A=1\n")
        fp = box.run("fingerprint")
        box.unit.write_text(box.unit.read_text().replace("qwen38-flash.service\n", "qwen38-flash.service qwen38-nemotron.service\n")
                            .replace("Description=27B", "Description=the 27B lane"))
        (dropin / "10-x.conf").write_text("[Unit]\nWants=docker.service network-online.target\nBefore=x.service\n[Service]\nEnvironment=A=1\n")
        self.assertEqual(box.run("fingerprint"), fp)
        (dropin / "10-x.conf").write_text("[Unit]\nWants=docker.service\n[Service]\nEnvironment=A=2\n")
        self.assertNotEqual(box.run("fingerprint"), fp, "a drop-in's environment is read by the engine")
        (dropin / "10-x.conf").write_text("[Unit]\nWants=docker.service\n[Service]\nEnvironment=A=1\n")
        self.assertEqual(box.run("fingerprint"), fp)
        box.unit.write_text(box.unit.read_text().replace("--chat-template", "--chat-template-x"))
        self.assertNotEqual(box.run("fingerprint"), fp, "the command the unit runs is read")

    def test_an_image_that_moved_under_its_name_changes_it(self):
        box = Box()
        fp = box.run("fingerprint")
        box.images.write_text(box.images.read_text().replace(f"{DIGEST}=sha256:img1", f"{DIGEST}=sha256:img2"))
        self.assertNotEqual(box.run("fingerprint"), fp)


class AUnitIsPutInPlaceDatedByWhatItsEngineReads(unittest.TestCase):
    """lanes.sh's unit_put, as every installer puts a lane's unit in place: an identical unit is
    left as it is, one changed only in the lines naming the other lanes keeps its date (which
    engine-inputs.py's running verdict and lane_serves_older go by), any other change is new."""

    def put(self, old, new, sudo_fails=""):
        d = pathlib.Path(tempfile.mkdtemp(prefix="unit-put-"))
        bin_ = d / "bin"
        bin_.mkdir()
        # sudo runs what it is given, as the user here, and fails on the command named
        (bin_ / "sudo").write_text(f'#!/bin/bash\n[ -n "{sudo_fails}" ] && [ "$1" = "{sudo_fails}" ] && exit 1\nexec "$@"\n')
        (bin_ / "sudo").chmod(0o755)
        unit, rendered = d / "qwen38-sglang.service", d / "rendered"
        if old is not None:
            unit.write_text(old)
            os.utime(unit, (1_700_000_000, 1_700_000_000))
        rendered.write_text(new)
        r = subprocess.run(["bash", "-c", f'set -euo pipefail\nCONFIG_DIR="{d}"; . "{REPO}/lanes.sh"\n'
                            f'UNIT_PUT="$(unit_put "{rendered}" "{unit}")"\necho "PUT=$UNIT_PUT"'],
                           capture_output=True, text=True, timeout=30, env={"PATH": f"{bin_}:/usr/bin:/bin"})
        return r, unit

    OLD = "[Unit]\nConflicts=qwen38-flash.service\nAfter=qwen38-flash.service\n[Service]\nExecStart=/bin/true\n"

    def test_an_identical_unit_is_left_as_it_is(self):
        r, unit = self.put(self.OLD, self.OLD)
        self.assertIn("PUT=same", r.stdout, r.stderr)
        self.assertEqual(int(unit.stat().st_mtime), 1_700_000_000)

    def test_a_unit_changed_only_in_the_lines_naming_the_other_lanes_keeps_its_date(self):
        new = self.OLD.replace("qwen38-flash.service\n", "qwen38-flash.service qwen38-nemotron.service\n")
        r, unit = self.put(self.OLD, new)
        self.assertIn("PUT=deps", r.stdout, r.stderr)
        self.assertEqual(unit.read_text(), new)
        self.assertEqual(int(unit.stat().st_mtime), 1_700_000_000)

    def test_any_other_change_is_new_and_dated_now(self):
        for old, new in ((self.OLD, self.OLD.replace("/bin/true", "/bin/false")), (None, self.OLD)):
            with self.subTest(old=old is not None):
                r, unit = self.put(old, new)
                self.assertIn("PUT=new", r.stdout, r.stderr)
                self.assertEqual(unit.read_text(), new)
                self.assertGreater(unit.stat().st_mtime, time.time() - 60)

    def test_a_step_that_fails_stops_the_installer(self):
        # in $(...) a function does not stop at a failure under set -e: unit_put returns on each
        for fails, new in (("install", self.OLD.replace("/bin/true", "/bin/false")),
                           ("touch", self.OLD.replace("After=qwen38-flash.service", "After=x.service"))):
            with self.subTest(fails=fails):
                r, _ = self.put(self.OLD, new, sudo_fails=fails)
                self.assertNotEqual(r.returncode, 0)
                self.assertNotIn("PUT=", r.stdout)

    def test_every_installer_puts_its_unit_through_it(self):
        for script in ("install.sh", "install-image.sh", "install-video.sh"):
            with self.subTest(script=script):
                text = (REPO / script).read_text()
                self.assertIn('UNIT_PUT="$(unit_put "', text)
                self.assertNotRegex(text, r'sudo install -m 644 "\$(RENDER|TMP_UNIT)"')
        # the three vLLM lanes' installers, through what they share (vllm-lane.sh)
        shared = (REPO / "vllm-lane.sh").read_text()
        self.assertIn('put="$(unit_put "$1" "$INSTALLED")"', shared)
        for script in ("install-kolibri.sh", "install-nemotron.sh", "install-nemotron-super.sh", "vllm-lane.sh"):
            with self.subTest(script=script):
                text = (REPO / script).read_text()
                self.assertNotRegex(text, r'sudo install -m 644 "\$(RENDER|TMP_UNIT|1)"')
                if script != "vllm-lane.sh":
                    self.assertIn('vlane_put_unit "$RENDER"', text)

    def test_the_engine_keeps_running_when_only_those_lines_change(self):
        """The whole chain: the unit put back with a lane named in its Conflicts= and After=
        after the engine started; at this run nothing it reads changed, and at the next run it
        still started after everything it reads (its date is the old one)."""
        box = Box()
        old = "[Unit]\nConflicts=qwen38-flash.service\nAfter=qwen38-flash.service\n" + box.unit.read_text()
        box.unit.write_text(old)
        before = time.time() - 600
        for f in [box.unit, *box.cfg.rglob("*"), *box.hf.rglob("*")]:
            os.utime(f, (before, before))
        box.started = int(time.time()) - 300                 # the engine started after all it reads
        fp = box.run("fingerprint")
        self.assertEqual(box.run("running"), "yes")
        bin_ = box.t / "sudo-bin"
        bin_.mkdir()
        (bin_ / "sudo").write_text('#!/bin/bash\nexec "$@"\n')
        (bin_ / "sudo").chmod(0o755)
        rendered = box.t / "rendered"
        rendered.write_text(old.replace("qwen38-flash.service\n", "qwen38-flash.service qwen38-nemotron.service\n"))
        r = subprocess.run(["bash", "-c", f'set -euo pipefail\nCONFIG_DIR="{box.cfg}"; . "{REPO}/lanes.sh"\n'
                            f'unit_put "{rendered}" "{box.unit}"'], capture_output=True, text=True, timeout=30,
                           env={"PATH": f"{bin_}:/usr/bin:/bin"})
        self.assertEqual(r.stdout.strip(), "deps", r.stderr)
        self.assertEqual(box.run("fingerprint"), fp, "this run: nothing the engine reads changed")
        self.assertEqual(box.run("running"), "yes", "the next run: it still started after everything it reads")


class WhatTheCockpitAsksFor(unittest.TestCase):
    """The cockpit calls running() in its own process, on its 30 s tier: it bounds each
    systemctl and docker call, and it hands the vLLM lanes the files their installer counts
    beyond the unit (vllm-lane.sh's LANE_FILES). Found in review, 2026-10-16."""

    def call(self, box, **kw):
        spec = importlib.util.spec_from_file_location("engine_inputs_api", TOOL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        before = dict(os.environ)
        os.environ.update(PATH=f"{box.bin}:/usr/bin:/bin", FAKE_IMAGES=str(box.images),
                          FAKE_ACTIVE=box.active, FAKE_STARTED=str(box.started))
        try:
            return mod.running(str(box.unit), str(box.cfg), str(box.hf), ["org/model@rev1"], **kw)
        finally:
            os.environ.clear()
            os.environ.update(before)

    def test_a_file_the_installer_counts_beyond_the_unit_is_one_more_input(self):
        box = Box()
        t = int(time.time()) - 100
        for f in (box.unit, box.template, box.cfg / "api-key", box.config_json):
            os.utime(f, (t, t))
        box.started = t
        auth = box.cfg / "vllm-auth" / "vllm_engine_auth.py"
        auth.parent.mkdir()
        auth.write_text("x\n")  # written now, after the start
        self.assertEqual(self.call(box), "yes")  # the unit alone does not name it
        self.assertEqual(self.call(box, extra=[str(auth)]),
                         f"no: {auth} changed after qwen38-sglang.service started")
        # an absent file is no input, as lane_serves_older's [ -e ] has it
        self.assertEqual(self.call(box, extra=[str(box.cfg / "absent.py")]), "yes")

    def test_a_call_that_does_not_answer_is_cut_when_a_bound_is_given(self):
        box = Box()
        (box.bin / "docker").write_text("#!/bin/sh\nexec sleep 30\n")
        t0 = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.call(box, timeout=1)
        self.assertLess(time.time() - t0, 10)

    def test_a_systemctl_that_does_not_answer_is_cut_too(self):
        box = Box()
        (box.bin / "systemctl").write_text("#!/bin/sh\nexec sleep 30\n")
        with self.assertRaises(subprocess.TimeoutExpired):
            self.call(box, timeout=1)

    def test_a_container_inspect_that_does_not_answer_is_cut_too(self):
        # the image lookup answers, the container's does not: the third call is bounded as well
        box = Box()
        fake = box.bin / "docker"
        fake.write_text(FAKE_DOCKER.replace('if [ "$1" = image ]; then', 'if [ "$1" = inspect ]; then exec sleep 30; fi\nif [ "$1" = image ]; then', 1))
        t0 = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.call(box, timeout=1)
        self.assertLess(time.time() - t0, 10)

    def test_the_installers_still_wait_as_before(self):
        spec = importlib.util.spec_from_file_location("engine_inputs_sig", TOOL)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIsNone(inspect.signature(mod.running).parameters["timeout"].default)
        self.assertEqual(inspect.signature(mod.running).parameters["extra"].default, ())


class TheRunningVerdict(unittest.TestCase):
    def test_an_engine_started_after_everything_it_reads_has_it(self):
        self.assertEqual(Box().run("running"), "yes")

    def test_a_file_written_after_the_start_is_not_in_the_running_engine(self):
        box = Box()
        box.started = int(time.time()) - 100
        self.assertRegex(box.run("running"), r"^no: .* changed after qwen38-sglang.service started$")

    def test_a_file_written_in_the_second_the_engine_started_is_in_it(self):
        # The installer writes the unit, then restarts the engine in the same second, and
        # systemd gives the start in whole seconds: compared with the file's fraction, the
        # run after every first install restarted an engine that had everything (the
        # reference box, 2026-09-30).
        box = Box()
        t = int(time.time()) - 100
        for f in (box.unit, box.template, box.cfg / "api-key", box.config_json):
            os.utime(f, (t + 0.9, t + 0.9))
        box.started = t
        self.assertEqual(box.run("running"), "yes")

    def test_a_file_written_the_second_after_the_start_is_not(self):
        box = Box()
        t = int(time.time()) - 100
        for f in (box.unit, box.template, box.cfg / "api-key", box.config_json):
            os.utime(f, (t, t))
        os.utime(box.template, (t + 1.1, t + 1.1))
        box.started = t
        self.assertRegex(box.run("running"),
                         r"^no: .*chat-template-sglang\.jinja changed after qwen38-sglang\.service started$")

    def test_a_stopped_engine_has_nothing(self):
        box = Box()
        box.active = "inactive"
        self.assertEqual(box.run("running"), "no: qwen38-sglang.service is inactive")

    def test_a_container_on_another_image_does_not_run_this_one(self):
        box = Box()
        box.images.write_text(box.images.read_text().replace("container:qwen38-sglang=sha256:img1",
                                                             "container:qwen38-sglang=sha256:old"))
        self.assertIn("runs another image", box.run("running"))


class TheInstallerDecides(unittest.TestCase):
    """Step 9's own lines, with engine-inputs.py and curl stubbed by their answers."""

    def decide(self, running="yes", fp_before="F1", fp_after="F1", health=True, restart=None):
        text = (REPO / "install.sh").read_text()
        start = text.index("# Kept only when the running engine started after every file it reads")
        end = text.index('[ "$ENGINE_KEEP" -eq 1 ] || sudo systemctl restart "$UNIT_NAME"')
        t = pathlib.Path(tempfile.mkdtemp(prefix="engine-decide-"))
        (t / "python3").write_text(f'#!/bin/sh\necho "{fp_after}"\n')
        (t / "curl").write_text(f"#!/bin/sh\nexit {0 if health else 22}\n")
        for n in ("python3", "curl"):
            (t / n).chmod(0o755)
        script = ("set -euo pipefail\nstep(){ echo \"STEP $*\"; }\n"
                  f'ENGINE_RUNNING_IT="{running}"; ENGINE_FP_BEFORE="{fp_before}"; LANE=27b; PORT=30000\n'
                  'REPO_DIR=/r; ENGINE_UNIT_PATH=/u; CONFIG_DIR=/c; HF_CACHE=/h; ENGINE_CKPTS=(a@b); OTHER_UNIT=""\n'
                  + (f"RESTART_ENGINE={restart}\n" if restart else "")
                  + text[start:end] + 'echo "KEEP=$ENGINE_KEEP"\n')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": f"{t}:/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_nothing_changed_keeps_the_engine(self):
        out = self.decide()
        self.assertIn("KEEP=1", out)
        self.assertIn("STEP 9/10 Keeping the running engine", out)
        self.assertNotIn("why:", out)

    def test_a_run_that_changed_an_input_restarts_it(self):
        out = self.decide(fp_after="F2")
        self.assertIn("KEEP=0", out)
        self.assertIn("why: this run changed what it reads", out)

    def test_an_engine_that_predates_an_input_restarts_it(self):
        out = self.decide(running="no: /c/chat-template-sglang.jinja changed after qwen38-sglang.service started")
        self.assertIn("KEEP=0", out)
        self.assertIn("why: /c/chat-template-sglang.jinja changed after", out)

    def test_an_engine_that_does_not_answer_restarts_it(self):
        self.assertIn("why: it does not answer /health", self.decide(health=False))

    def test_restart_engine_1_always_restarts(self):
        out = self.decide(restart="1")
        self.assertIn("KEEP=0", out)
        self.assertIn("why: RESTART_ENGINE=1", out)

    def test_what_the_engine_reads_is_rewritten_only_when_it_changes(self):
        # A rewrite, even an identical one, dates the file after the engine's start, and
        # the next run restarted it: on the reference box every second run did, until the
        # unit and the flash launcher were compared first (the template, in patch-template.py).
        text = (REPO / "install.sh").read_text()
        self.assertIn('UNIT_PUT="$(unit_put "$TMP_UNIT" "/etc/systemd/system/$UNIT_NAME")"', text)
        self.assertNotIn('sudo install -m 644 "$TMP_UNIT"', text, "the unit goes in through unit_put alone")
        # the launcher is installed in the branch where the comparison found a difference,
        # and only there (it also says which of the two happened, since 2026-09-30)
        # the written name is a variable since the lane's FP4-Local config (v1.37.0):
        # the same compare-then-write path covers whichever launcher MODEL_CHOICE picked
        self.assertRegex(text, r'if cmp -s "\$TMP_LAUNCH" "\$CONFIG_DIR/\$LAUNCH_NAME"; then\n'
                               r'\s*echo "launch script unchanged[^\n]*\n\s*else\n'
                               r'\s*install -m 755 "\$TMP_LAUNCH" "\$CONFIG_DIR/\$LAUNCH_NAME"\n')
        self.assertEqual(text.count('install -m 755 "$TMP_LAUNCH"'), 1, "no other path writes the launcher")

    def test_the_restart_itself_is_the_one_the_decision_guards(self):
        self.assertIn('[ "$ENGINE_KEEP" -eq 1 ] || sudo systemctl restart "$UNIT_NAME"',
                      (REPO / "install.sh").read_text())

    def test_a_first_install_just_starts_it(self):
        out = self.decide(running="no: not installed yet", fp_before="")
        self.assertIn("KEEP=0", out)
        self.assertNotIn("why:", out)
        self.assertIn("STEP 9/10 Starting", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
