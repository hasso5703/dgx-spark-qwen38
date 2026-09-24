#!/usr/bin/env python3
"""install-image.sh finds the cache it serves from, measures space where things land, and
does not leave a box without an engine when it removes the lane that booted.

Found in review, 2026-09-24:
  - HF_CACHE came from the environment or the default, never from the installed unit
    (Environment=HF_HOME=), so an update re-downloaded 31 GB into ~/.cache and rewrote
    HF_HOME on a box whose lane lived on another disk;
  - free space was measured under $HOME, not where the checkpoint and the runtime go, and
    "cached" meant the folder existed, which huggingface_hub creates before the first byte;
  - --uninstall on a box whose boot lane was the image left no engine enabled at all.
The script's own lines run here with its /etc paths moved to a temporary directory.
"""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install-image.sh").read_text()


def block(start, end, inclusive=True):
    i = TEXT.index(start)
    j = TEXT.index(end, i)
    return TEXT[i:TEXT.index("\n", j) + 1] if inclusive else TEXT[i:j]


def bash(script, env, path_dirs=()):
    path = ":".join([*map(str, path_dirs), "/usr/bin", "/bin"])
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": path, **env})
    return r.returncode, r.stdout + r.stderr


class TheCacheItServesFrom(unittest.TestCase):
    CACHE = block('installed_env(){', 'HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"')

    def run_cache(self, image_unit=None, text_unit=None, env_cache=None):
        d = pathlib.Path(tempfile.mkdtemp(prefix="img-cache-"))
        (d / "image.service").write_text(image_unit or "")
        (d / "text.service").write_text(text_unit or "")
        env = {"HOME": str(d)}
        if env_cache:
            env["HF_CACHE"] = env_cache
        script = (f'set -euo pipefail\nINSTALLED="{d}/image.service"; CONFIG_DIR="{d}"; TEXT_UNITS="{d}/text.service"\n'
                  + self.CACHE + 'echo "CACHE=$HF_CACHE"\n')
        rc, out = bash(script, env)
        return next(ln[6:] for ln in out.splitlines() if ln.startswith("CACHE="))

    def test_the_installed_unit_wins_over_the_default(self):
        self.assertEqual(self.run_cache(image_unit="[Service]\nEnvironment=PYTHONUNBUFFERED=1\n"
                                                   "Environment=HF_HOME=/data/hf\n"), "/data/hf")

    def test_a_first_install_follows_the_text_lane(self):
        self.assertEqual(self.run_cache(text_unit="ExecStart=docker run -v /mnt/hf:/root/.cache/huggingface x\n"),
                         "/mnt/hf")

    def test_the_environment_still_wins(self):
        self.assertEqual(self.run_cache(image_unit="Environment=HF_HOME=/data/hf\n", env_cache="/elsewhere"),
                         "/elsewhere")


class TheSpaceWhereItLands(unittest.TestCase):
    SPACE = block('existing(){ local p="$1"', 'echo "OK: $(uname -m), key present')

    def run_space(self, have_gib, free_w, free_r=None):
        d = pathlib.Path(tempfile.mkdtemp(prefix="img-space-"))
        blobs = d / "hf/hub/models--Qwen--Qwen-Image-2.1/blobs"
        blobs.mkdir(parents=True)
        with open(blobs / "b", "wb") as f:
            f.truncate(int(have_gib * 1024 ** 3))
        stub = d / "bin"
        stub.mkdir()
        two = free_r is not None
        (stub / "df").write_text("#!/bin/sh\ncase \"$*\" in *lane*) echo Avail; echo %sG;; *) echo Avail; echo %sG;; esac\n"
                                 % (free_r if two else free_w, free_w))
        (stub / "stat").write_text("#!/bin/sh\ncase \"$*\" in *lane*) echo %s;; *) echo 1;; esac\n" % (2 if two else 1))
        for f in stub.iterdir():
            f.chmod(0o755)
        (d / "lane").mkdir()
        script = ('set -euo pipefail\ndie(){ echo "DIE: $*"; exit 1; }\n'
                  f'HF_CACHE="{d}/hf"; LANE_DIR="{d}/lane"; MODEL=Qwen/Qwen-Image-2.1; WEIGHTS_GB=31; RUNTIME_GB=11\n'
                  + self.SPACE)
        return bash(script, {"HOME": str(d)}, [stub])

    def test_a_download_that_just_started_needs_the_whole_checkpoint(self):
        rc, out = self.run_space(have_gib=0.000001, free_w=20)
        self.assertEqual(rc, 1, out)
        self.assertIn("DIE:", out)

    def test_a_whole_checkpoint_needs_the_runtime_only(self):
        rc, out = self.run_space(have_gib=30.86, free_w=20)
        self.assertEqual(rc, 0, out)
        self.assertIn("already cached", out)

    def test_each_disk_is_measured_for_its_own_part(self):
        rc, out = self.run_space(have_gib=0.0, free_w=500, free_r=5)
        self.assertEqual(rc, 1, out)
        self.assertIn("runtime", out)


class RemovingTheLaneThatBooted(unittest.TestCase):
    UNINSTALL = block('if [ "$ACTION" = uninstall ]; then', '  exit 0')

    def run_uninstall(self, image_enabled, text_enabled, before="qwen38-sglang.service"):
        d = pathlib.Path(tempfile.mkdtemp(prefix="img-uninst-"))
        units = d / "etc"
        units.mkdir()
        (units / "qwen38-image.service").write_text("[Service]\n")
        (units / "qwen38-sglang.service").write_text("[Service]\n")
        (d / "lane-before-image").write_text(before + "\n")
        stub = d / "bin"
        stub.mkdir()
        enabled = []
        if image_enabled:
            enabled.append("qwen38-image.service")
        if text_enabled:
            enabled.append("qwen38-sglang.service")
        (stub / "systemctl").write_text(
            "#!/bin/sh\ncase \"$1\" in is-enabled) for u in %s; do [ \"$3\" = \"$u\" ] && exit 0; done; exit 1;; esac\n"
            "echo \"systemctl $*\" >> \"%s/calls\"\n" % (" ".join(enabled) or "none", d))
        (stub / "sudo").write_text("#!/bin/sh\n\"$@\"\n")
        for f in stub.iterdir():
            f.chmod(0o755)
        text = self.UNINSTALL.replace("/etc/systemd/system", str(units))
        script = ('set -euo pipefail\nstep(){ :; }\nACTION=uninstall; UNIT=qwen38-image.service\n'
                  f'INSTALLED="{units}/qwen38-image.service"; CONFIG_DIR="{d}"; LANE_DIR="{d}/lane"; '
                  f'VENV="{d}/lane/venv"; SRC="{d}/lane/sglang"; HF_CACHE="{d}/hf"; MODEL=Qwen/Qwen-Image-2.1\n'
                  + text + "\nfi\n")
        rc, out = bash(script, {"HOME": str(d)}, [stub])
        calls = (d / "calls").read_text() if (d / "calls").exists() else ""
        return rc, out, calls

    def test_the_lane_before_images_is_enabled_again(self):
        rc, out, calls = self.run_uninstall(image_enabled=True, text_enabled=False)
        self.assertEqual(rc, 0, out)
        self.assertIn("systemctl enable qwen38-sglang.service", calls)
        self.assertIn("sudo systemctl start qwen38-sglang.service", out)

    def test_a_box_whose_text_lane_is_enabled_is_left_as_it_is(self):
        rc, out, calls = self.run_uninstall(image_enabled=True, text_enabled=True)
        self.assertNotIn("systemctl enable qwen38-sglang.service", calls)

    def test_an_image_lane_that_was_not_the_boot_lane_changes_nothing_else(self):
        rc, out, calls = self.run_uninstall(image_enabled=False, text_enabled=True)
        self.assertNotIn("systemctl enable", calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
