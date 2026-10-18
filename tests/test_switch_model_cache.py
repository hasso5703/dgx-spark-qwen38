#!/usr/bin/env python3
"""A switch downloads into the cache the installed engine mounts.

switch-model.sh took HF_CACHE from the environment or the default, never from the unit.
On a box installed with HF_CACHE=/data/hf, the cockpit's Switch (which passes no
HF_CACHE) downloaded, YaRN-patched and templated into ~/.cache/huggingface, rewrote the
unit to the new model, and left it mounting /data/hf: the offline engine found no
checkpoint at its next start and looped under Restart=always, after the script had said
"Switch queued" (found in review, 2026-09-24). install.sh already reads the mount back.

The script runs as written, on a copy whose unit paths point at this test's files, with a
fake docker that records the download container's argv and then fails, so nothing past
the download is reached and nothing on the host is touched.
"""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]

FAKE_DOCKER = """#!/bin/sh
case "$1" in
  image) exit 0 ;;                       # the serving image is present
  run) printf '%s\\n' "$*" >> "$DOCKER_LOG"; exit 1 ;;   # the download "fails": stop here
esac
exit 0
"""


class Box:
    def __init__(self, mount):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="switch-cache-"))
        self.home = self.t / "home"
        (self.home / ".config/qwen38").mkdir(parents=True)
        units = self.t / "units"
        units.mkdir()
        (units / "qwen38-sglang.service").write_text(
            "[Service]\nExecStart=/bin/bash -c 'exec docker run --rm --name qwen38-sglang \\\n"
            f"  -v {mount}:/root/.cache/huggingface \\\n"
            "  lmsysorg/sglang@sha256:" + "a" * 64 + " \\\n"
            "  python3 -m sglang.launch_server --model-path RadixArk/Qwen3.8-27B-NVFP4 \\\n"
            "  --host 127.0.0.1 --port 30000'\n")
        repo = self.t / "repo"
        repo.mkdir()
        (repo / "install.sh").write_text((REPO / "install.sh").read_text())
        text = (REPO / "switch-model.sh").read_text()
        for unit in ("qwen38-sglang.service", "qwen38-flash.service"):
            old = f'"/etc/systemd/system/{unit}"'
            assert old in text, f"switch-model.sh no longer names {unit} that way"
            text = text.replace(old, f'"{units}/{unit}"')
        (repo / "switch-model.sh").write_text(text)
        (repo / "lanes.sh").write_text((REPO / "lanes.sh").read_text())   # sourced by the scripts since v1.30
        (repo / "ckpt-fetch.py").write_text((REPO / "ckpt-fetch.py").read_text())   # the fetch, since v1.31
        (repo / "switch-model.sh").chmod(0o755)
        self.bin = self.t / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "systemctl").write_text("#!/bin/sh\nexit 1\n")     # nothing enabled
        # a disk with room for the checkpoint, whatever this machine's has: the real df
        # answered here, and a 29 GB disk got the 45 GB refusal (found in a cloud review,
        # 2026-10-18); the room check has its own tests, in tests/test_switch_room.py
        (self.bin / "df").write_text("#!/bin/sh\necho Avail; echo 500G\n")
        for n in ("docker", "systemctl", "df"):
            (self.bin / n).chmod(0o755)
        self.log = self.t / "docker.log"
        self.script = repo / "switch-model.sh"

    def switch(self, target, **env):
        e = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "DOCKER_LOG": str(self.log)}
        e.update(env)
        r = subprocess.run([str(self.script), target], capture_output=True, text=True, env=e, timeout=60)
        runs = self.log.read_text().splitlines() if self.log.exists() else []
        return r.returncode, r.stdout + r.stderr, runs


class TheSwitchFollowsTheInstalledCache(unittest.TestCase):
    def test_the_download_goes_where_the_unit_mounts(self):
        box = Box("/data/hf")
        rc, out, runs = box.switch("uncensored")
        self.assertNotEqual(rc, 0, "the fake download fails, so the script stops there")
        self.assertEqual(len(runs), 1, out)
        self.assertIn("-v /data/hf:/hf", runs[0])
        self.assertNotIn(".cache/huggingface:/hf", runs[0])

    def test_the_cache_is_asked_before_the_hub(self):
        # as the image, video and Kolibri switches do: a cockpit Load of a text lane whose
        # checkpoint the cache holds whole needs no network (v1.31; it asked the Hub every time)
        rc, out, runs = Box("/data/hf").switch("stock")
        self.assertEqual(len(runs), 1, out)
        self.assertIn("/opt/qwen38/ckpt-fetch.py RadixArk/Qwen3.8-27B-NVFP4 ", runs[0])
        self.assertIn(" --offline-first --refs-main fill", runs[0])

    def test_an_explicit_hf_cache_still_wins(self):
        box = Box("/data/hf")
        _, out, runs = box.switch("uncensored", HF_CACHE="/elsewhere/hf")
        self.assertEqual(len(runs), 1, out)
        self.assertIn("-v /elsewhere/hf:/hf", runs[0])

    def test_the_default_box_is_unchanged(self):
        box = Box(None)
        default = str(box.home / ".cache/huggingface")
        unit = box.t / "units/qwen38-sglang.service"
        unit.write_text(unit.read_text().replace("None", default))
        _, out, runs = box.switch("uncensored")
        self.assertEqual(len(runs), 1, out)
        self.assertIn(f"-v {default}:/hf", runs[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
