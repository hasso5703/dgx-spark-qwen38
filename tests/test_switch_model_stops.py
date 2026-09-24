#!/usr/bin/env python3
"""A switch the cockpit stops takes its download with it.

The cockpit stops a job that overruns its timeout with a TERM to the job's whole process
group (since v1.18.7; it used to kill only the job's own process). For a switch, that
group holds switch-model.sh and the `docker run` that downloads the checkpoint, and
docker run passes the TERM on to its container, where python3 is PID 1: a PID 1 with no
handler for a signal ignores it. Measured on the reference box, 2026-09-24, with the
pinned flash image: without --init the container outlived the TERM, then a SIGKILL of
its docker client, and went on with nobody attached; with --init both were gone within
6 s. This keeps the flag on every container switch-model.sh runs.
"""
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "switch-model.sh").read_text()


class EveryContainerOfASwitchCanBeStopped(unittest.TestCase):
    def test_every_docker_run_has_an_init(self):
        # a command, not a comment that names one: at the start of a line
        runs = [m.group(0) for m in re.finditer(r"(?m)^[ \t]*docker run(?:[^\n]*\\\n)*[^\n]*", TEXT)]
        self.assertTrue(runs, "switch-model.sh runs no container any more: this test is stale")
        for run in runs:
            self.assertIn("--init", run.split("--entrypoint")[0], run[:160])


if __name__ == "__main__":
    unittest.main(verbosity=2)
