#!/usr/bin/env python3
"""A native install that restores the pre-YaRN configs must say so when an
installed 1m unit will keep reading them.

`--no-service` and `--no-start` write no unit. On a box whose 27B unit already
serves `--context-length 1010000`, a native run still restores the cached
configs to 262,144, and that unit then dies at load the next time systemd
starts it: the target asks for a window the config no longer derives. Nothing
said so until 2026-09-18, when a native install run against a 1m box's cache
silently stranded the reference box between a boot and a restart.

The warning fires late in install.sh, past the downloads, so the harness in
test_install_context_mode.py (which stops the installer early on purpose)
cannot reach it. This extracts the guard and runs it against the three states
that matter instead, which also fails loudly if the block is renamed away.
"""
import pathlib
import shlex
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
INSTALL = (REPO / "install.sh").read_text()
HEAD = ('  if [ "$LANE" = "27b" ] && [ "$NO_SERVICE" -eq 1 ] && [ -r "$SGL_UNIT_PATH" ]; then\n'
        '    _INSTALLED_CTX=')
TAIL = '  if [ "$LANE" = "27b" ]; then'
SENTINEL = "WARNING: the installed unit serves"


def guard() -> str:
    """The guard block, or a failure naming what moved."""
    i = INSTALL.find(HEAD)
    if i == -1:
        raise AssertionError("the native-restore guard is gone from install.sh (or its opening changed)")
    j = INSTALL.find(TAIL, i)
    if j == -1:
        raise AssertionError("the guard no longer sits above the 27B restore block")
    block = INSTALL[i:j]
    if SENTINEL not in block:
        raise AssertionError("the guard no longer warns")
    return block


def run_guard(unit_text: str, *, tmp, lane="27b", no_service=1, no_start=0):
    unit = tmp / "unit"
    unit.write_text(unit_text)
    # set -euo pipefail and the ERR trap are what install.sh:11 runs this under.
    # Without them a dropped `|| true` inside the block stays green here while
    # the real installer dies mid-restore.
    script = ('set -euo pipefail\n'
              f'LANE={lane}\nSGL_UNIT_PATH={shlex.quote(str(unit))}\n'
              f'NO_SERVICE={no_service}\nNO_START={no_start}\n' + guard())
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    return r.stdout + r.stderr


ONE_M = "ExecStart=... --context-length 1010000 --model-path X ...\n"
NATIVE = "ExecStart=... --context-length 262144 --model-path X ...\n"


class Guard(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="yarn-guard-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_it_warns_when_a_1m_unit_would_be_stranded(self):
        self.assertIn(SENTINEL, run_guard(ONE_M, tmp=self.tmp))

    def test_it_is_silent_for_no_start(self):
        # --no-start reaches step 8: the native template is rendered over that
        # unit and enabled, so nothing is stranded. Warning there would be a
        # lie, and it would offer CONTEXT_MODE=1m to someone who just asked to
        # leave 1m. This test pinned the opposite for a few hours on 2026-09-18.
        out = run_guard(ONE_M, no_service=0, no_start=1, tmp=self.tmp)
        self.assertNotIn(SENTINEL, out)

    def test_it_is_silent_when_the_unit_is_rewritten(self):
        # No --no-service and no --no-start: the unit is rewritten native, so
        # there is nothing to strand and nothing to say.
        self.assertNotIn(SENTINEL, run_guard(ONE_M, no_service=0, no_start=0, tmp=self.tmp))

    def test_it_is_silent_on_a_native_unit(self):
        self.assertNotIn(SENTINEL, run_guard(NATIVE, tmp=self.tmp))

    def test_it_is_silent_on_the_flash_lane(self):
        # 1m is a 27B mode and flash configs are never patched.
        self.assertNotIn(SENTINEL, run_guard(ONE_M, lane="flash", tmp=self.tmp))

    def test_it_names_both_ways_out(self):
        out = run_guard(ONE_M, tmp=self.tmp)
        self.assertIn("without --no-service", out)
        self.assertIn("CONTEXT_MODE=1m ./install.sh", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
