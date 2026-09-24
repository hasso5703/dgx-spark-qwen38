#!/usr/bin/env python3
"""Smaller installer defects found in review (2026-09-24), each against install.sh's own lines."""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()


def line(prefix):
    return next(ln for ln in TEXT.splitlines() if ln.startswith(prefix))


class ADfThatFailsSaysSo(unittest.TestCase):
    """A df that fails under pipefail ended the install at its own line, and the
    "found unknown GB" message written for exactly that case never showed."""

    def run_lines(self, *lines):
        d = pathlib.Path(tempfile.mkdtemp(prefix="df-fail-"))
        (d / "df").write_text("#!/bin/sh\necho 'df: cannot reach it' >&2\nexit 1\n")
        (d / "df").chmod(0o755)
        script = ("set -euo pipefail\ndie(){ echo \"DIE: $*\"; exit 1; }\n"
                  f'HF_CACHE="{d}"; NEED_GB=45; DOCKER_ROOT="{d}"; DOCKER_NEED_GB=40; IMG_LABEL=img\n'
                  + "\n".join(lines) + "\n")
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": f"{d}:/usr/bin:/bin"})
        return r.stdout + r.stderr

    def test_the_hf_cache_disk(self):
        out = self.run_lines(line("FREE_DISK_GB="), line('[ -n "$FREE_DISK_GB" ]'))
        self.assertIn("DIE: Need ~45 GB free", out)
        self.assertIn("found unknown GB", out)

    def test_the_docker_disk(self):
        out = self.run_lines(line("DOCKER_FREE_GB="), line('[ "${DOCKER_FREE_GB:-0}"'))
        self.assertIn("DIE: Need ~40 GB free on", out)


class TheDownloadHintNamesTheDraftPin(unittest.TestCase):
    def test_it_is_draft2(self):
        i = TEXT.index("Checkpoint download failed.")
        hint = TEXT[i:TEXT.index("\n", i)]
        self.assertIn("DRAFT2_REV=main", hint)
        self.assertNotIn(" DRAFT_REV=main", hint, "the retired DSpark pin")


class PathsTheUnitsCannotCarry(unittest.TestCase):
    """HOME, HF_CACHE and PLE_DIR go into the units as they are: inside a quoted bash -c
    line, in a docker -v src:dst, and in systemd's syntax, where % is a specifier."""
    START = TEXT.index('for _p in "HOME=$HOME" "HF_CACHE=$HF_CACHE" "PLE_DIR=$PLE_DIR"; do')
    BLOCK = TEXT[START:TEXT.index("unset _p\n", START)]

    def check(self, home="/home/u", hf="/home/u/.cache/huggingface", ple="/home/u/flashnext-ple"):
        script = "set -euo pipefail\ndie(){ echo \"DIE: $*\"; exit 1; }\n" + self.BLOCK + "\necho FINE\n"
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": "/usr/bin:/bin", "HOME": home, "HF_CACHE": hf, "PLE_DIR": ple})
        return r.stdout

    def test_ordinary_paths_pass(self):
        self.assertIn("FINE", self.check())
        self.assertIn("FINE", self.check(hf="/mnt/data-1/hf_cache+x@2"))

    def test_what_breaks_a_unit_is_refused_by_name(self):
        for bad in ("/mnt/my disk/hf", "/mnt/a:b", "/mnt/it's", "/mnt/$x", "/mnt/100%", "relative/hf"):
            out = self.check(hf=bad)
            self.assertIn("DIE: HF_CACHE=", out, bad)
            self.assertNotIn("FINE", out, bad)
        self.assertIn("DIE: HOME=", self.check(home="/home/jean dupont"))
        self.assertIn("DIE: PLE_DIR=", self.check(ple="/data/ple:x"))


class TheBindsAreCheckedAgainOnceReadBack(unittest.TestCase):
    def test_a_read_back_bind_is_checked_too(self):
        first = TEXT.index("\ncheck_binds\n")
        again = TEXT.index("\n  check_binds\n")
        self.assertLess(TEXT.index("Keeping the installed proxy bind"), again)
        self.assertLess(first, TEXT.index("Keeping the installed engine bind"))


class ABusyProxyPortIsOursOnlyIfOurProxyHoldsIt(unittest.TestCase):
    START = TEXT.index('if [ "$NO_SERVICE" -eq 0 ] && ss -tlnH 2>/dev/null | awk \'{print $4}\' | grep -q ":$PROXY_PORT\\$"; then')
    BLOCK = TEXT[START:TEXT.index("\nfi\n", TEXT.index("keepalive proxy) is already in use", START)) + 4]

    def run_block(self, proxy_port, unit_port):
        d = pathlib.Path(tempfile.mkdtemp(prefix="ka-port-"))
        (d / "ss").write_text(f"#!/bin/sh\necho 'LISTEN 0 128 0.0.0.0:{proxy_port} 0.0.0.0:*'\n")
        (d / "systemctl").write_text("#!/bin/sh\nexit 0\n")          # the proxy is active
        for f in ("ss", "systemctl"):
            (d / f).chmod(0o755)
        (d / "ka.service").write_text(f"ExecStart=/usr/bin/python3 /x/keepalive-proxy.py {unit_port}\n")
        block = self.BLOCK.replace("/etc/systemd/system/qwen38-keepalive.service", str(d / "ka.service"))
        script = ("set -euo pipefail\ndie(){ echo \"DIE: $*\"; exit 1; }\n"
                  f"NO_SERVICE=0; PROXY_PORT={proxy_port}\n" + block + "\necho PASSED\n")
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": f"{d}:/usr/bin:/bin"})
        return r.stdout

    def test_our_proxy_on_its_port_is_reinstalled_over(self):
        out = self.run_block(30001, 30001)
        self.assertIn("already running on :30001", out)
        self.assertIn("PASSED", out)

    def test_another_program_on_the_asked_port_is_refused(self):
        out = self.run_block(8080, 30001)
        self.assertIn("DIE: Port 8080 (keepalive proxy) is already in use by another program", out)


class ContradictoryImageFlags(unittest.TestCase):
    def test_both_image_flags_are_refused_before_anything(self):
        cut = TEXT.index('step "1/10 Preflight checks"')
        d = pathlib.Path(tempfile.mkdtemp(prefix="img-flags-"))
        (d / "install.sh").write_text(TEXT[:cut] + 'echo "FLAGS OK"; exit 0\n')
        r = subprocess.run(["bash", str(d / "install.sh"), "--with-image", "--no-image"], capture_output=True,
                           text=True, timeout=60, cwd=str(REPO), env={"PATH": "/usr/bin:/bin", "HOME": str(d)})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--no-image and --with-image contradict each other", r.stderr)
        self.assertNotIn("FLAGS OK", r.stdout)


class TheOpencodeDownloadSaysWhenItCouldNotPlaceTheBinary(unittest.TestCase):
    """oc_fetch_pinned ended on the rm of its temp dir, so it returned 0 whatever the
    install and the rename before it did: a full disk read "installed"."""
    FUNC = TEXT[TEXT.index("oc_fetch_pinned(){"):TEXT.index("\n}\n", TEXT.index("oc_fetch_pinned(){")) + 3]

    def run_fetch(self, install_ok):
        import hashlib
        import tarfile
        d = pathlib.Path(tempfile.mkdtemp(prefix="oc-fetch-"))
        (d / "src").mkdir()
        (d / "src" / "opencode").write_text("#!/bin/sh\necho 1.18.32\n")
        with tarfile.open(d / "oc.tar.gz", "w:gz") as t:
            t.add(d / "src" / "opencode", arcname="opencode")
        sha = hashlib.sha256((d / "oc.tar.gz").read_bytes()).hexdigest()
        (d / "bin").mkdir()
        (d / "bin" / "curl").write_text(f'#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\ncp "{d}/oc.tar.gz" "$2"\n')
        (d / "bin" / "install").write_text("#!/bin/sh\nexit 0\n" if install_ok else "#!/bin/sh\necho 'No space left on device' >&2\nexit 1\n")
        if install_ok:
            (d / "bin" / "install").write_text('#!/bin/sh\nshift 2\ncp "$1" "$2"\n')
        for f in ("curl", "install"):
            (d / "bin" / f).chmod(0o755)
        script = (f'set -euo pipefail\nOPENCODE_VERSION=1.18.32; OPENCODE_SHA256={sha}\n'
                  f'OC_HOME_DIR="{d}/home"; OC_HOME_BIN="{d}/home/opencode"\n' + self.FUNC
                  + '\nif oc_fetch_pinned; then echo RC0; else echo RC1; fi\n')
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": f"{d}/bin:/usr/bin:/bin"})
        return r.stdout + r.stderr, d

    def test_a_binary_that_could_not_be_placed_is_a_failure(self):
        out, _ = self.run_fetch(install_ok=False)
        self.assertIn("RC1", out)
        self.assertIn("could not put opencode", out)

    def test_a_binary_placed_is_a_success(self):
        out, d = self.run_fetch(install_ok=True)
        self.assertIn("RC0", out)
        self.assertTrue((d / "home" / "opencode").exists())


class TheImageLaneKeptByNoImageIsNotCalledMissing(unittest.TestCase):
    def test_the_summary_line(self):
        i = TEXT.index('elif [ -f /etc/systemd/system/qwen38-image.service ] && [ "${IMAGE_ON:-0}" -eq 0 ]; then')
        self.assertIn("installed, not updated by this run (--no-image)", TEXT[i:i + 400])
        self.assertLess(i, TEXT.index('"  Images     : not installed;'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
