"""Every cockpit collector, against real command output and against garbage.

A collector is the only thing between a command's stdout and what a person reads
off the dashboard, so a wrong parse is a lying panel, and this repo has been bitten
by exactly that (the stage parser and the decode telemetry read an empty stdout for
a night because docker logs writes to stderr). Two things are asserted here:

  * PARSE: given output captured from the reference box (tests/fixtures/), the
    collector returns the values a person would read off that output by hand.
  * SURVIVE: given output no collector expects (empty, truncated mid-line, an
    nvidia-smi [N/A], a French locale, 2 MB of one line, NUL bytes, a stack
    trace where a table was), it returns a dict and never raises. @guard turns a
    raise into {"error": ...}, so the test also checks WHICH collectors degrade
    into an error, because an error dict is an honest answer and a wrong number
    is not.

Nothing here touches the box: cockpit.run is replaced by a table, every path
points at a temp directory, and no collector is allowed to reach the network.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
REPO = HERE.parents[2]
FIX = HERE.parent / "fixtures"


def fixture(name: str) -> str:
    return (FIX / name).read_text()


class FakeBox:
    """Answers cockpit.run() from a table keyed by a fragment of the argv."""

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.calls = []

    def __call__(self, argv, timeout=5.0, merge_err=False):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for key, val in self.table.items():
            if key in joined:
                return val
        return ""


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-coll-"))
        (cls.tmp / "api-key").write_text("k\n")
        os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.tmp),
                          COCKPIT_REPO_DIR=str(REPO), COCKPIT_PORT="0",
                          COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0")
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_coll_under_test",
                                                      DASH / "cockpit.py")
        cls.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cp)
        # staticmethod: a plain function stored on a class becomes a method,
        # and would then receive the TestCase as its first argument.
        cls.real_run = staticmethod(cls.cp.run)
        cls._real_run = cls.cp.run

    @classmethod
    def tearDownClass(cls):
        cls.cp.run = cls._real_run
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def tearDown(self):
        self.cp.run = type(self)._real_run

    def box(self, table=None):
        fake = FakeBox(table)
        self.cp.run = fake
        return fake

    def collectors(self):
        """Every collector this module exposes, by name."""
        return {n: getattr(self.cp, n) for n in dir(self.cp)
                if n.startswith("collect_") and callable(getattr(self.cp, n))}


class Parse(Base):
    def test_units_reads_systemctl_show(self):
        self.box({"systemctl show": fixture("systemctl-show.txt")})
        out = self.cp.collect_units()
        self.assertNotIn("error", out)
        for unit, got in out["units"].items():
            self.assertEqual(got["active"], "active", unit)
            self.assertEqual(got["sub"], "running", unit)
            self.assertEqual(got["enabled"], "enabled", unit)
            self.assertTrue(got["since"].startswith("Wed 2026-09-09"), got)

    def test_units_reports_a_missing_unit_as_unknown_not_as_active(self):
        self.box({})            # systemctl printed nothing at all
        out = self.cp.collect_units()
        for unit, got in out["units"].items():
            self.assertEqual(got["active"], "?", unit)

    def test_containers_reads_ps_and_stats(self):
        self.box({"docker ps --format": fixture("docker-ps.txt"),
                  "docker stats": fixture("docker-stats.txt")})
        out = self.cp.collect_containers()
        self.assertNotIn("error", out)
        self.assertIn("qwen38-flash", out["containers"])
        c = out["containers"]["qwen38-flash"]
        self.assertEqual(c["image"], "lmsysorg/sglang")
        self.assertEqual(c["cpu"], "101.61%")
        self.assertIn("5.83GiB", c["mem"])

    def test_gpu_reads_power_and_temperature(self):
        self.box({"--query-gpu=power.draw,temperature.gpu": "10.58, 46\n"})
        out = self.cp.collect_gpu()
        self.assertNotIn("error", out)
        self.assertAlmostEqual(out["power_w"], 10.58, places=2)
        self.assertAlmostEqual(out["temp_c"], 46.0, places=1)

    def test_gpu_does_not_invent_numbers_when_nvidia_smi_is_silent(self):
        self.box({})
        out = self.cp.collect_gpu()
        self.assertIsNone(out["power_w"])
        self.assertIsNone(out["temp_c"])
        self.assertEqual(out["procs"], [])

    def test_feed_reads_the_keepalive_journal(self):
        self.box({"journalctl -u qwen38-keepalive.service": fixture("keepalive-journal.txt")})
        out = self.cp.collect_feed()
        self.assertNotIn("error", out)
        self.assertTrue(out["rows"], "the real journal produced no request rows")
        for r in out["rows"]:
            self.assertIn("kind", r)
            self.assertIn(r["kind"], ("ok", "gone", "fail", "live", "unknown"))

    def test_decode_telemetry_needs_a_running_container_to_say_anything(self):
        self.box({})            # docker ps -q answers nothing: no lane is up
        out = self.cp.collect_decode_telemetry()
        self.assertIsNone(out["lane"])

    def test_decode_telemetry_reads_the_engine_log_of_the_running_lane(self):
        log = ("[2026-09-10 10:00:00] Decode batch. #running-req: 3, "
               "token usage: 0.12, accept len: 2.41\n")
        self.box({"docker ps -q": "abc123\n", "docker logs": log})
        out = self.cp.collect_decode_telemetry()
        self.assertIsNotNone(out["lane"])
        self.assertEqual(out["decode"], {"running": 3, "token_usage": 0.12,
                                         "accept_len": 2.41})

    def test_the_zombie_guard_reads_both_sides_of_the_wire(self):
        engine = ("[2026-09-10 10:00:00] Received output for rid='abc' but the state "
                  "was deleted in TokenizerManager.\n") * 3
        journal = ("[proxy] v6.14 on :30001 -> http://127.0.0.1:30000 (keepalive 10s, "
                   "max silence 3600s)\n"
                   "[proxy] aborted upstream rid=abc (client gone)\n")
        self.box({"docker ps -q": "abc123\n", "docker logs": engine,
                  "journalctl": journal})
        out = self.cp.collect_guard()
        self.assertEqual(out["zombies"]["lines"], 3)
        self.assertEqual(out["guard"]["version"], "6.14")
        self.assertEqual(out["guard"]["aborted"], 1)
        self.assertIn(out["state"], ("ok", "warn", "err", ""))

    def test_an_untracked_file_is_not_a_modified_working_tree(self):
        """A screenshot dropped in the checkout is not the served code drifting.

        git status --porcelain prints both, and the cockpit used to call any
        non-empty output "modified (uncommitted changes)", which is the sentence
        a person reads to decide whether the box runs the repo's code. Seen on
        2026-09-17 with a downloaded .png sitting in the working tree.
        """
        self.box({"git": '?? "telechargement (2).png"\n'})
        out = self.cp.collect_repo()
        self.assertIs(out["dirty"], False)
        self.assertEqual(out["untracked"], 1)

    def test_it_asks_git_once_so_two_answers_cannot_disagree(self):
        """run() turns a timeout into "", so two calls can contradict each other.

        With one call per fact, a timeout on the first and a success on the
        second reported dirty=False with untracked=1: "clean (1 untracked
        file)" over a modified install.sh, which is the exact sentence this
        split was written to make trustworthy.
        """
        calls = []

        def flaky(argv, *a, **kw):
            calls.append(argv)
            if "status" in argv:
                # first status answers empty (the timeout shape), any later one
                # answers with real content
                return "" if len([c for c in calls if "status" in c]) == 1 \
                    else " M install.sh\n?? shot.png\n"
            return ""

        self.cp.run = flaky
        out = self.cp.collect_repo()
        self.assertEqual(len([c for c in calls if "status" in c]), 1,
                         "collect_repo must ask git status exactly once")
        # and with a single answer the two facts always agree
        self.assertIs(out["dirty"], False)
        self.assertEqual(out["untracked"], 0)

    def test_a_tracked_change_is_still_a_modified_working_tree(self):
        self.box({"git": " M install.sh\n?? note.txt\n"})
        out = self.cp.collect_repo()
        self.assertIs(out["dirty"], True)
        self.assertEqual(out["untracked"], 1)

    def test_a_clean_tree_is_clean(self):
        self.box({"git": ""})
        out = self.cp.collect_repo()
        self.assertIs(out["dirty"], False)
        self.assertEqual(out["untracked"], 0)

    def test_every_collector_answers_with_a_dict_on_a_healthy_box(self):
        self.box({"docker ps --format": fixture("docker-ps.txt"),
                  "docker stats": fixture("docker-stats.txt"),
                  "systemctl show": fixture("systemctl-show.txt"),
                  "docker logs": fixture("engine-log-tail.txt"),
                  "journalctl": fixture("keepalive-journal.txt"),
                  "docker images": fixture("docker-images.txt")})
        for name, fn in self.collectors().items():
            with self.subTest(collector=name):
                out = fn()
                self.assertIsInstance(out, dict, name)


class Survive(Base):
    """Output no collector expects. None of these may raise, and none may
    silently turn into a plausible-looking number."""

    HOSTILE = {
        "empty": "",
        "one newline": "\n",
        "spaces": "    \n   \n",
        "nvidia N/A": "[N/A], [N/A]\n",
        "not applicable words": "No running processes found\n",
        "french locale": ("               total       utilise      libre\n"
                          "Mem:             121         109           2\n"),
        "truncated mid line": "[2026-09-10 10:00:00] Decode batch. #running-req: 3, token us",
        "header only": "Name  CPU  MEM\n",
        "a stack trace": ("Traceback (most recent call last):\n"
                          '  File "x", line 1\nValueError: nope\n'),
        "json where text was": json.dumps({"unexpected": True}),
        "one very long line": "x" * 200000,
        "many lines": "line\n" * 20000,
        "nul bytes": "a\x00b\x00c\n",
        "control chars": "".join(chr(i) for i in range(1, 32)) + "\n",
        "unicode": "éèê你好\U0001f600 running-req: ∞\n",
        "negative numbers": "#running-req: -1, token usage: -0.5, accept len: -2\n",
        "huge numbers": "#running-req: 99999999999999999999, token usage: 1e400, accept len: nan\n",
        "delimiters only": "|||\n===\n:::\n,,,\n",
        "partial fields": "qwen38-flash|\n|image|\n||\n",
        "docker error": ("Error response from daemon: No such container: qwen38-flash\n"),
        "sudo refusal": "sudo: a password is required\n",
    }

    def test_no_collector_raises_on_any_hostile_output(self):
        for label, text in self.HOSTILE.items():
            for name, fn in self.collectors().items():
                with self.subTest(output=label, collector=name):
                    self.box({"": text})       # every command answers this
                    try:
                        out = fn()
                    except Exception as e:      # noqa: BLE001
                        self.fail(f"{name} raised on {label}: {type(e).__name__}: {e}")
                    self.assertIsInstance(out, dict, f"{name} on {label}")

    def test_a_collector_that_cannot_parse_says_so_instead_of_guessing(self):
        """The contract of @guard: a failure is a named error in the payload, which
        the UI renders as a stale panel, never as a fresh zero."""
        self.box({"": self.HOSTILE["a stack trace"]})
        out = self.cp.collect_decode_telemetry()
        if "error" in out:
            self.assertRegex(out["error"], r"^[A-Za-z_]+Error|^[A-Za-z]+: ")
        else:
            self.assertIsNone(out.get("decode"),
                              "a stack trace was parsed into decode telemetry")

    def test_guard_turns_a_raise_into_a_named_error(self):
        @self.cp.guard
        def boom():
            raise ValueError("measured nothing")

        out = boom()
        self.assertEqual(out, {"error": "ValueError: measured nothing"})

    def test_guard_does_not_swallow_a_keyboard_interrupt(self):
        @self.cp.guard
        def stop():
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            stop()

    def test_no_collector_reaches_a_real_command(self):
        """The stub records every argv: nothing may escape it during this suite."""
        fake = self.box({"": ""})
        for fn in self.collectors().values():
            fn()
        for argv in fake.calls:
            self.assertIsInstance(argv, list)
            self.assertTrue(all(isinstance(a, str) for a in argv), argv)


class RunItself(Base):
    """cockpit.run, the one place a subprocess is spawned."""

    def test_it_never_uses_a_shell(self):
        out = self.real_run(["echo", "$HOME;id"])
        self.assertIn("$HOME;id", out, "the argument was interpreted, not passed")

    def test_a_missing_binary_is_an_empty_string_not_an_exception(self):
        self.assertEqual(self.real_run(["/nonexistent/binary-xyz"]), "")

    def test_a_timeout_is_an_empty_string_not_an_exception(self):
        self.assertEqual(self.real_run(["sleep", "5"], timeout=0.3), "")

    def test_merge_err_is_what_reads_a_docker_log(self):
        """docker logs writes the container's stderr to ITS stderr: a reader
        without merge_err sees an empty string. This cost a night once."""
        script = "import sys; sys.stderr.write('on stderr\\n')"
        self.assertEqual(self.real_run(["python3", "-c", script]), "")
        self.assertIn("on stderr",
                      self.real_run(["python3", "-c", script], merge_err=True))


class FitVerdict(Base):
    """When the banner fires, the two numbers under it must be the two that failed.

    Seen on the box 2026-09-19: the proxy was carrying the flash lane's 250,000
    token ceiling while the 27B lane served, so opencode's 548,000 context could
    not be relayed. True warning, right headline, and then it printed "730,000
    asked, 827,968 servable", which is the worst case against the pool: a pair
    that says the ask FITS. A warning whose own numbers contradict it gets read
    as a bug in the cockpit, which is how a real misconfiguration survives a
    person looking straight at it.
    """

    def test_the_ceiling_case_shows_the_context_against_the_ceiling(self):
        """The box's own numbers that day."""
        v = self.cp.fit_verdict(ctx=548_000, outp=182_000, usable=827_968, prompt_cap=250_000)
        self.assertFalse(v["ok"])
        self.assertIn("proxy", v["why"])
        self.assertEqual((v["asked"], v["limit"]), (548_000, 250_000))

    def test_the_pool_case_shows_the_worst_case_against_the_pool(self):
        """No ceiling in force, so the prompt passes and the pair is the other one."""
        v = self.cp.fit_verdict(ctx=700_000, outp=200_000, usable=827_968, prompt_cap=827_968)
        self.assertFalse(v["ok"])
        self.assertIn("pool", v["why"])
        self.assertEqual((v["asked"], v["limit"]), (900_000, 827_968))

    def test_limits_that_fit_report_ok(self):
        v = self.cp.fit_verdict(ctx=558_000, outp=186_000, usable=827_968, prompt_cap=827_968)
        self.assertTrue(v["ok"])
        self.assertEqual(v["why"], "")

    def test_a_warning_never_shows_an_ask_below_its_limit(self):
        """The invariant the screenshot broke, over the whole grid."""
        for ctx in (1, 100_000, 250_000, 548_000, 700_000, 900_000):
            for outp in (0, 32_000, 186_000, 200_000):
                for usable in (250_000, 827_968, 900_000):
                    for cap in (200_000, 250_000, usable):
                        v = self.cp.fit_verdict(ctx=ctx, outp=outp, usable=usable,
                                                prompt_cap=min(cap, usable))
                        with self.subTest(ctx=ctx, outp=outp, usable=usable, cap=cap):
                            if v["ok"]:
                                self.assertLessEqual(v["asked"], v["limit"])
                            else:
                                self.assertGreater(v["asked"], v["limit"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TheBootBarNeverGoesBack(Base):
    """Some boots flood the log the lifecycle reads the last 300 lines of: inductor compile
    errors during the graph capture, 450 lines in six minutes on the reference box on
    22/09. Every milestone left the window, the parse read stage None, and the lane went
    from "capturing graphs" back to "starting". Within one activation the stage stays
    the last one the log proved; a new activation starts from what its own log says."""
    MILESTONES = "\n".join([
        "[2026-09-22 19:51:23] Load weight begin. avail mem=113.93 GB",
        "[2026-09-22 19:53:22] Load weight end. elapsed=118.38 s, type=Qwen3_5ForConditionalGeneration",
        "[2026-09-22 19:53:22] Load weight begin. avail mem=92.04 GB",
        "[2026-09-22 19:53:32] Load weight end. elapsed=9.44 s, type=DFlash2DraftModel",
        "[2026-09-22 19:53:38] KV Cache is allocated. dtype: torch.float8_e4m3fn, #tokens: 914573",
        "[2026-09-22 19:53:41] Capture target verify CUDA graph begin. backend=full",
    ]) + "\n"
    FLOOD = "  torch._dynamo.utils.warn_once(msg)\n" * 300
    U = "qwen38-sglang.service"

    def setUp(self):
        self.cp.BOOT_SEEN.clear()
        with self.cp.STATE_LOCK:
            self.cp.STATE["engine_fast"] = {"data": {"healthy": False}}

    def tick(self, logs, enter):
        self.box({
            f"systemctl show {self.U}": f"ActiveState=active\nSubState=running\n"
                                        f"ActiveEnterTimestampMonotonic={enter}\n",
            "docker ps -q -f name=^qwen38-sglang$": "c0ffee\n",
            "docker logs --tail 300 qwen38-sglang": logs,
        })
        out = self.cp.collect_lifecycle()
        return (out.get("data", out))["engines"][self.U]["state"]

    def test_a_flood_inside_one_boot_keeps_the_stage_it_had_reached(self):
        self.assertEqual(self.tick(self.MILESTONES, enter="1000"), "capturing-graphs")
        self.assertEqual(self.tick(self.FLOOD, enter="1000"), "capturing-graphs")
        self.assertEqual(self.tick(self.FLOOD, enter="1000"), "capturing-graphs")

    def test_a_new_activation_does_not_inherit_the_last_ones_stage(self):
        self.assertEqual(self.tick(self.MILESTONES, enter="1000"), "capturing-graphs")
        self.assertEqual(self.tick(self.FLOOD, enter="2000"), "starting")

    def test_evidence_always_wins_over_what_was_seen(self):
        self.assertEqual(self.tick(self.MILESTONES, enter="1000"), "capturing-graphs")
        fired = self.MILESTONES + "[2026-09-22 19:58:40] The server is fired up and ready to roll!\n"
        self.assertEqual(self.tick(fired, enter="1000"), "warming-up")

    def test_a_cockpit_started_in_the_middle_of_the_flood_reads_the_start_once(self):
        self.cp.BOOT_HEAD_READ.clear()
        reads = []
        for _ in range(3):
            fake = self.box({
                f"systemctl show {self.U}": "ActiveState=active\nSubState=running\n"
                                            "ActiveEnterTimestampMonotonic=1000\n",
                "docker ps -q -f name=^qwen38-sglang$": "c0ffee\n",
                "docker logs --tail 300 qwen38-sglang": self.FLOOD,
                "docker logs --since": self.MILESTONES})
            out = self.cp.collect_lifecycle()
            self.assertEqual(out.get("data", out)["engines"][self.U]["state"], "capturing-graphs")
            reads.append(sum(1 for c in fake.calls if c[:3] == ["docker", "logs", "--since"] and "--until" in c))
        self.assertEqual(reads, [1, 0, 0], "the start of the boot is read once per activation")

    def test_a_serving_engine_that_lost_health_is_degraded_not_starting(self):
        """Its tail is decode lines by then. Before the head read, a cockpit that had not
        watched this boot parsed stage None there, and a serving engine read "starting"."""
        self.cp.BOOT_HEAD_READ.clear()
        fired = self.MILESTONES + "[2026-09-22 19:58:40] The server is fired up and ready to roll!\n"
        decode = "[2026-09-22 20:40:00] Decode batch, #running-req: 1, #token: 5000, gen throughput (token/s): 70.1\n" * 300
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"] = {self.U: "ready"}
        self.cp.UNHEALTHY_TICKS[self.U] = 5            # past the hysteresis: it really lost health
        self.box({f"systemctl show {self.U}": "ActiveState=active\nSubState=running\n"
                                              "ActiveEnterTimestampMonotonic=1000\n",
                  "docker ps -q -f name=^qwen38-sglang$": "c0ffee\n",
                  "docker logs --tail 300 qwen38-sglang": decode,
                  "docker logs --since": fired})
        out = self.cp.collect_lifecycle()
        self.assertEqual(out.get("data", out)["engines"][self.U]["state"], "degraded")
