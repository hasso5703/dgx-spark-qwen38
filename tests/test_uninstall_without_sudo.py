#!/usr/bin/env python3
"""uninstall.sh removes what it can without sudo, and finds every cache the lanes use.

Found in review and in a sandbox run, 2026-09-24:
  - it called sudo unconditionally, and `sudo rm -f` had no fallback under set -e, so on a
    box without sudo rights (the one --no-service exists for) the first refusal ended the
    script before it removed any of the user's own files;
  - its inventory and reclaim commands looked at the environment's HF_CACHE and PLE_DIR
    only, so a box installed on another disk had its weights and PLE file neither listed
    nor reclaimable.
A copy of the script runs here with its three system paths moved into a sandbox, the
image lane folder pinned to the sandbox, and sudo, docker and systemctl replaced by stubs
that only record what they were asked (sudo never runs anything).
"""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "uninstall.sh").read_text()


class Box:
    def __init__(self, units=(), sudo_ok=True, mounted_cache=None):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="uninst-"))
        self.etc = self.d / "etc"
        self.etc.mkdir()
        self.home = self.d / "home"
        (self.home / ".config/qwen38").mkdir(parents=True)
        (self.home / ".config/qwen38/api-key").write_text("k\n")
        (self.home / ".local/bin").mkdir(parents=True)
        (self.home / ".local/bin/oc").write_text("#!/bin/bash\n# oc launcher installed by dgx-spark-qwen38\n")
        self.lane = self.d / "lane"
        (self.lane / "venv").mkdir(parents=True)
        for u in units:
            (self.etc / u).write_text("[Service]\n" + (f"ExecStart=docker run -v {mounted_cache}:/root/.cache/huggingface x\n"
                                                        if mounted_cache and u == "qwen38-sglang.service" else ""))
        text = (TEXT.replace("SYSTEMD_DIR=/etc/systemd/system", f"SYSTEMD_DIR={self.etc}")
                    .replace("SUDOERS_FILE=/etc/sudoers.d/qwen38-cockpit", f"SUDOERS_FILE={self.etc}/sudoers")
                    .replace("PYSPY_WRAPPER=/usr/local/bin/qwen38-pyspy-scheduler", f"PYSPY_WRAPPER={self.etc}/pyspy"))
        assert "/etc/" not in text.replace(str(self.etc), "") and "/usr/local" not in text, "a real system path is left"
        self.script = self.d / "uninstall.sh"
        self.script.write_text(text)
        (self.d / "oc-merge-limits.py").write_text((REPO / "oc-merge-limits.py").read_text())
        stub = self.d / "bin"
        stub.mkdir()
        log = self.d / "calls"
        (stub / "sudo").write_text(f"#!/bin/sh\necho \"sudo $*\" >> {log}\nexit {0 if sudo_ok else 1}\n")
        (stub / "docker").write_text(f"#!/bin/sh\necho \"docker $*\" >> {log}\nexit 1\n")
        (stub / "systemctl").write_text(f"#!/bin/sh\necho \"systemctl $*\" >> {log}\nexit 1\n")
        for f in stub.iterdir():
            f.chmod(0o755)
        self.stub, self.log = stub, log

    def run(self, *args):
        r = subprocess.run(["bash", str(self.script), *args], capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL, cwd=str(self.d),
                           env={"PATH": f"{self.stub}:/usr/bin:/bin", "HOME": str(self.home),
                                "IMAGE_LANE_DIR": str(self.lane)})
        calls = self.log.read_text() if self.log.exists() else ""
        return r.returncode, r.stdout + r.stderr, calls


class WithoutSudo(unittest.TestCase):
    def test_a_box_with_no_service_needs_no_sudo_and_is_cleaned(self):
        box = Box(units=(), sudo_ok=False)
        rc, out, calls = box.run("--yes")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("sudo", calls, "sudo was asked for with nothing of root's to remove")
        self.assertFalse((box.home / ".local/bin/oc").exists(), "the launcher was left")
        self.assertFalse((box.home / ".config/qwen38").exists(), "the config was left")

    def test_refused_sudo_leaves_the_running_services_whole(self):
        box = Box(units=("qwen38-sglang.service", "qwen38-image.service"), sudo_ok=False)
        rc, out, calls = box.run("--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("sudo was refused", out)
        self.assertNotIn("docker rm", calls, "a container was removed under a unit that restarts it")
        self.assertTrue((box.etc / "qwen38-sglang.service").exists())
        self.assertTrue((box.lane / "venv").exists(), "the image lane's runtime went while it may run")
        self.assertTrue((box.home / ".config/qwen38/api-key").exists(), "the key the services read went")

    def test_granted_sudo_removes_the_units(self):
        box = Box(units=("qwen38-sglang.service",), sudo_ok=True)
        rc, out, calls = box.run("--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"sudo rm -f {box.etc}/qwen38-sglang.service", calls)
        self.assertIn("services removed.", out)


class EveryCacheTheLanesUse(unittest.TestCase):
    def test_weights_on_a_mounted_cache_are_listed(self):
        cache = pathlib.Path(tempfile.mkdtemp(prefix="uninst-hf-"))
        (cache / "hub/models--RadixArk--Qwen3.8-27B-NVFP4").mkdir(parents=True)
        box = Box(units=("qwen38-sglang.service",), mounted_cache=cache)
        rc, out, _ = box.run("--list")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"weights   {cache}/hub/models--RadixArk--Qwen3.8-27B-NVFP4", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
