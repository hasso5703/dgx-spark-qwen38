#!/usr/bin/env python3
"""install.sh leaves an oc it did not write where it is.

The launcher goes to ~/.local/bin/oc, and install.sh meant to leave any unrelated `oc`
alone (OpenShift's CLI is called oc too). It only looked at the first oc on the PATH, and
only when that one was elsewhere: an oc at ~/.local/bin/oc itself was overwritten, and so
was one in a ~/.local/bin the running shell's PATH lacked (found in review, 2026-09-24).
The installer's own lines run here with HOME in a temporary directory.
"""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()
START = TEXT.index('OC_BIN="$HOME/.local/bin/oc"')
A = TEXT.index('echo "installed the oc launcher at', START)
END = TEXT.index("\nfi\n", A) + len("\nfi\n")
BLOCK = TEXT[START:END]
FOREIGN = b"\x7fELF\x02\x01 openshift client, not ours\n"


def run(existing=None, on_path=True):
    home = pathlib.Path(tempfile.mkdtemp(prefix="foreign-oc-"))
    bindir = home / ".local/bin"
    bindir.mkdir(parents=True)
    if existing is not None:
        (bindir / "oc").write_bytes(existing)
        (bindir / "oc").chmod(0o755)
    path = (f"{bindir}:" if on_path else "") + "/usr/bin:/bin"
    script = ("set -euo pipefail\n"
              f'HOME="{home}"; CONFIG_DIR="{home}/.config/qwen38"; REPO_DIR=/repo; OC_OUT_CAP=200000; OC_OUT=200000\n'
              "OPENCODE_VERSION=1.18.32\n" + BLOCK)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": path, "HOME": str(home)})
    return r.returncode, r.stdout + r.stderr, (bindir / "oc").read_bytes() if (bindir / "oc").exists() else None


class AForeignOcIsLeftAlone(unittest.TestCase):
    def test_an_oc_at_the_launchers_own_path_is_kept(self):
        rc, out, after = run(existing=FOREIGN)
        self.assertEqual(after, FOREIGN, "the foreign oc was overwritten")
        self.assertIn("an unrelated 'oc' command exists", out)
        self.assertIn("OPENCODE_CONFIG=", out, "the command offered instead sends prompts to the cloud")

    def test_it_is_kept_when_this_shells_path_lacks_the_folder(self):
        rc, out, after = run(existing=FOREIGN, on_path=False)
        self.assertEqual(after, FOREIGN)

    def test_our_own_launcher_is_rewritten(self):
        rc, out, after = run(existing=b"#!/bin/bash\n# oc launcher installed by dgx-spark-qwen38: old\n")
        self.assertEqual(rc, 0, out)
        self.assertIn(b"OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX", after)

    def test_a_box_without_oc_gets_the_launcher(self):
        rc, out, after = run()
        self.assertEqual(rc, 0, out)
        self.assertIn(b"dgx-spark-qwen38", after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
