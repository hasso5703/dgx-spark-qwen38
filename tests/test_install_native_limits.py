#!/usr/bin/env python3
"""An explicit CONTEXT_MODE=native on a 1m box brings opencode's limits down with it.

install.sh keeps the opencode limits of a 27B unit that serves a larger window than this
run computed, so that a re-install cannot shrink a 1m user's limits. Since the mode
converges on the installed unit, the only way to reach that guard is an explicit
CONTEXT_MODE=native, and that installs the native unit at step 8: the guard then kept
700,000/200,000 against a 262,144 window, a 400 past it, and advised CONTEXT_MODE=1m
(found in review, 2026-09-24). The installer's lines run here with oc-merge-limits.py
replaced by a stub that records what it is asked to write.
"""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()
START = TEXT.index("    # Never downgrade a 27B unit that serves a larger window than this run's")
END = TEXT.index("    # Offered whatever the limits branch decided above", START)
BLOCK = TEXT[START:END]


def run(env_mode):
    d = pathlib.Path(tempfile.mkdtemp(prefix="native-limits-"))
    (d / "unit").write_text("ExecStart=... --context-length 1010000 ...\n")
    (d / "oc-merge-limits.py").write_text(
        "import sys\nopen(sys.argv[0] + '.log', 'a').write(' '.join(sys.argv[1:]) + '\\n')\n")
    script = ("set -euo pipefail\n"
              f'REPO_DIR="{d}"; SGL_UNIT_PATH="{d}/unit"; OC_USER_CFG="{d}/opencode.json"\n'
              f"CONTEXT_MODE=native; _ENV_CONTEXT_MODE={env_mode}; OC_CTX=173000; OC_OUT=64000; OC_KEEP=38000\n"
              + BLOCK)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    log = d / "oc-merge-limits.py.log"
    return r.stdout + r.stderr, log.read_text() if log.exists() else ""


class NativeOnA1mBox(unittest.TestCase):
    def test_an_explicit_native_writes_the_native_limits(self):
        out, merged = run("native")
        self.assertIn("qwen38 qwen3.8-27b 173000 64000", merged, out)
        self.assertNotIn("keeping the existing", out)

    def test_a_mode_nobody_asked_for_still_keeps_them(self):
        out, merged = run("")
        self.assertIn("keeping the existing", out)
        self.assertNotIn("173000", merged)


if __name__ == "__main__":
    unittest.main(verbosity=2)
