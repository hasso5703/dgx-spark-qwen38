#!/usr/bin/env python3
"""--no-service on a box whose installed unit serves 1m says what it found.

A plain run keeps the context mode the installed 27B unit serves, and --no-service cannot
serve 1m (./run.sh has no keepalive proxy), so on a 1m box `./install.sh --no-service` was
refused with "CONTEXT_MODE=1m needs the systemd path ... pass CONTEXT_MODE=native
explicitly": a variable the operator never set, under a README that says --no-service is
native "with no refusal" (found in review, 2026-09-24). The test in
test_install_context_mode.py that should have seen it stops at an earlier refusal. It is
still a refusal, since a native --no-service install would leave that unit on configs it
cannot start from, but it names the installed unit and both ways out.

A copy of install.sh runs against a fixture box (a 1m unit, a systemctl that says it is
enabled) and stops right after the refusals, before step 1 touches anything.
"""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "install.sh").read_text()
STEP1 = 'step "1/10 Preflight checks"'


def box(context_length):
    d = pathlib.Path(tempfile.mkdtemp(prefix="nosvc-1m-"))
    (d / "qwen38-sglang.service").write_text(
        "[Service]\nExecStart=/usr/bin/docker run lmsysorg/sglang python3 -m sglang.launch_server "
        f"--model-path RadixArk/Qwen3.8-27B-NVFP4 --context-length {context_length} --port 30000\n")
    text = TEXT
    for var, unit in (("SGL_UNIT_PATH", "qwen38-sglang.service"), ("FLASH_UNIT_PATH", "qwen38-flash.service")):
        old = '%s="/etc/systemd/system/%s"' % (var, unit)
        assert old in text, f"install.sh no longer assigns {var} the way this test rewrites it"
        text = text.replace(old, '%s="%s/%s"' % (var, d, unit))
    assert STEP1 in text
    text = text.replace(STEP1, 'echo "PAST THE REFUSALS"; exit 0\n' + STEP1, 1)
    (d / "install.sh").write_text(text)
    (d / "install.sh").chmod(0o755)
    (d / "bin").mkdir()
    (d / "bin/systemctl").write_text(
        "#!/bin/sh\ncase \"$*\" in *is-enabled*qwen38-sglang.service*) exit 0;; esac\nexit 1\n")
    (d / "bin/systemctl").chmod(0o755)
    (d / "home").mkdir()
    return d


def run(d, args, **env):
    r = subprocess.run([str(d / "install.sh"), *args], capture_output=True, text=True, timeout=60,
                       cwd=str(REPO), env={"PATH": f"{d}/bin:/usr/local/bin:/usr/bin:/bin",
                                           "HOME": str(d / "home"), **env})
    return r.returncode, r.stdout + r.stderr


class NoServiceOnA1mBox(unittest.TestCase):
    def test_the_refusal_names_the_installed_unit_not_a_variable(self):
        rc, out = run(box(1010000), ["--no-service"])
        self.assertEqual(rc, 1, out)
        self.assertNotIn("PAST THE REFUSALS", out)
        self.assertIn("installed 27B unit serves the 1M window", out)
        self.assertNotIn("pass CONTEXT_MODE=native explicitly", out)

    def test_an_explicit_native_passes(self):
        rc, out = run(box(1010000), ["--no-service"], CONTEXT_MODE="native")
        self.assertIn("PAST THE REFUSALS", out)

    def test_an_explicit_1m_is_still_refused_as_before(self):
        rc, out = run(box(1010000), ["--no-service"], CONTEXT_MODE="1m")
        self.assertEqual(rc, 1)
        self.assertIn("needs the systemd path", out)

    def test_a_native_box_passes(self):
        rc, out = run(box(262144), ["--no-service"])
        self.assertIn("PAST THE REFUSALS", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
