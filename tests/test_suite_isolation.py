#!/usr/bin/env python3
"""A test file must hand the process back the environment it borrowed.

`tests/test_tools.py` points HOME at a throwaway directory, and it has to: the
scripts it loads read the serving API key at module level, so importing one on a
machine without `~/.config/qwen38/api-key` raises before a single test runs.

Doing it at module level is subtly worse than it looks, because unittest imports
every module of a run before executing any of them: the assignment reached files
that run long before that one. `test_install_sudo` replays install.sh for real,
and install.sh sizes its disk preflight from whether the checkpoints are cached
(10 GB when they are, 230 GB when they are not). Under the borrowed HOME it saw
an empty cache, died at step 1 for lack of space, and reported that as a failure
of the step 8 refusal it never reached. Full-suite runs only, on a box that has
the cache, which is the kind of failure that gets called a flake and muted
(2026-09-18).

Two gates, for the two moments a file can leak: importing, and running.
"""
import os
import pathlib
import subprocess
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]

# Imports every module the way a suite run does, and names the first one that
# leaves HOME somewhere else.
IMPORT_PROBE = """
import importlib, os, pathlib
before = os.environ.get("HOME")
for path in sorted(pathlib.Path("tests").glob("test_*.py")):
    name = path.stem
    if name == "test_suite_isolation":
        continue
    importlib.import_module("tests." + name)
    now = os.environ.get("HOME")
    if now != before:
        print("LEAK=" + name + " " + str(before) + " -> " + str(now))
        before = now
print("DONE")
"""

# And running one, since a file can also leave HOME moved on the way out.
RUN_PROBE = """
import os, unittest
before = os.environ.get("HOME")
unittest.main(module=None, argv=["probe", "tests.test_tools"], exit=False)
print("BEFORE=" + str(before))
print("AFTER=" + str(os.environ.get("HOME")))
"""


def run(code, timeout=600):
    return subprocess.run([sys.executable, "-c", code], cwd=REPO,
                          capture_output=True, text=True, timeout=timeout,
                          env=dict(os.environ))


class SuiteIsolation(unittest.TestCase):
    def test_no_test_file_moves_home_at_import(self):
        """unittest imports them all first, so an import-time move hits everyone."""
        proc = run(IMPORT_PROBE)
        self.assertIn("DONE", proc.stdout,
                      f"the import probe did not finish:\n{proc.stdout}\n{proc.stderr[-2000:]}")
        leaks = [l for l in proc.stdout.splitlines() if l.startswith("LEAK=")]
        self.assertEqual(leaks, [], "importing these files moved HOME for every file after them")

    def test_running_the_tools_file_leaves_home_where_it_found_it(self):
        """The same fact on the way out, where a teardown would be the fix."""
        proc = run(RUN_PROBE)
        seen = {}
        for line in proc.stdout.splitlines():
            for tag in ("BEFORE=", "AFTER="):
                if line.startswith(tag):
                    seen[tag] = line[len(tag):]
        self.assertIn("BEFORE=", seen,
                      f"probe did not report HOME:\n{proc.stdout}\n{proc.stderr[-2000:]}")
        self.assertEqual(seen.get("BEFORE="), seen.get("AFTER="),
                         "tests/test_tools.py left HOME pointing at its throwaway directory")


if __name__ == "__main__":
    unittest.main(verbosity=2)
