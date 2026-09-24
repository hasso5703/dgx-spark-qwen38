#!/usr/bin/env python3
"""The image lane downloads its checkpoint at a pinned revision, and serves that one.

install-image.sh fetched Qwen/Qwen-Image-2.1 at main, so a push upstream would have
changed what a fresh install serves, the one thing every other pin in this repo prevents
(found in review, 2026-09-24). The unit serves the repo by name with HF_HUB_OFFLINE=1,
which resolves refs/main, and a download by commit writes no ref: the pinned commit has to
become main. These run the script's own download step, as written, against a fake
huggingface_hub that keeps the library's signature and lays out the cache as it does.
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = (REPO / "install-image.sh").read_text()

FAKE_HUB = r'''import json, os
def snapshot_download(repo_id, *, revision=None, **kw):
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps({"repo": repo_id, "revision": revision}) + "\n")
    sha = revision if revision and len(revision) == 40 else "a" * 40
    root = os.path.join(os.environ["HF_HOME"], "hub", "models--" + repo_id.replace("/", "--"))
    path = os.path.join(root, "snapshots", sha)
    os.makedirs(path, exist_ok=True)
    if not (revision and len(revision) == 40):      # a branch download writes its ref
        os.makedirs(os.path.join(root, "refs"), exist_ok=True)
        open(os.path.join(root, "refs", revision or "main"), "w").write(sha)
    return path
'''


def download_step() -> str:
    start = SCRIPT.index("import os, time\nfrom huggingface_hub import snapshot_download")
    return SCRIPT[start:SCRIPT.index("\nPY\n", start)]


# the revision the lane was measured with on 2026-09-22 (the box's cache, refs/main)
VALIDATED = "790c92633540aa0cb11d9abf19eb46d861714758"


class TheImageCheckpoint(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="image-pin-"))
        self.addCleanup(shutil.rmtree, self.t, ignore_errors=True)
        (self.t / "py" / "huggingface_hub").mkdir(parents=True)
        (self.t / "py" / "huggingface_hub" / "__init__.py").write_text(FAKE_HUB)
        self.hf = self.t / "hf"
        self.ref = self.hf / "hub" / "models--Qwen--Qwen-Image-2.1" / "refs" / "main"

    def run_step(self, rev):
        env = {**os.environ, "PYTHONPATH": str(self.t / "py"), "HF_HOME": str(self.hf),
               "FAKE_LOG": str(self.t / "log"), "MODEL_REPO": "Qwen/Qwen-Image-2.1", "MODEL_REV": rev}
        r = subprocess.run([sys.executable, "-c", download_step()], env=env, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [line for line in (self.t / "log").read_text().splitlines()]

    def test_the_pin_is_what_is_downloaded_and_served(self):
        log = self.run_step(VALIDATED)
        self.assertIn(f'"revision": "{VALIDATED}"', log[0])
        self.assertTrue(self.ref.exists(), "refs/main not written: the offline unit would not find the pin")
        self.assertEqual(self.ref.read_text().strip(), VALIDATED)

    def test_a_main_that_moved_is_put_back_on_the_pin(self):
        self.ref.parent.mkdir(parents=True)
        self.ref.write_text("b" * 40)
        self.run_step(VALIDATED)
        self.assertEqual(self.ref.read_text().strip(), VALIDATED)

    def test_the_default_is_the_validated_revision(self):
        # assertTrue, not assertIn: a failure must not print the whole script
        self.assertTrue(f'IMAGE_MODEL_PIN_REV="{VALIDATED}"' in SCRIPT, "the validated revision is not the pin")
        # the default model is written out (the uninstall inventory reads it there) and is the pinned one
        self.assertTrue('IMAGE_MODEL_PIN="Qwen/Qwen-Image-2.1"' in SCRIPT, "the pin names another repo")
        self.assertTrue('MODEL="${MODEL:-Qwen/Qwen-Image-2.1}"' in SCRIPT, "the default model is not the pinned one")
        self.assertTrue('MODEL_REV="${IMAGE_MODEL_REV:-$IMAGE_MODEL_PIN_REV}"' in SCRIPT, "the pin is not the default revision")


    def test_security_md_says_the_checkpoint_is_pinned(self):
        text = " ".join((REPO / "SECURITY.md").read_text().split())
        self.assertNotIn("current revision rather than a pinned one", text)
        self.assertIn("source commit and checkpoint revision are pinned", text)

if __name__ == "__main__":
    unittest.main()
