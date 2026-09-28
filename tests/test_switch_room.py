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

# the room checks read free space through one function, defined once before every
# branch runs; every replay carries it in front of the block it exercises
_lines = TEXT.splitlines(keepends=True)
_at = next(i for i, ln in enumerate(_lines) if ln.startswith("dl_free_gb(){"))
DLFREE = "".join(_lines[_at:_at + 2])


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
              f'HF_CACHE="{d}/hf"; TARGET_REPO=org/target; TARGET_LANE={lane}\n'
              + DLFREE + BLOCK + "echo ROOM-OK\n")
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


VID_START = TEXT.index('VID_WANT_GB="$(grep')
VID_END = TEXT.index('(nothing was changed). Free some space first, or point HF_HOME at a bigger disk."', VID_START)
VID_BLOCK = TEXT[VID_START:TEXT.index("\n", VID_END) + 1]


def run_video(free_gb, cached_gib=0.0):
    d = pathlib.Path(tempfile.mkdtemp(prefix="sw-room-"))
    blobs = d / "hf/hub/models--MiniMaxAI--MiniMax-H3/blobs"
    blobs.mkdir(parents=True)
    if cached_gib:
        with open(blobs / "b", "wb") as f:
            f.truncate(int(cached_gib * 1024 ** 3))
    (d / "df").write_text(f"#!/bin/sh\necho Avail; echo {free_gb}G\n")
    (d / "df").chmod(0o755)
    script = ('set -euo pipefail\ndie(){ echo "DIE: $*"; exit 1; }\n'
              f'REPO_DIR="{REPO}"; HF_CACHE="{d}/hf"; VID_MODEL=MiniMaxAI/MiniMax-H3; '
              f'VID_MODEL_DIR="{d}/hf/hub/models--MiniMaxAI--MiniMax-H3"\n'
              + DLFREE + VID_BLOCK + "echo ROOM-OK\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{d}:/usr/bin:/bin"})
    return r.stdout + r.stderr


class TheVideoSwitchChecksForRoomToo(unittest.TestCase):
    """145 GB of fl2va weights behind a button, under a serving engine: the video
    branch skipped the room check the text branch had learned (reviews, 2026-09-28).
    The need is read from install-video.sh, so the two files drift together."""

    def test_a_video_switch_without_room_stops_before_anything(self):
        # this box's real numbers of 2026-09-28: 90 GB free, empty cache, 145 needed
        out = run_video(free_gb=90)
        self.assertIn("DIE: not enough room", out)
        self.assertNotIn("ROOM-OK", out)

    def test_the_complete_cache_of_a_real_box_answers_without_a_die(self):
        self.assertIn("ROOM-OK", run_video(free_gb=90, cached_gib=135))

    def test_a_video_switch_with_room_goes_on(self):
        self.assertIn("ROOM-OK", run_video(free_gb=200))


class TheCockpitGivesItTheTime(unittest.TestCase):
    def test_the_switch_job_outlives_a_flash_download(self):
        cockpit = (REPO / "dashboard/cockpit.py").read_text()
        block = cockpit[cockpit.index('"switch": {'):]
        timeout = int(re.search(r'"timeout":\s*(\d+)', block).group(1))
        self.assertGreaterEqual(timeout, 2 * 3600, "124 GB at 89 MB/s is 23 min, at 20 MB/s 1 h 45")


if __name__ == "__main__":
    unittest.main(verbosity=2)
