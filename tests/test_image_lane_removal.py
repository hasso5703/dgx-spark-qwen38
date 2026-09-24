#!/usr/bin/env python3
"""Removing the image lane removes the image lane, and nothing it did not put there.

IMAGE_LANE_DIR is the operator's to choose (the lane is 7 GB of venv and checkout, and a
bigger disk is a reasonable home for it), and install-image.sh only ever writes venv/ and
sglang/ into it. Both uninstall paths ran `rm -rf` on the whole directory, so a lane put
at IMAGE_LANE_DIR=/mnt/data took everything else in /mnt/data with it (reproduced in a
sandbox: a thesis in a sibling folder was gone; found in review, 2026-09-24).

These run the two scripts' own removal lines, as written, on a directory this test fills.
"""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
UNINSTALL = (REPO / "uninstall.sh").read_text()
INSTALL_IMAGE = (REPO / "install-image.sh").read_text()


def removal(text, start):
    """The block that removes the runtime: from `start` to the `fi` that closes it, at the
    same indentation (install-image.sh's sits inside the --uninstall branch)."""
    i = text.index(start)
    indent = text[text.rindex("\n", 0, i) + 1:i]
    end = text.index("\n" + indent + "fi\n", i) + len(indent) + 4
    return "\n".join(line[len(indent):] for line in text[i - len(indent):end].splitlines()) + "\n"


def lane_dir(extra=True):
    d = pathlib.Path(tempfile.mkdtemp(prefix="lane-")) / "data"
    for sub in ("venv/bin", "sglang/python"):
        (d / sub).mkdir(parents=True)
        (d / sub / "f").write_text("x")
    if extra:
        (d / "my-other-project").mkdir()
        (d / "my-other-project/thesis.tex").write_text("years of work")
    return d


def run(snippet, d, **names):
    env = "".join(f'{k}="{v}"\n' for k, v in names.items())
    r = subprocess.run(["bash", "-c", "set -euo pipefail\n" + env + snippet], capture_output=True,
                       text=True, timeout=30)
    return r.returncode, r.stdout + r.stderr


class BothPathsRemoveOnlyTheLane(unittest.TestCase):
    CASES = (
        ("uninstall.sh", lambda: removal(UNINSTALL, 'if [ -d "$IMAGE_LANE_DIR" ]; then'),
         lambda d: {"IMAGE_LANE_DIR": d}),
        ("install-image.sh --uninstall", lambda: removal(INSTALL_IMAGE, 'if [ -d "$LANE_DIR" ]; then'),
         lambda d: {"LANE_DIR": d, "VENV": f"{d}/venv", "SRC": f"{d}/sglang"}),
    )

    def test_a_shared_directory_keeps_everything_else(self):
        for name, snippet, names in self.CASES:
            with self.subTest(path=name):
                d = lane_dir(extra=True)
                rc, out = run(snippet(), d, **names(d))
                self.assertEqual(rc, 0, out)
                self.assertFalse((d / "venv").exists())
                self.assertFalse((d / "sglang").exists())
                self.assertEqual((d / "my-other-project/thesis.tex").read_text(), "years of work")
                self.assertIn("did not put there", out)

    def test_a_directory_of_its_own_goes_entirely(self):
        for name, snippet, names in self.CASES:
            with self.subTest(path=name):
                d = lane_dir(extra=False)
                rc, out = run(snippet(), d, **names(d))
                self.assertEqual(rc, 0, out)
                self.assertFalse(d.exists())

    def test_no_whole_directory_removal_is_left(self):
        self.assertNotIn('rm -rf "$IMAGE_LANE_DIR"', UNINSTALL)
        self.assertNotIn('rm -rf "$LANE_DIR"', INSTALL_IMAGE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
