#!/usr/bin/env python3
"""Secrets do not ride on a command line, where any local user reads them in /proc.

The download's docker run carried `-e HF_TOKEN=<token>`, and the smoke test's curl carried
`-H "Authorization: Bearer <key>"` (found in review, 2026-09-24). The token is passed by
name now (docker reads it from its own environment), and the key as a header file."""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = {name: (REPO / name).read_text() for name in ("install.sh", "switch-model.sh")}


class NoSecretOnACommandLine(unittest.TestCase):
    def test_no_value_is_spelled_on_a_docker_or_curl_line(self):
        for name, text in SCRIPTS.items():
            self.assertNotIn('-e HF_TOKEN="$HF_TOKEN"', text, name)
            self.assertNotIn('-H "Authorization: Bearer $', text, name)

    def test_the_token_reaches_docker_by_name(self):
        for name, text in SCRIPTS.items():
            i = text.index("DL_TOKEN_ARGS=()")
            block = text[i:text.index("\n", text.index("DL_TOKEN_ARGS=(-e HF_TOKEN)", i)) + 1]
            d = pathlib.Path(tempfile.mkdtemp(prefix="argv-"))
            (d / "docker").write_text('#!/bin/sh\necho "ARGV $*"\necho "ENV ${HF_TOKEN:-unset}"\n')
            (d / "docker").chmod(0o755)
            script = "set -euo pipefail\n" + block + 'docker run "${DL_TOKEN_ARGS[@]}" image\n'
            out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                                 env={"PATH": f"{d}:/usr/bin:/bin", "HF_TOKEN": "hf_secret"}).stdout
            self.assertIn("ARGV run -e HF_TOKEN image", out, name)
            self.assertNotIn("hf_secret", out.split("ENV")[0], f"{name}: the token is in docker's argv")
            self.assertIn("ENV hf_secret", out, f"{name}: docker does not see the token")


if __name__ == "__main__":
    unittest.main(verbosity=2)
