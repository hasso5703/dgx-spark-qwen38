#!/usr/bin/env python3
"""The configs of a context mode are written right before the unit that reads them.

A 27B unit on checkpoint configs of the other mode crashes at load: a 1m unit asks for
a window a native config no longer derives, and a native server crashes on a YaRN-
patched config (measured 2026-09-11). install.sh patched or restored those configs at
step 6 and wrote the unit at step 8, so any failure in between (the opencode download of
step 7, a sudo that could not ask any more at step 8) left the installed unit of the old
mode on configs of the new one, to crash the next time systemd started it (found in
review, 2026-09-24). The patch now runs in one function, called at step 8 immediately
before the unit is installed, or at the end of step 7 on --no-service, which writes none.

install.sh cannot run to its step 8 here, so this reads its order of execution: where the
patch tool is called, and where the function that calls it runs.
"""
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()
FUNC_START = TEXT.index("apply_context_configs(){")
FUNC_END = TEXT.index("\n}\n", FUNC_START)
STEP7 = TEXT.index('step "7/10')



class TheConfigsMoveWithTheUnit(unittest.TestCase):
    def test_the_patch_tool_is_only_called_from_the_function(self):
        for m in re.finditer(r"patch-yarn\.py", TEXT):
            line_start = TEXT.rfind("\n", 0, m.start()) + 1
            if TEXT[line_start:m.start()].lstrip().startswith("#"):
                continue
            self.assertTrue(FUNC_START < m.start() < FUNC_END,
                            f"patch-yarn.py runs outside apply_context_configs, line "
                            f"{TEXT.count(chr(10), 0, m.start()) + 1}")

    def test_it_runs_after_step_7_and_right_before_the_unit(self):
        calls = [m.start() for m in re.finditer(r"(?m)^\s*apply_context_configs\b(?!\(\))", TEXT)]
        self.assertEqual(len(calls), 2, "expected one call for --no-service and one at step 8")
        for pos in calls:
            self.assertGreater(pos, STEP7, "the configs are written before step 7 can fail")
        no_service, step8 = calls
        exit_pos = TEXT.index("exit 0", no_service)
        block = TEXT[TEXT.rfind('if [ "$NO_SERVICE" -eq 1 ]; then', 0, no_service):exit_pos]
        self.assertIn("Prepared (no systemd", block, "the first call is not the --no-service exit")
        after = TEXT[step8:].split("\n", 1)[1]
        nxt = next(ln for ln in after.splitlines() if ln.strip() and not ln.strip().startswith("#"))
        self.assertIn('sudo install -m 644 "$TMP_UNIT"', nxt,
                      "something that can fail runs between the configs and the unit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
