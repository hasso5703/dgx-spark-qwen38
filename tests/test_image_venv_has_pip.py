#!/usr/bin/env python3
"""The image lane's venv has a pip, and one left without it is made again.

venv is in python3's standard library either way; ensurepip, which gives a venv its pip,
comes with python3-venv. The preflight checked `import venv`, so a box without that
package made a venv with a python and no pip, and every later run took it for finished
and died on its first pip call (found in review, 2026-09-24). This runs install-image.sh's
own lines against a venv whose python has no pip."""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install-image.sh").read_text()
START = TEXT.index('if [ ! -x "$VENV/bin/python" ]; then')
BLOCK = TEXT[START:TEXT.index("\nfi\n", START) + 4]


class TheVenv(unittest.TestCase):
    def test_the_preflight_asks_for_ensurepip(self):
        self.assertIn("python3 -c 'import venv, ensurepip'", TEXT)

    def test_a_venv_without_pip_is_made_again(self):
        d = pathlib.Path(tempfile.mkdtemp(prefix="img-venv-"))
        self.addCleanup(shutil.rmtree, d, True)
        venv = d / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text('#!/bin/sh\n[ "$1" = -V ] && { echo "Python 3.12.3"; exit 0; }\n'
                                             'echo "No module named pip" >&2\nexit 1\n')
        (venv / "bin" / "python").chmod(0o755)
        script = ('set -euo pipefail\ndie(){ echo "DIE: $*"; exit 1; }\n'
                  f'VENV="{venv}"\n' + BLOCK + '\n"$VENV/bin/python" -m pip --version\n')
        # The system PATH only: pip runs `rustc --version` for its user agent when it finds
        # one, and a rustup rustc writes ~/.rustup/settings.toml into the HOME it is given,
        # which the offline-suite gate caught in its witness HOME.
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=240,
                           env={**os.environ, "PATH": "/usr/local/bin:/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("has no pip: making it again", r.stdout)
        self.assertIn("pip ", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
