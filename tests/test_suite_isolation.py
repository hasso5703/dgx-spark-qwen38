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
import json
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

# And running each one, in a process of its own, since a file can also leave the
# environment changed on the way out: a teardown is the fix there. Only
# tests/test_tools.py was run, and only HOME compared, while four files left UPSTREAM,
# KEEPALIVE_S and the rest set for every module after them (found in review, 2026-09-24).
RUN_PROBE = """
import json, os, sys, unittest
sys.path.insert(0, os.getcwd())
before = dict(os.environ)
suite = unittest.defaultTestLoader.loadTestsFromName("tests." + sys.argv[1])
with open(os.devnull, "w") as null:
    unittest.TextTestRunner(stream=null, verbosity=0).run(suite)
after = dict(os.environ)
print("CHANGED=" + json.dumps(sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))))
"""


def modules():
    """Every unittest module of tests/ but this one."""
    return [p.stem for p in sorted((REPO / "tests").glob("test_*.py"))
            if p.stem != "test_suite_isolation" and "def test_" in p.read_text()]


def run(code, *args, timeout=600):
    return subprocess.run([sys.executable, "-c", code, *args], cwd=REPO,
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

    def test_no_test_file_leaves_the_environment_changed(self):
        """The same fact on the way out, for every file and every variable."""
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            procs = dict(zip(modules(), pool.map(lambda m: run(RUN_PROBE, m, timeout=1200), modules())))
        leaks = {}
        for name, proc in procs.items():
            line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("CHANGED=")), None)
            self.assertIsNotNone(line, f"the probe of {name} did not finish:\n{proc.stderr[-2000:]}")
            changed = json.loads(line[len("CHANGED="):])
            if changed:
                leaks[name] = changed
        self.assertEqual(leaks, {}, "these files left environment variables changed for the files after them")

if __name__ == "__main__":
    unittest.main(verbosity=2)
