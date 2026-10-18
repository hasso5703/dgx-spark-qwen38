#!/usr/bin/env python3
"""The other text lane's run, installed on demand (QWEN38_SECONDARY + QWEN38_UNITS_ONLY=1), run as
written to its last line. Its guards are tested one by one elsewhere; this runs the whole path,
so a step that trips over what the guards skipped (a variable only a skipped step set, a file
only a skipped step wrote) shows up here, and not as a lane missing from every new box.

The copy ends at a wall just past its last line (installer_wall); the commands that act on the
box are stubs that answer as a box with no lane image would (docker has none, nvidia-smi sees
the GPU), sudo runs what it is given in the test's own folders, and the units are written to a
folder the test owns. The machine is a GB10 whatever runs the suite: uname says aarch64 and the
copy reads the reference box's MemTotal, as the preflight refuses anything else (GitHub's x86_64
runner, 16 GB, stopped it there: found by the CI run of v1.33.0, 2026-10-15)."""
import pathlib
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import installer_wall as wall  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
DONE = '    step "Done: installed as $UNIT_NAME ($MODEL_CHOICE), not enabled at boot, not prepared yet"\n'


def run_units_only(lane):
    units = wall.units_dir()
    script = pathlib.Path(wall.walled(at=DONE, after=True, units=units))
    meminfo = wall._tmp("meminfo-") / "meminfo"
    meminfo.write_text("MemTotal:       127535272 kB\n")      # the reference box's
    text, read = script.read_text(), "awk '/^MemTotal/{print int($2/1048576)}' /proc/meminfo"
    assert text.count(read) == 1, "install.sh reads the memory another way now"
    script.write_text(text.replace(read, read.replace("/proc/meminfo", str(meminfo))))
    for f in REPO.iterdir():      # what install.sh reads beside itself (its templates, its helpers)
        if f.is_file() and not (script.parent / f.name).exists():
            shutil.copy2(f, script.parent / f.name)
    shutil.copytree(REPO / "dashboard", script.parent / "dashboard", dirs_exist_ok=True)
    home = wall._tmp("home-")
    (home / ".config/qwen38").mkdir(parents=True)
    (home / ".config/qwen38/api-key").write_text("k\n")
    stubs, docker_root = wall._tmp("stubs-"), wall._tmp("docker-root-")
    calls = stubs / "calls"
    calls.touch()
    for name, body in (
            ("nvidia-smi", 'case "$*" in *driver_version*) echo 580.173.02 ;; *memory.total*) echo "[N/A]" ;; *) echo "NVIDIA GB10" ;; esac\n'),
            ("uname", 'echo aarch64\n'),
            ("docker", f'case "$1 $2" in "info "*) echo "{docker_root}" ;; "image inspect") exit 1 ;; esac\nexit 0\n'),
            ("sudo", 'while [ -n "${1:-}" ] && [ "${1#-}" != "$1" ]; do shift; done\n'
                     '[ "$#" -eq 0 ] || [ "$1" = true ] && exit 0\n"$@"\n'),
            ("systemctl", 'case "$1" in is-enabled|is-active) exit 1 ;; esac\nexit 0\n'),
            # the preflight has to find it, and a lane's run then asks it nothing (the parent
            # checked the ports): the host's was used, and a host without iproute2 died there,
            # "ss not found" (a cloud container, 2026-10-18)
            ("ss", 'exit 0\n')):
        (stubs / name).write_text(f'#!/bin/bash\necho "{name} $*" >> "{calls}"\n' + body)
        (stubs / name).chmod(0o755)
    env, fenced = wall.fenced_env(home=home, first=str(stubs), QWEN38_SECONDARY=lane, QWEN38_UNITS_ONLY="1",
                                  MODEL_CHOICE="flash" if lane == "flash" else "stock",
                                  HF_CACHE=str(home / "hf"), PORT="30000", PROXY_PORT="30001")
    r = subprocess.run([str(script)], capture_output=True, text=True, env=env, cwd=wall.cwd(), timeout=180)
    return (r.returncode, r.stdout + r.stderr, calls.read_text().splitlines(), wall.reached(fenced),
            pathlib.Path(units), home)


class TheUnitsOnlyRunToItsEnd(unittest.TestCase):
    def check(self, lane, unit):
        rc, out, calls, fenced, units, home = run_units_only(lane)
        self.assertIn(wall.WALL, out, out[-1500:])      # it reached its last line
        self.assertEqual(rc, 97, out[-1500:])
        self.assertEqual(fenced, [])                      # nothing past the stubs
        # no image pulled or run, no checkpoint fetched: docker was only asked what it has
        self.assertEqual(sorted({" ".join(c.split()[:3]) for c in calls if c.startswith("docker ")}),
                         ["docker image inspect", "docker info", "docker info --format"], calls)
        self.assertFalse((home / "hf" / "hub").exists() and any((home / "hf" / "hub").iterdir()))
        # root for the unit and nothing else: not enabled, not started
        sudo = [c for c in calls if c.startswith("sudo ") and c not in ("sudo -n true", "sudo -v")]
        self.assertEqual([c.split()[1] for c in sudo], ["install", "systemctl"], sudo)
        self.assertEqual(sudo[-1], "sudo systemctl daemon-reload")
        self.assertTrue((units / unit).is_file())
        self.assertIn("not prepared yet", out)
        return out, units, home

    def test_the_flash_lane(self):
        out, units, home = self.check("flash", "qwen38-flash.service")
        launcher = (home / ".config/qwen38/launch-flash.sh").read_text()
        self.assertIn('[ -s "', launcher)                  # the vocabulary waited for, not required
        self.assertIn("&& TIER+=(--speculative-token-map /out/token-map-", launcher)

    def test_the_27b_lane(self):
        out, units, home = self.check("27b", "qwen38-sglang.service")
        unit = (units / "qwen38-sglang.service").read_text()
        self.assertIn("--speculative-draft-model-path", unit)
        self.assertIn("checkpoints' configs for the 1m window: set at the lane's first Load", out)


if __name__ == "__main__":
    unittest.main()
