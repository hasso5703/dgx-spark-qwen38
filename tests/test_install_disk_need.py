#!/usr/bin/env python3
"""install.sh measures what is left to download, on the disk it will land on.

huggingface_hub creates a checkpoint's snapshot folder before the first byte of its
first file (file_download.py, 1.31.0, the version in the pinned image), and install.sh
read "a snapshot folder exists" as "the checkpoint is cached": its disk need dropped from
180 to 10 GB, so a flash download interrupted at 74 GB of 124, then resumed, was checked
against 10 and could fill the disk. The PLE table the flash lane writes at every boot
(47.7 GiB) was checked against HF_CACHE's disk, never PLE_DIR's (found in review,
2026-09-24).

The installer's own lines run here against a fake cache, with df and stat answered by
stubs where a test needs two disks.
"""
import json
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()
START = TEXT.index("NEED_GB=45; DOCKER_NEED_GB=40")
END = TEXT.index('[ -n "$FREE_DISK_GB" ] && [ "$FREE_DISK_GB" -ge "$NEED_GB" ] || die', START)
BLOCK = TEXT[START:END]
SHARDS = [f"model-0000{i}-of-00004.safetensors" for i in range(1, 5)]


def cache(root, shards_done, blob_gb=0.0, index=True):
    """A repo folder the way huggingface_hub leaves it: blobs, and a snapshot whose
    files are links to them, a link appearing only once its blob is whole."""
    repo = root / "hub" / "models--org--model"
    snap = repo / "snapshots" / "abc123"
    blobs = repo / "blobs"
    snap.mkdir(parents=True)
    blobs.mkdir(parents=True)
    if index:
        (snap / "model.safetensors.index.json").write_text(json.dumps(
            {"weight_map": {f"t{i}": s for i, s in enumerate(SHARDS)}}))
    for i, s in enumerate(SHARDS[:shards_done]):
        (blobs / f"b{i}").write_bytes(b"x")
        (snap / s).symlink_to(f"../../blobs/b{i}")
    if blob_gb:
        with open(blobs / "partial.incomplete", "wb") as f:
            f.truncate(int(blob_gb * 1024 ** 3))       # sparse: its apparent size is what counts
    return root


def run(lane, root, free_gb=1000, ple_other_disk=None):
    t = pathlib.Path(tempfile.mkdtemp(prefix="disk-need-bin-"))
    stub, ple = "", f"{root}/ple"
    if ple_other_disk is not None:
        # PLE_DIR on a second disk, whose folder exists but not the table's own: stat
        # names another device for it, df gives it its own space
        (root / "otherdisk").mkdir()
        ple = f"{root}/otherdisk/ple"
        (t / "stat").write_text("#!/bin/sh\ncase \"$*\" in *otherdisk*) echo 99;; *) echo 1;; esac\n")
        (t / "df").write_text(f"#!/bin/sh\necho Avail; echo {ple_other_disk}G\n")
        for f in ("stat", "df"):
            (t / f).chmod(0o755)
        stub = f'PATH="{t}:$PATH"\n'
    script = (stub + "set -euo pipefail\ndie(){ echo \"DIE: $*\"; exit 1; }\n"
              f'HF_CACHE="{root}"; MODEL_REPO=org/model; MODEL_REV=abc123; LANE={lane}\n'
              f'PLE_DIR="{ple}"; FREE_DISK_GB={free_gb}\n'
              + BLOCK + 'echo "NEED=$NEED_GB"\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    for ln in out.splitlines():
        if ln.startswith("NEED="):
            return int(ln[5:]), out
    return None, out


class TheNeedIsWhatIsLeft(unittest.TestCase):
    def tmp(self):
        return pathlib.Path(tempfile.mkdtemp(prefix="disk-need-"))

    def test_a_snapshot_folder_alone_is_not_a_cached_checkpoint(self):
        need, out = run("27b", cache(self.tmp(), shards_done=0))
        self.assertEqual(need, 45, out)

    def test_a_whole_checkpoint_needs_working_room_only(self):
        need, out = run("27b", cache(self.tmp(), shards_done=4))
        self.assertEqual(need, 10, out)

    def test_an_interrupted_download_is_charged_what_is_left(self):
        """The 2026-09-18 case: 74 GB of the flash checkpoint in cache, no PLE table yet."""
        need, out = run("flash", cache(self.tmp(), shards_done=2, blob_gb=74))
        self.assertEqual(need, 180 - 74 + 50, out)

    def test_a_single_file_checkpoint_counts_when_its_file_is_there(self):
        root = cache(self.tmp(), shards_done=0, index=False)
        snap = root / "hub/models--org--model/snapshots/abc123"
        (root / "hub/models--org--model/blobs/whole").write_bytes(b"x")
        (snap / "model.safetensors").symlink_to("../../blobs/whole")
        need, out = run("27b", root)
        self.assertEqual(need, 10, out)


class ThePleTableIsCheckedOnItsOwnDisk(unittest.TestCase):
    def test_a_ple_dir_on_a_full_disk_is_refused(self):
        root = cache(pathlib.Path(tempfile.mkdtemp(prefix="disk-need-")), shards_done=4)
        need, out = run("flash", root, ple_other_disk=20)
        self.assertIsNone(need, out)
        self.assertIn("PLE_DIR", out)

    def test_a_ple_dir_on_its_own_roomy_disk_adds_nothing_here(self):
        root = cache(pathlib.Path(tempfile.mkdtemp(prefix="disk-need-")), shards_done=4)
        need, out = run("flash", root, ple_other_disk=500)
        self.assertEqual(need, 10, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
