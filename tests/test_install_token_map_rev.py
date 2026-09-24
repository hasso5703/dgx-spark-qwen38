#!/usr/bin/env python3
"""The flash draft vocabulary is built for a branch revision too.

install.sh looked for the checkpoint at snapshots/$MODEL_REV, and with MODEL_REV=main (a
documented setting) that folder never exists: snapshots are named by the commit that
refs/main holds, so the reduced draft vocabulary was skipped on every run, with a note
saying the checkpoint was not in place yet (found in review, 2026-09-24).
"""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()
A = TEXT.index('echo "building the reduced draft vocabulary')
START = TEXT.index("\n", A) + 1
BLOCK = TEXT[START:TEXT.index('if [ -z "$SNAP_DIR" ]; then', START)]


def snap_dir(rev, with_ref):
    hf = pathlib.Path(tempfile.mkdtemp(prefix="map-rev-"))
    repo = hf / "hub" / "models--org--flash"
    (repo / "snapshots" / "abc123").mkdir(parents=True)
    if with_ref:
        (repo / "refs").mkdir()
        (repo / "refs" / "main").write_text("abc123")
    script = f'set -euo pipefail\nHF_CACHE="{hf}"; MODEL_REPO=org/flash; MODEL_REV={rev}\n' + BLOCK + 'echo "SNAP=$SNAP_DIR"\n'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("SNAP=")), "SNAP=")
    return line[5:]


class TheSnapshotOfABranch(unittest.TestCase):
    def test_main_leads_to_the_commit_it_names(self):
        self.assertTrue(snap_dir("main", with_ref=True).endswith("/snapshots/abc123"))

    def test_a_commit_still_leads_to_itself(self):
        self.assertTrue(snap_dir("abc123", with_ref=False).endswith("/snapshots/abc123"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
