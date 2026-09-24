#!/usr/bin/env python3
"""uninstall.sh's image lines: what it lists, and a reclaim command that really reclaims.

Since v1.18.6 install.sh tags every digest pull (qwen38-pinned:<lane>-<digest>): an image
with no tag is dangling to Docker, and `docker image prune` deleted the serving images of
a box (both were listed dangling on the reference box, 2026-09-23). A tagged image keeps
its digest reference too, so the reclaim command has to name both, or it only untags; and
the prune warning is for an image that really has no tag. These run the uninstaller's own
functions, as written, against a fake docker."""
import pathlib
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
    (t / "docker").write_text(FAKE_DOCKER)
    (t / "docker").chmod(0o755)
    script = ("set -euo pipefail\nFOUND_IMAGES=()\n" + local + "\n" + base + "\n" + head
              + "inventory_images\n" + tail)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{t}:/usr/bin:/bin", "FAKE_TAGGED": "1" if tagged else "0",
                            "FAKE_OLD": "1" if old else "0"})
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


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
        text = (REPO / "install.sh").read_text()
        self.assertIn('tag="qwen38-pinned:$1-', text)
        self.assertIn('pin_tag "$LANE" "$PULLED_IMAGE"', text)
        self.assertIn('then pin_tag 27b "$IMAGE"; else pin_tag flash "$FLASH_IMAGE"; fi', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
