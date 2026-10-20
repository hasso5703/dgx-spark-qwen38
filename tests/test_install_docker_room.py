#!/usr/bin/env python3
"""The docker room install.sh asks for depends on whether it has anything to pull.

The preflight asked for 40 GB free on the docker root whatever was there, so an update
on a box with the pinned image already in place and 36 GB free was refused, with
nothing to download (reference box, 2026-09-23). These run the installer's own lines,
as written, against a fake docker and a fake df."""
import math
import pathlib
import re
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


LOCAL_IMAGE = "eugr/spark-vllm-b12x@sha256:local"


def run(lane, free, images, fp4=0):
    t = pathlib.Path(tempfile.mkdtemp(prefix="docker-room-"))
    for name, body in (("docker", FAKE_DOCKER), ("df", FAKE_DF)):
        (t / name).write_text(body)
        (t / name).chmod(0o755)
    need, label = ("35", "30 GB Docker image") if lane == "flash" else ("40", "39 GB Docker image")
    script = ("set -euo pipefail\ndie(){ echo \"DIE: $*\"; exit 1; }\n"
              f"LANE={lane}; DOCKER_NEED_GB={need}; IMG_LABEL='{label}'; UNITS_ONLY=0\n"   # a whole install
              "IMAGE=lmsysorg/sglang@sha256:27b; FLASH_IMAGE=lmsysorg/sglang@sha256:flash\n"
              f"FLASH_FP4_LOCAL={fp4}; FLASH_LOCAL_IMAGE={LOCAL_IMAGE}\n"
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

    def test_the_fp4local_config_pulls_the_b12x_image_not_sglangs(self):
        # the lane's other engine: its pull target is the b12x image, and neither
        # serving image already being here excuses the room for it
        self.assertIn(f"PASS need=5 pull={LOCAL_IMAGE}",
                      run("flash", 20, [LOCAL_IMAGE], fp4=1))
        self.assertIn("DIE: Need ~35 GB free",
                      run("flash", 20, ["lmsysorg/sglang@sha256:flash"], fp4=1))



class TheRoomCoversAPullsPeak(unittest.TestCase):
    """What install.sh and prepare-lane.sh ask before a pull covers its peak in either of Docker's
    image stores (the containerd one, Docker 29's default on a new install, keeps the downloads
    beside the unpacked image), in GiB as df -BG rounds them up. They asked for 40 and 35, the
    image alone, and Kolibri's installer 23 (found in review, 2026-10-15)."""
    # (GB unpacked, GB to download) of each image, from the registry and the reference box
    PEAK = {"27b": (33.7, 14.8), "flash": (34.4, 15.3), "flash-fp4local": (18.19, 9.70),
            "kolibri": (22.1, 9.6), "nemotron": (24.46, 10.30)}

    @staticmethod
    def need(unpacked, download):
        return math.ceil((unpacked + download) * 1e9 / 2 ** 30 * 1.02) + 1

    def test_each_lane_asks_for_its_peak_and_the_two_scripts_agree(self):
        text = INSTALL.read_text()
        n27 = int(re.search(r'^NEED_GB=45; DOCKER_NEED_GB=(\d+);', text, re.M).group(1))
        nfl = int(re.search(r'^\s+NEED_GB=180; DOCKER_NEED_GB=(\d+);', text, re.M).group(1))
        nlo = int(re.search(r'^\s+NEED_GB=106; DOCKER_NEED_GB=(\d+);', text, re.M).group(1))
        prep = (REPO / "prepare-lane.sh").read_text()
        p27 = int(re.search(r'pull_image "\$img" 27b (\d+)', prep).group(1))
        pfl = int(re.search(r'pull_image "\$img" flash (\d+)', prep).group(1))
        plo = int(re.search(r'pull_image "\$FLASH_LOCAL_SERVE_IMAGE" flash-local (\d+)', prep).group(1))
        self.assertGreaterEqual(n27, self.need(*self.PEAK["27b"]))
        self.assertGreaterEqual(nfl, self.need(*self.PEAK["flash"]))
        self.assertGreaterEqual(nlo, self.need(*self.PEAK["flash-fp4local"]))
        self.assertEqual((p27, pfl, plo), (n27, nfl, nlo))   # one number each lane, both scripts
        for lane in ("kolibri", "nemotron"):
            with self.subTest(lane=lane):
                inst = (REPO / f"install-{lane}.sh").read_text()
                self.assertGreaterEqual(int(re.search(r'^IMAGE_GB=(\d+)', inst, re.M).group(1)), self.need(*self.PEAK[lane]))


class TheCheckpointsRoomIsInOnePlace(unittest.TestCase):
    """The room a text lane's checkpoint asks, with its caches: install.sh's NEED_GB, the switch's
    and a first Load's (lanes.sh ckpt_room_gb), one number each lane."""

    def test_install_the_switch_and_a_first_load_ask_the_same(self):
        text = INSTALL.read_text()
        n27 = int(re.search(r"^NEED_GB=(\d+); DOCKER_NEED_GB=", text, re.M).group(1))
        nfl = int(re.search(r"^\s+NEED_GB=(\d+); DOCKER_NEED_GB=49;", text, re.M).group(1))
        nlo = int(re.search(r"^\s+NEED_GB=(\d+); DOCKER_NEED_GB=28;", text, re.M).group(1))
        lanes = (REPO / "lanes.sh").read_text()
        fn = lanes[lanes.index("ckpt_room_gb(){"):]
        self.assertIn(f"local n={n27} have dir=", fn)
        self.assertIn(f'[ "$1" = flash ] && case "$3" in *FP4-Local*) n={nlo} ;; *) n={nfl} ;; esac', fn)
        self.assertIn('DL_NEED_GB="$(ckpt_room_gb "$TARGET_LANE" "$HF_CACHE" "$TARGET_REPO" "$TARGET_REV")"', (REPO / "switch-model.sh").read_text())
        self.assertIn("ckpt_room_gb flash", (REPO / "prepare-lane.sh").read_text())
        self.assertIn("ckpt_room_gb 27b", (REPO / "prepare-lane.sh").read_text())


    def test_what_it_answers(self):
        # 10 for a target whole in the cache, else the lane's number less what its blobs hold, 10
        # at least (a sparse file: its apparent size is what du counts)
        d = pathlib.Path(tempfile.mkdtemp(prefix="ckpt-room-"))
        repo = d / "hub" / "models--org--m"
        (repo / "blobs").mkdir(parents=True)

        def ask(lane):
            r = subprocess.run(["bash", "-c", f'set -euo pipefail\n. "{REPO}/lanes.sh"\nckpt_room_gb {lane} "{d}" org/m abc123'],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            return int(r.stdout)
        self.assertEqual((ask("flash"), ask("27b")), (180, 45))
        with open(repo / "blobs" / "x.incomplete", "wb") as f:
            f.truncate(100 * 1024 ** 3)
        self.assertEqual((ask("flash"), ask("27b")), (80, 10))
        with open(repo / "blobs" / "x.incomplete", "wb") as f:
            f.truncate(200 * 1024 ** 3)
        self.assertEqual(ask("flash"), 10)
        (repo / "blobs" / "x.incomplete").unlink()
        (repo / "snapshots" / "abc123").mkdir(parents=True)
        (repo / "snapshots" / "abc123" / "model.safetensors").write_text("w")
        self.assertEqual((ask("flash"), ask("27b")), (10, 10))


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
        # in lanes.sh since prepare-lane.sh tags the image a lane's first Load pulls (2026-10-15)
        text = (REPO / "lanes.sh").read_text()
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

    def test_install_sh_has_it_before_it_tags(self):
        text = INSTALL.read_text()
        self.assertLess(text.index('. "$REPO_DIR/lanes.sh"'), text.index('pin_tag "$PIN_KEY" "$PULLED_IMAGE"'))
        self.assertNotIn("pin_tag() {", text)       # one definition, in lanes.sh

if __name__ == "__main__":
    unittest.main(verbosity=2)
