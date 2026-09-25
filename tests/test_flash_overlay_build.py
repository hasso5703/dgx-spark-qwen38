#!/usr/bin/env python3
"""flash-sglang/build-image.sh puts in the image exactly the files its manifest verifies.

It checked MANIFEST.sha256 and then copied kda_kernels whole, so anything else in that
directory went into the image too, and a hash-checked .pyc in a __pycache__ there is what
Python imports instead of the verified kernel.py (found in review, 2026-09-24). A copy of
the directory with such a file in it is built here against a fake docker that records the
build context instead of building.
"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]

FAKE_DOCKER = """#!/bin/sh
case "$1 $2" in
  "image inspect") exit 0 ;;
esac
if [ "$1" = build ]; then
  for last; do :; done                    # the context is the last argument
  (cd "$last" && find . -type f | sort) > "$FAKE_LOG"
fi
exit 0
"""


class TheOverlayBuild(unittest.TestCase):
    def test_only_the_verified_files_are_staged(self):
        t = pathlib.Path(tempfile.mkdtemp(prefix="overlay-build-"))
        self.addCleanup(shutil.rmtree, t, ignore_errors=True)
        src = t / "flash-sglang"
        shutil.copytree(REPO / "flash-sglang", src)
        stray = src / "kda_kernels" / "qwen38_qsa_sm121" / "__pycache__" / "kernel.cpython-312.pyc"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"not the verified kernel")
        (t / "bin").mkdir()
        (t / "bin" / "docker").write_text(FAKE_DOCKER)
        (t / "bin" / "docker").chmod(0o755)
        log = t / "context.txt"
        r = subprocess.run(["bash", str(src / "build-image.sh")], capture_output=True, text=True, timeout=60,
                           env={**os.environ, "PATH": f"{t / 'bin'}:{os.environ['PATH']}", "FAKE_LOG": str(log),
                                "BASE_IMAGE": "base@sha256:" + "0" * 64, "TAG": "overlay:test"})
        self.assertEqual(r.returncode, 0, r.stderr)
        staged = {line[2:] for line in log.read_text().splitlines()}
        manifest = {line.split()[1] for line in (REPO / "flash-sglang" / "MANIFEST.sha256").read_text().splitlines()}
        self.assertEqual(staged - {"Dockerfile"}, manifest)


if __name__ == "__main__":
    unittest.main()
