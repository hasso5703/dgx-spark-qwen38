"""Property-based tests: what must hold for EVERY input, not for the ones we chose.

Every example-based test in this repo asserts what one input produces. That finds
the bugs someone thought of. These assert invariants over generated inputs, which
is what finds the rest: a parser that must never raise, a classifier that must be
total, a limit computation that must never exceed the pool it is given.

Hypothesis shrinks a failure to its smallest form and remembers it, so a
counterexample found once on a runner is replayed on every later run
(.hypothesis/ is not committed; the tests are deterministic without it).

The suite SKIPS cleanly when hypothesis is not installed, because this repo's
runtime is stdlib-only and a developer must be able to run the tests with no pip.
CI installs it, so there it is a hard gate.
"""
import importlib.util
import json
import os
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
REPO = HERE.parents[2]
sys.path.insert(0, str(DASH))

import lifecycle as lc  # noqa: E402
import recipes as rc  # noqa: E402

# This is the one suite in the repo with a dependency, which is why it is not
# named test_*.py: the discovered suite must keep running with nothing but the
# standard library. If the interpreter running it has no hypothesis, it re-execs
# into a venv that does, and failing that it says so and exits 0 without
# pretending to have checked anything. CI installs hypothesis and then requires
# the summary line printed at the end, so a skip there is a failure, never a
# silent pass.
# Repo-local first, so the lookup does not depend on HOME: ci-local.sh runs every
# step under a throwaway one, and a suite that silently skipped there would be the
# exact "green that checked nothing" this file exists to prevent.
VENVS = [Path(os.environ["QWEN38_TEST_PYTHON"])] if os.environ.get("QWEN38_TEST_PYTHON") else []
VENVS += [REPO / ".venv-test/bin/python", REPO / ".venv/bin/python",
          Path.home() / ".local/share/qwen38-testenv/bin/python"]

try:
    from hypothesis import HealthCheck, assume, given, settings
    from hypothesis import strategies as st
except ImportError:                                    # pragma: no cover
    import subprocess
    for cand in VENVS:
        if cand.exists() and subprocess.run(
                [str(cand), "-c", "import hypothesis"],
                capture_output=True).returncode == 0:
            os.execv(str(cand), [str(cand), str(HERE), *sys.argv[1:]])
    print("SKIPPED: hypothesis is not installed and no test venv has it.")
    print("  python3 -m venv ~/.local/share/qwen38-testenv "
          "&& ~/.local/share/qwen38-testenv/bin/pip install hypothesis")
    raise SystemExit(0)

# Deterministic and bounded: a test suite is not a fuzzing campaign. The
# deadline is off because a 200 KB generated line is legitimately slow to parse
# and a per-example deadline would flake on a loaded box.
MAX_EXAMPLES = 300
PROFILE = settings(max_examples=MAX_EXAMPLES, deadline=None,
                   suppress_health_check=[HealthCheck.too_slow])

# Text that looks like a log line. An alphabet can only hold single characters,
# so the multi-character tokens every parser here keys on are composed as
# FRAGMENTS instead: the generator then spends its budget near the boundary
# (a real prefix followed by nonsense) rather than in random unicode, which is
# where parser bugs actually live.
FRAGMENTS = st.sampled_from([
    "rid=", "rid='", "[proxy]", "POST", "GET", " in 1.0s", " in 999.9s",
    "body=42b", "Received output for", "but the state was deleted",
    "TokenizerManager.", "aborted upstream", "drained an abandoned",
    "v6.14 on :30001", "#running-req:", "token usage:", "accept len:",
    "2026-09-10 10:00:00", "2026-09-10T10:00:00+02:00", "127.0.0.1:5555",
    "/v1/chat/completions", "/v1/messages", "CLIENT GONE on write",
    "oversize check:", "REFUSED oversize", "(client gone)", "drain ceiling reached",
    " -> ", "\n", "\n\n", "  ", "=", ":", "|", "'",
])
NOISE = st.text(
    alphabet=st.one_of(st.characters(min_codepoint=32, max_codepoint=126),
                       st.sampled_from(list("\n\r\t\x00"))),
    max_size=24)
LOGGY = st.lists(st.one_of(FRAGMENTS, NOISE), max_size=80).map("".join)


class ParsersNeverRaise(unittest.TestCase):
    """A parser reads text nobody controls: a journal, a container log, a config
    written by another tool. Raising is never the right answer, because the
    caller is a 30 s sampler that would then show an error panel forever."""

    @PROFILE
    @given(LOGGY)
    def test_parse_feed(self, raw):
        rows = lc.parse_feed(raw)
        self.assertIsInstance(rows, list)
        for r in rows:
            self.assertIsInstance(r, dict)
            self.assertIn(r["kind"], ("ok", "gone", "fail", "live", "unknown"))
            self.assertIsInstance(r["bytes"], int)
            self.assertTrue(r["secs"] is None or isinstance(r["secs"], float))

    @PROFILE
    @given(LOGGY)
    def test_parse_zombies(self, raw):
        z = lc.parse_zombies(raw)
        self.assertIsInstance(z["lines"], int)
        self.assertGreaterEqual(z["lines"], 0)
        self.assertEqual(z["distinct"], len({r["rid"] for r in z["requests"]})
                         if z["distinct"] <= 5 else z["distinct"])
        self.assertLessEqual(len(z["requests"]), 5)
        self.assertLessEqual(sum(r["lines"] for r in z["requests"]), z["lines"])

    @PROFILE
    @given(LOGGY)
    def test_parse_guard(self, raw):
        g = lc.parse_guard(raw)
        for key in ("aborted", "abort_failed", "drained", "ceiling"):
            self.assertIsInstance(g[key], int)
            self.assertGreaterEqual(g[key], 0)
        self.assertTrue(g["version"] is None or isinstance(g["version"], str))
        self.assertTrue(g["drain_max_s"] is None or g["drain_max_s"] >= 0)

    @PROFILE
    @given(st.lists(LOGGY, max_size=60))
    def test_parse_boot_log(self, lines):
        b = lc.parse_boot_log(lines)
        self.assertIsInstance(b, dict)
        self.assertIn("stage", b)

    @PROFILE
    @given(LOGGY)
    def test_profile_from_text(self, raw):
        prof = rc.profile_from_text(raw)
        self.assertEqual(set(prof), {"engine", "model", "drafter", "serve",
                                     "switches", "env"})
        self.assertIsInstance(prof["switches"], dict)
        for flag, on in prof["switches"].items():
            self.assertIn(flag, rc.SWITCHES)
            self.assertIsInstance(on, bool)

    @PROFILE
    @given(LOGGY)
    def test_parse_assignments(self, raw):
        out = rc.parse_assignments(raw)
        self.assertIsInstance(out, dict)
        for k, v in out.items():
            self.assertRegex(k, r"^[A-Z][A-Z0-9_]*$")
            self.assertIsInstance(v, str)


class ClassifiersAreTotal(unittest.TestCase):
    @PROFILE
    @given(st.text(max_size=200))
    def test_every_string_gets_exactly_one_outcome_kind(self, outcome):
        kind = lc.outcome_kind(outcome)
        self.assertIn(kind, ("ok", "gone", "fail", "live", "unknown"))

    @PROFILE
    @given(st.text(max_size=200))
    def test_an_ok_prefix_always_means_ok(self, tail):
        assume("\n" not in tail)
        self.assertEqual(lc.outcome_kind("ok" + tail), "ok")

    @PROFILE
    @given(st.sampled_from(["CLIENT GONE on write", "CLIENT GONE during keepalive",
                            "no outcome (client vanished mid-request)"]),
           st.text(alphabet=" .()", max_size=12))
    def test_a_client_departure_stays_a_departure_however_it_is_decorated(self, base, deco):
        self.assertEqual(lc.outcome_kind(base + deco), "gone")


class SwitchesAreTokens(unittest.TestCase):
    """_switches must match whole arguments. A substring match would let
    --disable-radix-cache answer for --disable-radix, which is how a drift panel
    starts lying."""

    @PROFILE
    @given(st.sampled_from(rc.SWITCHES), st.text(alphabet="abcdefgh-", max_size=8))
    def test_a_longer_flag_never_satisfies_a_shorter_one(self, flag, suffix):
        assume(suffix)
        longer = flag + suffix
        assume(longer not in rc.SWITCHES)
        self.assertFalse(rc._switches(f"python3 -m sglang.launch_server {longer}")[flag],
                         f"{longer} answered for {flag}")

    @PROFILE
    @given(st.lists(st.sampled_from(rc.SWITCHES), unique=True, max_size=7),
           st.sampled_from(["\n", " ", " \\\n  ", "\t"]))
    def test_a_switch_is_found_whatever_the_whitespace(self, flags, sep):
        text = "python3 -m sglang.launch_server" + sep + sep.join(flags)
        got = rc._switches(text)
        for f in rc.SWITCHES:
            self.assertEqual(got[f], f in flags, f)

    @PROFILE
    @given(st.sampled_from(rc.SWITCHES))
    def test_a_flag_named_in_a_comment_is_prose_not_a_flag(self, flag):
        text = f"# the {flag} below is what parks the scheduler\npython3 -m sglang.launch_server"
        self.assertFalse(rc.profile_from_text(text)["switches"][flag])


class DriftIsSound(unittest.TestCase):
    @PROFILE
    @given(LOGGY)
    def test_a_profile_never_drifts_from_itself(self, raw):
        prof = rc.profile_from_text(raw)
        self.assertEqual(rc.drift(prof, prof), [])

    @PROFILE
    @given(LOGGY, LOGGY)
    def test_drift_is_symmetric_in_what_it_finds(self, a, b):
        pa, pb = rc.profile_from_text(a), rc.profile_from_text(b)
        keys_ab = {r["key"] for r in rc.drift(pa, pb)}
        keys_ba = {r["key"] for r in rc.drift(pb, pa)}
        self.assertEqual(keys_ab, keys_ba)

    @PROFILE
    @given(LOGGY, LOGGY)
    def test_every_drift_row_names_two_different_values(self, a, b):
        pa, pb = rc.profile_from_text(a), rc.profile_from_text(b)
        for row in rc.drift(pa, pb):
            self.assertNotEqual(row["recipe"], row["installed"], row)


class ValidateIsClosed(unittest.TestCase):
    """A custom recipe is a JSON file a user writes: validate() is the only thing
    between that file and a launcher."""

    BASE = {"id": "x", "lane": "flash",
            "engine": {"family": "sglang", "image": "a/b@sha256:" + "0" * 64},
            "model": {"repo": "a/b", "revision": "0" * 40},
            "drafter": {"algorithm": "none"},
            "serve": {"context_length": 262144}}

    @PROFILE
    @given(st.text(max_size=40))
    def test_only_declared_switch_names_are_accepted(self, name):
        assume(name not in rc.SWITCHES)
        errs = rc.validate({**self.BASE, "switches": {name: True}})
        self.assertTrue(any("unknown switch" in e for e in errs), (name, errs))

    @PROFILE
    @given(st.one_of(st.integers(), st.text(max_size=8), st.none(),
                     st.lists(st.booleans(), max_size=2), st.floats(allow_nan=False)))
    def test_a_switch_value_must_be_a_boolean(self, value):
        assume(not isinstance(value, bool))
        errs = rc.validate({**self.BASE, "switches": {"--sleep-on-idle": value}})
        self.assertTrue(any("true or false" in e for e in errs), (value, errs))

    @PROFILE
    @given(st.text(max_size=60))
    def test_a_serve_path_may_never_carry_shell_metacharacters(self, path):
        errs = rc.validate({**self.BASE,
                            "serve": {"context_length": 4096,
                                      "speculative_token_map": path}})
        dirty = re.search(r"[\s;&|`$<>(){}\\'\"]", path) or not path or len(path) > 512
        if dirty:
            self.assertTrue(any("speculative_token_map" in e for e in errs),
                            (repr(path), errs))

    @PROFILE
    @given(st.text(max_size=50))
    def test_an_image_must_be_pinned(self, image):
        errs = rc.validate({**self.BASE,
                            "engine": {"family": "sglang", "image": image}})
        if not rc.IMAGE_RE.match(image or "") or image.endswith(":latest"):
            self.assertTrue(any("engine.image" in e for e in errs), (repr(image), errs))

    @PROFILE
    @given(st.dictionaries(st.text(max_size=6), st.text(max_size=6), max_size=4))
    def test_validate_never_raises_whatever_the_env_block_is(self, env):
        errs = rc.validate({**self.BASE, "env": env})
        self.assertIsInstance(errs, list)

    @PROFILE
    @given(st.recursive(
        st.none() | st.booleans() | st.integers() | st.text(max_size=6),
        lambda kids: st.lists(kids, max_size=3) | st.dictionaries(st.text(max_size=6), kids, max_size=3),
        max_leaves=12))
    def test_validate_never_raises_on_arbitrary_json(self, blob):
        errs = rc.validate(blob)
        self.assertIsInstance(errs, list)
        if not isinstance(blob, dict):
            self.assertEqual(errs, ["recipe must be an object"])


class OcLimitsFitThePool(unittest.TestCase):
    """oc-fit-limits computes what an agent client may ask for.

    The contract came out of writing these: fit(pool, ceiling) rounds both
    numbers DOWN to a thousand, so a pool too small to hold a thousand-token
    answer correctly returns (0, 0) rather than a number that does not fit. The
    invariant is the sum, never the positivity.
    """

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("ocfit", REPO / "oc-fit-limits.py")
        cls.oc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.oc)

    POOLS = st.integers(min_value=0, max_value=4_000_000)
    CEIL = st.integers(min_value=0, max_value=1_500_000)

    @PROFILE
    @given(POOLS, CEIL)
    def test_what_it_grants_always_fits_the_pool(self, pool, ceiling):
        ctx, out = self.oc.fit(pool, ceiling)
        self.assertLessEqual(ctx + out, pool,
                             f"pool {pool}, ceiling {ceiling}: {ctx} + {out} does not fit")

    @PROFILE
    @given(POOLS, CEIL)
    def test_it_never_grants_a_negative_budget(self, pool, ceiling):
        ctx, out = self.oc.fit(pool, ceiling)
        self.assertGreaterEqual(ctx, 0)
        self.assertGreaterEqual(out, 0)

    @PROFILE
    @given(POOLS, CEIL)
    def test_both_numbers_are_whole_thousands(self, pool, ceiling):
        ctx, out = self.oc.fit(pool, ceiling)
        self.assertEqual(ctx % 1000, 0)
        self.assertEqual(out % 1000, 0)

    @PROFILE
    @given(POOLS, st.integers(min_value=1, max_value=1_500_000))
    def test_a_lane_ceiling_is_never_exceeded(self, pool, ceiling):
        ctx, _ = self.oc.fit(pool, ceiling)
        self.assertLessEqual(ctx, ceiling)

    @PROFILE
    @given(POOLS, POOLS, CEIL)
    def test_a_bigger_pool_never_grants_a_smaller_context(self, a, b, ceiling):
        small, big = min(a, b), max(a, b)
        self.assertLessEqual(self.oc.fit(small, ceiling)[0],
                             self.oc.fit(big, ceiling)[0])

    @PROFILE
    @given(st.integers(min_value=200_000, max_value=4_000_000))
    def test_a_realistic_pool_grants_a_usable_budget(self, pool):
        """Every pool this repo has ever measured is above 180,000 tokens."""
        ctx, out = self.oc.fit(pool, 0)
        self.assertGreater(ctx, 0, pool)
        self.assertGreater(out, 0, pool)

    @PROFILE
    @given(st.text(max_size=300))
    def test_the_prompt_ceiling_is_read_or_zero_never_a_crash(self, raw):
        got = self.oc.ceiling_from_env(raw)
        self.assertIsInstance(got, int)
        self.assertGreaterEqual(got, 0)

    @PROFILE
    @given(st.integers(min_value=0, max_value=10 ** 9), st.booleans())
    def test_the_ceiling_is_found_wherever_systemd_puts_it(self, value, first):
        """systemd prints one Environment= prefix for the FIRST variable only, so
        a parser keyed on the prefix misses the ceiling when it is not first.
        That bug shipped once and failed silently."""
        pair = f"PROMPT_CEILING_TOKENS={value}"
        other = "MAX_SILENCE_S=3600"
        text = (f"Environment={pair} {other}" if first
                else f"Environment={other} {pair}")
        self.assertEqual(self.oc.ceiling_from_env(text), value)


def main() -> int:
    """A summary line CI can require, so a suite that did not run cannot pass."""
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    total = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    ok = total - len(result.failures) - len(result.errors) - len(result.skipped)
    print(f"\nproperty checks: {ok} passed of {total}, "
          f"{MAX_EXAMPLES} generated examples each")
    return 0 if result.wasSuccessful() and ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
