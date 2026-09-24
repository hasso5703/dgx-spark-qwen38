#!/usr/bin/env python3
"""The docker room install.sh asks for depends on whether it has anything to pull.

The preflight asked for 40 GB free on the docker root whatever was there, so an update
on a box with the pinned image already in place and 36 GB free was refused, with
nothing to download (reference box, 2026-09-23). These run the installer's own lines,
as written, against a fake docker and a fake df."""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
INSTALL = REPO / "install.sh"

FAKE_DOCKER = """#!/bin/sh
case "$1 $2" in
  "info --format") echo /var/lib/docker ;;
  "image inspect") for i in $FAKE_IMAGES; do [ "$i" = "$3" ] && exit 0; done; exit 1 ;;
  *) exit 1 ;;
esac
"""
FAKE_DF = """#!/bin/sh
printf 'Disp.\\n %sG\\n' "$FAKE_FREE"
"""


def block() -> str:
    text = INSTALL.read_text()
    start = text.index("DOCKER_ROOT=$(docker info")
    end = text.index("\n", text.index('|| die "Need ~${DOCKER_NEED_GB} GB free on $DOCKER_ROOT', start)) + 1
    return text[start:end]


def run(lane, free, images):
    t = pathlib.Path(tempfile.mkdtemp(prefix="docker-room-"))
    for name, body in (("docker", FAKE_DOCKER), ("df", FAKE_DF)):
        (t / name).write_text(body)
        (t / name).chmod(0o755)
    need, label = ("35", "30 GB Docker image") if lane == "flash" else ("40", "39 GB Docker image")
    script = ("set -euo pipefail\ndie(){ echo \"DIE: $*\"; exit 1; }\n"
              f"LANE={lane}; DOCKER_NEED_GB={need}; IMG_LABEL='{label}'\n"
              "IMAGE=lmsysorg/sglang@sha256:27b; FLASH_IMAGE=lmsysorg/sglang@sha256:flash\n"
              + block() + 'echo "PASS need=$DOCKER_NEED_GB pull=$PULL_TARGET"\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{t}:/usr/bin:/bin", "FAKE_FREE": str(free),
                            "FAKE_IMAGES": " ".join(images)})
    return r.stdout + r.stderr


class TheDockerRoomFollowsWhatThereIsToPull(unittest.TestCase):
    def test_an_update_with_the_image_in_place_is_not_refused_for_its_size(self):
        out = run("27b", 36, ["lmsysorg/sglang@sha256:27b"])
        self.assertIn("PASS need=5 pull=lmsysorg/sglang@sha256:27b", out)

    def test_a_first_pull_still_needs_the_room_for_the_image(self):
        out = run("27b", 36, [])
        self.assertIn("DIE: Need ~40 GB free on /var/lib/docker for the 39 GB Docker image; found 36 GB", out)

    def test_even_an_image_in_place_needs_the_containers_room(self):
        out = run("27b", 3, ["lmsysorg/sglang@sha256:27b"])
        self.assertIn("DIE: Need ~5 GB free on /var/lib/docker for the container (its image is already here)", out)

    def test_the_flash_lane_looks_for_its_own_image(self):
        self.assertIn("PASS need=5 pull=lmsysorg/sglang@sha256:flash",
                      run("flash", 20, ["lmsysorg/sglang@sha256:flash"]))
        # the 27B's image being here says nothing about the flash lane's
        self.assertIn("DIE: Need ~35 GB free", run("flash", 20, ["lmsysorg/sglang@sha256:27b"]))



PIN_DOCKER = """#!/bin/sh
# image inspect REF [--format {{.Id}}] | tag SRC DST ; state in $FAKE_DIR
if [ "$1 $2" = "image inspect" ]; then
  f="$FAKE_DIR/$(printf '%s' "$3" | tr '/:@' '___')"
  [ -f "$f" ] || exit 1
  cat "$f"; exit 0
fi
if [ "$1" = tag ]; then
  echo "tag $2 $3" >> "$FAKE_DIR/calls"
  cp "$FAKE_DIR/$(printf '%s' "$2" | tr '/:@' '___')" "$FAKE_DIR/$(printf '%s' "$3" | tr '/:@' '___')"; exit 0
fi
exit 1
"""


class ThePinnedImagesAreTagged(unittest.TestCase):
    """An image pulled by digest has no tag, and `docker image prune` deletes untagged
    images: both serving images of the reference box were in that set (2026-09-23)."""

    def run_pin(self, present, tagged=False, ref="lmsysorg/sglang@sha256:" + "d6e7288627be" + "0" * 52):
        text = INSTALL.read_text()
        start = text.index("pin_tag() {")
        fn = text[start:text.index("\n}\n", start) + 3]
        t = pathlib.Path(tempfile.mkdtemp(prefix="pin-"))
        (t / "docker").write_text(PIN_DOCKER)
        (t / "docker").chmod(0o755)
        if present:
            (t / ref.replace("/", "_").replace(":", "_").replace("@", "_")).write_text("sha256:img\n")
        if tagged:
            (t / "qwen38-pinned_27b-d6e7288627be").write_text("sha256:img\n")
        r = subprocess.run(["bash", "-c", "set -euo pipefail\n" + fn + f'pin_tag 27b "{ref}"\n'],
                           capture_output=True, text=True, timeout=30,
                           env={"PATH": f"{t}:/usr/bin:/bin", "FAKE_DIR": str(t)})
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = (t / "calls").read_text() if (t / "calls").exists() else ""
        return r.stdout, calls

    def test_a_digest_pull_gets_its_tag(self):
        out, calls = self.run_pin(present=True)
        self.assertIn("tagged qwen38-pinned:27b-d6e7288627be", out)
        self.assertIn("tag lmsysorg/sglang@sha256:d6e7288627be", calls)

    def test_an_image_already_tagged_is_left_alone_and_quiet(self):
        out, calls = self.run_pin(present=True, tagged=True)
        self.assertEqual((out, calls), ("", ""))

    def test_an_image_that_is_not_here_is_not_tagged(self):
        self.assertEqual(self.run_pin(present=False), ("", ""))

    def test_a_tag_reference_needs_no_tag(self):
        self.assertEqual(self.run_pin(present=True, ref="lmsysorg/sglang:v0.5.19"), ("", ""))

if __name__ == "__main__":
    unittest.main(verbosity=2)
