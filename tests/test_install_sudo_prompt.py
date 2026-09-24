#!/usr/bin/env python3
"""The installer asks for the sudo password when a terminal can take it.

`sudo -n true || die` never prompts. On a fresh box where sudo wants a password (DGX OS
does), the one-liner died at step 8, after ~20 min of pulls, with "run sudo -v": nothing
before step 8 ever called sudo, and no page said to run it first (found in review,
2026-09-24; the reference box never showed it, because the installs there ran with an
askpass helper). Checked on that box's sudo: with a terminal, `sudo -v` asks and waits;
without one it fails at once ("a terminal is required"), so a background run still gets
the named refusal instead of a prompt nobody will answer.

These run the installer's own lines, as written, against a fake sudo that records its
calls: the ticket check at the end of step 1, before any download, and the one at step 8,
which renews a ticket that expired during the downloads (sudo keeps one 15 min).
"""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()

FAKE_SUDO = """#!/bin/sh
echo "sudo $*" >> "$SUDO_LOG"
case "$1" in
  -n) exit "$TICKET" ;;
  -v) exit "$ANSWER" ;;
esac
exit 0
"""


def block(start, end):
    i = TEXT.index(start)
    return TEXT[i:TEXT.index(end, i)]


def run(snippet, ticket, answer, no_service=0):
    """(rc, output, sudo calls). ticket/answer: the exit codes of `sudo -n true` and `sudo -v`."""
    t = pathlib.Path(tempfile.mkdtemp(prefix="sudo-prompt-"))
    (t / "sudo").write_text(FAKE_SUDO)
    (t / "sudo").chmod(0o755)
    log = t / "calls"
    script = ("set -euo pipefail\n"
              "die(){ echo \"DIE: $*\"; exit 1; }\n"
              f"NO_SERVICE={no_service}\n" + snippet + "\necho REACHED\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{t}:/usr/bin:/bin", "SUDO_LOG": str(log),
                            "TICKET": str(ticket), "ANSWER": str(answer)})
    calls = log.read_text().splitlines() if log.exists() else []
    return r.returncode, r.stdout + r.stderr, calls


STEP8 = next(line for line in TEXT.splitlines() if line.startswith("sudo -n true"))
STEP1 = block('if [ "$NO_SERVICE" -eq 0 ] && ! sudo -n true 2>/dev/null; then', 'echo "OK (aarch64')


class StepOne(unittest.TestCase):
    def test_it_asks_before_the_downloads_when_there_is_no_ticket(self):
        rc, out, calls = run(STEP1, ticket=1, answer=0)
        self.assertEqual(rc, 0, out)
        self.assertIn("asks for your password now, before the downloads", out)
        self.assertEqual(calls, ["sudo -n true", "sudo -v"])

    def test_it_says_nothing_when_the_ticket_is_valid(self):
        rc, out, calls = run(STEP1, ticket=0, answer=1)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("password", out)
        self.assertEqual(calls, ["sudo -n true"])

    def test_without_a_terminal_it_refuses_before_anything_is_pulled(self):
        rc, out, _ = run(STEP1, ticket=1, answer=1)
        self.assertEqual(rc, 1)
        self.assertIn("run 'sudo -v' in this terminal", out)

    def test_no_service_never_touches_sudo(self):
        rc, out, calls = run(STEP1, ticket=1, answer=1, no_service=1)
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls, [])

    def test_it_sits_before_the_first_download(self):
        self.assertLess(TEXT.index(STEP1), TEXT.index('step "2/10'))
        self.assertLess(TEXT.index('step "1/10'), TEXT.index(STEP1))


class StepEight(unittest.TestCase):
    def test_a_valid_ticket_goes_straight_on(self):
        rc, out, calls = run(STEP8, ticket=0, answer=1)
        self.assertEqual(rc, 0, out)
        self.assertIn("REACHED", out)
        self.assertEqual(calls, ["sudo -n true"])

    def test_an_expired_ticket_is_renewed_with_a_prompt(self):
        # the downloads take longer than sudo's 15 min ticket on a fresh box
        rc, out, calls = run(STEP8, ticket=1, answer=0)
        self.assertEqual(rc, 0, out)
        self.assertIn("REACHED", out)
        self.assertEqual(calls, ["sudo -n true", "sudo -v"])

    def test_no_terminal_is_still_a_named_refusal(self):
        rc, out, _ = run(STEP8, ticket=1, answer=1)
        self.assertEqual(rc, 1)
        self.assertIn("sudo -v", out)
        self.assertNotIn("REACHED", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
