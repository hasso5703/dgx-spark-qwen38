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


if __name__ == "__main__":
    unittest.main(verbosity=2)
