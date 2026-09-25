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
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import installer_wall as wall  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
INSTALL = str(REPO / "install.sh")


def fresh_box_install():
    """A copy of install.sh that finds no unit at all, so the default is what answers.

    The fresh-box default is invisible on any machine that already has a unit,
    because convergence correctly wins there, and that is every developer box
    and the reference box itself. The copy reads its units from an empty
    directory instead of the box's (a unit path it did not redirect fails
    loudly), and ends at a wall before step 1."""
    return wall.walled(units=wall.units_dir())


def box_27b(context_length):
    """A copy of install.sh on a box whose installed 27B unit serves this window."""
    return wall.walled(units=wall.units_dir({"qwen38-sglang.service": (
        "[Service]\nExecStart=/usr/bin/docker run lmsysorg/sglang python3 -m sglang.launch_server "
        f"--model-path RadixArk/Qwen3.8-27B-NVFP4 --context-length {context_length} --port 30000\n")}))
# Contradictory cockpit flags: the first refusal BELOW the context-mode
# resolution, so the resolution has run and said what it decided.
STOP = ["--no-cockpit", "--with-cockpit"]
# MODEL_CHOICE is pinned on every 27B case. Without it the lane comes from
# whatever unit this box has enabled, so the same test read "1m" on a 27B box
# and "silently native" on a flash one: it failed the first time the reference
# box was mid-switch, which is the test depending on the machine rather than on
# the code.
STOCK = {"MODEL_CHOICE": "stock"}


def run(args=(), script=None, home=None, **env_extra):
    """A walled copy of install.sh (a fresh box unless `script` is another one), with the
    commands that act on the box fenced: the refusal a test stops at is checked to have
    fired, instead of trusted to."""
    env, record = wall.fenced_env(home=home or tempfile.mkdtemp(prefix="ctxmode-home-"),
                                  **env_extra)
    r = subprocess.run([script or fresh_box_install(), *args], capture_output=True, text=True,
                       env=env, cwd=wall.cwd(), timeout=60)
    out = r.stdout + r.stderr
    assert wall.WALL not in out, f"the run went past its refusal:\n{out[-800:]}"
    assert not wall.reached(record), wall.reached(record)
    return r.returncode, out


DEFAULT_LINE = "Context mode: 1m (1,010,000 tokens)."
KEPT_NATIVE = "Keeping the installed context mode: native."
KEPT_1M = "Keeping the installed context mode: 1m."


class TheDefault(unittest.TestCase):
    def test_the_mode_is_always_decided_out_loud(self):
        # Exactly one of the three lines fires on a 27B install: the default,
        # or one of the two convergences. Silence would mean a mode nobody
        # chose and nobody was told about. Asked of the three boxes it can be,
        # rather than of whichever one runs the suite.
        for script, line in ((fresh_box_install(), DEFAULT_LINE), (box_27b(262144), KEPT_NATIVE),
                             (box_27b(1010000), KEPT_1M)):
            _, out = run(STOP, script=script, **STOCK)
            said = [s for s in (DEFAULT_LINE, KEPT_NATIVE, KEPT_1M) if s in out]
            self.assertEqual(said, [line])

    def test_a_fresh_27b_install_defaults_to_1m(self):
        _, out = run(STOP, script=fresh_box_install(), **STOCK)
        self.assertIn(DEFAULT_LINE, out)
        self.assertNotIn(KEPT_NATIVE, out)

    def test_an_installed_native_unit_beats_the_default(self):
        # The direction that did not exist before v1.12.1, on a box whose 27B unit
        # serves the native window. It used to run only where the suite's own box had
        # a 27B unit enabled: skipped in CI, and the reference box is on 1m, so the
        # native direction ran nowhere.
        _, out = run(STOP, script=box_27b(262144), **STOCK)
        self.assertIn(KEPT_NATIVE, out)
        self.assertNotIn(DEFAULT_LINE, out)

    def test_an_installed_1m_unit_is_kept_too(self):
        _, out = run(STOP, script=box_27b(1010000), **STOCK)
        self.assertIn(KEPT_1M, out)
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


class TheResolvedMode(unittest.TestCase):
    """The mode the installer goes on with, read from the run: the tests above read its
    messages, so a --no-service run that said "native" and went on with 1m, a flash lane
    given 1m by default (YaRN patched into its checkpoint), or a native box converged to
    1m passed them (found in review, 2026-09-24)."""

    def mode(self, args, script=None, **env):
        from test_install_bind import binds, probed
        _, out = run(args, script=probed(script or fresh_box_install()), **env)
        return binds(out)

    def test_each_case_goes_on_with_the_mode_it_says(self):
        cases = (("fresh 27B", [*STOP], None, STOCK, "1m"),
                 ("--no-service", [*STOP, "--no-service"], None, STOCK, "native"),
                 ("installed native", [*STOP], box_27b(262144), STOCK, "native"),
                 ("installed 1m", [*STOP], box_27b(1010000), STOCK, "1m"),
                 ("flash lane", [*STOP], None, {"MODEL_CHOICE": "flash"}, "native"))
        for name, args, script, env, want in cases:
            with self.subTest(case=name):
                self.assertEqual(self.mode(args, script, **env)["ctx"], want)


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
        home = pathlib.Path(tempfile.mkdtemp(prefix="ctxmode-flash-"))
        cfg = home / ".config/qwen38"
        cfg.mkdir(parents=True)
        (cfg / "launch-flash.sh").write_text(
            "exec docker run --name qwen38-flash lmsysorg/sglang@sha256:" + "a" * 64 +
            " python3 -m sglang.launch_server --model-path RadixArk/Qwen3.8-Flash-Next-NVFP4"
            " --host 127.0.0.1 --port 30000\n")
        units = wall.units_dir({"qwen38-flash.service":
                                f"[Service]\nExecStart=/bin/bash {cfg}/launch-flash.sh\n"})
        # nothing enabled here: the fence answers "no" to is-enabled for every unit
        rc, out = run(STOP, script=wall.walled(units=units), CONTEXT_MODE="1m", home=home)
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
