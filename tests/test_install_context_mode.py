#!/usr/bin/env python3
"""A plain 27B install serves the 1M window, and nothing is refused for it.

Until v1.12.1 the default was `native` and 1M was opt-in behind an env var, so
the window this stack is built around was off for anyone who did not read the
README section about it. Flipping a default is easy; flipping it without
breaking the paths that cannot serve it is the work, and that is what these
tests hold:

  the flash lane and --no-service cannot serve 1m, so an unset CONTEXT_MODE
  falls back to native there IN SILENCE, while an explicit CONTEXT_MODE=1m
  still refuses by name on both. A default that refuses something the operator
  never typed is a bug, and the old refusals sat above the flag parsing where
  a naive default would have hit them.

  an installed unit still wins over the default, in BOTH directions. Before
  this change only the 1m direction was converged (nothing could downgrade a
  1m box), so with 1m as the default a plain re-run on a native box would have
  patched YaRN into its cached configs and moved its memory fraction. That is
  not what an update means.

Every run stops at a later refusal on purpose, past the whole context-mode
resolution and long before the preflight downloads anything.
"""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
INSTALL = str(REPO / "install.sh")
SGL_UNIT = pathlib.Path("/etc/systemd/system/qwen38-sglang.service")


def fresh_box_install():
    """A copy of install.sh whose unit paths point at an empty directory.

    The fresh-box default is invisible on any machine that already has a unit,
    because convergence correctly wins there, and that is every developer box
    and the reference box itself. Rather than skip the assertion where it
    matters most, the two path assignments are rewritten to a temp dir, so the
    convergence block finds nothing and the default is what answers. The
    rewrite is checked: if either assignment is renamed upstream, this fails
    loudly instead of quietly testing the real /etc again.
    """
    empty = tempfile.mkdtemp(prefix="no-units-")
    text = pathlib.Path(INSTALL).read_text()
    out = text
    for var, unit in (("SGL_UNIT_PATH", "qwen38-sglang.service"),
                      ("FLASH_UNIT_PATH", "qwen38-flash.service")):
        old = '%s="/etc/systemd/system/%s"' % (var, unit)
        if old not in out:
            raise AssertionError("install.sh no longer assigns %s the way this test rewrites it" % var)
        out = out.replace(old, '%s="%s/%s"' % (var, empty, unit))
    copy = pathlib.Path(empty) / "install.sh"
    copy.write_text(out)
    copy.chmod(0o755)
    return str(copy)
# Contradictory cockpit flags: the first refusal BELOW the context-mode
# resolution, so the resolution has run and said what it decided.
STOP = ["--no-cockpit", "--with-cockpit"]
# MODEL_CHOICE is pinned on every 27B case. Without it the lane comes from
# whatever unit this box has enabled, so the same test read "1m" on a 27B box
# and "silently native" on a flash one: it failed the first time the reference
# box was mid-switch, which is the test depending on the machine rather than on
# the code.
STOCK = {"MODEL_CHOICE": "stock"}


def run(args=(), script=None, **env_extra):
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin",
           "HOME": tempfile.mkdtemp(prefix="ctxmode-home-")}
    env.update(env_extra)
    r = subprocess.run([script or INSTALL, *args], capture_output=True, text=True,
                       env=env, cwd=str(REPO), timeout=60)
    return r.returncode, r.stdout + r.stderr


DEFAULT_LINE = "Context mode: 1m (1,010,000 tokens)."
KEPT_NATIVE = "Keeping the installed context mode: native."
KEPT_1M = "Keeping the installed context mode: 1m."


class TheDefault(unittest.TestCase):
    def test_the_mode_is_always_decided_out_loud(self):
        # Exactly one of the three lines fires on a 27B install: the default,
        # or one of the two convergences. Silence would mean a mode nobody
        # chose and nobody was told about.
        _, out = run(STOP, **STOCK)
        said = [s for s in (DEFAULT_LINE, KEPT_NATIVE, KEPT_1M) if s in out]
        self.assertEqual(len(said), 1, "expected exactly one, got %r" % said)

    def test_a_fresh_27b_install_defaults_to_1m(self):
        _, out = run(STOP, script=fresh_box_install(), **STOCK)
        self.assertIn(DEFAULT_LINE, out)
        self.assertNotIn(KEPT_NATIVE, out)

    def test_an_installed_native_unit_beats_the_default(self):
        # The direction that did not exist before v1.12.1. Same run, with the
        # unit paths left alone, on a box that has one.
        # The convergence follows the ENABLED lane, not merely a unit file on
        # disk: a box holding both units but serving flash has no installed 27B
        # choice to keep. Skipping on that is the test agreeing with the code
        # rather than asserting a promise the code does not make.
        if not SGL_UNIT.exists():
            self.skipTest("no 27B unit installed here to converge on")
        enabled = subprocess.run(["systemctl", "is-enabled", "--quiet", "qwen38-sglang.service"],
                                 capture_output=True)
        if enabled.returncode != 0:
            self.skipTest("the 27B unit is not the enabled lane here, so there is no installed choice")
        _, out = run(STOP, **STOCK)
        installed_1m = "--context-length 1010000" in SGL_UNIT.read_text()
        self.assertIn(KEPT_1M if installed_1m else KEPT_NATIVE, out)
        self.assertNotIn(DEFAULT_LINE, out)

    def test_the_flash_lane_falls_back_to_native_without_refusing(self):
        rc, out = run(STOP, MODEL_CHOICE="flash")
        self.assertEqual(rc, 1)
        self.assertIn("contradict each other", out)          # it got to the stop
        self.assertNotIn("is a 27B mode", out)               # not refused on its way
        self.assertNotIn(DEFAULT_LINE, out)                  # and it is not on 1m

    def test_no_service_is_never_refused_for_a_default_nobody_typed(self):
        rc, out = run([*STOP, "--no-service"], **STOCK)
        self.assertEqual(rc, 1)
        self.assertIn("contradict each other", out)          # it got to the stop
        self.assertNotIn("needs the systemd path", out)      # not refused on its way
        self.assertNotIn(DEFAULT_LINE, out)                  # and it is not on 1m

    def test_no_service_falls_back_to_native_and_says_so(self):
        _, out = run([*STOP, "--no-service"], script=fresh_box_install(), **STOCK)
        self.assertIn("--no-service installs the native 262144 window", out)
        self.assertNotIn(DEFAULT_LINE, out)


class TheExplicitRefusalsSurvive(unittest.TestCase):
    def test_explicit_1m_on_the_flash_lane_is_still_refused_by_name(self):
        rc, out = run(STOP, MODEL_CHOICE="flash", CONTEXT_MODE="1m")
        self.assertEqual(rc, 1)
        self.assertIn("is a 27B mode", out)

    def test_explicit_1m_on_a_box_that_serves_flash_is_refused_too(self):
        """Without MODEL_CHOICE the refusal above read LANE while it still said 27b; the
        convergence then moved it to flash and nothing looked again, so step 6 patched
        YaRN into the flash checkpoint's config.json in the shared cache and the lane
        restarted on it (found in review, 2026-09-24)."""
        d = pathlib.Path(tempfile.mkdtemp(prefix="ctxmode-flash-"))
        cfg = d / "home/.config/qwen38"
        cfg.mkdir(parents=True)
        (cfg / "launch-flash.sh").write_text(
            "exec docker run --name qwen38-flash lmsysorg/sglang@sha256:" + "a" * 64 +
            " python3 -m sglang.launch_server --model-path RadixArk/Qwen3.8-Flash-Next-NVFP4"
            " --host 127.0.0.1 --port 30000\n")
        (d / "qwen38-flash.service").write_text(f"[Service]\nExecStart=/bin/bash {cfg}/launch-flash.sh\n")
        text = pathlib.Path(INSTALL).read_text()
        for var, unit in (("SGL_UNIT_PATH", "qwen38-sglang.service"),
                          ("FLASH_UNIT_PATH", "qwen38-flash.service")):
            text = text.replace('%s="/etc/systemd/system/%s"' % (var, unit), '%s="%s/%s"' % (var, d, unit))
        (d / "install.sh").write_text(text)
        (d / "install.sh").chmod(0o755)
        (d / "bin").mkdir()
        (d / "bin/systemctl").write_text("#!/bin/sh\nexit 1\n")      # nothing enabled here
        (d / "bin/systemctl").chmod(0o755)
        rc, out = run(STOP, script=str(d / "install.sh"), CONTEXT_MODE="1m",
                      HOME=str(d / "home"), PATH=f"{d}/bin:/usr/local/bin:/usr/bin:/bin")
        self.assertIn("Keeping the installed target model: flash", out, "the fixture is a flash box")
        self.assertEqual(rc, 1)
        self.assertIn("is a 27B mode", out)

    def test_explicit_1m_with_no_service_is_still_refused_by_name(self):
        # No STOP here: the cockpit contradiction sits above this refusal and
        # would shadow it.
        rc, out = run(["--no-service"], CONTEXT_MODE="1m")
        self.assertEqual(rc, 1)
        self.assertIn("needs the systemd path", out)

    def test_a_bad_value_is_still_refused_and_empty_is_not_one(self):
        rc, out = run(STOP, CONTEXT_MODE="2m", **STOCK)
        self.assertEqual(rc, 1)
        self.assertIn('CONTEXT_MODE must be "native" or "1m"', out)


class TheWiring(unittest.TestCase):
    src = pathlib.Path(INSTALL).read_text()

    def test_convergence_covers_both_directions(self):
        self.assertIn('echo "Keeping the installed context mode: 1m.', self.src)
        self.assertIn('echo "Keeping the installed context mode: native.', self.src)

    def test_the_default_is_resolved_after_the_flags_are_parsed(self):
        # It depends on --no-service, so a default computed at the top of the
        # file (where the old one lived) cannot see it.
        self.assertLess(self.src.index('for arg in "$@"; do'),
                        self.src.index('if [ -z "$CONTEXT_MODE" ]; then\n  if [ "$LANE" = "flash" ]'))

    def test_the_1m_limits_are_fitted_to_the_real_pool_after_boot(self):
        # The static 1m limits overshoot the measured 863,398-token floor, and
        # fitting them was a documented manual step while 1m was opt-in.
        self.assertIn('oc-fit-limits.py" --engine', self.src)
        i = self.src.index('oc-fit-limits.py" --engine')
        self.assertIn('[ "$CONTEXT_MODE" = "1m" ]', self.src[i - 700:i])

    def test_the_1m_unit_template_is_the_one_that_carries_the_window(self):
        tpl = (REPO / "qwen38-sglang-1m.service.template").read_text()
        self.assertIn("--context-length 1010000", tpl)
        # 0.76 since v1.14: the official image claims a smaller static budget
        # than the locally built one did, so the same 0.70 cost 15% of the pool
        # while leaving 10 GB unused. Measured, see the IMAGE pin in install.sh.
        self.assertIn("--mem-fraction-static 0.76", tpl)
        self.assertIn("SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN=1", tpl)


if __name__ == "__main__":
    unittest.main(verbosity=2)
