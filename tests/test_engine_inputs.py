#!/usr/bin/env python3
"""install.sh restarts the engine only when it has to.

It restarted it on every run, so an update that changed nothing the engine reads still
cost a full boot: 8 min on the 27B, 12 on flash (reference box, 2026-09-23). The engine
is now kept when it started after every file it reads was last written, the run changed
none of them by content, and it answers. These pin engine-inputs.py (what the engine
reads, found from its unit) and the installer's own decision lines, run as written,
against fake systemctl, docker, python3 and curl."""
import os
import pathlib
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
FAKE_SYSTEMCTL = """#!/bin/sh
echo "ActiveState=$FAKE_ACTIVE"
echo "ExecMainStartTimestamp=@$FAKE_STARTED"
"""


class Box:
    """A config dir, an HF cache and a unit, the way the installer lays them out."""

    def __init__(self, flash=False):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="engine-in-"))
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

    def run(self, mode):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "FAKE_IMAGES": str(self.images),
               "FAKE_ACTIVE": self.active, "FAKE_STARTED": str(self.started)}
        r = subprocess.run([sys.executable, str(TOOL), mode, str(self.unit), str(self.cfg), str(self.hf),
                            "org/model@rev1"], capture_output=True, text=True, env=env, timeout=30)
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

    def test_the_flash_unit_brings_in_its_launcher_and_what_it_names(self):
        box = Box(flash=True)
        fp = box.run("fingerprint")
        (box.cfg / "token-map-65536.pt").write_bytes(b"\x00\x02")
        self.assertNotEqual(box.run("fingerprint"), fp, "the token map named by the launcher is an input")
        fp = box.run("fingerprint")
        (box.cfg / "launch-flash.sh").write_text((box.cfg / "launch-flash.sh").read_text() + "# a flag\n")
        self.assertNotEqual(box.run("fingerprint"), fp, "the launcher itself is an input")


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

    def test_an_image_that_moved_under_its_name_changes_it(self):
        box = Box()
        fp = box.run("fingerprint")
        box.images.write_text(box.images.read_text().replace(f"{DIGEST}=sha256:img1", f"{DIGEST}=sha256:img2"))
        self.assertNotEqual(box.run("fingerprint"), fp)


class TheRunningVerdict(unittest.TestCase):
    def test_an_engine_started_after_everything_it_reads_has_it(self):
        self.assertEqual(Box().run("running"), "yes")

    def test_a_file_written_after_the_start_is_not_in_the_running_engine(self):
        box = Box()
        box.started = int(time.time()) - 100
        self.assertRegex(box.run("running"), r"^no: .* changed after qwen38-sglang.service started$")

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
        self.assertIn('cmp -s "$TMP_UNIT" "/etc/systemd/system/$UNIT_NAME" || sudo install -m 644 "$TMP_UNIT"', text)
        self.assertIn('cmp -s "$TMP_LAUNCH" "$CONFIG_DIR/launch-flash.sh" || install -m 755 "$TMP_LAUNCH"', text)

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
