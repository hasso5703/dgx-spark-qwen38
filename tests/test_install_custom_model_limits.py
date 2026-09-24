#!/usr/bin/env python3
"""A box that serves a custom --model-path can be updated.

install.sh keeps a --model-path it does not know, as MODEL_CHOICE=custom, and at step 7
asks oc-limits.sh for opencode's limits. The table had no row for it, and the refusal
could not fire: `read <<<"$(cmd)"` returns 0 whatever cmd returned. So every update of
such a box died at step 7 under "returned no limits" (found in review, 2026-09-24).
"""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()


class ACustomModelHasLimits(unittest.TestCase):
    def test_the_table_has_a_row_for_a_kept_custom_model(self):
        for mode, want in (("native", "173000 64000 local"), ("1m", "480000 160000 local, 1M")):
            r = subprocess.run([str(REPO / "oc-limits.sh"), "custom", mode],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, want), r.stderr)

    def test_a_refusal_of_the_table_is_named(self):
        start = TEXT.index('OC_ROW="$("$REPO_DIR/oc-limits.sh" "$MODEL_CHOICE" "$OC_SELECTOR")"')
        block = TEXT[start:TEXT.index("# The output CEILING", start)]
        fake = pathlib.Path(tempfile.mkdtemp(prefix="oc-refuses-"))
        (fake / "oc-limits.sh").write_text("#!/bin/sh\necho 'oc-limits: unknown target \"x\"' >&2\nexit 2\n")
        (fake / "oc-limits.sh").chmod(0o755)
        script = ('set -euo pipefail\ndie(){ echo "DIE: $*"; exit 1; }\n'
                  f'REPO_DIR="{fake}"; MODEL_CHOICE=x; OC_SELECTOR=1m\n' + block + "echo REACHED\n")
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        self.assertIn("DIE: oc-limits.sh refused MODEL_CHOICE=x", r.stdout, r.stdout + r.stderr)
        self.assertNotIn("REACHED", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
