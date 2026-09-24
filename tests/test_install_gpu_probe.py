#!/usr/bin/env python3
"""A GPU the driver cannot reach is named, not reported as a line number.

`nvidia-smi` exits non-zero when it cannot talk to the driver, and says why. install.sh
read the GPU name in a bare `$(nvidia-smi ... | head -1)` under set -euo pipefail, so the
install ended in its ERR trap ("Install failed at line N") with nvidia-smi's explanation
captured and never printed (found in review, 2026-09-24). The preflight's own lines run
here against an nvidia-smi that fails the way a driver not loaded does.
"""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()
TRAP = next(ln for ln in TEXT.splitlines() if ln.startswith("trap ") and "ERR" in ln)
START = TEXT.index('command -v nvidia-smi >/dev/null || die')
BLOCK = TEXT[START:TEXT.index('echo "GPU: $GPU_NAME"', START)] + 'echo "GPU: $GPU_NAME"\n'
MESSAGE = ("NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver. "
           "Make sure that the latest NVIDIA driver is installed and running.")


def run(stub):
    d = pathlib.Path(tempfile.mkdtemp(prefix="gpu-probe-"))
    (d / "nvidia-smi").write_text(stub)
    (d / "nvidia-smi").chmod(0o755)
    script = 'set -euo pipefail\n' + TRAP + '\ndie(){ printf "ERROR: %s\\n" "$*" >&2; exit 1; }\n' + BLOCK
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{d}:/usr/bin:/bin"})
    return r.returncode, r.stdout + r.stderr


class TheDriverFailureIsNamed(unittest.TestCase):
    def test_a_driver_that_is_not_loaded_is_named(self):
        rc, out = run(f"#!/bin/sh\necho \"{MESSAGE}\"\nexit 9\n")
        self.assertEqual(rc, 1)
        self.assertIn("nvidia-smi cannot reach the GPU", out)
        self.assertIn("couldn't communicate with the NVIDIA driver", out)
        self.assertNotIn("Install failed at line", out)

    def test_a_gpu_that_answers_is_read(self):
        rc, out = run("#!/bin/sh\necho 'NVIDIA GB10'\n")
        self.assertEqual(rc, 0, out)
        self.assertIn("GPU: NVIDIA GB10", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
