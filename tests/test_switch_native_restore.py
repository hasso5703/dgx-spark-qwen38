#!/usr/bin/env python3
"""A switch on a native 27B box restores a target's config that still carries YaRN.

A native server crashes at load on a YaRN-patched config.json (measured 2026-09-11). A
27B target served while the box was in 1m mode keeps that patch in the shared cache when
the box goes back to native, since install.sh restores the target it installs and not the
others. switch-model.sh skipped the patch in native mode and restored nothing, so a switch
to such a target queued an engine that could not start (found in review, 2026-09-24).
The switch's own lines run here with patch-yarn.py replaced by a stub that records calls.
"""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "switch-model.sh").read_text()
START = TEXT.index('if [ "$TARGET_LANE" = "27b" ] && grep -q -- \'--context-length 1010000\' "$TARGET_UNIT"; then')
BLOCK = TEXT[START:TEXT.index("\nfi\n", START) + 4]


def run(context_length, restore_rc=0):
    d = pathlib.Path(tempfile.mkdtemp(prefix="sw-restore-"))
    (d / "unit").write_text(f"ExecStart=... --context-length {context_length} ...\n")
    (d / "patch-yarn.py").write_text(
        "import sys\nopen(sys.argv[0] + '.log', 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"sys.exit({restore_rc} if '--restore' in sys.argv else 0)\n")
    script = ('set -euo pipefail\ndie(){ echo "DIE: $*"; exit 1; }\n'
              f'REPO_DIR="{d}"; HF_CACHE=/hf; TARGET_LANE=27b; TARGET_REPO=org/uncensored; '
              f'TARGET_REV=abc; TARGET_UNIT="{d}/unit"\n' + BLOCK + "echo REACHED\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    log = d / "patch-yarn.py.log"
    return r.stdout + r.stderr, log.read_text() if log.exists() else ""


class ANativeSwitchRestores(unittest.TestCase):
    def test_the_target_is_restored_on_a_native_unit(self):
        out, calls = run(262144)
        self.assertIn("--restore /hf org/uncensored abc", calls, out)
        self.assertIn("REACHED", out)

    def test_a_restore_that_fails_stops_the_switch_before_the_unit(self):
        out, _ = run(262144, restore_rc=1)
        self.assertIn("DIE:", out)
        self.assertNotIn("REACHED", out)

    def test_a_1m_unit_still_gets_the_patch(self):
        out, calls = run(1010000)
        self.assertIn("/hf org/uncensored abc", calls)
        self.assertNotIn("--restore", calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
