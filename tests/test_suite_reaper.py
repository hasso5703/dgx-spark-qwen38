#!/usr/bin/env python3
"""tests/suite_reaper.py tells what a command leaves running from what others do.

The offline suite's "no listening socket left behind" compared every listener of
the machine before and after the suite, and failed twice on the reference box on
2026-10-03 for ports VS Code opened while the suite ran. The reaper judges the
command's own processes only, found through the subreaper bit rather than an
environment variable or a session, which a test's subprocess is free to drop.
"""
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
REAPER = str(REPO / "tests" / "suite_reaper.py")

# A listener that writes its port to a file, then waits to be stopped.
LISTENER = ("import socket, sys, time; s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(); "
            "open(sys.argv[1], 'w').write(str(s.getsockname()[1])); time.sleep(60)")


def reaper(command, grace="1"):
    return subprocess.run([sys.executable, REAPER, "bash", "-c", command], capture_output=True,
                          text=True, timeout=60, env=dict(os.environ, SUITE_REAPER_GRACE_S=grace))


def refused(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    except ConnectionRefusedError:
        return True
    return False


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


class TheCommandsOwnLeftovers(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.portfile = os.path.join(self.dir.name, "port")
        self.addCleanup(self.dir.cleanup)

    def wait_for_port(self):
        return f'while [ ! -s "{self.portfile}" ]; do sleep 0.05; done'

    def test_a_command_that_leaves_nothing_keeps_its_own_exit_status(self):
        r = reaper("exit 0")
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertEqual(reaper("exit 7").returncode, 7)

    def test_an_orphaned_listener_is_named_with_its_port_then_stopped(self):
        r = reaper(f'{sys.executable} -c "{LISTENER}" "{self.portfile}" & {self.wait_for_port()}')
        port = int(pathlib.Path(self.portfile).read_text())
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("left a listening socket behind", r.stdout)
        self.assertIn(f"127.0.0.1:{port} held by pid", r.stdout)
        self.assertTrue(refused(port), "the listener must be stopped once reported")

    def test_a_new_session_and_an_empty_environment_do_not_hide_it(self):
        # what an environment marker or a session id would have missed
        r = reaper(f'setsid env -i {sys.executable} -c "{LISTENER}" "{self.portfile}" & '
                   f'{self.wait_for_port()}')
        port = int(pathlib.Path(self.portfile).read_text())
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn(f"127.0.0.1:{port} held by pid", r.stdout)
        self.assertTrue(refused(port))

    def test_a_process_left_running_without_a_socket_is_named_then_stopped(self):
        pidfile = os.path.join(self.dir.name, "pid")
        r = reaper(f'sleep 60 & echo $! > "{pidfile}"')
        pid = int(pathlib.Path(pidfile).read_text())
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn(f"pid {pid}: sleep 60", r.stdout)
        self.assertNotIn("listening socket", r.stdout)
        self.assertTrue(gone(pid))

    def test_a_process_on_its_way_out_is_given_the_grace(self):
        r = reaper("(sleep 0.3) & exit 0", grace="3")
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_another_programs_listener_is_not_the_commands(self):
        # the VS Code case: a listener that appears while the command runs, held by a
        # process the command did not start
        other = subprocess.Popen([sys.executable, "-c", LISTENER, self.portfile])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        deadline = time.monotonic() + 10
        while not os.path.exists(self.portfile) or not pathlib.Path(self.portfile).read_text():
            self.assertLess(time.monotonic(), deadline, "the other listener never started")
            time.sleep(0.05)
        r = reaper("sleep 0.5")
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertIsNone(other.poll(), "the reaper must leave other programs alone")

    def test_no_command_is_a_usage_error(self):
        r = subprocess.run([sys.executable, REAPER], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertIn("Usage:", r.stderr)

    def test_a_command_that_cannot_start_says_so(self):
        r = subprocess.run([sys.executable, REAPER, "/nonexistent/command"], capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(r.returncode, 127)
        self.assertIn("cannot run /nonexistent/command", r.stderr)


if __name__ == "__main__":
    unittest.main()
