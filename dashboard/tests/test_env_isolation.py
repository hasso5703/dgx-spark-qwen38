#!/usr/bin/env python3
"""Every test module hands the next one the environment it found.

unittest runs all of these files in one process, one after the other, so whatever a module
leaves in os.environ is what the modules after it start from. The cockpit reads its
configuration from the environment when it is loaded: test_cockpit_jobs.py set
COCKPIT_DRY_RUN=0, pointed HOME and COCKPIT_CONFIG_DIR at directories it then deleted, and
left all of it behind, so every module after it ran with a HOME that did not exist and
passed or failed on that rather than on the code (found in review, 2026-09-24).

One test of each module is run, which is enough to run all of its fixtures (module, then
class), and the environment is compared before and after, in a process of its own.
"""
import os
import pathlib
import subprocess
import sys
import unittest

TESTS = pathlib.Path(__file__).resolve().parent
DASH = TESTS.parent

PROBE = """
import os, pathlib, sys, unittest
sys.path.insert(0, os.getcwd())
for path in sorted(pathlib.Path("tests").glob("test_*.py")):
    if path.stem == "test_env_isolation":
        continue
    name = "tests." + path.stem
    suite = unittest.defaultTestLoader.loadTestsFromName(name)
    stack, first = [suite], None
    while stack and first is None:
        item = stack.pop(0)
        if isinstance(item, unittest.TestSuite):
            stack[:0] = list(item)
        else:
            first = item
    if first is None:
        continue
    before = dict(os.environ)
    unittest.TestSuite([first]).run(unittest.TestResult())
    after = dict(os.environ)
    for var in sorted(set(before) | set(after)):
        if before.get(var) != after.get(var):
            print("LEAK " + name + " " + var + ": " + repr(before.get(var)) + " -> " + repr(after.get(var)))
print("DONE")
"""


class EveryModuleRestoresTheEnvironment(unittest.TestCase):
    def test_no_module_leaves_the_environment_changed(self):
        proc = subprocess.run([sys.executable, "-c", PROBE], cwd=DASH, capture_output=True,
                              text=True, timeout=600, env=dict(os.environ))
        self.assertIn("DONE", proc.stdout,
                      f"the probe did not finish:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
        leaks = [line for line in proc.stdout.splitlines() if line.startswith("LEAK ")]
        self.assertEqual(leaks, [], "these modules changed the environment the next one gets")


if __name__ == "__main__":
    unittest.main(verbosity=2)
