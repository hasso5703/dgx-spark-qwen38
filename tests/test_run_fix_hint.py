#!/usr/bin/env python3
"""The command run.sh offers to restore native configs is one install.sh accepts.

run.sh refuses to start on YaRN-patched configs and names the fix. It named
`CONTEXT_MODE=native ./install.sh --no-service --no-start`, and install.sh refuses
--no-service with --no-start, so the way out it offered was itself refused (found in
review, 2026-09-24). This runs the command's flags through install.sh's own flag parser.
"""
import pathlib
import re
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
RUN = (REPO / "run.sh").read_text()


class TheHintIsAccepted(unittest.TestCase):
    def test_the_offered_flags_pass_install_sh_parsing(self):
        prep = re.search(r'^PREP="\./install\.sh ([^"]*)"', RUN, re.M).group(1).split()
        hint = re.search(r'restore the native configs with: CONTEXT_MODE=native \$PREP([^"]*)"', RUN).group(1).split()
        flags = prep + hint
        self.assertNotIn("--no-start", flags, "install.sh refuses --no-service with --no-start")
        # and install.sh itself does not refuse them, at the flag stage (stopped right after)
        text = (REPO / "install.sh").read_text()
        cut = text.index('step "1/10 Preflight checks"')
        d = pathlib.Path(tempfile.mkdtemp(prefix="run-hint-"))
        (d / "install.sh").write_text(text[:cut] + 'echo "FLAGS OK"; exit 0\n')
        (d / "install.sh").chmod(0o755)
        r = subprocess.run([str(d / "install.sh"), *flags], capture_output=True, text=True, timeout=60,
                           cwd=str(REPO), env={"PATH": "/usr/bin:/bin", "HOME": str(d),
                                               "CONTEXT_MODE": "native", "MODEL_CHOICE": "stock"})
        self.assertIn("FLAGS OK", r.stdout, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
