#!/usr/bin/env python3
"""The image and video lanes ask for python3-dev before they build anything.

sglang[diffusion] asks for xatlas, which PyPI ships for no aarch64 Python, so pip builds it
on the box, against Python's headers. DGX OS installs python3-pip without its recommends,
python3-dev among them: on a box set up that way the build stopped on a missing
/usr/include/python3.12, inside the lane's long pip install (a user's box, 2026-10-05;
proved in a clean Ubuntu 24.04 container). The reference box had the headers from an install
by hand, so nothing here saw it. These run each installer's own line, against a python3
whose include directory holds the header or not.
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
INSTALLERS = ("install-image.sh", "install-video.sh")
NEEDLE = 'Python.h" ] || die "python3-dev is missing'


def check_line(text):
    return next(ln for ln in text.splitlines() if NEEDLE in ln)


class TheHeadersAreAskedForFirst(unittest.TestCase):
    def run_check(self, installer, with_header):
        d = pathlib.Path(tempfile.mkdtemp(prefix="lane-headers-"))
        self.addCleanup(shutil.rmtree, d, True)
        inc = d / "include" / "python3.12"
        inc.mkdir(parents=True)
        if with_header:
            (inc / "Python.h").write_text("/* the header */\n")
        (d / "bin").mkdir()
        (d / "bin" / "python3").write_text(f'#!/bin/sh\necho "{inc}"\n')   # sysconfig's include
        (d / "bin" / "python3").chmod(0o755)
        script = ("set -euo pipefail\n"
                  "die(){ printf 'ERROR: %s\\n' \"$*\" >&2; exit 1; }\n"
                  + check_line((REPO / installer).read_text()) + "\necho PASSED\n")
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                              env={"PATH": f"{d}/bin:/usr/bin:/bin"})

    def test_a_box_without_the_headers_is_told_what_to_install(self):
        for installer in INSTALLERS:
            with self.subTest(installer):
                r = self.run_check(installer, with_header=False)
                self.assertNotEqual(r.returncode, 0)
                self.assertNotIn("PASSED", r.stdout)
                self.assertIn("python3-dev is missing", r.stderr)
                self.assertIn("sudo apt-get install -y python3-dev", r.stderr)

    def test_a_box_with_the_headers_goes_on(self):
        for installer in INSTALLERS:
            with self.subTest(installer):
                r = self.run_check(installer, with_header=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("PASSED", r.stdout)

    def test_it_comes_before_the_pip_install_that_builds(self):
        for installer in INSTALLERS:
            with self.subTest(installer):
                text = (REPO / installer).read_text()
                self.assertLess(text.index(NEEDLE), text.index('pip" install --quiet --pre "sglang[diffusion]'))

    def test_both_lanes_ask_the_same_way(self):
        self.assertEqual(*(check_line((REPO / f).read_text()) for f in INSTALLERS))

    def test_the_line_reads_the_include_directory_python_itself_reports(self):
        # sysconfig, not a path written down: a box whose python3 is not 3.12 has its own.
        self.assertIn("sysconfig.get_paths()[\"include\"]", check_line((REPO / INSTALLERS[0]).read_text()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
