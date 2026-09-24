#!/usr/bin/env python3
"""The image lane's smoke generation says why it failed, transport failures included.

A curl that failed at the transport (the lane dying mid-request, the 300 s passing) made
the assignment of its status code fail, and set -e ended install-image.sh before the
message and the journal meant for that case (found in review, 2026-09-24). This runs the
script's own lines with curl and sudo replaced by stubs."""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install-image.sh").read_text()
START = TEXT.index('OUT="$(mktemp)"')
BLOCK = TEXT[START:TEXT.index("\nfi\n", TEXT.index("did not make the smoke image", START)) + 4]


def run(curl_body):
    d = pathlib.Path(tempfile.mkdtemp(prefix="img-smoke-"))
    (d / "curl").write_text(curl_body)
    (d / "sudo").write_text('#!/bin/sh\necho "SUDO $*"\n')
    for f in ("curl", "sudo"):
        (d / f).chmod(0o755)
    script = ("set -euo pipefail\ndie(){ echo \"DIE: $*\"; exit 1; }\n"
              "IMAGE_BIND=127.0.0.1; PORT=30020; UNIT=qwen38-image.service\n" + BLOCK + "\necho SURVIVED\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{d}:/usr/bin:/bin"})
    return r.stdout + r.stderr


class TheSmokeSaysWhy(unittest.TestCase):
    def test_a_transport_failure_prints_the_journal_and_the_reason(self):
        out = run("#!/bin/sh\nprintf 000\nexit 28\n")
        self.assertIn("HTTP 000", out)
        self.assertIn("SUDO journalctl -u qwen38-image.service", out)
        self.assertIn("DIE: the lane started but did not make the smoke image", out)

    def test_a_refusal_prints_them_too(self):
        out = run("#!/bin/sh\nwhile [ \"$1\" != -o ]; do shift; done\necho '{\"detail\":\"boom\"}' > \"$2\"\nprintf 500\n")
        self.assertIn("HTTP 500", out)
        self.assertIn("boom", out)
        self.assertIn("DIE:", out)

    def test_a_200_goes_on(self):
        self.assertIn("SURVIVED", run("#!/bin/sh\nprintf 200\n"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
