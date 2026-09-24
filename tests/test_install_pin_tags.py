#!/usr/bin/env python3
"""A pin that moves takes its qwen38-pinned tag with it.

install.sh tags each digest it pulls, so that a docker image prune cannot delete a lane's
serving image. The tag of a pin a later release replaced was never removed, and kept the
retired image out of every prune: 30 to 39 GB per bumped pin (found in review,
2026-09-24). This runs install.sh's own function against a stub docker."""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()
FUNC = TEXT[TEXT.index("unpin_stale() {"):TEXT.index("\n}\n", TEXT.index("unpin_stale() {")) + 3]

DOCKER = r'''#!/bin/bash
D="$(dirname "$0")"
case "$1 $2" in
  "image inspect") grep -m1 "^$3 " "$D/refs" | cut -d' ' -f2 || exit 1; grep -q "^$3 " "$D/refs" ;;
  "images --no-trunc") cat "$D/tags" ;;
  "ps -aq") id="${4#ancestor=}"; grep -qx "$id" "$D/used" && echo c0ffee; exit 0 ;;
  "rmi "*) echo "$2" >> "$D/removed" ;;
  *) exit 2 ;;
esac
'''


def run(tags, used=(), current="lmsysorg/sglang@sha256:new"):
    d = pathlib.Path(tempfile.mkdtemp(prefix="pin-tags-"))
    (d / "docker").write_text(DOCKER)
    (d / "docker").chmod(0o755)
    (d / "refs").write_text("lmsysorg/sglang@sha256:new sha256:NEW\n")
    (d / "tags").write_text("".join(f"{t} {i}\n" for t, i in tags))
    (d / "used").write_text("".join(f"{u}\n" for u in used))
    script = f"set -euo pipefail\n{FUNC}\nunpin_stale 27b '{current}' || true\necho END\n"
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{d}:/usr/bin:/bin"})
    removed = (d / "removed").read_text().split() if (d / "removed").exists() else []
    return r.stdout + r.stderr, removed


class RetiredPinsAreUntagged(unittest.TestCase):
    def test_the_install_calls_it_for_both_lanes(self):
        self.assertIn('unpin_stale "$LANE" "$PULLED_IMAGE" || true', TEXT)
        self.assertIn('unpin_stale 27b "$IMAGE" || true', TEXT)
        self.assertIn('unpin_stale flash "$FLASH_IMAGE" || true', TEXT)

    def test_a_retired_pin_of_the_lane_goes(self):
        out, removed = run([("qwen38-pinned:27b-new", "sha256:NEW"), ("qwen38-pinned:27b-old", "sha256:OLD")])
        self.assertEqual(removed, ["qwen38-pinned:27b-old"], out)
        self.assertIn("END", out)

    def test_the_other_lane_and_images_in_use_stay(self):
        _, removed = run([("qwen38-pinned:27b-new", "sha256:NEW"), ("qwen38-pinned:flash-x", "sha256:FL"),
                          ("qwen38-pinned:27b-old", "sha256:OLD")], used=["sha256:OLD"])
        self.assertEqual(removed, [])

    def test_nothing_is_touched_when_the_current_pin_is_absent(self):
        _, removed = run([("qwen38-pinned:27b-old", "sha256:OLD")], current="lmsysorg/sglang@sha256:missing")
        # the stub knows only the new ref, so an absent current pin cannot be inspected
        self.assertEqual(removed, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
