#!/usr/bin/env python3
"""Each of the four drafter pins follows the installed unit unless it is passed.

With any one of DRAFT2_REPO, DRAFT2_REV, DRAFT2_QUANT or DRAFT2_TOKENS passed, the whole
convergence was skipped and the other three fell back to the defaults: DRAFT2_TOKENS=8 on a
box serving the BF16 draft (the documented rollback) moved it to the NVFP4 draft without a
word (found in review, 2026-09-24). The revision and the quantization belong to the repo,
so they follow the unit only together with its repo. This runs install.sh's own block."""
import pathlib
import re
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()
ENVS = "\n".join(ln for ln in TEXT.splitlines() if ln.startswith(("_ENV_DRAFT2_REPO=", "_ENV_DRAFT2_QUANT=")))
DEFAULTS = "\n".join(ln for ln in TEXT.splitlines() if re.match(r"DRAFT2_(REPO|REV|QUANT|TOKENS)=\"\$\{DRAFT2_", ln))
START = TEXT.index("  CUR_DRAFT=\"$(grep -oE -- '--speculative-draft-model-path")
BLOCK = TEXT[START:TEXT.index('\nfi\nif [ -n "$INSTALLED_CHOICE" ]; then', START)]
BF16 = ("ExecStart=... --speculative-draft-model-path z-lab/Qwen3.8-27B-DFlash2 "
        "--speculative-draft-model-revision 50307d4c4cde6860d4eee73e2547cd786fe8e8a4 "
        "--speculative-draft-model-quantization unquant --speculative-num-draft-tokens 8 ...\n")
DEFAULT_REPO = re.search(r'DRAFT2_REPO="\$\{DRAFT2_REPO:-([^}]+)\}"', TEXT).group(1)


def pins(unit_text, **env):
    d = pathlib.Path(tempfile.mkdtemp(prefix="draft-pins-"))
    (d / "unit").write_text(unit_text)
    script = ("set -euo pipefail\n" + ENVS + "\n" + DEFAULTS + f'\nUNIT_PATH="{d}/unit"\n' + BLOCK
              + '\necho "PINS $DRAFT2_REPO $DRAFT2_REV $DRAFT2_QUANT $DRAFT2_TOKENS"\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", **env})
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("PINS ")]
    assert line, r.stdout + r.stderr
    return line[0].split()[1:]


class EachPinFollowsTheUnit(unittest.TestCase):
    def test_the_env_block_names_all_four(self):
        self.assertIn('_ENV_DRAFT2_REV="${DRAFT2_REV:-}"', TEXT)
        self.assertIn('_ENV_DRAFT2_TOKENS="${DRAFT2_TOKENS:-}"', TEXT)

    def test_one_pin_passed_keeps_the_other_three(self):
        repo, rev, quant, tokens = pins(BF16, DRAFT2_TOKENS="16")
        self.assertEqual((repo, rev, quant), ("z-lab/Qwen3.8-27B-DFlash2", "50307d4c4cde6860d4eee73e2547cd786fe8e8a4",
                                              "unquant"))
        self.assertEqual(tokens, "16")

    def test_a_revision_passed_stays_on_the_installed_repo(self):
        self.assertEqual(pins(BF16, DRAFT2_REV="main")[:3], ["z-lab/Qwen3.8-27B-DFlash2", "main", "unquant"])

    def test_nothing_passed_keeps_all_four(self):
        self.assertEqual(pins(BF16), ["z-lab/Qwen3.8-27B-DFlash2", "50307d4c4cde6860d4eee73e2547cd786fe8e8a4",
                                      "unquant", "8"])

    def test_a_repo_passed_takes_its_own_pins(self):
        self.assertEqual(pins(BF16, DRAFT2_REPO=DEFAULT_REPO)[0], DEFAULT_REPO)
        self.assertNotEqual(pins(BF16, DRAFT2_REPO=DEFAULT_REPO)[2], "unquant", "the old repo's quantization stayed")

    def test_the_default_repo_keeps_a_depth_it_was_given(self):
        unit = f"ExecStart=... --speculative-draft-model-path {DEFAULT_REPO} --speculative-num-draft-tokens 12 ...\n"
        self.assertEqual(pins(unit)[3], "12")
        self.assertEqual(pins(unit, DRAFT2_TOKENS="16")[3], "16")


if __name__ == "__main__":
    unittest.main(verbosity=2)
