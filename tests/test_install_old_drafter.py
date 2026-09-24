#!/usr/bin/env python3
"""A box updated from before v1.9 is told how to reach v1.9's draft.

install.sh keeps the drafter the installed unit serves, so that the documented rollback
to the BF16 draft survives plain re-runs. That rollback is also the default of v1.2.3 to
v1.8.6, and a unit cannot say which it is: a box that was only ever updated kept the BF16
draft at depth 8 and never got the calibrated NVFP4 draft v1.9 measured at +30% (found in
review, 2026-09-24). The rule stays, and such a box is now told, with the exact command.
"""
import pathlib
import re
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()
DEFAULTS = "\n".join(ln for ln in TEXT.splitlines() if re.match(r"DRAFT2_(REPO|REV|QUANT|TOKENS)=\"\$\{DRAFT2_", ln))
START = TEXT.index('  if [ -z "$_ENV_DRAFT2_REPO$_ENV_DRAFT2_REV$_ENV_DRAFT2_QUANT$_ENV_DRAFT2_TOKENS" ]; then')
BLOCK = TEXT[START:TEXT.index('\nfi\nif [ -n "$INSTALLED_CHOICE" ]; then', START)]
OLD = ("ExecStart=... --speculative-draft-model-path z-lab/Qwen3.8-27B-DFlash2 "
       "--speculative-draft-model-revision 50307d4c4cde6860d4eee73e2547cd786fe8e8a4 "
       "--speculative-draft-model-quantization unquant --speculative-num-draft-tokens 8 ...\n")


def run(unit_text):
    unit = pathlib.Path(tempfile.mkdtemp(prefix="old-drafter-")) / "unit"
    unit.write_text(unit_text)
    script = ("set -euo pipefail\n_ENV_DRAFT2_REPO=; _ENV_DRAFT2_REV=; _ENV_DRAFT2_QUANT=; _ENV_DRAFT2_TOKENS=\n"
              + DEFAULTS + f'\nUNIT_PATH="{unit}"\n' + BLOCK + "\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    return r.stdout + r.stderr


class ThePreV19DraftIsNamed(unittest.TestCase):
    def test_a_box_on_the_old_default_is_told_the_way_over(self):
        out = run(OLD)
        self.assertIn("Keeping the installed drafter: z-lab/Qwen3.8-27B-DFlash2", out)
        self.assertIn("NOTE: that is the BF16 draft", out)
        default = re.search(r'DRAFT2_REPO="\$\{DRAFT2_REPO:-([^}]+)\}"', TEXT).group(1)
        self.assertIn(f"DRAFT2_REPO={default} ", out, "the command does not name today's default")
        self.assertIn("./install.sh", out)

    def test_a_box_on_the_current_draft_hears_nothing(self):
        current = run("ExecStart=... --speculative-draft-model-path "
                      + re.search(r'DRAFT2_REPO="\$\{DRAFT2_REPO:-([^}]+)\}"', TEXT).group(1)
                      + " --speculative-num-draft-tokens 16 ...\n")
        self.assertNotIn("NOTE", current)
        self.assertNotIn("Keeping the installed drafter", current)


if __name__ == "__main__":
    unittest.main(verbosity=2)
