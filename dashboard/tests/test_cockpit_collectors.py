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

    def test_the_zombie_guard_reports_the_proxy_that_runs(self):
        """journalctl -g is given a pattern, and -n 1 answers the newest line matching it:
        the fake applies the collector's own pattern to a journal holding a banner of the
        old shape and, after it, the banner the proxy prints today (rendered from its
        source). The old pattern, "on :", only matched the first, so the page showed
        v6.20 on the reference box while v6.24 ran."""
        import re
        src = (REPO / "keepalive-proxy.py").read_text()
        tpl = re.search(r'log\(f"(v([\d.]+) on \{BIND\}:\{port\} -> [^"]*)"\)', src)
        current = tpl.group(2)
        banner = "[proxy] " + re.sub(r"\{[^}]*\}", "x", tpl.group(1).replace("{BIND}", "0.0.0.0")
                                                             .replace("{port}", "30001"))
        journal = ["[proxy] v6.20 on :30001 -> http://127.0.0.1:30000 (keepalive 10s, max silence 3600s)",
                   "[proxy] aborted upstream rid=abc (client gone)", banner]

        class Journal(FakeBox):
            def __call__(self, argv, timeout=5.0, merge_err=False):
                self.calls.append(list(argv))
                if argv[0] == "journalctl" and "-g" in argv:
                    pattern = argv[argv.index("-g") + 1]
                    hits = [ln for ln in journal if re.search(pattern, ln, re.I)]
                    return (hits[-1] + "\n") if hits else ""
                return "\n".join(journal) + "\n" if argv[0] == "journalctl" else ""
        self.cp.run = Journal()
        out = self.cp.collect_guard()
        self.assertEqual(out["guard"]["version"], current)
        self.assertIs(out["guard"]["predates_abort"], False)

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

    def test_no_hostile_output_becomes_a_plausible_value(self):
        """The half of the contract the test above cannot see, since @guard makes every
        answer a dict: no refusal counted, no watt or degree read, no unit state made up
        out of garbage. Only the type was asserted, so a kernel collector that counted
        every journal line as a driver refusal passed (found in review, 2026-09-24)."""
        for label, text in self.HOSTILE.items():
            self.assertNotIn("NV_ERR_NO_MEMORY", text)
            self.assertNotIn("ActiveState=", text)
            with self.subTest(output=label):
                self.box({"": text})
                self.cp.KERNEL_LAST["count"] = None
                k = self.cp.collect_kernel()
                self.assertEqual((k.get("nvrm_oom_1h"), k.get("nvrm_last")), (0, None), k)
                g = self.cp.collect_gpu()
                if "error" not in g:
                    self.assertEqual((g["power_w"], g["temp_c"]), (None, None), g)
                u = self.cp.collect_units()
                if "error" not in u:
                    for unit, st in u["units"].items():
                        self.assertEqual((st["active"], st["sub"], st["enabled"]), ("?", "?", "?"), unit)

    def test_the_kernel_collector_counts_driver_refusals_only(self):
        self.box({"journalctl": (
            "2026-09-24T10:00:01+0200 spark kernel: usb 1-1: new device\n"
            "2026-09-24T10:00:02+0200 spark kernel: NVRM: nvAssertFailed NV_ERR_NO_MEMORY\n"
            "2026-09-24T10:00:03+0200 spark kernel: EXT4-fs (nvme0n1p2): re-mounted\n"
            "2026-09-24T10:00:04+0200 spark kernel: NVRM: alloc NV_ERR_NO_MEMORY\n")})
        self.cp.KERNEL_LAST["count"] = None
        k = self.cp.collect_kernel()
        self.assertEqual((k["nvrm_oom_1h"], k["nvrm_last"]), (2, "2026-09-24T10:00:04+0200"))

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

    def test_the_window_case_shows_the_request_against_the_window(self):
        """The flash pair of v1.18.6: it passes the proxy and the pool, and the engine still
        refuses it, because it takes input + max_tokens against its 262,144 window and the
        prompt reaches opencode's compaction point plus one step (found in review,
        2026-09-24). The ask shown is that request, against the window."""
        v = self.cp.fit_verdict(ctx=225_000, outp=32_000, usable=519_203, prompt_cap=250_000,
                                window=262_144)
        self.assertFalse(v["ok"])
        self.assertIn("window", v["why"])
        self.assertEqual((v["asked"], v["limit"]), (225_000 + 25_000 + 32_000, 262_144))
        ok = self.cp.fit_verdict(ctx=205_000, outp=32_000, usable=519_203, prompt_cap=250_000,
                                 window=262_144)
        self.assertTrue(ok["ok"], ok)
        # the 1M lane is far from its window: nothing about it changes
        self.assertTrue(self.cp.fit_verdict(ctx=558_000, outp=186_000, usable=827_968,
                                            prompt_cap=827_968, window=1_010_000)["ok"])

    def test_a_warning_never_shows_an_ask_below_its_limit(self):
        """The invariant the screenshot broke, over the whole grid."""
        for ctx in (1, 100_000, 250_000, 548_000, 700_000, 900_000):
            for outp in (0, 32_000, 186_000, 200_000):
                for usable in (250_000, 827_968, 900_000):
                    for cap in (200_000, 250_000, usable):
                        for window in (0, 262_144, 1_010_000):
                            v = self.cp.fit_verdict(ctx=ctx, outp=outp, usable=usable,
                                                    prompt_cap=min(cap, usable), window=window)
                            with self.subTest(ctx=ctx, outp=outp, usable=usable, cap=cap, window=window):
                                if v["ok"]:
                                    self.assertLessEqual(v["asked"], v["limit"])
                                else:
                                    self.assertGreater(v["asked"], v["limit"])


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



class AWedgedEngineThatLosesHealthIsDegraded(Base):
    """A wedge is only reachable from ready, so when that engine also stops answering it has
    long been serving: "degraded". The check that keeps a fresh boot in warming-up did not
    know the state, and drew a boot bar with an overdue warning over it (found in review,
    2026-09-24)."""
    U = "qwen38-sglang.service"

    def test_it_reads_degraded_not_warming_up(self):
        self.cp.BOOT_HEAD_READ.clear()
        self.cp.BOOT_SEEN.clear()
        fired = ("[2026-09-22 19:53:22] Load weight end. elapsed=118.38 s\n"
                 "[2026-09-22 19:58:40] The server is fired up and ready to roll!\n")
        with self.cp.STATE_LOCK:
            self.cp.STATE["engine_fast"] = {"data": {"healthy": False}}
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"] = {self.U: "wedged"}
        self.cp.UNHEALTHY_TICKS[self.U] = 5
        self.cp.LAST_PROGRESS["ts"] = None
        self.box({f"systemctl show {self.U}": "ActiveState=active\nSubState=running\n"
                                              "ActiveEnterTimestampMonotonic=1000\n",
                  "docker ps -q -f name=^qwen38-sglang$": "c0ffee\n",
                  "docker logs --tail 300 qwen38-sglang": fired,
                  "docker logs --since": fired})
        out = self.cp.collect_lifecycle()
        self.assertEqual(out.get("data", out)["engines"][self.U]["state"], "degraded")


class AStopTimeoutIsNotACrash(Base):
    """A unit systemd killed because it did not stop in time ends "failed" with
    Result=timeout: that is how a Stop during a generation looked on 2026-09-23, and the
    page said "failed, read its journal" about a lane nothing had gone wrong with. The
    lifecycle carries systemd's own word for how the run ended, and the page reads it."""
    U = "qwen38-sglang.service"

    def test_the_result_travels_with_the_engine(self):
        self.box({f"systemctl show {self.U}": "ActiveState=failed\nSubState=failed\nResult=timeout\n"
                                              "ActiveEnterTimestampMonotonic=1000\n"})
        out = self.cp.collect_lifecycle()
        e = out.get("data", out)["engines"][self.U]
        self.assertEqual((e["state"], e["result"]), ("failed", "timeout"))

    def test_the_page_tells_a_stop_timeout_from_a_crash(self):
        js = (REPO / "dashboard" / "static" / "app.js").read_text()
        self.assertIn("e.state === 'failed' && e.result === 'timeout'", js)
        self.assertIn("was killed while stopping.", js)


class TheCockpitsOwnProbeIsNotAClient(Base):
    """GET /health runs a one-token generation in these builds (input_ids=[0],
    max_new_tokens=1, SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION on), and the cockpit sends
    it every 30 s while the engine is idle. Its prefill line counted as client activity:
    the canary, which waits for 60 s without any, never ran once (0 in the audit log of
    the reference box, 2026-09-24, 334 probe lines in 3 h), and the pool guard read the
    probe's 0.00 over the last real usage. The lines are the live ones, verbatim."""
    PROBE = ("[2026-09-24 09:44:53] Prefill batch, #new-seq: 1, #new-token: 1, #cached-token: 0, "
             "full token usage: 0.00, mamba usage: 0.00, #running-req: 0, #queue-req: 0, "
             "#pending-token: 0, cuda graph: False, input throughput (token/s): 0.03\n")
    REAL = ("[2026-09-24 09:45:10] Prefill batch, #new-seq: 1, #new-token: 8192, #cached-token: 40960, "
            "full token usage: 0.71, mamba usage: 0.12, #running-req: 0, #queue-req: 0, "
            "#pending-token: 0, cuda graph: False, input throughput (token/s): 5210.40\n")

    def setUp(self):
        self.cp.LAST_PROGRESS["ts"] = None
        self.cp.LAST_USAGE.update(value=0.93, mamba=0.2, ts=1.0)

    def read(self, logs):
        self.box({"docker ps -q -f name=^qwen38-sglang$": "c0ffee\n",
                  "docker logs --since 30s qwen38-sglang": logs})
        self.cp.collect_decode_telemetry()

    def test_probe_lines_are_neither_activity_nor_a_pool_reading(self):
        self.read(self.PROBE * 3)
        self.assertIsNone(self.cp.LAST_PROGRESS["ts"])
        self.assertEqual(self.cp.LAST_USAGE["value"], 0.93, "the last real reading is kept")

    def test_a_client_line_still_is(self):
        self.read(self.PROBE + self.REAL)
        self.assertIsNotNone(self.cp.LAST_PROGRESS["ts"])
        self.assertEqual(self.cp.LAST_USAGE["value"], 0.71)

    def test_the_canary_runs_on_an_engine_that_only_answered_probes(self):
        self.read(self.PROBE * 3)
        saved = (self.cp.DRY_RUN, self.cp.ENGINE_BASE, dict(self.cp.CANARY))
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"] = {"qwen38-sglang.service": "ready"}
        with self.cp.STATE_LOCK:
            self.cp.STATE["engine_fast"] = {"data": {"load": [{"num_reqs": 0, "num_waiting_reqs": 0}]}}
        self.cp.DRY_RUN, self.cp.ENGINE_BASE = False, "http://127.0.0.1:9"   # closed: it fails fast
        try:
            out = self.cp.collect_canary()
        finally:
            self.cp.DRY_RUN, self.cp.ENGINE_BASE = saved[0], saved[1]
            self.cp.CANARY.clear(); self.cp.CANARY.update(saved[2])
        self.assertFalse(out.get("skipped"), "it was attempted, not skipped as 'engine busy'")


class AnOrphanContainerHoldsTheBox(Base):
    """The same stop timeout with the container still up: systemd killed the docker client,
    the daemon kept the container and its pool. The lifecycle said "failed", which no gate
    counts as busy, so Start on the other lane was allowed next to a live ~100 GB engine.
    The orphan banner only covered a "stopped" unit."""
    U = "qwen38-sglang.service"

    def test_it_is_an_orphan_the_gate_counts_and_the_banner_names(self):
        self.box({f"systemctl show {self.U}": "ActiveState=failed\nSubState=failed\nResult=timeout\n"
                                              "ActiveEnterTimestampMonotonic=1000\n",
                  "docker ps -q -f name=^qwen38-sglang$": "c0ffee\n"})
        out = self.cp.collect_lifecycle()
        d = out.get("data", out)
        self.assertEqual(d["engines"][self.U]["state"], "orphan")
        self.assertEqual([o["unit"] for o in d["orphans"]], [self.U])
        blocked = d["blocked"].get("unit:start:qwen38-flash.service") or []
        self.assertTrue(blocked and "outside systemd" in blocked[0], blocked)
        self.assertNotIn(f"unit:start:{self.U}", d["blocked"],
                         "its own unit may start: ExecStartPre removes the orphan first")


class TheEngineIsAskedItsCurrentRoutes(Base):
    """SGLang logs a deprecation warning for every call of /get_load and /get_server_info,
    on the 27B image and the flash image alike, and says both will go. This cockpit asked
    /get_load every second: 597 warnings in 10 minutes of the 27B's journal (2026-09-23).
    It asks /v1/loads?include=core and /server_info, keeps handing the rest of the cockpit
    and the page the /get_load shape, and falls back only on an engine that answers 404."""

    def engine(self, routes):
        import http.server
        import threading
        seen = []
        load = {"loads": [{"dp_rank": 0, "num_running_reqs": 2, "num_waiting_reqs": 1,
                           "num_total_tokens": 500, "num_used_tokens": 400}]}
        legacy = [{"dp_rank": 0, "num_reqs": 7, "num_waiting_reqs": 0, "num_tokens": 9,
                   "num_pending_tokens": 0, "ts_tic": 1.0}]
        answers = {"/v1/loads?include=core": load, "/get_load": legacy,
                   "/server_info": {"max_total_num_tokens": 900000},
                   "/get_server_info": {"max_total_num_tokens": 800000}}

        class Engine(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(self.path)
                if self.path in routes:
                    out = json.dumps(answers[self.path]).encode()
                    self.send_response(200); self.send_header("Content-Length", str(len(out)))
                    self.end_headers(); self.wfile.write(out); return
                self.send_response(404); self.end_headers()

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        saved = self.cp.ENGINE_BASE
        self.cp.ENGINE_BASE = f"http://127.0.0.1:{srv.server_address[1]}"
        self.addCleanup(setattr, self.cp, "ENGINE_BASE", saved)
        self.cp.ENGINE_ROUTES.update(self.cp.CURRENT_ROUTES)
        self.addCleanup(self.cp.ENGINE_ROUTES.update, self.cp.CURRENT_ROUTES)
        return seen

    def test_a_current_engine_gives_the_old_shape_from_the_new_route(self):
        seen = self.engine({"/v1/loads?include=core", "/server_info"})
        self.assertEqual(self.cp.engine_load(), [{"dp_rank": 0, "num_reqs": 3, "num_waiting_reqs": 1,
                                                  "num_tokens": 500, "num_pending_tokens": 100}])
        self.assertEqual(self.cp.engine_server_info(timeout=3)["max_total_num_tokens"], 900000)
        self.assertEqual(seen, ["/v1/loads?include=core", "/server_info"], "a deprecated route was asked")

    def test_an_engine_without_the_new_routes_still_answers(self):
        seen = self.engine({"/get_load", "/get_server_info"})
        self.assertEqual(self.cp.engine_load()[0]["num_reqs"], 7)
        self.assertEqual(self.cp.engine_server_info(timeout=3)["max_total_num_tokens"], 800000)
        self.cp.engine_load()
        self.assertEqual(seen, ["/v1/loads?include=core", "/get_load", "/server_info", "/get_server_info",
                                "/get_load"], "the fallback is remembered for that engine")

    def test_the_poll_reads_liveness_from_the_new_route(self):
        self.engine({"/v1/loads?include=core", "/server_info"})
        out = self.cp.collect_engine_fast()
        self.assertTrue(out["healthy"], out)
        self.assertEqual(out["load"][0]["num_reqs"], 3)

    def test_a_port_with_no_engine_is_asked_the_new_route_again(self):
        self.engine({"/get_load"})
        self.cp.engine_load()
        self.assertEqual(self.cp.ENGINE_ROUTES["load"], "/get_load")
        self.cp.ENGINE_BASE = "http://127.0.0.1:1"          # the lane switched, nothing listens
        with self.assertRaises(Exception):
            self.cp.engine_load()
        self.assertEqual(self.cp.ENGINE_ROUTES["load"], "/v1/loads?include=core")


if __name__ == "__main__":
    unittest.main(verbosity=2)
