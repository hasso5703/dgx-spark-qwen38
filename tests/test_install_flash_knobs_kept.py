#!/usr/bin/env python3
"""A flash box that turned off the reduced draft vocabulary or the replayssm verify state
keeps it off across plain re-runs, as it keeps its tier.

SPEC_TOKEN_MAP_SIZE=0 and FLASH_REPLAYSSM_SPEC=0 were undone by the next ./install.sh,
which read neither back from the installed launcher (found in review, 2026-09-24). A
launcher that does not speculate carries neither, and one rendered before a knob existed
says nothing about it: both keep the defaults. This runs install.sh's own lines."""
import pathlib
import re
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()
FUNC = TEXT[TEXT.index("resolve_flash_tier_args() {"):TEXT.index("\n}\n", TEXT.index("resolve_flash_tier_args() {")) + 3]
DEFAULTS = "\n".join(ln for ln in TEXT.splitlines()
                     if re.match(r'(FLASH_TIER|FLASH_REPLAYSSM_SPEC|SPEC_TOKEN_MAP_SIZE)="\$\{', ln)
                     or ln.startswith("TOKEN_MAP_NAME="))
START = TEXT.index("    # The tier lives in the launcher as the concurrency it pins")
BLOCK = TEXT[START:TEXT.index('    if [ -z "${_ENV_PLE_DIR:-}" ] && [ -n "$CUR_PLE" ]', START)]

HEAD = "# Engine since v1.8: the OFFICIAL SGLang image\n"
NEW = "# No --allow-auto-truncate. It was here undocumented until 2026-09-13\n"
CONTEXT = ("TIER=(--max-running-requests 4 --max-mamba-cache-size 20 --mamba-radix-cache-strategy extra_buffer "
           "--speculative-algorithm NEXTN --speculative-num-steps 3 --speculative-eagle-topk 1 "
           "--speculative-num-draft-tokens 4{replay})\n")
MAP = "TIER+=(--speculative-token-map /out/token-map-{n}.pt)\n"


def knobs(launcher, **env):
    d = pathlib.Path(tempfile.mkdtemp(prefix="flash-knobs-"))
    (d / "launch-flash.sh").write_text(launcher)
    script = ("set -euo pipefail\n" + DEFAULTS + "\n" + FUNC + "\nresolve_flash_tier_args\n"
              f'FLASH_TIER_ENV="${{FLASH_TIER:-}}"; _ENV_SPEC_TOKEN_MAP_SIZE="${{SPEC_TOKEN_MAP_SIZE_ENV:-}}"\n'
              f'_ENV_FLASH_REPLAYSSM_SPEC="${{FLASH_REPLAYSSM_SPEC_ENV:-}}"\nFLASH_LAUNCH="{d}/launch-flash.sh"\n'
              + BLOCK + '\necho "KNOBS $SPEC_TOKEN_MAP_SIZE $TOKEN_MAP_NAME $FLASH_REPLAYSSM_SPEC $FLASH_TIER_ARGS"\n')
    extra = {}
    for k, v in env.items():
        extra[k] = v
        if k in ("SPEC_TOKEN_MAP_SIZE", "FLASH_REPLAYSSM_SPEC"):
            extra[k + "_ENV"] = v
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", **extra})
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("KNOBS ")]
    assert line, r.stdout + r.stderr
    size, name, replay, *args = line[0].split()[1:]
    return size, name, replay, " ".join(args)


class TheKnobsFollowTheLauncher(unittest.TestCase):
    def test_both_off_stay_off(self):
        size, name, replay, args = knobs(HEAD + NEW + CONTEXT.format(replay=""))
        self.assertEqual((size, name, replay), ("0", "token-map-0.pt", "0"))
        self.assertNotIn("--enable-linear-replayssm-spec", args)

    def test_a_size_of_its_own_is_kept(self):
        size, name, _, _ = knobs(HEAD + NEW + CONTEXT.format(replay=" --enable-linear-replayssm-spec")
                                 + MAP.format(n=32768))
        self.assertEqual((size, name), ("32768", "token-map-32768.pt"))

    def test_the_defaults_stay_on_a_default_launcher(self):
        size, _, replay, args = knobs(HEAD + NEW + CONTEXT.format(replay=" --enable-linear-replayssm-spec")
                                      + MAP.format(n=65536))
        self.assertEqual((size, replay), ("65536", "1"))
        self.assertIn("--enable-linear-replayssm-spec", args)

    def test_a_launcher_older_than_the_knob_gets_the_default(self):
        size, _, replay, _ = knobs(CONTEXT.format(replay=""))      # neither marker: before v1.8
        self.assertEqual((size, replay), ("65536", "1"))
        _, _, replay, _ = knobs(HEAD + CONTEXT.format(replay="") + MAP.format(n=65536))   # v1.8 to v1.10.1
        self.assertEqual(replay, "1")

    def test_a_value_passed_wins(self):
        size, _, replay, _ = knobs(HEAD + NEW + CONTEXT.format(replay=""), SPEC_TOKEN_MAP_SIZE="65536",
                                   FLASH_REPLAYSSM_SPEC="1")
        self.assertEqual((size, replay), ("65536", "1"))

    def test_a_throughput_launcher_says_nothing_of_either(self):
        size, _, replay, _ = knobs(HEAD + NEW + "TIER=(--max-running-requests 24 --max-mamba-cache-size 96 "
                                   "--mamba-radix-cache-strategy extra_buffer_lazy)\n")
        self.assertEqual((size, replay), ("65536", "1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
