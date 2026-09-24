#!/usr/bin/env python3
"""Smaller installer defects found in review (2026-09-24), each against install.sh's own lines."""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()


def line(prefix):
    return next(ln for ln in TEXT.splitlines() if ln.startswith(prefix))


class ADfThatFailsSaysSo(unittest.TestCase):
    """A df that fails under pipefail ended the install at its own line, and the
    "found unknown GB" message written for exactly that case never showed."""

    def run_lines(self, *lines):
        d = pathlib.Path(tempfile.mkdtemp(prefix="df-fail-"))
        (d / "df").write_text("#!/bin/sh\necho 'df: cannot reach it' >&2\nexit 1\n")
        (d / "df").chmod(0o755)
        script = ("set -euo pipefail\ndie(){ echo \"DIE: $*\"; exit 1; }\n"
                  f'HF_CACHE="{d}"; NEED_GB=45; DOCKER_ROOT="{d}"; DOCKER_NEED_GB=40; IMG_LABEL=img\n'
                  + "\n".join(lines) + "\n")
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": f"{d}:/usr/bin:/bin"})
        return r.stdout + r.stderr

    def test_the_hf_cache_disk(self):
        out = self.run_lines(line("FREE_DISK_GB="), line('[ -n "$FREE_DISK_GB" ]'))
        self.assertIn("DIE: Need ~45 GB free", out)
        self.assertIn("found unknown GB", out)

    def test_the_docker_disk(self):
        out = self.run_lines(line("DOCKER_FREE_GB="), line('[ "${DOCKER_FREE_GB:-0}"'))
        self.assertIn("DIE: Need ~40 GB free on", out)


class TheDownloadHintNamesTheDraftPin(unittest.TestCase):
    def test_it_is_draft2(self):
        i = TEXT.index("Checkpoint download failed.")
        hint = TEXT[i:TEXT.index("\n", i)]
        self.assertIn("DRAFT2_REV=main", hint)
        self.assertNotIn(" DRAFT_REV=main", hint, "the retired DSpark pin")


if __name__ == "__main__":
    unittest.main(verbosity=2)
