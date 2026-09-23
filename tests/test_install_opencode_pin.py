#!/usr/bin/env python3
"""install.sh puts the opencode version this repo tests on every box, and only that.

The repo tunes four things to opencode's own behaviour (the compaction threshold, the
hidden 32,000-token output cap, the overflow phrases the proxy answers with, --yolo and
OPENCODE_PERMISSION for the Agent tab), all read out of one version's binary. Before the
pin, the installer took whatever opencode was on PATH, and opencode installs its own
patch releases, so two boxes installed a week apart ran two versions (the reference box
sat on 1.18.27 while 1.18.32 was out). These run the installer's own lines, as written,
in a throwaway HOME, against a fake curl and fake opencode binaries.
"""
import hashlib
import io
import os
import pathlib
import subprocess
import tarfile
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
INSTALL = REPO / "install.sh"
PINNED = "1.18.32"

FAKE_OC = """#!/bin/sh
here="$(cd "$(dirname "$0")" && pwd)"
case "$1" in
  --version) cat "$here/VERSION" ;;
  upgrade) echo "upgrade $2" >> "$OC_LOG"; printf '%s\\n' "$2" > "$here/VERSION" ;;
esac
"""
FAKE_CURL = """#!/bin/sh
out=""; url=""
while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2 ;; -m|--retry) shift 2 ;; -*) shift ;; *) url="$1"; shift ;; esac; done
echo "curl $url" >> "$OC_LOG"
cp "$OC_TARBALL" "$out"
"""


def block() -> str:
    text = INSTALL.read_text()
    start = text.index('OC_HOME_BIN="$HOME/.opencode/bin/opencode"')
    tail = text.index('echo "added ~/.opencode/bin to your PATH', start)
    end = text.index("\n  fi\nfi\n", tail) + len("\n  fi\nfi\n")
    return text[start:end]


def pinned_default() -> str:
    import re
    return re.search(r'^OPENCODE_VERSION="\$\{OPENCODE_VERSION:-([0-9.]+)\}"', INSTALL.read_text(), re.M).group(1)


class TheOpencodePin(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="oc-pin-"))
        self.home = self.t / "home"
        self.home.mkdir()
        (self.home / ".bashrc").write_text("# a user's bashrc\n")
        self.bin = self.t / "fakebin"
        self.bin.mkdir()
        (self.bin / "curl").write_text(FAKE_CURL)
        (self.bin / "curl").chmod(0o755)
        self.log = self.t / "calls.log"
        # the release asset: one "opencode" at the top of the archive, like the real one
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            body = f"#!/bin/sh\n[ \"$1\" = --version ] && echo {PINNED}\n".encode()
            info = tarfile.TarInfo("opencode"); info.size = len(body); info.mode = 0o755
            tf.addfile(info, io.BytesIO(body))
        self.tarball = self.t / "asset.tar.gz"
        self.tarball.write_bytes(buf.getvalue())
        self.sha = hashlib.sha256(buf.getvalue()).hexdigest()

    def opencode_at(self, where: pathlib.Path, version: str) -> pathlib.Path:
        where.mkdir(parents=True, exist_ok=True)
        (where / "opencode").write_text(FAKE_OC)
        (where / "opencode").chmod(0o755)
        (where / "VERSION").write_text(version + "\n")
        return where / "opencode"

    def run_block(self, *, path_extra=(), pin="1", sha=None, env_version="", env_sha=""):
        script = ("set -euo pipefail\ndie(){ echo \"DIE: $*\"; exit 1; }\n"
                  f'OPENCODE_VERSION="{PINNED}"; OPENCODE_SHA256="{sha or self.sha}"; OPENCODE_PIN="{pin}"\n'
                  f'_ENV_OPENCODE_VERSION="{env_version}"; _ENV_OPENCODE_SHA256="{env_sha}"\n'
                  + block() + 'echo "PATH_AFTER=$PATH"\n')
        path = ":".join([str(self.bin), *map(str, path_extra), "/usr/bin", "/bin"])
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                           env={"HOME": str(self.home), "PATH": path, "OC_LOG": str(self.log),
                                "OC_TARBALL": str(self.tarball)})
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        return r.returncode, r.stdout + r.stderr, calls

    @property
    def home_bin(self):
        return self.home / ".opencode" / "bin" / "opencode"

    def version_of(self, binary):
        return subprocess.run([str(binary), "--version"], capture_output=True, text=True).stdout.strip()

    def test_the_default_pin_is_the_version_these_tests_hold(self):
        self.assertEqual(pinned_default(), PINNED)

    def test_absent_it_installs_the_release_checked_against_its_sha256(self):
        rc, out, calls = self.run_block()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.version_of(self.home_bin), PINNED)
        self.assertEqual(calls, [f"curl https://github.com/anomalyco/opencode/releases/download/v{PINNED}"
                                 "/opencode-linux-arm64.tar.gz"])
        self.assertIn(".opencode/bin", (self.home / ".bashrc").read_text())
        self.assertIn(f"{self.home}/.opencode/bin", out.split("PATH_AFTER=")[1])

    def test_the_pinned_version_is_left_alone(self):
        self.opencode_at(self.home / ".opencode" / "bin", PINNED)
        rc, out, calls = self.run_block()
        self.assertEqual(rc, 0, out)
        self.assertIn("the version this repo tests", out)
        self.assertEqual(calls, [])

    def test_an_older_one_where_the_installer_puts_it_is_replaced_the_same_way(self):
        self.opencode_at(self.home / ".opencode" / "bin", "1.18.27")
        rc, out, calls = self.run_block()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.version_of(self.home_bin), PINNED)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("curl "))
        self.assertNotIn("installed by dgx-spark-qwen38", (self.home / ".bashrc").read_text(),
                         "the user's PATH line was theirs already; nothing to add")

    def test_an_older_one_from_npm_goes_through_opencodes_own_upgrader(self):
        npm_bin = self.opencode_at(self.t / "npm" / "bin", "1.18.20")
        rc, out, calls = self.run_block(path_extra=[npm_bin.parent])
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls, [f"upgrade {PINNED}"])
        self.assertEqual(self.version_of(npm_bin), PINNED)
        self.assertFalse(self.home_bin.exists(), "a second copy was installed beside the npm one")

    def test_a_newer_one_is_kept_and_said_so(self):
        self.opencode_at(self.home / ".opencode" / "bin", "1.18.40")
        rc, out, calls = self.run_block()
        self.assertEqual(rc, 0, out)
        self.assertIn("newer than the", out)
        self.assertEqual(calls, [])
        self.assertEqual(self.version_of(self.home_bin), "1.18.40")

    def test_a_download_that_does_not_match_its_sha256_is_not_installed(self):
        self.opencode_at(self.home / ".opencode" / "bin", "1.18.27")
        rc, out, _ = self.run_block(sha="0" * 64)
        self.assertEqual(rc, 0, out)                              # opencode is one integration
        self.assertIn("does not match its pinned sha256", out)
        self.assertEqual(self.version_of(self.home_bin), "1.18.27")

    def test_opencode_pin_0_keeps_whatever_is_there(self):
        self.opencode_at(self.home / ".opencode" / "bin", "1.18.27")
        rc, out, calls = self.run_block(pin="0")
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls, [])
        self.assertEqual(self.version_of(self.home_bin), "1.18.27")

    def test_a_version_without_its_checksum_is_refused(self):
        rc, out, _ = self.run_block(env_version="1.18.40", env_sha="")
        self.assertEqual(rc, 1)
        self.assertIn("needs OPENCODE_SHA256 too", out)


class TheConfigStopsSelfUpdating(unittest.TestCase):
    def test_the_generated_config_and_the_users_get_notify(self):
        text = INSTALL.read_text()
        self.assertIn('doc["autoupdate"] = "notify"', text)
        self.assertIn('oc-merge-limits.py" "$OC_USER_CFG" --autoupdate notify', text)

    def test_the_merge_keeps_a_stricter_false_and_the_users_comments(self):
        t = pathlib.Path(tempfile.mkdtemp(prefix="oc-au-"))
        cfg = t / "opencode.json"
        cfg.write_text('{\n  // mine\n  "model": "x"\n}\n')
        run = lambda: subprocess.run(["python3", str(REPO / "oc-merge-limits.py"), str(cfg), "--autoupdate", "notify"],
                                     capture_output=True, text=True)
        self.assertEqual(run().returncode, 0)
        self.assertIn('"autoupdate": "notify"', cfg.read_text())
        self.assertIn("// mine", cfg.read_text())
        self.assertIn("unchanged", run().stdout)
        cfg.write_text('{"autoupdate": false}\n')
        self.assertEqual(run().returncode, 0)
        self.assertEqual(cfg.read_text(), '{"autoupdate": false}\n')



class TheCockpitNamesThePin(unittest.TestCase):
    """The Agent tab says when the served opencode is not the version install.sh pins,
    and reads that version out of install.sh, so the two can never name different ones."""

    def test_the_cockpit_reads_the_installers_pin(self):
        code = ("import importlib.util, os, sys, tempfile\n"
                "t = tempfile.mkdtemp(); open(t + '/api-key', 'w').write('k')\n"
                "os.environ.update(COCKPIT_DRY_RUN='1', COCKPIT_CONFIG_DIR=t, COCKPIT_PORT='0',\n"
                "                  COCKPIT_AGENT_PORT='0', COCKPIT_REPO_DIR=sys.argv[1])\n"
                "sys.path.insert(0, sys.argv[1] + '/dashboard')\n"
                "spec = importlib.util.spec_from_file_location('ck', sys.argv[1] + '/dashboard/cockpit.py')\n"
                "ck = importlib.util.module_from_spec(spec); spec.loader.exec_module(ck)\n"
                "print(ck.opencode_pinned())\n")
        r = subprocess.run(["python3", "-c", code, str(REPO)], capture_output=True, text=True, timeout=60,
                           env={**os.environ, "HOME": tempfile.mkdtemp(prefix="oc-ck-")})
        self.assertEqual(r.stdout.strip().splitlines()[-1], pinned_default(), r.stderr[-400:])

    def test_the_agent_tab_says_when_it_serves_another_version(self):
        js = (REPO / "dashboard" / "static" / "app.js").read_text()
        self.assertIn("if (d.pinned && sv.version && sv.version !== d.pinned)", js)
        self.assertIn("./install.sh brings it in line", js)

if __name__ == "__main__":
    unittest.main(verbosity=2)
