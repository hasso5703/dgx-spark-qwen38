#!/usr/bin/env python3
"""install.sh names an engine that dies at load instead of waiting 20 minutes on it.

Every engine unit has Restart=always and RestartSec=15, which never reaches systemd's
default limit (5 starts in 10 s), so between two attempts `systemctl is-active` says
"activating", never "failed", and the wait loop's only crash check could not fire: a
unit dying at load got "still loading... be patient" for 20 minutes, with its journal
unread (found in review, 2026-09-24). systemd counts the relaunches in NRestarts, and on
the reference box's systemd 255 a manual restart does not zero that count, so the loop
compares against the count right after the restart.

These run the installer's own lines against a fake systemctl whose NRestarts the test
moves, and a fake journalctl.
"""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()


def lines(start, end):
    i = TEXT.index(start)
    return TEXT[i:TEXT.index(end, i)]


FUNC = next(ln for ln in TEXT.splitlines() if ln.startswith("engine_restarts() {"))
BASE = next(ln for ln in TEXT.splitlines() if ln.startswith('ENGINE_RESTARTS0="$(engine_restarts)"'))
CHECK = lines('  NOW_RESTARTS="$(engine_restarts)"', "\n  fi\n") + "\n  fi\n"


def run(counts):
    """counts: NRestarts answered by each successive `systemctl show` call."""
    t = pathlib.Path(tempfile.mkdtemp(prefix="crash-loop-"))
    (t / "counts").write_text("\n".join(map(str, counts)) + "\n")
    (t / "systemctl").write_text(
        "#!/bin/sh\nf=\"$COUNTS\"; n=$(head -1 \"$f\"); tail -n +2 \"$f\" > \"$f.new\"; "
        "[ -s \"$f.new\" ] && mv \"$f.new\" \"$f\"; echo \"$n\"\n")
    (t / "journalctl").write_text("#!/bin/sh\necho 'the last lines of the dead engine'\n")
    for n in ("systemctl", "journalctl"):
        (t / n).chmod(0o755)
    script = ("set -euo pipefail\ndie(){ echo \"DIE: $*\"; exit 1; }\nUNIT_NAME=qwen38-sglang.service\n"
              + FUNC + "\n" + BASE + "\n" + CHECK + "echo STILL-WAITING\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{t}:/usr/bin:/bin", "COUNTS": str(t / "counts")})
    return r.returncode, r.stdout + r.stderr


class ACrashLoopIsNamed(unittest.TestCase):
    def test_a_relaunch_after_the_restart_ends_the_wait_with_the_journal(self):
        rc, out = run([2, 3])            # 2 before this boot, one more relaunch since
        self.assertEqual(rc, 1, out)
        self.assertIn("died during startup and systemd is relaunching it", out)
        self.assertIn("1 relaunch(es)", out)
        self.assertIn("the last lines of the dead engine", out)

    def test_an_old_count_from_before_the_restart_is_not_a_crash(self):
        rc, out = run([2, 2])            # a manual restart did not zero it
        self.assertEqual(rc, 0, out)
        self.assertIn("STILL-WAITING", out)

    def test_it_sits_inside_the_wait_loop_after_the_restart(self):
        self.assertLess(TEXT.index(BASE), TEXT.index('for i in $(seq 1 150); do'))
        self.assertLess(TEXT.index('for i in $(seq 1 150); do'), TEXT.index(CHECK.strip().splitlines()[0]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
