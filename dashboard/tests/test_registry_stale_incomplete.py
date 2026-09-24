"""A blob an interrupted download left does not keep a checkpoint "downloading" forever.

Any <sha>.incomplete in a repo's blobs marked it downloading and its pinned revision
absent, including one nothing had written to since a download was cut short (found in
review, 2026-09-24). A revision is here when its snapshot holds every file its index
names, and a download is in progress while a blob is still being written to."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
import recipes as rc   # noqa: E402
import registry as rg  # noqa: E402

REPO_ID, REV = "Acme/Tiny-NVFP4", "a" * 40


def cache(root, shards_present, stale_incomplete=False, fresh_incomplete=False):
    d = root / f"models--{REPO_ID.replace('/', '--')}"
    blobs, snap = d / "blobs", d / "snapshots" / REV
    blobs.mkdir(parents=True)
    snap.mkdir(parents=True)
    (snap / "model.safetensors.index.json").write_text(json.dumps(
        {"weight_map": {"a": "model-00001.safetensors", "b": "model-00002.safetensors"}}))
    for name in shards_present:
        (blobs / name).write_bytes(b"w" * 10)
        (snap / name).symlink_to(blobs / name)
    if stale_incomplete:
        f = blobs / "old.incomplete"
        f.write_bytes(b"x")
        os.utime(f, (time.time() - 3600, time.time() - 3600))
    if fresh_incomplete:
        (blobs / "new.incomplete").write_bytes(b"x")


def presence(root):
    registry = {"images": [], "managed_repos": [REPO_ID], "models": rg.scan_hf_cache(root)}
    recipe = {"engine": {"image": "x"}, "model": {"repo": REPO_ID, "revision": REV}, "drafter": {}}
    return rc.presence(recipe, registry)


class Downloads(unittest.TestCase):
    def check(self, shards, stale=False, fresh=False):
        with tempfile.TemporaryDirectory() as td:
            cache(Path(td), shards, stale, fresh)
            p = presence(Path(td))
            return p["model"], p["downloading"]

    def test_a_whole_checkpoint_with_a_stale_leftover_is_here(self):
        self.assertEqual(self.check(["model-00001.safetensors", "model-00002.safetensors"], stale=True),
                         (True, False))

    def test_a_cut_download_is_not_here_and_not_downloading(self):
        self.assertEqual(self.check(["model-00001.safetensors"], stale=True), (False, False))

    def test_a_download_in_progress(self):
        self.assertEqual(self.check(["model-00001.safetensors"], fresh=True), (False, True))

    def test_a_snapshot_folder_alone_is_not_a_checkpoint(self):
        self.assertEqual(self.check([]), (False, False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
