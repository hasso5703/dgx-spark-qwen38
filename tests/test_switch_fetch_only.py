#!/usr/bin/env python3
"""switch-model.sh <target> --fetch: the target's checkpoint, fetched as a switch fetches it
(the cache first, resumable, its room checked), and nothing else: no unit rewritten, no boot
moved, no engine touched, no record written. Library's "Download" and a lane installed
without its weights (the on-demand install) get their weights this way before a Load.

The script runs as written on a copy whose unit paths point at this test's files, with a
fake docker whose download succeeds and a fake systemctl that records every call.
"""
import pathlib
import re
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SWITCH = (REPO / "switch-model.sh").read_text()

FAKE_DOCKER = """#!/bin/sh
case "$1" in
  image) exit 0 ;;
  run) printf '%s\\n' "$*" >> "$DOCKER_LOG"; exit 0 ;;   # the download succeeds
esac
exit 0
"""


class Box:
    def __init__(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="switch-fetch-"))
        self.home = self.t / "home"
        (self.home / ".config/qwen38").mkdir(parents=True)
        units = self.t / "units"
        units.mkdir()
        self.unit = units / "qwen38-sglang.service"
        self.unit.write_text(
            "[Service]\nExecStart=/bin/bash -c 'exec docker run --rm --name qwen38-sglang \\\n"
            f"  -v {self.home}/.cache/huggingface:/root/.cache/huggingface \\\n"
            "  lmsysorg/sglang@sha256:" + "a" * 64 + " \\\n"
            "  python3 -m sglang.launch_server --model-path RadixArk/Qwen3.8-27B-NVFP4 \\\n"
            "  --host 127.0.0.1 --port 30000'\n")
        repo = self.t / "repo"
        repo.mkdir()
        (repo / "install.sh").write_text((REPO / "install.sh").read_text())
        text = SWITCH
        for unit in ("qwen38-sglang.service", "qwen38-flash.service", "qwen38-nemotron.service",
                     "qwen38-nemotron-super.service"):
            old = f'"/etc/systemd/system/{unit}"'
            assert old in text, f"switch-model.sh no longer names {unit} that way"
            text = text.replace(old, f'"{units}/{unit}"')
        (repo / "switch-model.sh").write_text(text)
        for f in ("lanes.sh", "ckpt-fetch.py", "install-nemotron.sh", "install-nemotron-super.sh"):
            (repo / f).write_text((REPO / f).read_text())
        # the Nemotron lane's unit, as install-nemotron.sh writes it: its image, its cache, the
        # checkpoint and the memory share on lines of their own
        pins = dict(line.split("=", 1) for line in (REPO / "install-nemotron.sh").read_text().splitlines()
                    if line.startswith("NEMOTRON_BOLT_") and "=" in line)
        self.nemotron_pins = {k: v.strip('"') for k, v in pins.items()}
        self.nemotron_unit = units / "qwen38-nemotron.service"
        self.nemotron_unit.write_text(
            "[Service]\nExecStart=/usr/bin/docker run --rm --name qwen38-nemotron \\\n"
            f"  -v {self.home}/.cache/huggingface:/root/.cache/huggingface \\\n"
            "  qwen38-nemotron:test \\\n"
            f"  {self.nemotron_pins['NEMOTRON_BOLT_REPO']} --revision {self.nemotron_pins['NEMOTRON_BOLT_REV']} \\\n"
            f"  --gpu-memory-utilization {self.nemotron_pins['NEMOTRON_BOLT_GMU']} \\\n"
            "  --port 30000\n")
        # the Nemotron Super lane's unit, the same way (the image is the Bolt lane's)
        spins = dict(line.split("=", 1) for line in (REPO / "install-nemotron-super.sh").read_text().splitlines()
                     if line.startswith("NEMOTRON_SUPER_") and "=" in line)
        self.super_pins = {k: v.strip('"') for k, v in spins.items()}
        self.super_unit = units / "qwen38-nemotron-super.service"
        self.super_unit.write_text(
            "[Service]\nExecStart=/usr/bin/docker run --rm --name qwen38-nemotron-super \\\n"
            f"  -v {self.home}/.cache/huggingface:/root/.cache/huggingface \\\n"
            "  qwen38-nemotron:test \\\n"
            f"  {self.super_pins['NEMOTRON_SUPER_REPO']} --revision {self.super_pins['NEMOTRON_SUPER_REV']} \\\n"
            f"  --gpu-memory-utilization {self.super_pins['NEMOTRON_SUPER_GMU']} \\\n"
            "  --port 30000\n")
        (repo / "switch-model.sh").chmod(0o755)
        self.bin = self.t / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "systemctl").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SYSTEMCTL_LOG"\nexit 1\n')
        (self.bin / "sudo").write_text('#!/bin/sh\nprintf "sudo %s\\n" "$*" >> "$SYSTEMCTL_LOG"\nexit 1\n')
        # a disk with room for the checkpoint, whatever this machine's has: the real df
        # answered here, and a 29 GB disk got the 45 GB refusal (found in a cloud review,
        # 2026-10-18); the room check has its own tests, in tests/test_switch_room.py
        (self.bin / "df").write_text("#!/bin/sh\necho Avail; echo 500G\n")
        for n in ("docker", "systemctl", "sudo", "df"):
            (self.bin / n).chmod(0o755)
        self.docker_log = self.t / "docker.log"
        self.sysctl_log = self.t / "systemctl.log"
        self.script = repo / "switch-model.sh"

    def run(self, *args):
        e = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home),
             "DOCKER_LOG": str(self.docker_log), "SYSTEMCTL_LOG": str(self.sysctl_log)}
        r = subprocess.run([str(self.script), *args], capture_output=True, text=True, env=e, timeout=60)
        runs = self.docker_log.read_text().splitlines() if self.docker_log.exists() else []
        calls = self.sysctl_log.read_text().splitlines() if self.sysctl_log.exists() else []
        return r.returncode, r.stdout + r.stderr, runs, calls


class FetchOnly(unittest.TestCase):
    def test_a_text_target_is_fetched_and_nothing_else_changes(self):
        box = Box()
        unit_before = box.unit.read_bytes()
        rc, out, runs, calls = box.run("stock", "--fetch")
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing else was changed", out)
        self.assertEqual(len(runs), 1, runs)
        self.assertIn("/opt/qwen38/ckpt-fetch.py RadixArk/Qwen3.8-27B-NVFP4 ", runs[0])
        self.assertEqual(box.unit.read_bytes(), unit_before)
        self.assertFalse([c for c in calls if re.search(r"\b(enable|disable|daemon-reload|restart|start|stop)\b", c)], calls)
        self.assertFalse((box.home / ".config/qwen38/text-lane").exists(), "--fetch wrote the text-lane record")

    def test_the_nemotron_lane_fetches_its_drafter_before_it_stops(self):
        # Library's Download of the Nemotron target stopped after the model, its drafter left for
        # the Load (found in review, 2026-10-15): the lane loads both, so both are fetched first
        box = Box()
        unit_before = box.nemotron_unit.read_bytes()
        rc, out, runs, calls = box.run("nemotron", "--fetch")
        self.assertEqual(rc, 0, out)
        self.assertIn("with its drafter, is in the cache; nothing else was changed", out)
        pins = box.nemotron_pins
        self.assertEqual(len(runs), 2, runs)
        self.assertIn(f"/opt/qwen38/ckpt-fetch.py {pins['NEMOTRON_BOLT_REPO']} {pins['NEMOTRON_BOLT_REV']} ", runs[0])
        self.assertIn(f"/opt/qwen38/ckpt-fetch.py {pins['NEMOTRON_BOLT_DRAFT_REPO']} {pins['NEMOTRON_BOLT_DRAFT_REV']} ", runs[1])
        self.assertIn(f"--need-gb {pins['NEMOTRON_BOLT_GB']} ", runs[0])
        self.assertIn("--need-gb 1 ", runs[1])
        self.assertEqual(box.nemotron_unit.read_bytes(), unit_before)
        self.assertFalse([c for c in calls if re.search(r"\b(enable|disable|daemon-reload|restart|start|stop)\b", c)], calls)
        self.assertFalse((box.home / ".config/qwen38/text-lane").exists(), "--fetch wrote the text-lane record")

    def test_the_nemotron_super_lane_fetches_its_drafter_before_it_stops(self):
        box = Box()
        unit_before = box.super_unit.read_bytes()
        rc, out, runs, calls = box.run("nemotron-super", "--fetch")
        self.assertEqual(rc, 0, out)
        self.assertIn("with its drafter, is in the cache; nothing else was changed", out)
        pins = box.super_pins
        self.assertEqual(len(runs), 2, runs)
        self.assertIn(f"/opt/qwen38/ckpt-fetch.py {pins['NEMOTRON_SUPER_REPO']} {pins['NEMOTRON_SUPER_REV']} ", runs[0])
        self.assertIn(f"/opt/qwen38/ckpt-fetch.py {pins['NEMOTRON_SUPER_DRAFT_REPO']} {pins['NEMOTRON_SUPER_DRAFT_REV']} ", runs[1])
        self.assertIn(f"--need-gb {pins['NEMOTRON_SUPER_GB']} ", runs[0])
        self.assertIn(" qwen38-nemotron:test ", runs[0], "in the image the two Nemotron lanes run")
        self.assertEqual(box.super_unit.read_bytes(), unit_before)
        self.assertFalse([c for c in calls if re.search(r"\b(enable|disable|daemon-reload|restart|start|stop)\b", c)], calls)

    def test_without_it_the_switch_goes_on_past_the_download(self):
        rc, out, runs, _ = Box().run("stock")
        self.assertEqual(len(runs), 1, out)
        self.assertNotIn("nothing else was changed", out)

    def test_an_unknown_option_is_refused_before_anything_runs(self):
        rc, out, runs, calls = Box().run("stock", "--fetchh")
        self.assertNotEqual(rc, 0)
        self.assertIn("unknown option", out)
        self.assertEqual((runs, calls), ([], []))

    def test_every_lane_stops_right_after_its_own_download(self):
        # the five branches that fetch (Kolibri, Nemotron, image, video, the text lanes): each
        # one's fetch command is followed at once by the --fetch stop, before anything it writes;
        # the Nemotron lane's loop over its model and its drafter ends first, with nothing else in it
        calls = [m.start() for m in re.finditer(r"ckpt-fetch\.py\"? \"\$", SWITCH)]
        self.assertEqual(len(calls), 5)
        for at in calls:
            rest = SWITCH[at:]
            die = rest.index("|| die ")
            after = rest[rest.index("\n", die) + 1:].splitlines()
            while after and re.fullmatch(r"\s*(NEM_ASK=1|done|#.*)", after[0]):
                after.pop(0)
            self.assertTrue(after[0].lstrip().startswith("fetched "), after[:3])


if __name__ == "__main__":
    unittest.main()
