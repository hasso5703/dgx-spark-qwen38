#!/usr/bin/env python3
"""No engine starts on GPU memory the last one has not given back.

Issue #26: a 27B engine died with "CUDA error: operation not permitted" and systemd started
it again 17 s later, the window sglang#40948 blames for a DGX Spark frozen 9.5 h until a
power cycle. Measured on the reference box (2026-10-01): a crashed engine's 92 GB comes back
2 to 5 s after its scheduler dies, so the relaunch lands on free memory; the freeze is the
other case, a driver that kept a dead engine's memory while the next start piled on top of
it. Every engine unit now runs engine-preflight.sh first, which refuses a start while what
is left of an engine holds GPU memory, while the driver holds memory no process accounts
for, or while nvidia-smi does not answer, and lets anything else through.

These run the guard as written against a fake /proc/meminfo, a fake nvidia-smi and a fake
/proc, and pin where the units and the installers put it."""
import importlib.util
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
GUARD = REPO / "engine-preflight.sh"
UNITS = {"qwen38-sglang.service.template": "qwen38-sglang.service",
         "qwen38-sglang-1m.service.template": "qwen38-sglang.service",
         "qwen38-flash.service.template": "qwen38-flash.service",
         "qwen38-image.service.template": "qwen38-image.service",
         "qwen38-video.service.template": "qwen38-video.service"}
# MemTotal minus every category the kernel accounts for: 1.35 GiB, the reference box's own
# figure with no engine and a desktop session (1.1 GiB measured)
MEMINFO = """MemTotal:       127535084 kB
MemFree:        102000000 kB
MemAvailable:   120000000 kB
Buffers:           10000 kB
Cached:         17000000 kB
SwapCached:        50000 kB
AnonPages:       4000000 kB
KReclaimable:    1500000 kB
SUnreclaim:      1400000 kB
KernelStack:       40000 kB
PageTables:       100000 kB
SecPageTables:         0 kB
Percpu:            20000 kB
"""


class Box:
    """A fake /proc with three processes: 101 an SGLang scheduler, 102 SGLang's launcher
    (a python3 whose command line names sglang), 103 somebody's own training script."""

    def __init__(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="preflight-"))
        self.proc = self.t / "proc"
        for pid, comm, cmd in (("101", "sglang::schedul", "x"),
                               ("102", "python3", "python3\0-m\0sglang.launch_server"),
                               ("103", "python3", "python3\0train.py")):
            (self.proc / pid).mkdir(parents=True)
            (self.proc / pid / "comm").write_text(comm + "\n")
            (self.proc / pid / "cmdline").write_text(cmd + "\0")
        self.meminfo(0)

    def meminfo(self, orphan_gib):
        """The fake meminfo, with orphan_gib more GiB that no kernel category accounts for."""
        free = 102000000 - int(orphan_gib * 1048576)
        (self.t / "meminfo").write_text(re.sub(r"MemFree: +\d+", f"MemFree:        {free}", MEMINFO))

    def smi(self, body):
        (self.t / "smi").write_text("#!/bin/sh\n" + body + "\n")
        (self.t / "smi").chmod(0o755)

    def run(self, wait_s=2, smi_timeout_s=10, extra=None):
        env = {"PATH": "/usr/bin:/bin", "QWEN38_PREFLIGHT_MEMINFO": str(self.t / "meminfo"),
               "QWEN38_PREFLIGHT_SMI": str(self.t / "smi"), "QWEN38_PREFLIGHT_PROC": str(self.proc),
               "QWEN38_PREFLIGHT_WAIT_S": str(wait_s), "QWEN38_PREFLIGHT_STEP_S": "1",
               "QWEN38_PREFLIGHT_SMI_TIMEOUT_S": str(smi_timeout_s), **(extra or {})}
        t0 = time.monotonic()
        r = subprocess.run(["bash", str(GUARD), "qwen38-test.service"], capture_output=True, text=True,
                           env=env, timeout=120)
        return r.returncode, r.stdout + r.stderr, time.monotonic() - t0

    def done(self):
        shutil.rmtree(self.t, ignore_errors=True)


class WhatBlocksAStart(unittest.TestCase):
    def setUp(self):
        self.box = Box()
        self.addCleanup(self.box.done)

    def test_nothing_held_starts_at_once(self):
        self.box.smi("exit 0")
        rc, out, secs = self.box.run()
        self.assertEqual(rc, 0, out)
        self.assertIn("preflight: qwen38-test.service starts: no GPU memory held from before", out)
        self.assertLess(secs, 2, out)

    def test_a_scheduler_still_holding_memory_is_refused_and_named(self):
        self.box.smi('echo "101, sglang::scheduler, 89531"')
        rc, out, _ = self.box.run()
        self.assertEqual(rc, 1, out)
        self.assertIn("preflight: qwen38-test.service waits: what is left of an engine still holds 87 GiB", out)
        self.assertIn("preflight: NOT starting qwen38-test.service:", out)
        self.assertIn("101 sglang::schedul 89531 MiB", out)
        self.assertEqual(out.count("waits:"), 1, "one waiting line per reason, not one per poll")

    def test_the_launcher_and_a_pid_the_system_lost_are_remnants_too(self):
        for line, named in (('echo "102, python3, 40000"', "102 python3 40000 MiB"),
                            ('echo "999, python3, 30000"', "999 gone 30000 MiB")):
            with self.subTest(named=named):
                self.box.smi(line)
                rc, out, _ = self.box.run()
                self.assertEqual(rc, 1, out)
                self.assertIn(named, out)

    def test_another_programs_gpu_memory_never_blocks_a_start(self):
        """Somebody's own GPU work is no remnant: the lane starts and the journal names it."""
        self.box.smi('echo "103, python3, 16578"')
        rc, out, _ = self.box.run()
        self.assertEqual(rc, 0, out)
        self.assertIn("other programs hold GPU memory: 103 python3 16578 MiB", out)

    def test_a_small_remnant_is_below_the_line(self):
        """The tokenizer of an engine being torn down holds 196 MiB for a few seconds."""
        self.box.smi('echo "101, sglang::scheduler, 196"')
        self.assertEqual(self.box.run()[0], 0)
        self.box.smi('echo "101, sglang::scheduler, 2049"')
        self.assertEqual(self.box.run()[0], 1)

    def test_memory_the_driver_keeps_for_nobody_is_refused_past_16_gib(self):
        self.box.smi("exit 0")
        self.box.meminfo(14.0)        # 1.35 + 14.0 = 15.35 GiB unaccounted: a desktop at its worst
        self.assertEqual(self.box.run()[0], 0)
        self.box.meminfo(15.0)        # 16.35 GiB
        rc, out, _ = self.box.run()
        self.assertEqual(rc, 1, out)
        self.assertIn("the GPU driver holds 16.3 GiB that no process accounts for", out)

    def test_listed_memory_is_not_counted_as_the_drivers_own(self):
        """An engine holding 50 GiB raises the unaccounted figure by 50 GiB: that is its own,
        not memory the driver keeps for nobody."""
        self.box.meminfo(50.0)
        self.box.smi('echo "103, python3, 51200"')
        self.assertEqual(self.box.run()[0], 0)

    def test_a_driver_that_does_not_answer_is_refused(self):
        for body in ("exit 9", "sleep 30"):
            with self.subTest(smi=body):
                self.box.smi(body)
                rc, out, secs = self.box.run(wait_s=1, smi_timeout_s=1)
                self.assertEqual(rc, 1, out)
                self.assertIn("the GPU driver does not answer nvidia-smi", out)
                self.assertLess(secs, 15, "a hung nvidia-smi is cut off, not waited for")

    def test_odd_lines_are_read_or_left_out(self):
        """[N/A] memory has nothing to count, and a process name may hold commas."""
        self.box.smi('echo "103, python3, [N/A]"; echo ""; echo "103, my,odd,app, 16578"')
        rc, out, _ = self.box.run()
        self.assertEqual(rc, 0, out)
        self.assertIn("other programs hold GPU memory: 103 python3 16578 MiB", out)
        self.box.smi('echo "101, sglang, odd, 60000"')
        self.assertEqual(self.box.run()[0], 1)

    def test_memory_released_while_it_waits_lets_the_start_through(self):
        flag = self.box.t / "released"
        self.box.smi(f'[ -f {flag} ] || echo "101, sglang::scheduler, 60000"')
        proc = subprocess.Popen(["sh", "-c", f"sleep 2; touch {flag}"])
        self.addCleanup(proc.wait)
        rc, out, secs = self.box.run(wait_s=20)
        self.assertEqual(rc, 0, out)
        self.assertIn("waits:", out)
        self.assertIn("starts:", out)
        self.assertLess(secs, 10, out)


class WhereItRuns(unittest.TestCase):
    def test_every_engine_unit_runs_it_before_it_starts(self):
        for tpl, unit in UNITS.items():
            with self.subTest(tpl=tpl):
                text = (REPO / tpl).read_text()
                line = f"ExecStartPre=/bin/bash __HOME__/.config/qwen38/engine-preflight.sh {unit}\n"
                self.assertEqual(text.count(line), 1, tpl)
                self.assertLess(text.index(line), text.index("ExecStart="), tpl)
                rm = re.search(r"ExecStartPre=-/usr/bin/docker rm -f \S+\n", text)
                if rm:
                    # the old container goes first, then the guard waits for its memory
                    self.assertLess(rm.start(), text.index(line), tpl)

    def test_no_attempt_outlasts_the_units_start_timeout(self):
        """systemd kills a start step after TimeoutStartSec (90 s by default, and no engine
        unit sets another): a guard killed mid-wait would refuse with no word said."""
        guard = GUARD.read_text()
        wait = int(re.search(r'WAIT_S="\$\{QWEN38_PREFLIGHT_WAIT_S:-(\d+)\}"', guard).group(1))
        step = int(re.search(r'STEP_S="\$\{QWEN38_PREFLIGHT_STEP_S:-(\d+)\}"', guard).group(1))
        smi = int(re.search(r'SMI_TIMEOUT_S="\$\{QWEN38_PREFLIGHT_SMI_TIMEOUT_S:-(\d+)\}"', guard).group(1))
        self.assertIn('timeout -k 2 "$SMI_TIMEOUT_S"', guard)
        for tpl in UNITS:
            text = (REPO / tpl).read_text()
            m = re.search(r"^TimeoutStartSec=(\d+)", text, re.M)
            budget = int(m.group(1)) if m else 90
            self.assertLess(wait + step + smi + 2 + 5, budget, tpl)

    def test_every_installer_that_writes_such_a_unit_puts_it_in_place(self):
        """Run on its own, install-image.sh or install-video.sh would leave a unit naming a
        script that is not there, and the lane would never start."""
        for inst, src in (("install.sh", "$REPO_DIR"), ("install-image.sh", "$HERE"), ("install-video.sh", "$HERE")):
            with self.subTest(inst=inst):
                text = (REPO / inst).read_text()
                self.assertIn(f'install -m 755 "{src}/engine-preflight.sh" "$CONFIG_DIR/engine-preflight.sh"', text)
                # written only when it changed, so an unchanged run rewrites nothing
                self.assertIn(f'cmp -s "{src}/engine-preflight.sh" "$CONFIG_DIR/engine-preflight.sh"', text)
        for inst in ("install-image.sh", "install-video.sh"):
            text = (REPO / inst).read_text()
            # in place before the unit naming it is rendered ("$HERE/$UNIT.template")
            self.assertLess(text.index("engine-preflight.sh"), text.index('"$HERE/$UNIT.template"'), inst)
        install = (REPO / "install.sh").read_text()
        self.assertLess(install.index('install -m 755 "$REPO_DIR/engine-preflight.sh"'),
                        install.index("qwen38-sglang.service.template"))

    def test_it_is_a_bash_script_that_says_what_it_does_on_one_line(self):
        guard = GUARD.read_text()
        self.assertTrue(guard.startswith("#!/usr/bin/env bash\n"))
        self.assertTrue(GUARD.stat().st_mode & 0o111, "executable in the repo")
        # the three lines the cockpit reads to say why a lane is not up yet
        for said in ('"preflight: $LANE starts:', '"preflight: $LANE waits:', '"preflight: NOT starting $LANE:'):
            self.assertIn(said, guard)


class TheCockpitReadsWhatItWrites(unittest.TestCase):
    """A contract with dashboard/lifecycle.py, run as written: the cockpit reads these lines
    from the unit's journal, and a pattern written against a remembered line would let the
    two drift apart. It sat in the cockpit's suite, where the mutation gate replays the suite
    for every mutant of lifecycle.py (309 of them), a second each."""

    def setUp(self):
        self.box = Box()
        self.addCleanup(self.box.done)
        spec = importlib.util.spec_from_file_location("lifecycle_read_by_the_guard_test",
                                                      REPO / "dashboard" / "lifecycle.py")
        self.lc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.lc)

    def test_its_lines_are_the_words_the_cockpit_reads(self):
        self.box.smi('echo "101, sglang::scheduler, 60000"')
        rc, out, _ = self.box.run(wait_s=1)
        self.assertEqual(rc, 1, out)
        held = "what is left of an engine still holds 58 GiB of GPU memory: 101 sglang::schedul 60000 MiB"
        lines = out.splitlines()
        self.assertEqual(self.lc.parse_preflight(lines[:1]), {"verdict": "waits", "reason": held})
        self.assertEqual(self.lc.parse_preflight(lines), {"verdict": "refused", "reason": held})
        self.box.smi("exit 0")
        rc, out, _ = self.box.run()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.lc.parse_preflight(out.splitlines())["verdict"], "starts")


if __name__ == "__main__":
    unittest.main()
