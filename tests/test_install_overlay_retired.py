#!/usr/bin/env python3
"""OVERLAY_FLASH=1, the flash lane's documented rollback, is refused instead of rendered.

It rebuilt the v1.5 to v1.7 overlay image and served it with the launcher of v1.8, which
does not fit it (found in review, 2026-09-24): the overlay's model code
(flash-sglang/qwen4_exp.py) keeps the 47.7 GiB PLE table in pinned host RAM unless
SGLANG_QWEN4_PLE_MMAP_DIR is set, and the launcher sets no such variable and passes
`--ple-offload-backend file --ple-offload-dir /ple` (sglang#37068) instead, with none of the
attention backends and sizes the overlay was validated with. Pinned host RAM for the
table does not boot on this box (the CI's own note), nothing had booted that pairing
since v1.8, and the CI checked only the pins. The overlay path shipped whole in v1.7.2.

The installer is run with --help only: it exits there, before its first system call.
"""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
INSTALL = REPO / "install.sh"


def run(*args, **env_extra):
    home = tempfile.mkdtemp(prefix="overlay-retired-")
    try:
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, **env_extra}
        r = subprocess.run(["bash", str(INSTALL), *args], capture_output=True, text=True, env=env, timeout=30)
        return r.returncode, r.stdout + r.stderr
    finally:
        shutil.rmtree(home, ignore_errors=True)


class TheRetiredOverlay(unittest.TestCase):
    def test_the_mismatch_it_retires_is_real(self):
        # the two halves that did not fit, read from the files that would have met
        overlay = (REPO / "flash-sglang" / "qwen4_exp.py").read_text()
        self.assertIn('os.environ.get("SGLANG_QWEN4_PLE_MMAP_DIR"', overlay)
        self.assertIn("pin_memory=True", overlay)
        self.assertNotIn("ple_offload_backend", overlay)
        launcher = (REPO / "qwen38-flash-launch.sh.template").read_text()
        self.assertNotIn("SGLANG_QWEN4_PLE_MMAP_DIR", launcher)
        self.assertIn("--ple-offload-backend file", launcher)

    def test_overlay_flash_is_refused_and_says_where_the_rollback_is(self):
        for var in ("OVERLAY_FLASH", "OVERLAY"):
            rc, out = run("--help", **{var: "1"})
            self.assertNotEqual(rc, 0, f"{var}=1 was accepted:\n{out[-400:]}")
            self.assertIn("v1.7.2", out, var)

    def test_help_no_longer_offers_it(self):
        rc, out = run("--help")
        self.assertEqual(rc, 0, out[-400:])
        self.assertNotIn("OVERLAY_FLASH", out)

    def test_zero_is_still_a_plain_install(self):
        rc, out = run("--help", OVERLAY_FLASH="0")
        self.assertEqual(rc, 0, out[-400:])


if __name__ == "__main__":
    unittest.main()
