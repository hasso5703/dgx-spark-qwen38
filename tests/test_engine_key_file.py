#!/usr/bin/env python3
"""The engine's key is on no command line.

Every lane passed `--api-key "$(cat .../api-key)"`, so the key was in the argv of the
docker client and of the server process, which any local user reads in /proc (found in
review, 2026-09-24). The units, the flash launcher and run.sh now hand the server
`--config /out/engine-secrets.yaml`, which SGLang merges into its arguments in memory
(checked in both serving images), and engine-secrets.sh writes that file from the api-key
file before every start, so a key changed by hand still reaches the next start."""
import os
import pathlib
import subprocess
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
HELPER = REPO / "engine-secrets.sh"
LAUNCHERS = ("qwen38-sglang.service.template", "qwen38-sglang-1m.service.template",
             "qwen38-flash-launch.sh.template", "run.sh")


def helper(key):
    d = pathlib.Path(tempfile.mkdtemp(prefix="engine-key-"))
    (d / "api-key").write_text(key)
    r = subprocess.run(["bash", str(HELPER), str(d)], capture_output=True, text=True, timeout=30)
    return r, d


class TheKeyFile(unittest.TestCase):
    def test_every_launcher_passes_the_file_and_no_key(self):
        for name in LAUNCHERS:
            text = (REPO / name).read_text()
            self.assertNotIn("--api-key", text.replace("# ", ""), name)
            self.assertIn("--config /out/engine-secrets.yaml", text, name)
            self.assertIn("engine-secrets.sh", text, name)

    def test_the_file_is_written_before_the_server_starts(self):
        for name in ("qwen38-sglang.service.template", "qwen38-sglang-1m.service.template"):
            text = (REPO / name).read_text()
            self.assertIn("ExecStartPre=/bin/bash __HOME__/.config/qwen38/engine-secrets.sh\n", text, name)
        flash = (REPO / "qwen38-flash-launch.sh.template").read_text()
        self.assertLess(flash.index("/bin/bash __HOME__/.config/qwen38/engine-secrets.sh"),
                        flash.index("exec /usr/bin/docker run"))
        install = (REPO / "install.sh").read_text()
        self.assertIn('install -m 755 "$REPO_DIR/engine-secrets.sh" "$CONFIG_DIR/engine-secrets.sh"', install)
        self.assertIn('bash "$CONFIG_DIR/engine-secrets.sh"', install)

    def test_the_helper_writes_a_private_yaml_once(self):
        r, d = helper("Abc123xyz\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        f = d / "engine-secrets.yaml"
        self.assertEqual(f.read_text(), 'api-key: "Abc123xyz"\n')
        self.assertEqual(os.stat(f).st_mode & 0o777, 0o600)
        before = os.stat(f).st_mtime_ns
        time.sleep(0.05)
        subprocess.run(["bash", str(HELPER), str(d)], check=True, timeout=30)
        self.assertEqual(os.stat(f).st_mtime_ns, before, "the same key rewrote the file: the engine would restart")

    def test_a_key_the_yaml_cannot_carry_is_refused(self):
        for bad in ("", 'ab"c', "ab\\c", "ab\tc"):
            r, d = helper(bad)
            self.assertNotEqual(r.returncode, 0, repr(bad))
            self.assertFalse((d / "engine-secrets.yaml").exists(), repr(bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
