#!/usr/bin/env python3
"""A switch checks for room before it downloads, and the cockpit gives it the time to.

switch-model.sh can download a whole checkpoint (22 to 126 GB), and a cockpit button can
start it: it checked no free space, blamed HF_TOKEN for any failed download, and the
cockpit's switch job timed out at 30 min, when a flash download alone takes about 23 at
the reference box's 89 MB/s (found in review, 2026-09-24). The switch's own lines run
here against a fake cache and a df stub.
"""
import pathlib
import re
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "switch-model.sh").read_text()
START = TEXT.index('DL_REPO_CACHE="$HF_CACHE/hub/models--${TARGET_REPO//\\//--}"')
BLOCK = TEXT[START:TEXT.index("DL_TOKEN_ARGS=()", START)]


def run(lane, free_gb, cached_gib=0.0):
    d = pathlib.Path(tempfile.mkdtemp(prefix="sw-room-"))
    blobs = d / "hf/hub/models--org--target/blobs"
    blobs.mkdir(parents=True)
    if cached_gib:
        with open(blobs / "b", "wb") as f:
            f.truncate(int(cached_gib * 1024 ** 3))
    (d / "df").write_text(f"#!/bin/sh\necho Avail; echo {free_gb}G\n")
    (d / "df").chmod(0o755)
    script = ('set -euo pipefail\ndie(){ echo "DIE: $*"; exit 1; }\n'
              f'HF_CACHE="{d}/hf"; TARGET_REPO=org/target; TARGET_LANE={lane}\n' + BLOCK + "echo ROOM-OK\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{d}:/usr/bin:/bin"})
    return r.stdout + r.stderr


class ASwitchChecksForRoom(unittest.TestCase):
    def test_a_flash_switch_without_room_stops_before_anything(self):
        out = run("flash", free_gb=50)
        self.assertIn("DIE: not enough room", out)
        self.assertNotIn("ROOM-OK", out)

    def test_what_is_cached_comes_off_the_need(self):
        self.assertIn("ROOM-OK", run("flash", free_gb=60, cached_gib=126))

    def test_a_27b_switch_with_room_goes_on(self):
        self.assertIn("ROOM-OK", run("27b", free_gb=100))


class TheCockpitGivesItTheTime(unittest.TestCase):
    def test_the_switch_job_outlives_a_flash_download(self):
        cockpit = (REPO / "dashboard/cockpit.py").read_text()
        block = cockpit[cockpit.index('"switch": {'):]
        timeout = int(re.search(r'"timeout":\s*(\d+)', block).group(1))
        self.assertGreaterEqual(timeout, 2 * 3600, "124 GB at 89 MB/s is 23 min, at 20 MB/s 1 h 45")


if __name__ == "__main__":
    unittest.main(verbosity=2)
