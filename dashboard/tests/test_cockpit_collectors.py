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

# The environment this module sets for the cockpit it loads is handed back when it ends,
# so the next module in the same process starts from what this one found.
ENV_BEFORE = {}


def setUpModule():
    ENV_BEFORE.update(os.environ)


def tearDownModule():
    for name in set(os.environ) - set(ENV_BEFORE):
        del os.environ[name]
    os.environ.update(ENV_BEFORE)


FIX = HERE.parent / "fixtures"
CLOSED = "http://127.0.0.1:1"        # nothing listens there: a request fails at once


def offline(url, timeout=5.0):
    """cockpit._get_json on a box with no route out, which every collector must survive."""
    raise OSError(f"offline test: {url}")


def fixture(name: str) -> str:
    return (FIX / name).read_text()


class FakeBox:
    """Answers cockpit.run() from a table keyed by a fragment of the argv."""

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.calls = []
        self.hits = dict.fromkeys(self.table, 0)   # which answers were ever given

    def __call__(self, argv, timeout=5.0, merge_err=False):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for key, val in self.table.items():
            if key in joined:
                self.hits[key] += 1
                return val
        return ""


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-coll-"))
        (cls.tmp / "api-key").write_text("k\n")
        # The engine, the proxy and the image lane on a closed port, and no request out:
        # with the defaults left in place this suite made 46 requests to the engine
        # serving on the reference box (/health there is a one-token generation), one to
        # the image lane and two to api.github.com (found in review, 2026-09-24). A test
        # that needs an engine starts its own and points ENGINE_BASE at it.
        os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.tmp),
                          COCKPIT_REPO_DIR=str(REPO), COCKPIT_PORT="0",
                          COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0",
                          COCKPIT_ENGINE=CLOSED, COCKPIT_PROXY=CLOSED, COCKPIT_IMAGE=CLOSED)
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_coll_under_test",
                                                      DASH / "cockpit.py")
        cls.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cp)
        cls.cp._get_json = offline
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

    def test_gpu_that_did_not_answer_has_no_process_list(self):
        """An empty list is a GPU with nothing on it: a query that timed out is no list, or
        the page says "No process on the GPU" of a box it could not read (2026-10-02)."""
        def silent(argv, timeout=5.0, merge_err=False):
            r = self.cp.Ran("")
            r.ok = False
            return r
        self.cp.run = silent
        out = self.cp.collect_gpu()
        self.assertEqual((out["power_w"], out["temp_c"], out["procs"]), (None, None, None), out)

    def test_feed_reads_the_keepalive_journal(self):
        self.box({"journalctl -u qwen38-keepalive.service": fixture("keepalive-journal.txt")})
        out = self.cp.collect_feed()
        self.assertNotIn("error", out)
        self.assertTrue(out["rows"], "the real journal produced no request rows")
        for r in out["rows"]:
            self.assertIn("kind", r)
            self.assertIn(r["kind"], ("ok", "gone", "fail", "live", "unknown"))

    def test_feed_keeps_a_long_request_after_its_start_left_the_window(self):
        """The badge and the table count the rows in flight. The first read goes back to the
        proxy's start; later ones read the 800 newest lines and carry what was in flight, so
        a generation whose start left the window stays counted and its end lands
        (2026-10-02: 8 in flight at the proxy and the engine, 2 in the cockpit's rows)."""
        L = "2026-10-02T23:{m:02d}:{s:02d}+02:00 gx10 python3[1]: [proxy] {rest}"
        start = L.format(m=16, s=1, rest="127.0.0.1:49398 -> POST /v1/chat/completions body=111922b")
        short = lambda m: [L.format(m=m, s=i, rest=f"127.0.0.1:{41000 + m * 60 + i} -> POST /v1/chat/completions body=9b")
                           for i in range(30)] + [L.format(m=m, s=i, rest=f"127.0.0.1:{41000 + m * 60 + i} POST /v1/chat/completions ok in 1.0s")
                                                   for i in range(30)]
        end = L.format(m=49, s=49, rest="127.0.0.1:49398 POST /v1/chat/completions ok in 2028.0s")
        calls, answers = [], {"since": "\n".join([start] + short(20)), "window": "\n".join(short(40))}

        def fake(argv, timeout=5.0, merge_err=False):
            calls.append(list(argv))
            if argv[:2] == ["systemctl", "show"]:
                return "@1790974535\n"
            return answers["since"] if "--since" in argv else answers["window"]
        self.cp.run = fake
        self.cp.FEED_CARRY.clear()
        self.cp.FEED_BOOTED[0] = False
        live = lambda out: [r["peer"] for r in out["rows"] if r["outcome"] == "in flight"]
        out = self.cp.collect_feed()
        self.assertIn("--since", calls[-1])
        self.assertEqual(calls[-1][calls[-1].index("--since") + 1], "@1790974535")
        self.assertEqual(live(out), ["127.0.0.1:49398"])
        out = self.cp.collect_feed()
        self.assertNotIn("--since", calls[-1])
        self.assertEqual(calls[-1][calls[-1].index("-n") + 1], "800")
        self.assertEqual(live(out), ["127.0.0.1:49398"], "the window no longer holds its start")
        # its end lands on the carried start (a few newer requests: it is among the 25 newest)
        answers["window"] = "\n".join(short(41)[:3] + short(41)[30:33] + [end])
        out = self.cp.collect_feed()
        self.assertEqual(live(out), [])
        self.assertIn(("127.0.0.1:49398", "ok", 2028.0), [(r["peer"], r["outcome"], r["secs"]) for r in out["rows"]])

    def test_a_first_feed_read_that_timed_out_is_tried_again(self):
        def silent(argv, timeout=5.0, merge_err=False):
            r = self.cp.Ran("" if argv[0] == "journalctl" else "@1790974535\n")
            r.ok = argv[0] != "journalctl"
            return r
        self.cp.run = silent
        self.cp.FEED_CARRY.clear()
        self.cp.FEED_BOOTED[0] = False
        self.cp.collect_feed()
        self.assertFalse(self.cp.FEED_BOOTED[0], "the read since the proxy's start is still to do")

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
                                         "accept_len": 2.41, "gen_tps": None})
        self.assertIsNone(out["prefill"], "no prefill line rode in this tail")

    def test_the_speed_and_the_cache_split_come_off_the_same_tail(self):
        """SGLang prints its own rates on these very lines: gen throughput on
        the decode line, input throughput and the radix cache split on the
        prefill line. Both lines are the reference box's, verbatim."""
        log = ("[2026-09-24 09:45:10] Prefill batch, #new-seq: 1, #new-token: 8192, #cached-token: 40960, "
               "full token usage: 0.71, mamba usage: 0.12, #running-req: 0, #queue-req: 0, "
               "#pending-token: 0, cuda graph: False, input throughput (token/s): 5210.40\n"
               "[2026-09-10 10:00:02] Decode batch, #running-req: 1, token usage: 0.02, "
               "accept len: 2.40, gen throughput (token/s): 70.1\n")
        self.box({"docker ps -q": "abc123\n", "docker logs": log})
        out = self.cp.collect_decode_telemetry()
        self.assertEqual(out["decode"], {"running": 1, "token_usage": 0.02,
                                         "accept_len": 2.40, "gen_tps": 70.1})
        self.assertEqual(out["prefill"], {"input_tps": 5210.4,
                                          "cached_tokens": 40960, "new_tokens": 8192})

    def test_several_prefill_lines_add_up_rather_than_replace(self):
        """A long prompt comes through the engine's log as several Prefill lines
        inside one tail. The reuse share is of the tail's totals: last-line-wins
        would drop most of the prompt, and the page could not see it."""
        log = ("[2026-09-24 09:45:10] Prefill batch, #new-seq: 1, #new-token: 8192, #cached-token: 40960, "
               "full token usage: 0.71, mamba usage: 0.12, #running-req: 0, #queue-req: 0, "
               "#pending-token: 0, cuda graph: False, input throughput (token/s): 5210.40\n"
               "[2026-09-24 09:45:11] Prefill batch, #new-seq: 1, #new-token: 512, #cached-token: 1024, "
               "full token usage: 0.72, mamba usage: 0.12, #running-req: 0, #queue-req: 0, "
               "#pending-token: 0, cuda graph: False, input throughput (token/s): 300.00\n")
        self.box({"docker ps -q": "abc123\n", "docker logs": log})
        out = self.cp.collect_decode_telemetry()
        self.assertEqual(out["prefill"], {"input_tps": 300.0,
                                          "cached_tokens": 41984, "new_tokens": 8704})

    def test_a_prefill_line_of_printed_zeros_is_still_a_reading(self):
        """The gate is `is not None`: a chunk whose instantaneous rate prints 0.0
        with no tokens moved is a speed of 0 and reaches the page, it does not
        vanish with the rate."""
        log = ("[2026-09-24 09:45:10] Prefill batch, #new-seq: 1, #new-token: 0, #cached-token: 0, "
               "full token usage: 0.0, mamba usage: 0.0, #running-req: 0, #queue-req: 0, "
               "#pending-token: 0, cuda graph: False, input throughput (token/s): 0.00\n")
        self.box({"docker ps -q": "abc123\n", "docker logs": log})
        out = self.cp.collect_decode_telemetry()
        self.assertEqual(out["prefill"], {"input_tps": 0.0, "cached_tokens": 0, "new_tokens": 0})

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
                  "journalctl": fixture("keepalive-journal.txt")})
        for name, fn in self.collectors().items():
            with self.subTest(collector=name):
                out = fn()
                self.assertIsInstance(out, dict, name)
        # and every captured output in the table reached a collector: one no collector
        # asks for is a fixture that tests nothing (found in review, 2026-09-24)
        self.assertEqual([k for k, n in self.cp.run.hits.items() if not n], [])


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



class TheLaneSaysWhyTheStartGuardHoldsIt(Base):
    """engine-preflight.sh writes to the unit's journal, and while it waits there is no
    container, so the container log the lifecycle reads never had its reason: a held lane
    read "starting", a refused one "keeps crashing" (issue #26)."""
    U = "qwen38-sglang.service"
    W = ("preflight: qwen38-sglang.service waits: what is left of an engine still holds 16 GiB of GPU "
         "memory: 1756018 sglang::stuck 16578 MiB\n")
    R = ("preflight: NOT starting qwen38-sglang.service: the GPU driver holds 50.9 GiB that no process "
         "accounts for, still after 60 s. Starting into memory the driver has not given back is how "
         "sglang#40948 froze a DGX Spark until a power cycle. Stop what holds it, or reboot if nothing "
         "does; systemd tries again in 15 s.\n")

    def setUp(self):
        self.cp.BOOT_SEEN.clear()
        with self.cp.STATE_LOCK:
            self.cp.STATE["engine_fast"] = {"data": {"healthy": False}}

    def tick(self, active, sub, journal, container=""):
        fake = self.box({f"systemctl show {self.U}": f"ActiveState={active}\nSubState={sub}\n"
                                                     "ActiveEnterTimestampMonotonic=0\nInvocationID=abc123\n",
                         "docker ps -q -f name=^qwen38-sglang$": container,
                         "_SYSTEMD_INVOCATION_ID=abc123": journal})
        out = self.cp.collect_lifecycle()
        return out.get("data", out)["engines"][self.U], fake

    def test_held_while_it_waits(self):
        e, fake = self.tick("activating", "start-pre", self.W)
        self.assertEqual(e["state"], "starting")
        self.assertEqual(e["held"], "what is left of an engine still holds 16 GiB of GPU memory: "
                                    "1756018 sglang::stuck 16578 MiB")
        self.assertIsNone(e["refused"])
        reads = [c for c in fake.calls if c[:1] == ["journalctl"] and self.U in c]
        self.assertEqual(len(reads), 1)
        self.assertIn("-g", reads[0])
        self.assertIn("_SYSTEMD_INVOCATION_ID=abc123", reads[0], "this run's lines, never an older run's")

    def test_refused_between_two_attempts(self):
        e, _ = self.tick("activating", "auto-restart", self.R)
        self.assertEqual((e["state"], e["restarting"]), ("failed", True))
        self.assertEqual(e["refused"], "the GPU driver holds 50.9 GiB that no process accounts for")
        self.assertIsNone(e["held"])

    def test_a_lane_that_is_simply_starting_has_no_reason(self):
        e, _ = self.tick("activating", "start-pre", "")
        self.assertEqual(e["state"], "starting")
        self.assertIsNone(e["held"])

    def test_a_serving_lane_never_reads_it(self):
        _, fake = self.tick("active", "running", self.W, container="c0ffee\n")
        self.assertFalse([c for c in fake.calls if c[:1] == ["journalctl"] and self.U in c and "-g" in c])


class AServerWithoutItsSchedulerIsRestartedOnce(Base):
    """SGLang v0.5.19 can be left with its server process alive and no scheduler (a SIGQUIT
    that reaches the server while the scheduler runs; reproduced on the reference box,
    2026-10-01): no HTTP, a unit systemd calls active, and nothing restarts it."""
    U = "qwen38-sglang.service"
    FIRED = ("[2026-10-01 12:12:30] Load weight end. elapsed=119.55 s\n"
             "[2026-10-01 12:17:42] The server is fired up and ready to roll!\n")

    def setUp(self):
        self.cp.BOOT_SEEN.clear()
        self.cp.BOOT_HEAD_READ.clear()
        self.cp.ZOMBIE_SINCE.clear()
        self.cp.LAST_ZOMBIE_RESTART.clear()
        self.actions = []
        real_alive, real_start = self.cp.scheduler_alive, self.cp.start_action
        self.addCleanup(setattr, self.cp, "scheduler_alive", real_alive)
        self.addCleanup(setattr, self.cp, "start_action", real_start)
        self.cp.start_action = lambda kind, args, origin="": (self.actions.append((kind, args, origin)), (0, "ok"))[1]
        self.alive = False
        self.cp.scheduler_alive = lambda container: self.alive

    def tick(self):
        with self.cp.STATE_LOCK:
            self.cp.STATE["engine_fast"] = {"data": {"healthy": False}}
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"] = {self.U: "degraded"}
        self.cp.UNHEALTHY_TICKS[self.U] = 5
        self.cp.LAST_PROGRESS["ts"] = None
        self.box({f"systemctl show {self.U}": "ActiveState=active\nSubState=running\n"
                                              "ActiveEnterTimestampMonotonic=1000\nInvocationID=abc\n",
                  "docker ps -q -f name=^qwen38-sglang$": "c0ffee\n",
                  "docker logs --tail 300 qwen38-sglang": self.FIRED,
                  "docker logs --since": self.FIRED})
        out = self.cp.collect_lifecycle()
        return out.get("data", out)["engines"][self.U]

    def test_it_is_named_then_restarted_once_after_its_delay(self):
        e = self.tick()
        self.assertEqual((e["state"], e["zombie"], e["zombie_in"]), ("degraded", True, 120))
        self.assertEqual(self.actions, [])
        self.cp.ZOMBIE_SINCE[self.U] = __import__("time").time() - 121
        self.tick()
        self.assertEqual(self.actions, [("unit", {"verb": "restart", "unit": self.U}, "zombie")])
        self.tick()
        self.assertEqual(len(self.actions), 1, "one restart per half hour, not one per tick")

    def test_a_server_with_its_scheduler_is_left_alone(self):
        self.alive = True
        e = self.tick()
        self.assertFalse(e["zombie"])
        self.assertNotIn(self.U, self.cp.ZOMBIE_SINCE)
        self.alive = None                     # an unreadable process list decides nothing
        self.assertFalse(self.tick()["zombie"])
        self.assertEqual(self.actions, [])

    def test_off_it_says_so_and_does_nothing(self):
        self.cp.ZOMBIE_RESTART = False
        self.addCleanup(setattr, self.cp, "ZOMBIE_RESTART", True)
        self.cp.ZOMBIE_SINCE[self.U] = __import__("time").time() - 600
        self.assertTrue(self.tick()["zombie"])
        self.assertEqual(self.actions, [])


class ACrashWhileServingIsNoCrashLoop(Base):
    """A serving engine that crashes waits 15 s for its next attempt, exactly like a boot that
    keeps dying, and the page said "keeps crashing, it dies during startup" for both (seen
    2026-10-01 after one crash of the serving flash lane). The lane tells them apart now."""
    U = "qwen38-sglang.service"

    def tick(self, active, sub, prev):
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"] = {self.U: prev}
        with self.cp.STATE_LOCK:
            self.cp.STATE["engine_fast"] = {"data": {"healthy": False}}
        self.box({f"systemctl show {self.U}": f"ActiveState={active}\nSubState={sub}\n"
                                              "ActiveEnterTimestampMonotonic=0\nInvocationID=abc\nNRestarts=1\n",
                  "docker ps -q -f name=^qwen38-sglang$": ""})
        out = self.cp.collect_lifecycle()
        return out.get("data", out)["engines"][self.U]

    def test_down_while_serving_then_down_at_boot(self):
        self.cp.CRASHED_SERVING.clear()
        e = self.tick("activating", "auto-restart", "ready")
        self.assertEqual((e["state"], e["restarting"], e["crashed_serving"]), ("failed", True, True))
        e = self.tick("activating", "auto-restart", "failed")       # the rest of the same 15 s
        self.assertTrue(e["crashed_serving"])
        e = self.tick("activating", "start", "failed")              # the next attempt boots
        self.assertEqual((e["state"], e["crashed_serving"]), ("starting", False))
        e = self.tick("activating", "auto-restart", "starting")     # and dies at boot: a loop
        self.assertEqual((e["state"], e["restarting"], e["crashed_serving"]), ("failed", True, False))

    def test_a_degraded_or_wedged_engine_that_goes_down_was_serving_too(self):
        for prev in ("degraded", "wedged"):
            with self.subTest(prev=prev):
                self.cp.CRASHED_SERVING.clear()
                self.assertTrue(self.tick("activating", "auto-restart", prev)["crashed_serving"])


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


class ACockpitRestartFacingAServingEngine(Base):
    """Every cockpit restart logged "warming-up -> ready" for the serving lane two seconds
    later (five times in the event log of 2026-09-29/30). The lifecycle tier's first pass
    ran before the first health sample existed, read "no sample" as "unhealthy", and
    turned the "degraded" that gave into "warming-up", having no previous state. And an
    engine that really was not answering when the cockpit came up read "warming up" for as
    long as it lasted: a boot state, which holds every switch back."""
    U = "qwen38-flash.service"
    FIRED = ("[2026-09-30 15:16:03] KV Cache is allocated. dtype: torch.bfloat16, #tokens: 491136\n"
             "[2026-09-30 15:16:35] The server is fired up and ready to roll!\n")
    NOW = 100_000.0

    def setUp(self):
        self.cp.BOOT_HEAD_READ.clear()
        self.cp.BOOT_SEEN.clear()
        self.cp.UNHEALTHY_TICKS.clear()
        self.cp.LAST_PROGRESS["ts"] = None
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"], self.cp.LIFE["witnessed"], self.cp.LIFE["enter"] = {}, {}, {}
        (self.tmp / self.cp.HISTORY_FILE_NAME).unlink(missing_ok=True)
        self.saved_mono = self.cp.monotonic_now
        self.cp.monotonic_now = lambda: self.NOW

    def tearDown(self):
        self.cp.monotonic_now = self.saved_mono
        (self.tmp / self.cp.HISTORY_FILE_NAME).unlink(missing_ok=True)
        super().tearDown()

    def tick(self, healthy, age_s):
        with self.cp.STATE_LOCK:
            self.cp.STATE["engine_fast"] = {"data": {"healthy": healthy}}
        enter = int((self.NOW - age_s) * 1e6)
        self.box({f"systemctl show {self.U}": "ActiveState=active\nSubState=running\n"
                                              f"ActiveEnterTimestampMonotonic={enter}\n",
                  "docker ps -q -f name=^qwen38-flash$": "c0ffee\n",
                  "docker logs --tail 300 qwen38-flash": self.FIRED,
                  "docker logs --since": self.FIRED})
        out = self.cp.collect_lifecycle()
        return out.get("data", out)

    def test_the_other_tiers_start_once_health_has_been_sampled(self):
        import threading
        import time
        seen = []
        with self.cp.STATE_LOCK:
            self.cp.STATE.pop("engine_fast", None)

        def spawn(period, cols):
            if "engine_fast" in cols:
                def first_sample():
                    time.sleep(0.3)
                    with self.cp.STATE_LOCK:
                        self.cp.STATE["engine_fast"] = {"data": {"healthy": True}}
                threading.Thread(target=first_sample, daemon=True).start()
                return
            with self.cp.STATE_LOCK:
                seen.append((tuple(cols), "engine_fast" in self.cp.STATE))
        self.cp.start_samplers(spawn=spawn, wait_s=5)
        rest = [t for t in self.cp.TIERS if "engine_fast" not in t[1]]
        deadline = time.time() + 6
        while len(seen) < len(rest) and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(seen), len(rest), seen)
        self.assertTrue(all(had for _, had in seen), seen)
        self.assertIn((("lifecycle",), True), seen)

    def test_a_health_tier_that_never_answers_does_not_hold_the_others_back(self):
        import time
        started = []
        with self.cp.STATE_LOCK:
            self.cp.STATE.pop("engine_fast", None)
        self.cp.start_samplers(spawn=lambda period, cols: started.append(tuple(cols)), wait_s=0.2)
        deadline = time.time() + 3
        while len(started) < len(self.cp.TIERS) and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(started), len(self.cp.TIERS), started)

    def test_an_engine_fired_up_hours_ago_and_silent_is_degraded(self):
        d = self.tick(healthy=False, age_s=3 * 3600)
        self.assertEqual(d["engines"][self.U]["state"], "degraded")
        self.assertNotIn("switch", d["blocked"], "a lane that has served is no boot in progress")

    def test_an_activation_younger_than_a_boot_still_reads_warming_up(self):
        d = self.tick(healthy=False, age_s=60)
        self.assertEqual(d["engines"][self.U]["state"], "warming-up")
        self.assertIn("switch", d["blocked"], "a warm-up holds the switch back, which is why the line matters")

    def test_a_boot_this_cockpit_watched_keeps_its_warm_up(self):
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["witnessed"][self.U] = True
        d = self.tick(healthy=False, age_s=3 * 3600)
        self.assertEqual(d["engines"][self.U]["state"], "warming-up")

    def test_the_line_is_the_boxs_own_median_boot_plus_the_grace(self):
        (self.tmp / self.cp.HISTORY_FILE_NAME).write_text(json.dumps({self.U: [600.0, 610.0, 620.0]}))
        edge = 610.0 + self.cp.WARMUP_GRACE_S
        self.assertEqual(self.tick(healthy=False, age_s=edge - 5)["engines"][self.U]["state"], "warming-up")
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"] = {}
        self.assertEqual(self.tick(healthy=False, age_s=edge + 5)["engines"][self.U]["state"], "degraded")

    def test_an_engine_that_answers_is_ready_at_the_first_pass(self):
        d = self.tick(healthy=True, age_s=3 * 3600)
        self.assertEqual(d["engines"][self.U]["state"], "ready")


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
        js = (REPO / "dashboard" / "static" / "js" / "base.js").read_text()
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
        return self.cp.collect_decode_telemetry()

    def test_probe_lines_are_neither_activity_nor_a_pool_nor_a_rate_reading(self):
        out = self.read(self.PROBE * 3)
        self.assertIsNone(self.cp.LAST_PROGRESS["ts"])
        self.assertEqual(self.cp.LAST_USAGE["value"], 0.93, "the last real reading is kept")
        self.assertIsNone(out["prefill"], "the probe's 0.03 is never shown as a prefill speed")

    def test_a_client_line_still_is(self):
        out = self.read(self.PROBE + self.REAL)
        self.assertIsNotNone(self.cp.LAST_PROGRESS["ts"])
        self.assertEqual(self.cp.LAST_USAGE["value"], 0.71)
        self.assertEqual(out["prefill"]["input_tps"], 5210.4)

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
                                                  "num_tokens": 500, "num_used_tokens": 400,
                                                  "num_pending_tokens": 100}])
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


class AnEngineThatServedLosesHealthAsDegraded(Base):
    """The cockpit reads an engine's log only while its health is down, so in a boot it
    watched, the last stage it saw was the one before "fired up". When the engine lost health
    later with no milestone left in the last 300 lines (a crash's tracebacks), that stage came
    back: the lane read "capturing graphs", a boot, and the zombie restart, which follows
    "degraded" only, never came (the reference box, 2026-10-01)."""
    U = "qwen38-flash.service"
    # the flash lane's own lines, from its journal of 2026-10-01
    BOOTING = ("[2026-10-01 19:23:17] Load weight begin. avail mem=109.83 GB\n"
               "[2026-10-01 19:30:58] Load weight end. elapsed=461.30 s, type=Qwen4ExpForConditionalGeneration, "
               "quant=modelopt_fp4, quant_algo=NVFP4, avail mem=30.04 GB\n"
               "[2026-10-01 19:32:30] KV Cache is allocated. dtype: torch.bfloat16, #tokens: 482176, K size: 5.52 GB\n"
               "[2026-10-01 19:33:06] Capture target verify CUDA graph begin. backend=full, num_tokens_per_req=4, "
               "bs=[1, 2, 3, 4, 5, 6, 7, 8], avail mem=12.92 GB\n")
    CRASH = ("Traceback (most recent call last):\n"
             '  File "/usr/lib/python3.12/asyncio/locks.py", line 212, in wait\n'
             "    await fut\n"
             "asyncio.exceptions.CancelledError\n") * 70

    def setUp(self):
        for d in (self.cp.BOOT_SEEN, self.cp.BOOT_HEAD_READ, self.cp.ZOMBIE_SINCE):
            d.clear()
        real_alive = self.cp.scheduler_alive
        self.addCleanup(setattr, self.cp, "scheduler_alive", real_alive)
        self.cp.scheduler_alive = lambda container: True     # a scheduler: no zombie here

    def tick(self, healthy, tail, prev):
        with self.cp.STATE_LOCK:
            self.cp.STATE["engine_fast"] = {"data": {"healthy": healthy}}
        with self.cp.LIFE_LOCK:
            self.cp.LIFE["states"] = {self.U: prev}
        self.cp.UNHEALTHY_TICKS[self.U] = 0 if healthy else 5
        self.cp.LAST_PROGRESS["ts"] = None
        self.box({f"systemctl show {self.U}": "ActiveState=active\nSubState=running\n"
                                              "ActiveEnterTimestampMonotonic=1000\nInvocationID=abc\n",
                  "docker ps -q -f name=^qwen38-flash$": "c0ffee\n",
                  "docker logs --tail 300 qwen38-flash": tail,
                  "docker logs --since": tail})
        out = self.cp.collect_lifecycle()
        return out.get("data", out)["engines"][self.U]["state"]

    def test_a_boot_it_watched_then_served_reads_degraded_once_health_goes(self):
        self.assertEqual(self.tick(False, self.BOOTING, "loading-weights"), "capturing-graphs")
        self.assertEqual(self.tick(True, "", "capturing-graphs"), "ready")
        self.assertEqual(self.tick(False, self.CRASH, "ready"), "degraded")

    def test_a_boot_that_never_served_still_reads_its_stage(self):
        self.assertEqual(self.tick(False, self.BOOTING, "loading-weights"), "capturing-graphs")
        self.assertEqual(self.tick(False, self.CRASH, "capturing-graphs"), "capturing-graphs")


class TheSchedulerProbeAsksDockerTheWayItAnswers(Base):
    """scheduler_alive() against a docker that answers as the daemon does. `docker top`
    refuses a field list without the PID ("Couldn't find PID field in ps output"): asked for
    `comm` alone, the probe never answered on the reference box, so no zombie was ever
    decided there. The tests above stand a lambda in for the probe and could not see it
    (found by the end-to-end test of a real zombie, 2026-10-01)."""

    DOCKER = """#!/bin/sh
[ "$1" = top ] || exit 2
c="$2"; shift 2
[ "$1" = -eo ] || { echo "unexpected: $*" >&2; exit 2; }
case ",$2," in
  *,pid,*) ;;
  *) echo "Error response from daemon: Couldn't find PID field in ps output" >&2; exit 1 ;;
esac
case "$c" in
  serving) printf 'PID                 COMMAND\n2218964             python3\n2220142             sglang::schedul\n' ;;
  zombie) printf 'PID                 COMMAND\n2218964             python3\n' ;;
  *) echo "Error response from daemon: No such container: $c" >&2; exit 1 ;;
esac
"""

    def setUp(self):
        bin_dir = Path(tempfile.mkdtemp(prefix="fake-docker-"))
        self.addCleanup(shutil.rmtree, bin_dir, True)
        (bin_dir / "docker").write_text(self.DOCKER)
        (bin_dir / "docker").chmod(0o755)
        path = os.environ["PATH"]
        self.addCleanup(os.environ.__setitem__, "PATH", path)
        os.environ["PATH"] = f"{bin_dir}:{path}"

    def test_a_scheduler_listed_is_alive_and_one_gone_is_not(self):
        self.assertIs(self.cp.scheduler_alive("serving"), True)
        self.assertIs(self.cp.scheduler_alive("zombie"), False)

    def test_a_container_it_cannot_read_decides_nothing(self):
        self.assertIsNone(self.cp.scheduler_alive("gone"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
