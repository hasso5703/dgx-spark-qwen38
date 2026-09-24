#!/usr/bin/env python3
"""The installer's smoke generation says what went wrong, and knows a corrupt answer.

Three defects of the same few lines (found in review, 2026-09-24):
  - the request was a `curl | python3` pipe inside $(...) under set -euo pipefail, so a
    curl that failed (28, its timeout) ended the install in the ERR trap as "Install
    failed at line N", and the message that points at the journal never showed;
  - a kept engine can be in the middle of somebody's long prefill, which the smoke request
    waits behind, and 300 s failed an update that had nothing wrong with it;
  - any non-empty text passed, so a wall of "!" (token 0, this hardware's known decode
    corruption) ended in "Installed, verified". The smoke calls the engine directly,
    past the proxy's own tripwire.
The installer's lines run here against a curl stub.
"""
import json
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()
TRAP = next(ln for ln in TEXT.splitlines() if ln.startswith("trap ") and "ERR" in ln)
A = TEXT.index('echo "health OK, running a real generation smoke test..."')
START = TEXT.index("\n", A) + 1
# up to and including the smoke's last check (what follows restarts the proxy)
LAST = TEXT.index('die "Server is up but the smoke generation failed ($SMOKE)', START)
BLOCK = TEXT[START:TEXT.index("\n", LAST) + 1]


def answer(content):
    return json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]})


def run(mode, keep=0):
    d = pathlib.Path(tempfile.mkdtemp(prefix="smoke-"))
    bodies = {"ready": answer("READY"), "bang": answer("!" * 200), "empty": answer("")}
    (d / "body").write_text(bodies.get(mode, ""))
    (d / "curl").write_text("#!/bin/sh\necho \"$*\" > \"$(dirname \"$0\")/args\"\n"
                            + ("exit 28\n" if mode == "timeout" else "cat \"$(dirname \"$0\")/body\"\n"))
    (d / "curl").chmod(0o755)
    script = ("set -euo pipefail\n" + TRAP + '\ndie(){ printf "ERROR: %s\\n" "$*" >&2; exit 1; }\n'
              f"PORT=30000; KEY=k; SMOKE_MODEL=qwen3.8-27b; UNIT_NAME=qwen38-sglang.service; ENGINE_KEEP={keep}\n"
              + BLOCK + "echo SMOKE-PASSED\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{d}:/usr/bin:/bin"})
    args = (d / "args").read_text() if (d / "args").exists() else ""
    return r.returncode, r.stdout + r.stderr, args


class TheSmokeSaysWhatWentWrong(unittest.TestCase):
    def test_a_curl_that_times_out_points_at_the_journal(self):
        rc, out, _ = run("timeout")
        self.assertEqual(rc, 1)
        self.assertIn("curl exit 28", out)
        self.assertIn("journalctl -u qwen38-sglang.service", out)
        self.assertNotIn("Install failed at line", out)

    def test_a_wall_of_bangs_is_not_an_answer(self):
        rc, out, _ = run("bang")
        self.assertEqual(rc, 1)
        self.assertNotIn("SMOKE-PASSED", out)
        self.assertIn("run of '!'", out)

    def test_an_empty_answer_still_fails(self):
        rc, out, _ = run("empty")
        self.assertEqual(rc, 1)
        self.assertIn("smoke generation failed (EMPTY)", out)

    def test_a_real_answer_passes(self):
        rc, out, args = run("ready")
        self.assertEqual(rc, 0, out)
        self.assertIn("SMOKE-PASSED", out)
        self.assertIn("-m 300 ", args)

    def test_a_kept_engine_is_given_the_time_a_long_prefill_takes(self):
        rc, out, args = run("ready", keep=1)
        self.assertEqual(rc, 0, out)
        self.assertIn("-m 1800 ", args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
