#!/usr/bin/env python3
"""uninstall.sh --yes must not leave opencode unable to start.

The providers install.sh writes read the API key through a {file:} reference, and
opencode refuses to start at all, every provider included, while one points at a file
that is gone ("bad file reference", opencode 1.18.32, reference box 2026-09-23). --yes
deletes that file, and since v1.18.4 every fresh box gets such a config. These run the
uninstaller's own lines, as written, in a throwaway HOME."""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
UNINSTALL = REPO / "uninstall.sh"


def purge_block() -> str:
    text = UNINSTALL.read_text()
    start = text.index('if [ "$PURGE_CONFIG" -eq 1 ]; then\n  # opencode refuses')
    end = text.index("\nfi\n", text.index("they answer again after ./install.sh", start)) + 4
    return text[start:end]


def box(key):
    return {"npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": "http://127.0.0.1:30001/v1", "apiKey": "{file:" + key + "}"},
            "models": {"qwen3.8-27b": {"limit": {"context": 1000, "input": 1000, "output": 100}}}}


class UninstallLeavesOpencodeStartable(unittest.TestCase):
    def run_it(self, user_cfg, purge):
        home = pathlib.Path(tempfile.mkdtemp(prefix="un-oc-"))
        cfg = home / ".config" / "qwen38"
        cfg.mkdir(parents=True)
        (cfg / "api-key").write_text("k\n")
        oc = home / ".config" / "opencode" / "opencode.json"
        if user_cfg is not None:
            oc.parent.mkdir(parents=True)
            oc.write_text(user_cfg(str(cfg / "api-key")))
        cfgs = " ".join(f'"{oc.parent / n}"' for n in ("config.json", "opencode.json", "opencode.jsonc"))
        script = (f'set -euo pipefail\nPURGE_CONFIG={purge}; CONFIG_DIR="{cfg}"; OC_USER_CFGS=({cfgs}); '
                  f'REPO_DIR="{REPO}"\n' + purge_block())
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                           env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout + r.stderr, home, cfg, oc

    def test_the_copy_install_sh_made_goes_with_the_key(self):
        mine = lambda k: json.dumps({"$schema": "https://opencode.ai/config.json", "autoupdate": "notify",
                                     "provider": {"qwen38": box(k)}, "model": "qwen38/qwen3.8-27b",
                                     "small_model": "qwen38/qwen3.8-27b"}, indent=2)
        out, home, cfg, oc = self.run_it(mine, purge=1)
        self.assertFalse(cfg.exists())
        self.assertFalse(oc.exists())
        self.assertIn("held only this box's providers (qwen38): moved to", out)
        self.assertEqual(len(list(oc.parent.glob("opencode.json.bak-*"))), 1, "no backup was kept")

    def test_a_users_own_config_keeps_everything_but_the_box(self):
        theirs = lambda k: json.dumps({"model": "qwen38/qwen3.8-27b", "plugin": ["x.js"],
                                       "provider": {"qwen38": box(k), "anthropic": {}}})
        out, home, cfg, oc = self.run_it(theirs, purge=1)
        self.assertFalse(cfg.exists())
        self.assertEqual(json.loads(oc.read_text()), {"plugin": ["x.js"], "provider": {"anthropic": {}}})
        self.assertIn("removed the qwen38 provider", out)

    def test_kept_config_says_the_providers_wait_for_a_reinstall(self):
        theirs = lambda k: json.dumps({"provider": {"qwen38": box(k)}})
        out, home, cfg, oc = self.run_it(theirs, purge=0)
        self.assertTrue((cfg / "api-key").exists())
        self.assertIn('"qwen38"', oc.read_text())
        self.assertIn("still lists this box's providers", out)

    def test_a_block_pasted_into_opencodes_own_jsonc_goes_too(self):
        # opencode writes opencode.jsonc itself on its first start, so that is where a
        # user following "merge the qwen38 block" may well have put it.
        home = pathlib.Path(tempfile.mkdtemp(prefix="un-oc-"))
        cfg = home / ".config" / "qwen38"
        cfg.mkdir(parents=True)
        (cfg / "api-key").write_text("k\n")
        jsonc = home / ".config" / "opencode" / "opencode.jsonc"
        jsonc.parent.mkdir(parents=True)
        jsonc.write_text('{\n  // mine\n  "provider": {"qwen38": ' + json.dumps(box(str(cfg / "api-key")))
                         + ', "x": {}}\n}\n')
        cfgs = " ".join(f'"{jsonc.parent / n}"' for n in ("config.json", "opencode.json", "opencode.jsonc"))
        r = subprocess.run(["bash", "-c", f'set -euo pipefail\nPURGE_CONFIG=1; CONFIG_DIR="{cfg}"; '
                            f'OC_USER_CFGS=({cfgs}); REPO_DIR="{REPO}"\n' + purge_block()],
                           capture_output=True, text=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("qwen38", jsonc.read_text())
        self.assertIn("// mine", jsonc.read_text())

    def test_a_cache_the_container_wrote_as_root_is_removed_quietly(self):
        # A directory the user cannot write stands for the root-owned compile cache: the
        # plain rm fails on it, and the fallback (a fake sudo here) removes the rest.
        home = pathlib.Path(tempfile.mkdtemp(prefix="un-cache-"))
        cfg = home / ".config" / "qwen38"
        locked = cfg / "sglang-cache" / "inductor" / "yl"
        locked.mkdir(parents=True)
        (locked / "c.py").write_text("x")
        locked.chmod(0o555)
        fake = home / "bin"
        fake.mkdir()
        (fake / "sudo").write_text('#!/bin/sh\necho "sudo $*" >> "$HOME/sudo.log"\n'
                                   'chmod -R u+w "$4" && exec "$@"\n')
        (fake / "sudo").chmod(0o755)
        cfgs = " ".join(f'"{home / ".config" / "opencode" / n}"' for n in ("config.json", "opencode.json", "opencode.jsonc"))
        r = subprocess.run(["bash", "-c", f'set -euo pipefail\nPURGE_CONFIG=1; CONFIG_DIR="{cfg}"; '
                            f'OC_USER_CFGS=({cfgs}); REPO_DIR="{REPO}"\n' + purge_block()],
                           capture_output=True, text=True, env={"HOME": str(home), "PATH": f"{fake}:/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(cfg.exists())
        self.assertNotIn("Permission", r.stdout + r.stderr)
        self.assertEqual((home / "sudo.log").read_text(), f"sudo rm -rf --one-file-system {cfg}\n")

    def test_no_opencode_config_is_no_opencode_line(self):
        out, home, cfg, oc = self.run_it(None, purge=1)
        self.assertEqual(out.strip(), "config removed.")

    @unittest.skipUnless(shutil.which("opencode") or os.path.exists(os.path.expanduser("~/.opencode/bin/opencode")),
                         "needs a real opencode to show it starts")
    def test_the_real_opencode_starts_after_the_purge(self):
        theirs = lambda k: json.dumps({"provider": {"qwen38": box(k), "other": {"npm": "@ai-sdk/openai-compatible",
                                       "options": {"baseURL": "http://127.0.0.1:9/v1"}, "models": {"m": {}}}}})
        out, home, cfg, oc = self.run_it(theirs, purge=1)
        binary = shutil.which("opencode") or os.path.expanduser("~/.opencode/bin/opencode")
        r = subprocess.run([binary, "models", "other"], capture_output=True, text=True, timeout=120,
                           cwd=str(home), env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
        self.assertNotIn("bad file reference", r.stdout + r.stderr)
        self.assertIn("other/m", r.stdout, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
