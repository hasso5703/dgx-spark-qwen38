#!/usr/bin/env python3
"""uninstall.sh's image lines: what it lists, and a reclaim command that really reclaims.

Since v1.18.6 install.sh tags every digest pull (qwen38-pinned:<lane>-<digest>): an image
with no tag is dangling to Docker, and `docker image prune` deleted the serving images of
a box (both were listed dangling on the reference box, 2026-09-23). A tagged image keeps
its digest reference too, so the reclaim command has to name both, or it only untags; and
the prune warning is for an image that really has no tag. These run the uninstaller's own
functions, as written, against a fake docker."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
DIGEST = "lmsysorg/sglang@sha256:d6e7288627be8b02be88e4bba38e73f6d50e2826869f753c13a4c4385ab3eda9"
TAG = "qwen38-pinned:27b-d6e7288627be"
# a pin this repo moved on from: its digest is in no list of the uninstaller, only its tag
OLD_DIGEST = "lmsysorg/sglang@sha256:" + "0" * 64
OLD_TAG = "qwen38-pinned:27b-000000000000"

FAKE_DOCKER = f"""#!/usr/bin/env python3
import os, sys
a = sys.argv[1:]
tagged = os.environ["FAKE_TAGGED"] == "1"
if a[:1] == ["info"]:
    sys.exit(0)
if a[:1] == ["images"]:
    if tagged and a[-1] == "qwen38-pinned":
        print("{TAG}|4183465428fb|33.4GB")
        if os.environ.get("FAKE_OLD") == "1":
            print("{OLD_TAG}|0ld0ld0ld0ld|30.1GB")
    sys.exit(0)
if a[:2] == ["image", "inspect"]:
    ref = a[2] if not a[2].startswith("-") else a[-1]
    if ref == "{OLD_TAG}" and os.environ.get("FAKE_OLD") == "1":
        fmt = a[a.index("--format") + 1] if "--format" in a else ""
        print(1 if "len .RepoTags" in fmt else "{OLD_TAG} {OLD_DIGEST} ")
        sys.exit(0)
    known = {{"{DIGEST}"}} | ({{"{TAG}"}} if tagged else set())
    if ref not in known:
        sys.exit(1)
    fmt = a[a.index("--format") + 1] if "--format" in a else ""
    if ".Id" in fmt:
        print("sha256:4183465428fb" + "0" * 52)
    elif ".Size" in fmt:
        print(33395401598)
    elif "len .RepoTags" in fmt:
        print(1 if tagged else 0)
    elif "RepoTags" in fmt:
        print(("{TAG} " if tagged else "") + "{DIGEST} ")
    sys.exit(0)
sys.exit(1)
"""


def run(tagged, old=False):
    text = (REPO / "uninstall.sh").read_text()
    head = text[text.index("image_refs() {"):text.index("\n}\n", text.index("inventory_images() {")) + 3]
    base = next(ln for ln in text.splitlines() if ln.startswith("BASE_IMAGES="))
    local = next(ln for ln in text.splitlines() if ln.startswith("LOCAL_IMAGE_REPOS="))
    tail_start = text.index('echo "To also reclaim disk space')
    # up to the weights, which are read per cache since v1.18.7 (a loop over the caches)
    tail = text[tail_start:text.index("while IFS= read -r cache; do", tail_start)]
    t = pathlib.Path(tempfile.mkdtemp(prefix="un-img-"))
    try:
        (t / "docker").write_text(FAKE_DOCKER)
        (t / "docker").chmod(0o755)
        script = ("set -euo pipefail\nFOUND_IMAGES=()\n" + local + "\n" + base + "\n" + head
                  + "inventory_images\n" + tail)
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": f"{t}:/usr/bin:/bin", "FAKE_TAGGED": "1" if tagged else "0",
                                "FAKE_OLD": "1" if old else "0"})
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout
    finally:
        # each run left its folder in /tmp: 50 after a day of CI on the reference box (2026-10-12)
        shutil.rmtree(t, ignore_errors=True)


class TheReclaimCommandReallyReclaims(unittest.TestCase):
    def test_a_tagged_image_is_removed_by_all_its_references(self):
        out = run(tagged=True)
        self.assertIn(f"docker rmi '{TAG}' '{DIGEST}'    # 33.4GB", out)
        self.assertEqual(out.count("docker rmi"), 1, "one image, one command")
        self.assertNotIn("never 'docker image prune'", out, "a tagged image is not dangling")

    def test_an_untagged_image_from_an_older_install_still_warns(self):
        out = run(tagged=False)
        self.assertIn(f"docker rmi '{DIGEST}'    # 33.4GB", out)
        self.assertIn("never 'docker image prune'", out)

    def test_an_image_of_an_older_pin_is_found_by_its_tag(self):
        out = run(tagged=True, old=True)
        self.assertIn(f"docker rmi '{OLD_TAG}' '{OLD_DIGEST}'    # 30.1GB", out)

    def test_the_installer_tags_what_it_pulls_by_digest(self):
        # pin_tag is in lanes.sh, shared with prepare-lane.sh (2026-10-15); install.sh calls it
        self.assertIn('tag="qwen38-pinned:$1-', (REPO / "lanes.sh").read_text())
        text = (REPO / "install.sh").read_text()
        self.assertIn('pin_tag "$PIN_KEY" "$PULLED_IMAGE"', text)
        self.assertIn('pin_tag 27b "$IMAGE"', text)


# Each key's current pin and a stale one: the 27B lane, the SGLang flash config and the
# FP4-Local config. "flash-" is a prefix of "flash-local-", so a prefix match let one flash
# config's install untag the other's image (found in review, 2026-10-20).
PINS = {"27b": ("a" * 12, "b" * 12), "flash": ("c" * 12, "d" * 12), "flash-local": ("e" * 12, "f" * 12)}
PIN_DOCKER = """#!/usr/bin/env python3
import os, sys
a = sys.argv[1:]
pins = {}
for key, (cur, old) in %r.items():
    pins[f"qwen38-pinned:{key}-{cur}"] = "id-" + cur
    pins[f"qwen38-pinned:{key}-{old}"] = "id-" + old
    pins[f"repo/{key}@sha256:{cur}" + "0" * 52] = "id-" + cur
if a[:1] == ["images"]:
    for tag, i in pins.items():
        if tag.startswith("qwen38-pinned:"):
            print(tag, i)
elif a[:2] == ["image", "inspect"]:
    if a[2] not in pins:
        sys.exit(1)
    print(pins[a[2]])
elif a[:1] == ["rmi"]:
    with open(os.environ["RMI_LOG"], "a") as f:
        f.write(a[1] + "\\n")
""" % PINS


def image(key):
    return f"repo/{key}@sha256:{PINS[key][0]}" + "0" * 52


def unpinned(lane, fp4):
    """The tags install.sh's own pin block untags, run against a stub docker."""
    text = (REPO / "install.sh").read_text()
    block = text[text.index("unpin_stale() {"):text.index('\nif [ -n "$ATS_CANNOT" ]', text.index("unpin_stale() {"))]
    t = pathlib.Path(tempfile.mkdtemp(prefix="pin-key-"))
    try:
        (t / "docker").write_text(PIN_DOCKER)
        (t / "docker").chmod(0o755)
        log = t / "rmi.log"
        log.write_text("")
        pulled = image("27b") if lane == "27b" else image("flash-local" if fp4 else "flash")
        script = ("set -euo pipefail\npin_tag(){ :; }\n"
                  f"LANE={lane}; FLASH_FP4_LOCAL={1 if fp4 else 0}; PULLED_IMAGE='{pulled}'\n"
                  f"IMAGE='{image('27b')}'; FLASH_IMAGE='{image('flash')}'; FLASH_LOCAL_IMAGE='{image('flash-local')}'\n"
                  + block + "\n")
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": f"{t}:/usr/bin:/bin", "RMI_LOG": str(log)})
        assert r.returncode == 0, r.stdout + r.stderr
        return set(log.read_text().split())
    finally:
        shutil.rmtree(t, ignore_errors=True)


def stale(*keys):
    return {f"qwen38-pinned:{k}-{PINS[k][1]}" for k in keys}


class EachInstallUntagsOnlyItsOwnStalePins(unittest.TestCase):
    def test_a_27b_install_leaves_the_fp4_local_pins(self):
        self.assertEqual(unpinned("27b", False), stale("27b", "flash"))

    def test_an_sglang_flash_install_leaves_the_fp4_local_pins(self):
        self.assertEqual(unpinned("flash", False), stale("27b", "flash"))

    def test_an_fp4_local_install_leaves_the_sglang_flash_pins(self):
        self.assertEqual(unpinned("flash", True), stale("27b", "flash-local"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
