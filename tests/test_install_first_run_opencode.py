#!/usr/bin/env python3
"""A first install gets past the opencode step, where neither opencode config exists yet.

Step 7 fingerprints the two configs opencode-web reads, to restart the server only when one
of them changed: `OC_SUM_BEFORE="$(oc_configs_sum)"`, whose body was `cat A B | sha256sum`.
On a box with nothing installed neither file exists, cat fails, pipefail makes the pipeline
fail, the assignment returns that status, and set -e ended the install there: every first
install of v1.18.7 stopped at "Install failed at line 1411" (the reference box, emptied for
the test, 2026-09-30). The suite never saw it, because every run of install.sh it makes
stops before step 1. switch-model.sh carried the same two lines.

These run each script's own lines under its own shell options, from an empty HOME.
"""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]


def lines(script):
    """The script's oc_configs_sum definition and the assignment that calls it first."""
    text = (REPO / script).read_text().splitlines()
    i = next(n for n, ln in enumerate(text) if ln.startswith("oc_configs_sum(){"))
    call = next(ln for ln in text[i + 1:] if '="$(oc_configs_sum)"' in ln)
    return text[i], call.strip()


def run(script, files=()):
    """(returncode, stdout) of the two lines under set -euo pipefail, from an empty HOME
    and an empty config dir holding `files` ({path relative to HOME: text})."""
    home = pathlib.Path(tempfile.mkdtemp(prefix="first-run-home-"))
    for rel, body in dict(files).items():
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text(body)
    define, call = lines(script)
    var = call.split("=", 1)[0]
    body = (f"set -euo pipefail\nCONFIG_DIR=\"$HOME/.config/qwen38\"\n{define}\n{call}\n"
            f'echo "REACHED ${var}"\n')
    r = subprocess.run(["bash", "-c", body], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(home)})
    return r.returncode, r.stdout


class TheFirstInstallGetsPastTheOpencodeStep(unittest.TestCase):
    def test_install_with_neither_config(self):
        rc, out = run("install.sh")
        self.assertEqual(rc, 0, "set -e ended the first install at the fingerprint")
        self.assertIn("REACHED ", out)

    def test_install_with_only_the_users_own_config(self):
        # opencode used before this repo: its own config is there, the repo's is not yet
        rc, out = run("install.sh", {".config/opencode/opencode.json": "{}\n"})
        self.assertEqual(rc, 0)

    def test_switch_with_one_config_gone(self):
        rc, out = run("switch-model.sh", {".config/qwen38/opencode.json": "{}\n"})
        self.assertEqual(rc, 0)


class TheFingerprintStillSeesAChange(unittest.TestCase):
    """What the fingerprint is for: a config that appears or changes reads different."""

    def sums(self, script):
        seen = []
        for files in ({}, {".config/qwen38/opencode.json": "{}\n"},
                      {".config/qwen38/opencode.json": "{}\n", ".config/opencode/opencode.json": "{}\n"},
                      {".config/qwen38/opencode.json": "{ }\n", ".config/opencode/opencode.json": "{}\n"}):
            rc, out = run(script, files)
            self.assertEqual(rc, 0)
            seen.append(out.split("REACHED ", 1)[1].strip())
        return seen

    def test_install(self):
        seen = self.sums("install.sh")
        self.assertEqual(len(set(seen)), 4, seen)

    def test_switch(self):
        seen = self.sums("switch-model.sh")
        self.assertEqual(len(set(seen)), 4, seen)


if __name__ == "__main__":
    unittest.main()
