"""Offline tests for lifecycle.py, built on REAL log lines from this box
(qwen38-flash boot of 2026-08-28 21:19 and qwen38-sglang boots of 08-28)."""
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE.parents[1]))
import lifecycle as lc  # noqa: E402

FLASH_BOOT = [
    "[2026-08-28 21:19:23] server_args=ServerArgs(model_path='RadixArk/Qwen3.8-Flash-Next-NVFP4', revision='7b71922')",
    "[2026-08-28 21:19:30] Load weight begin. avail mem=111.97 GB",
    "[2026-08-28 21:19:32] PLE table -> mmap /ple/ple_table_51200245760_51200245760.bin (47.7 GiB, dtype=torch.float8_e4m3fn)",
    "[2026-08-28 21:19:32] PLE table: madvise(MADV_RANDOM) ok",
    "[2026-08-28 21:32:38] Load weight end. elapsed=787.55 s, type=Qwen4ExpForConditionalGeneration, quant=modelopt_fp4, quant_algo=NVFP4, avail mem=29.01 GB, mem usage=82.97 GB.",
    "[2026-08-28 21:32:39] Load weight begin. avail mem=28.81 GB",
    "[2026-08-28 21:34:13] Load weight end. elapsed=94.49 s, type=Qwen4ExpForCausalLMMTP, quant=modelopt_fp4, quant_algo=NVFP4, avail mem=30.67 GB, mem usage=-1.86 GB.",
    "[2026-08-28 21:34:15] KV Cache is allocated. dtype: torch.bfloat16, #tokens: 159552, K size: 1.83 GB, V size: 1.83 GB",
    "[2026-08-28 21:34:50] Capture target verify CUDA graph begin. backend=full, num_tokens_per_req=4, bs=[1, 2], avail mem=26.27 GB",
    "[2026-08-28 21:34:51] Capture target verify CUDA graph end. elapsed=1.61 s, mem usage=0.11 GB, avail mem=26.15 GB.",
    "[2026-08-28 21:35:15] The server is fired up and ready to roll!",
]


def cut(lines, upto):
    """Log tail as it would look while the boot is still inside stage upto."""
    return lines[:upto]


class ParseBootLog(unittest.TestCase):
    def test_full_boot_reaches_warming_up(self):
        b = lc.parse_boot_log(FLASH_BOOT)
        self.assertEqual(b["stage"], "warming-up")
        self.assertTrue(b["fired_up"])
        self.assertTrue(b["ple_mmap"])
        self.assertEqual(b["weight_ends"], 2)

    def test_mid_weight_load(self):
        b = lc.parse_boot_log(cut(FLASH_BOOT, 4))   # after PLE mmap lines
        self.assertEqual(b["stage"], "loading-weights")
        self.assertFalse(b["fired_up"])

    def test_draft_load_detected(self):
        b = lc.parse_boot_log(cut(FLASH_BOOT, 6))   # target done, draft begun
        self.assertEqual(b["stage"], "loading-draft")

    def test_kv_and_graphs(self):
        self.assertEqual(lc.parse_boot_log(cut(FLASH_BOOT, 8))["stage"],
                         "allocating-kv")
        self.assertEqual(lc.parse_boot_log(cut(FLASH_BOOT, 9))["stage"],
                         "capturing-graphs")

    def test_restarted_container_resets(self):
        # Old life reached ready, new life only started loading: the parser
        # must trust ONLY the newest boot.
        lines = FLASH_BOOT + [FLASH_BOOT[0], FLASH_BOOT[1]]
        b = lc.parse_boot_log(lines)
        self.assertEqual(b["stage"], "loading-weights")
        self.assertFalse(b["fired_up"])

    def test_empty_tail(self):
        self.assertIsNone(lc.parse_boot_log([])["stage"])

    def test_decode_noise_ignored(self):
        noise = ["[2026-08-28 21:35:16] Prefill batch, #new-seq: 1",
                 "[2026-08-28 21:36:00] Decode batch, #running-req: 1"]
        b = lc.parse_boot_log(FLASH_BOOT + noise)
        self.assertEqual(b["stage"], "warming-up")


class JournalFlags(unittest.TestCase):
    def test_rebuild_detected(self):
        j = ["août 28 18:14:37 gx10 bash[9]: qwen38-flash: previous boot "
             "never reached health; rebuilding the PLE table"]
        self.assertTrue(lc.journal_flags(j)["rebuild"])
        self.assertFalse(lc.journal_flags(["Started qwen38-flash.service"])["rebuild"])


class DeriveState(unittest.TestCase):
    def s(self, **kw):
        base = dict(unit_active="active", unit_sub="running",
                    container_running=True, healthy=False,
                    boot={"stage": None, "fired_up": False}, rebuild=False)
        base.update(kw)
        return lc.derive_state(**base)

    def test_stopped_failed_stopping(self):
        self.assertEqual(self.s(unit_active="inactive")["state"], "stopped")
        self.assertEqual(self.s(unit_active="failed")["state"], "failed")
        self.assertEqual(self.s(unit_active="deactivating")["state"], "stopping")

    def test_starting_before_container(self):
        self.assertEqual(self.s(container_running=False)["state"], "starting")

    def test_loading_stages_pass_through(self):
        for st in ("loading-weights", "loading-draft", "allocating-kv",
                   "capturing-graphs"):
            self.assertEqual(self.s(boot={"stage": st, "fired_up": False})["state"], st)

    def test_ready_wins_when_healthy(self):
        self.assertEqual(self.s(healthy=True)["state"], "ready")

    def test_degraded_after_fired_up_without_health(self):
        got = self.s(boot={"stage": "warming-up", "fired_up": True})
        self.assertEqual(got["state"], "degraded")

    def test_rebuild_flag_carried(self):
        self.assertTrue(self.s(rebuild=True)["rebuild"])


class BlockedReasons(unittest.TestCase):
    def test_two_engines_never_run_at_once(self):
        states = {"qwen38-flash.service": "ready",
                  "qwen38-sglang.service": "stopped"}
        r = lc.blocked_reasons("unit", {"unit": "qwen38-sglang.service",
                                        "verb": "start"}, states)
        self.assertEqual(len(r), 1)
        self.assertIn("never run at once", r[0])

    def test_start_allowed_when_other_stopped(self):
        states = {"qwen38-flash.service": "stopped",
                  "qwen38-sglang.service": "stopped"}
        self.assertEqual(lc.blocked_reasons("unit",
                         {"unit": "qwen38-flash.service", "verb": "start"},
                         states), [])

    def test_blocked_even_while_other_is_loading(self):
        states = {"qwen38-flash.service": "loading-weights",
                  "qwen38-sglang.service": "stopped"}
        r = lc.blocked_reasons("unit", {"unit": "qwen38-sglang.service",
                                        "verb": "start"}, states)
        self.assertEqual(len(r), 1)

    def test_keepalive_never_blocked(self):
        states = {"qwen38-flash.service": "ready"}
        self.assertEqual(lc.blocked_reasons("unit",
                         {"unit": "qwen38-keepalive.service", "verb": "restart"},
                         states), [])

    def test_switch_blocked_during_boot(self):
        states = {"qwen38-flash.service": "capturing-graphs",
                  "qwen38-sglang.service": "stopped"}
        self.assertEqual(len(lc.blocked_reasons("switch", {"target": "stock"},
                                                states)), 1)
        states["qwen38-flash.service"] = "ready"
        self.assertEqual(lc.blocked_reasons("switch", {"target": "stock"},
                                            states), [])

    def test_stop_is_never_blocked(self):
        states = {"qwen38-flash.service": "loading-weights",
                  "qwen38-sglang.service": "stopped"}
        self.assertEqual(lc.blocked_reasons("unit",
                         {"unit": "qwen38-flash.service", "verb": "stop"},
                         states), [])

    def test_warn_on_mid_boot_flash_stop(self):
        states = {"qwen38-flash.service": "loading-weights"}
        w = lc.warn_reasons("unit", {"unit": "qwen38-flash.service",
                                     "verb": "stop"}, states)
        self.assertEqual(len(w), 1)
        self.assertIn("rebuilds", w[0])


class EtaHistory(unittest.TestCase):
    def test_record_and_median(self):
        h = {}
        for v in (698, 755, 966):   # real Started->fired-up durations, s
            h = lc.record_boot(h, "qwen38-flash.service", v, rebuild=False)
        self.assertEqual(lc.eta_for(h, "qwen38-flash.service", False), 755)

    def test_rebuild_bucket_separate_with_fallback(self):
        h = lc.record_boot({}, "qwen38-flash.service", 700, rebuild=False)
        self.assertEqual(lc.eta_for(h, "qwen38-flash.service", True), 700)
        h = lc.record_boot(h, "qwen38-flash.service", 966, rebuild=True)
        self.assertEqual(lc.eta_for(h, "qwen38-flash.service", True), 966)

    def test_bounded_history(self):
        h = {}
        for i in range(30):
            h = lc.record_boot(h, "u", 100 + i, rebuild=False)
        self.assertEqual(len(h["u"]), 12)

    def test_no_history_gives_none(self):
        self.assertIsNone(lc.eta_for({}, "u", False))


class NoMarkerTail(unittest.TestCase):
    def test_decode_only_tail_claims_nothing(self):
        tail = ["[2026-08-29 00:40:00] Decode batch, #running-req: 1",
                "[2026-08-29 00:40:01] Prefill batch, #new-seq: 1"] * 150
        b = lc.parse_boot_log(tail)
        self.assertIsNone(b["stage"])
        self.assertEqual(b["done"], [])   # regression: used to claim ALL stages


import registry as rg  # noqa: E402


class RegistryParsers(unittest.TestCase):
    PINS_FIXTURE = '''
STOCK_REV="52d1adc5f38aa5ebf099c29ed7025ba34cfbb854"
UNC_REV="21565d389fe573a32c1c425e0c7ade204ddb2263"
FLASH_REV="7b719225242aacd3dbd3f9407468c2ee9a9d2594"
DRAFT_REV="${DRAFT_REV:-85ef153be924f17ce4bf62726954eeaa4a73e854}"
DRAFT2_REV="50307d4c4cde6860d4eee73e2547cd786fe8e8a4"
'''

    def test_parse_pins_real_shapes(self):
        pins = rg.parse_pins(self.PINS_FIXTURE)
        self.assertEqual(pins["STOCK_REV"],
                         "52d1adc5f38aa5ebf099c29ed7025ba34cfbb854")
        self.assertEqual(pins["DRAFT_REV"],
                         "85ef153be924f17ce4bf62726954eeaa4a73e854")
        self.assertEqual(pins["DRAFT2_REV"],
                         "50307d4c4cde6860d4eee73e2547cd786fe8e8a4")
        self.assertEqual(len(pins), 5)

    def test_parse_docker_images(self):
        rows = rg.parse_docker_images([
            "qwen38-flash:v1.5 30.2GB 7f2a4c0a1885",
            "lmsysorg/sglang:qwen38-27b 38.6GB 0076dffa60b7",
            "node:24 1.13GB d975b5c585b1"])
        self.assertTrue(rows[0]["engine"] and rows[1]["engine"])
        self.assertFalse(rows[2]["engine"])

    def test_classify_pinned_vs_stray(self):
        models = [{"repo_id": "RadixArk/Qwen3.8-27B-NVFP4",
                   "disk_bytes": 100,
                   "revisions": [
                       {"rev": "52d1adc5f38aa5ebf099c29ed7025ba34cfbb854", "bytes": 90},
                       {"rev": "319f741cce68d7914884900c138a1fbb70a42f30", "bytes": 90},
                   ]},
                  {"repo_id": "unsloth/Llama-OuteTTS-1.0-1B",
                   "disk_bytes": 5,
                   "revisions": [{"rev": "52b90117" + "0"*32, "bytes": 5}]}]
        pins = rg.parse_pins(self.PINS_FIXTURE)
        got = rg.classify(models, pins)
        r = {x["rev"][:7]: x["status"] for x in got[0]["revisions"]}
        self.assertEqual(r["52d1adc"], "pinned")
        self.assertEqual(r["319f741"], "stray")
        self.assertTrue(got[0]["managed"])
        self.assertEqual(got[1]["revisions"][0]["status"], "unmanaged")
        self.assertFalse(got[1]["managed"])

    def test_scan_on_synthetic_cache(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            m = root / "models--Acme--Tiny" / "blobs"
            m.mkdir(parents=True)
            (m / "aaa").write_bytes(b"x" * 10)
            snap = root / "models--Acme--Tiny" / "snapshots" / "deadbeef"
            snap.mkdir(parents=True)
            (snap / "w.bin").symlink_to(m / "aaa")
            got = rg.scan_hf_cache(root)
            self.assertEqual(got[0]["repo_id"], "Acme/Tiny")
            self.assertEqual(got[0]["disk_bytes"], 10)
            self.assertEqual(got[0]["revisions"][0]["bytes"], 10)


class FeedOutcomes(unittest.TestCase):
    """Every outcome the proxy can write must reach the feed, and read as what it is.

    The list is not copied here: it is read out of keepalive-proxy.py, so a new
    outcome string added to the proxy either classifies or fails this test. The
    feed is the panel a person looks at when something is wrong, and it went
    three releases painting a client that walked away in the same red as an
    engine that refused."""

    SUFFIX = " [12306b relayed, first event at 3.7s, last at 7.3s]"

    @classmethod
    def setUpClass(cls):
        src = (REPO / "keepalive-proxy.py").read_text()
        raw = re.findall(r'self\._done\(\s*f?"([^"]+)"', src)
        # f-string holes carry an HTTP status in every case that has one
        cls.outcomes = sorted({re.sub(r"\{[^}]+\}", "502", o) for o in raw})
        assert len(cls.outcomes) >= 12, cls.outcomes

    def _line(self, outcome, suffix=""):
        return ("2026-09-10T09:00:00+02:00 gx10 python3[1]: [proxy] 127.0.0.1:5555 -> "
                "POST /v1/chat/completions body=42b\n"
                "2026-09-10T09:00:07+02:00 gx10 python3[1]: [proxy] 127.0.0.1:5555 POST "
                f"/v1/chat/completions {outcome} in 7.4s{suffix}")

    def test_every_outcome_reaches_the_feed_with_its_duration(self):
        for outcome in self.outcomes:
            for suffix in ("", self.SUFFIX):
                rows = lc.parse_feed(self._line(outcome, suffix))
                self.assertEqual(len(rows), 1, outcome)
                self.assertEqual(rows[0]["outcome"], outcome[:40], outcome)
                self.assertEqual(rows[0]["secs"], 7.4, outcome)

    def test_every_outcome_gets_a_kind_the_ui_knows(self):
        known = {"ok", "gone", "fail", "live", "unknown"}
        for outcome in self.outcomes:
            kind = lc.parse_feed(self._line(outcome))[0]["kind"]
            self.assertIn(kind, known, outcome)

    def test_a_client_that_left_is_not_a_failure(self):
        """v6.14 outcomes: the client walked away and the proxy handled it. Reading
        those as 'fail' is what made a quiet lane look broken."""
        for outcome in ("CLIENT GONE on write", "CLIENT GONE on write (draining)",
                        "CLIENT GONE during keepalive",
                        "no outcome (client vanished mid-request)"):
            self.assertIn(outcome, self.outcomes, f"{outcome} is no longer in the proxy")
            self.assertEqual(lc.parse_feed(self._line(outcome))[0]["kind"], "gone", outcome)

    def test_a_real_failure_still_reads_as_one(self):
        for outcome in ("503 engine unreachable", "400 oversize refused", "UPSTREAM CUT",
                        "DROPPED upstream silent", "REFUSED corrupted output"):
            self.assertEqual(lc.parse_feed(self._line(outcome))[0]["kind"], "fail", outcome)

    def test_a_delivered_answer_reads_as_ok(self):
        for outcome in ("ok", "ok get", "ok non-sse", "ok non-sse CORRUPTED"):
            self.assertEqual(lc.parse_feed(self._line(outcome))[0]["kind"], "ok", outcome)

    def test_an_unfinished_request_is_live_then_unknown(self):
        start = ("2026-09-10T09:00:00+02:00 gx10 python3[1]: [proxy] 127.0.0.1:5555 -> "
                 "POST /v1/chat/completions body=42b")
        self.assertEqual(lc.parse_feed(start)[0]["kind"], "live")
        # 11 minutes of newer traffic later, it is not in flight, it is unaccounted for
        later = start + ("\n2026-09-10T09:11:00+02:00 gx10 python3[1]: [proxy] 127.0.0.1:6666 -> "
                         "POST /v1/chat/completions body=42b")
        self.assertEqual(lc.parse_feed(later)[0]["kind"], "unknown")


class MutantsThatSurvived(unittest.TestCase):
    """Written from a mutation run, not from imagination.

    /tmp mutation walk of 2026-09-10 edited one operator at a time in
    lifecycle.py and ran the whole suite after each edit: 210 mutants, 157
    caught, 53 survived. A survivor is a place where the code could be wrong and
    every test would stay green, so each test below names the mutant it kills.
    Score after this class: see the CI step "Mutation score".
    """

    # ---- blocked_reasons: both halves of a condition matter ------------------
    def test_a_verb_that_is_not_a_start_does_not_need_the_other_lane_stopped(self):
        """Kills line 156 And->Or: with 'or', stopping one lane would be blocked
        because the OTHER lane happens to be busy, which is backwards."""
        states = {"qwen38-sglang.service": "ready", "qwen38-flash.service": "stopped"}
        self.assertEqual(
            lc.blocked_reasons("unit", {"unit": "qwen38-flash.service", "verb": "stop"}, states),
            [])

    def test_a_unit_that_is_not_an_engine_is_never_blocked_by_an_engine(self):
        """Kills line 156 In->NotIn on the unit half."""
        states = {"qwen38-sglang.service": "ready", "qwen38-flash.service": "ready"}
        for verb in ("start", "restart", "stop"):
            self.assertEqual(
                lc.blocked_reasons("unit",
                                   {"unit": "qwen38-keepalive.service", "verb": verb},
                                   states), [], verb)

    def test_starting_an_engine_while_the_other_is_ready_is_blocked(self):
        states = {"qwen38-sglang.service": "ready", "qwen38-flash.service": "stopped"}
        reasons = lc.blocked_reasons(
            "unit", {"unit": "qwen38-flash.service", "verb": "start"}, states)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn("two engines never run at once", reasons[0])

    def test_starting_an_engine_while_the_other_is_stopped_is_allowed(self):
        states = {"qwen38-sglang.service": "stopped", "qwen38-flash.service": "stopped"}
        self.assertEqual(
            lc.blocked_reasons("unit", {"unit": "qwen38-flash.service", "verb": "start"},
                               states), [])

    # ---- warn_reasons: the two branches say different things -----------------
    def test_stopping_the_flash_lane_mid_boot_warns_about_the_ple_table(self):
        """Kills line 177 And->Or and line 180 Eq->NotEq: the flash-mid-boot
        warning and the ready warning are different sentences and must not
        collapse into one."""
        for state in lc.TRANSITIONAL:
            warns = lc.warn_reasons("unit",
                                    {"unit": "qwen38-flash.service", "verb": "stop"},
                                    {"qwen38-flash.service": state})
            self.assertEqual(len(warns), 1, (state, warns))
            self.assertIn("PLE", warns[0], state)

    def test_stopping_a_ready_lane_warns_about_the_clients(self):
        warns = lc.warn_reasons("unit", {"unit": "qwen38-sglang.service", "verb": "stop"},
                                {"qwen38-sglang.service": "ready"})
        self.assertEqual(len(warns), 1, warns)
        self.assertIn(":30001", warns[0])

    def test_stopping_a_stopped_lane_warns_about_nothing(self):
        self.assertEqual(
            lc.warn_reasons("unit", {"unit": "qwen38-sglang.service", "verb": "stop"},
                            {"qwen38-sglang.service": "stopped"}), [])

    def test_starting_a_lane_is_not_a_warning(self):
        """Kills line 177 In->NotIn: only stop and restart warn."""
        self.assertEqual(
            lc.warn_reasons("unit", {"unit": "qwen38-sglang.service", "verb": "start"},
                            {"qwen38-sglang.service": "ready"}), [])

    # ---- record_pool: the zero an engine reports before ready ----------------
    def test_a_zero_pool_is_not_recorded(self):
        """Kills line 223 (0 -> 1, LtE -> Lt): an engine reports 0 before it is
        ready, and recording that would poison the spread forever."""
        h = lc.record_pool({}, "u", "t", 0)
        self.assertEqual(h, {})
        self.assertEqual(lc.record_pool({}, "u", "t", -5), {})

    def test_a_pool_of_one_token_is_recorded(self):
        h = lc.record_pool({}, "u", "t", 1)
        self.assertEqual(list(h.values()), [[1]])

    # ---- pool_shortfall: the page alignment is not a shortfall ---------------
    def test_a_pool_aligned_down_to_its_page_is_not_a_shortfall(self):
        """Kills line 240 (128 -> 129, Sub -> Add, GtE -> Gt): SGLang aligns a
        pin down to its page, so 190,000 serving as 189,952 is the pin honoured."""
        self.assertIsNone(lc.pool_shortfall(pinned=190_000, served=189_952))
        self.assertIsNone(lc.pool_shortfall(pinned=190_000, served=190_000 - 128))

    def test_a_pool_short_by_more_than_a_page_is_a_shortfall(self):
        msg = lc.pool_shortfall(pinned=190_000, served=190_000 - 129)
        self.assertIsNotNone(msg)
        self.assertIn("held memory", msg)

    def test_a_pool_larger_than_the_pin_is_not_a_shortfall(self):
        self.assertIsNone(lc.pool_shortfall(pinned=190_000, served=250_000))

    # ---- pool_spread: two boots is the floor for a spread -------------------
    def test_one_boot_reports_itself_but_no_spread(self):
        """Kills line 249 (2 -> 3, Lt -> LtE)."""
        h = lc.record_pool({}, "u", "t", 463_616)
        got = lc.pool_spread(h, "u", "t")
        self.assertEqual(got, {"n": 1, "last": 463_616})

    def test_two_boots_make_a_spread(self):
        h = lc.record_pool(lc.record_pool({}, "u", "t", 400_000), "u", "t", 463_616)
        got = lc.pool_spread(h, "u", "t")
        self.assertEqual(got["n"], 2)
        self.assertEqual((got["min"], got["max"], got["last"]),
                         (400_000, 463_616, 463_616))

    def test_no_boot_at_all_is_none(self):
        self.assertIsNone(lc.pool_spread({}, "u", "t"))

    # ---- wedge_decision / wedge_plan boundaries -----------------------------
    def test_an_idle_engine_is_wedged_exactly_at_the_canary_threshold(self):
        """Kills line 274 Gt->GtE around the canary threshold."""
        kw = dict(health_ok=True, num_reqs=0, progress_age=None, stall_after=120)
        self.assertFalse(lc.decide_wedge(canary_fails=1, threshold=2, **kw))
        self.assertTrue(lc.decide_wedge(canary_fails=2, threshold=2, **kw))
        self.assertTrue(lc.decide_wedge(canary_fails=3, threshold=2, **kw))

    def test_a_busy_engine_is_wedged_only_past_the_stall_window(self):
        kw = dict(health_ok=True, num_reqs=1, canary_fails=0, threshold=2)
        self.assertFalse(lc.decide_wedge(progress_age=120, stall_after=120, **kw))
        self.assertTrue(lc.decide_wedge(progress_age=121, stall_after=120, **kw))
        self.assertFalse(lc.decide_wedge(progress_age=None, stall_after=120, **kw))

    def test_an_unhealthy_engine_is_never_called_wedged_here(self):
        self.assertFalse(lc.decide_wedge(health_ok=False, num_reqs=0, canary_fails=99,
                                           threshold=2, progress_age=9999, stall_after=1))

    def test_a_restart_waits_for_the_full_grace_window(self):
        """Kills line 289 GtE->Gt: at exactly the grace the restart is due."""
        kw = dict(decided=True, prev_state="wedged", now=1000.0, autoheal=True,
                  cooldown_ok=True, job_running=False)
        self.assertFalse(lc.wedge_plan(wedged_since=1000.0 - 599, grace=600, **kw)["restart"])
        self.assertTrue(lc.wedge_plan(wedged_since=1000.0 - 600, grace=600, **kw)["restart"])

    def test_a_running_job_defers_a_restart(self):
        kw = dict(decided=True, prev_state="wedged", wedged_since=0.0, now=10_000.0,
                  grace=600, autoheal=True, cooldown_ok=True)
        self.assertFalse(lc.wedge_plan(job_running=True, **kw)["restart"])
        self.assertTrue(lc.wedge_plan(job_running=False, **kw)["restart"])

    def test_autoheal_off_never_restarts_however_wedged(self):
        plan = lc.wedge_plan(decided=True, prev_state="wedged", wedged_since=0.0,
                             now=10_000.0, grace=0, autoheal=False, cooldown_ok=True,
                             job_running=False)
        self.assertEqual(plan["state"], "wedged")
        self.assertFalse(plan["restart"])

    # ---- the memory floor -----------------------------------------------------
    def test_the_memory_floor_is_a_floor_not_a_ceiling(self):
        """Kills line 307 Lt->LtE and the num_reqs boundary: exactly at the floor
        is above it, and nothing running means nothing to abort."""
        kw = dict(floor_gib=3.0, last_abort_ts=None, now=1000.0, cooldown_s=60)
        self.assertEqual(lc.decide_mem_floor(avail_gib=3.0, num_reqs=2, **kw)[0], False)
        self.assertEqual(lc.decide_mem_floor(avail_gib=2.9, num_reqs=2, **kw)[0], True)
        self.assertEqual(lc.decide_mem_floor(avail_gib=2.9, num_reqs=0, **kw)[0], False)

    def test_the_cooldown_is_respected_to_the_second(self):
        kw = dict(avail_gib=1.0, num_reqs=4, floor_gib=3.0, now=1000.0, cooldown_s=60)
        self.assertFalse(lc.decide_mem_floor(last_abort_ts=1000.0 - 59, **kw)[0])
        self.assertTrue(lc.decide_mem_floor(last_abort_ts=1000.0 - 60, **kw)[0])

    # ---- parse_feed's window --------------------------------------------------
    def test_the_feed_returns_at_most_the_window_it_was_asked_for(self):
        """Kills line 341 (25 -> 26): the default window is 25 rows."""
        lines = []
        for i in range(40):
            peer = f"127.0.0.1:{5000 + i}"
            lines.append(f"2026-09-10T09:00:00+02:00 h p[1]: [proxy] {peer} -> "
                         f"POST /v1/chat/completions body=10b")
            lines.append(f"2026-09-10T09:00:01+02:00 h p[1]: [proxy] {peer} "
                         f"POST /v1/chat/completions ok in 1.0s")
        raw = "\n".join(lines)
        self.assertEqual(len(lc.parse_feed(raw)), 25)
        self.assertEqual(len(lc.parse_feed(raw, last=3)), 3)
        self.assertEqual(len(lc.parse_feed(raw, last=100)), 40)


class MoreMutantsThatSurvived(unittest.TestCase):
    """Second mutation pass, after the first class took the score from 74.8% to
    90.8%. These are the survivors that remained."""

    def _boot(self, *lines):
        return lc.parse_boot_log(list(lines))

    ARGS = "[2026-09-10 10:00:00] server_args=ServerArgs(model_path='x')"
    WBEGIN = "[2026-09-10 10:00:01] Load weight begin. avail mem=111.97 GB"
    WEND = ("[2026-09-10 10:10:00] Load weight end. elapsed=1.0 s, type=T, "
            "quant=modelopt_fp4, quant_algo=NVFP4, avail mem=29.01 GB, mem usage=82.97 GB.")
    KV = ("[2026-09-10 10:11:00] KV Cache is allocated. dtype: torch.bfloat16, "
          "#tokens: 159552, K size: 1.83 GB, V size: 1.83 GB")
    GBEGIN = "[2026-09-10 10:12:00] Capture target verify CUDA graph begin. backend=full"
    GEND = "[2026-09-10 10:12:01] Capture target verify CUDA graph end. elapsed=1.61 s"
    READY = "[2026-09-10 10:13:00] The server is fired up and ready to roll!"

    # ---- a fresh ServerArgs line is a fresh boot ----------------------------
    def test_a_second_boot_in_the_same_log_resets_everything(self):
        """Kills line 60's reset flags: a docker log tail can span a previous
        life of the container, and carrying its 'ready' into this boot would
        report a starting engine as up."""
        b = self._boot(self.ARGS, self.WBEGIN, self.WEND, self.KV, self.GBEGIN,
                       self.GEND, self.READY,
                       self.ARGS, self.WBEGIN)          # a new boot begins here
        self.assertFalse(b["fired_up"], "the previous boot's readiness leaked")
        self.assertEqual(b["stage"], "loading-weights")
        self.assertEqual(b["weight_ends"], 0)

    def test_the_previous_boot_is_reported_while_it_is_the_only_one(self):
        b = self._boot(self.ARGS, self.WBEGIN, self.WEND, self.KV, self.GBEGIN,
                       self.GEND, self.READY)
        self.assertTrue(b["fired_up"])

    # ---- the draft-load threshold ------------------------------------------
    def test_two_weight_ends_mean_the_draft_is_loading(self):
        """Kills line 84 (2 -> 3, GtE -> Gt): the second checkpoint IS the draft,
        and this is what tells a 9-minute boot from a stuck one."""
        one = self._boot(self.ARGS, self.WBEGIN, self.WEND)
        self.assertNotEqual(one["stage"], "loading-draft")
        two = self._boot(self.ARGS, self.WBEGIN, self.WEND, self.WBEGIN, self.WEND)
        self.assertEqual(two["stage"], "loading-draft")

    def test_a_second_begin_with_one_end_also_means_the_draft(self):
        b = self._boot(self.ARGS, self.WBEGIN, self.WEND, self.WBEGIN)
        self.assertEqual(b["stage"], "loading-draft")

    def test_one_begin_alone_is_the_target_not_the_draft(self):
        b = self._boot(self.ARGS, self.WBEGIN)
        self.assertEqual(b["stage"], "loading-weights")

    # ---- done stages --------------------------------------------------------
    def test_the_current_stage_is_not_listed_as_done(self):
        """Kills line 95 Eq->NotEq: the loop stops AT the current stage."""
        b = self._boot(self.ARGS, self.WBEGIN, self.WEND, self.KV)
        self.assertNotIn(b["stage"], b["done"])
        self.assertTrue(set(b["done"]).issubset(set(lc.STAGES)))

    def test_stages_before_the_current_one_are_all_done(self):
        b = self._boot(self.ARGS, self.WBEGIN, self.WEND, self.KV, self.GBEGIN)
        idx = lc.STAGES.index(b["stage"])
        self.assertEqual(b["done"], list(lc.STAGES[:idx]))

    # ---- journal_flags ------------------------------------------------------
    def test_a_rebuild_line_anywhere_raises_the_flag(self):
        """Kills line 105's any(): one line in a long tail is enough."""
        self.assertTrue(lc.journal_flags(["noise"] * 50
                                         + ["rebuilding the PLE table"])["rebuild"])
        self.assertFalse(lc.journal_flags(["noise"] * 50)["rebuild"])
        self.assertFalse(lc.journal_flags([])["rebuild"])

    # ---- derive_state's rebuild flag ---------------------------------------
    def test_the_rebuild_flag_is_carried_through_untouched(self):
        """Kills line 111 bool(rebuild)."""
        boot = self._boot(self.ARGS, self.WBEGIN)
        for value in (True, False):
            got = lc.derive_state(unit_active="active", unit_sub="running",
                                  container_running=True, healthy=False,
                                  boot=boot, rebuild=value)
            self.assertIs(got["rebuild"], value)

    # ---- warn_reasons: the flash lane is the only one with a PLE table ------
    def test_only_the_flash_lane_warns_about_the_ple_table(self):
        """Kills line 176 And->Or: the 27B lane has no n-gram table to dirty."""
        warns = lc.warn_reasons("unit",
                                {"unit": "qwen38-sglang.service", "verb": "stop"},
                                {"qwen38-sglang.service": "loading-weights"})
        self.assertEqual(warns, [], warns)

    # ---- record_pool keeps a bounded history -------------------------------
    def test_the_pool_history_is_bounded_to_its_keep(self):
        """Kills line 219's LtE and the keep arithmetic: an unbounded history
        would grow a state file forever."""
        h = {}
        for i in range(30):
            h = lc.record_pool(h, "u", "t", 400_000 + i, keep=12)
        self.assertEqual(len(list(h.values())[0]), 12)
        self.assertEqual(list(h.values())[0][-1], 400_029, "the newest boot was dropped")

    def test_a_keep_of_one_holds_only_the_last_boot(self):
        h = lc.record_pool(lc.record_pool({}, "u", "t", 1, keep=1), "u", "t", 2, keep=1)
        self.assertEqual(list(h.values())[0], [2])

    # ---- parse_feed's timestamp and its 10-minute orphan window ------------
    def test_only_an_iso_timestamp_is_read_as_one(self):
        """Kills line 346 And->Or: both the length and the T position matter, or
        a body= line's own text becomes the clock."""
        raw = ("2026-09-10T09:00:00+02:00 h p[1]: [proxy] 1.2.3.4:5 -> "
               "POST /v1/chat/completions body=10b")
        self.assertEqual(lc.parse_feed(raw)[0]["ts"], "2026-09-10T09:00:00")
        # a line with no timestamp at all must not invent one
        self.assertEqual(lc.parse_feed("[proxy] 1.2.3.4:5 -> POST /x body=1b")[0]["ts"][:1],
                         "[")

    def test_an_orphan_becomes_unknown_only_after_ten_minutes(self):
        """Kills line 386 Gt->GtE: at exactly 600 s it is still in flight."""
        def feed(seconds):
            start = ("2026-09-10T09:00:00+02:00 h p[1]: [proxy] 1.2.3.4:5 -> "
                     "POST /v1/chat/completions body=10b")
            mm, ss = divmod(seconds, 60)
            later = (f"2026-09-10T09:{mm:02d}:{ss:02d}+02:00 h p[1]: [proxy] "
                     "9.9.9.9:9 -> POST /v1/chat/completions body=10b")
            return lc.parse_feed(start + "\n" + later)[0]
        self.assertEqual(feed(600)["kind"], "live")
        self.assertEqual(feed(601)["kind"], "unknown")


class ZombieGuard(unittest.TestCase):
    """Real lines: the engine's flood from the reference box (2026-09-09 21:15:49,
    the single line the flash container logged in 14 h) and the proxy's own
    journal from the same night."""

    ENGINE = "\n".join([
        "[2026-09-09 21:15:49] Received output for rid='5e5de8901d2349fd8d9107f21df3ab92' but the state was deleted in TokenizerManager.",
        "[2026-09-09 21:15:49] Received output for rid='5e5de8901d2349fd8d9107f21df3ab92' but the state was deleted in TokenizerManager.",
        "[2026-09-09 21:16:01] Received output for rid='5e5de8901d2349fd8d9107f21df3ab92' but the state was deleted in TokenizerManager.",
        "[2026-09-09 21:20:00] Received output for rid='b64416f1aa1c4d0f9c0f3d2e8a7b6c5d' but the state was deleted in TokenizerManager.",
        "[2026-09-09 21:20:00] Decode batch. #running-req: 1, token usage: 0.01, accept len: 2.16",
    ])
    PROXY = "\n".join([
        "2026-09-10T09:00:00+02:00 gx10 python3[1]: [proxy] v6.14 on :30001 -> http://127.0.0.1:30000 (keepalive 10s, max silence 3600s)",
        "2026-09-10T09:01:00+02:00 gx10 python3[1]: [proxy] aborted upstream rid=837fff7d986c425d9acb51090cc802c9 (client gone)",
        "2026-09-10T09:02:00+02:00 gx10 python3[1]: [proxy] 127.0.0.1:35624 POST /v1/chat/completions CLIENT GONE on write in 7.4s [12306b relayed, first event at 3.7s, last at 7.3s]",
        "2026-09-10T09:03:00+02:00 gx10 python3[1]: [proxy] drained an abandoned /v1/messages for 18s",
        "2026-09-10T09:04:00+02:00 gx10 python3[1]: [proxy] abort_request failed for rid=deadbeef: timed out",
    ])

    def test_flood_is_grouped_by_request_worst_first(self):
        z = lc.parse_zombies(self.ENGINE)
        self.assertEqual(z["lines"], 4)          # the Decode batch line is not one
        self.assertEqual(z["distinct"], 2)
        self.assertEqual(z["requests"][0]["lines"], 3)
        self.assertEqual(z["requests"][0]["rid"], "5e5de8901d2349fd8d9107f21df3ab92")
        # first line 21:15:49, last 21:16:01: the span IS the dead decode
        self.assertEqual(z["requests"][0]["secs"], 12.0)

    def test_a_quiet_window_counts_nothing(self):
        z = lc.parse_zombies("[2026-09-10 09:00:00] Decode batch. #running-req: 0\n")
        self.assertEqual((z["lines"], z["distinct"], z["requests"]), (0, 0, []))

    def test_proxy_counters_and_running_version(self):
        g = lc.parse_guard(self.PROXY)
        self.assertEqual(g["version"], "6.14")
        self.assertEqual(g["port"], 30001)
        self.assertEqual(g["aborted"], 1)
        self.assertEqual(g["drained"], 1)
        self.assertEqual(g["drain_max_s"], 18.0)
        self.assertEqual(g["abort_failed"], 1)
        self.assertEqual(g["reasons"], {"client gone": 1})

    def test_an_end_line_is_never_counted_as_an_abort(self):
        """The end line names the same event ("CLIENT GONE on write") and must not
        double the abort count: the feed reports outcomes, this reports actions."""
        g = lc.parse_guard(self.PROXY.splitlines()[2])
        self.assertEqual((g["aborted"], g["drained"]), (0, 0))

    def test_verdict_is_the_flood_first(self):
        big = "\n".join(f"[2026-09-09 21:1{i % 10}:49] Received output for rid='r{i % 3}' "
                         "but the state was deleted in TokenizerManager." for i in range(150))
        state, msg = lc.guard_verdict(lc.parse_zombies(big), lc.parse_guard(self.PROXY), True)
        self.assertEqual(state, "err")
        self.assertIn("150 flood lines", msg)

    def test_verdict_reports_work_done_when_the_engine_log_is_clean(self):
        state, msg = lc.guard_verdict(lc.parse_zombies(""), lc.parse_guard(self.PROXY), True)
        # an abort the engine never answered outranks the tally: it means a zombie
        self.assertEqual(state, "warn")
        self.assertIn("did not answer", msg)

    def test_verdict_says_why_a_prefill_loss_can_only_be_drained(self):
        """The engine env decides: without SGLANG_ENABLE_REQUEST_HEADER_OVERRIDES the
        proxy cannot name a request that has not emitted anything yet."""
        state, msg = lc.guard_verdict(lc.parse_zombies(""), lc.parse_guard(""), False)
        self.assertEqual(state, "")
        self.assertIn("does not take the proxy's rid", msg)

    # ---- the mutants that survived in this file's own newest code -----------
    def test_the_longest_drain_is_the_maximum_not_the_last(self):
        """Kills line 461 (Gt->GtE, Or->And): a shorter drain after a long one
        must not replace it, and the first drain must be recorded at all."""
        raw = "\n".join(["[proxy] drained an abandoned /v1/messages for 30s",
                         "[proxy] drained an abandoned /v1/messages for 5s"])
        g = lc.parse_guard(raw)
        self.assertEqual(g["drained"], 2)
        self.assertEqual(g["drain_max_s"], 30.0)

    def test_the_first_drain_sets_the_maximum_from_none(self):
        g = lc.parse_guard("[proxy] drained an abandoned /generate for 7s")
        self.assertEqual(g["drain_max_s"], 7.0)

    def test_a_zero_second_drain_is_still_recorded(self):
        """Kills line 461 Or->And: with 'and', a first drain of 0 s would leave
        drain_max_s at None and read as no drain at all."""
        g = lc.parse_guard("[proxy] drained an abandoned /generate for 0s")
        self.assertEqual(g["drained"], 1)
        self.assertEqual(g["drain_max_s"], 0.0)

    def test_the_flood_threshold_between_warn_and_err_is_a_hundred_lines(self):
        """Kills line 476 (100 -> 101, GtE -> Gt): 100 is already an error."""
        quiet = lc.parse_guard("")
        for lines, want in ((99, "warn"), (100, "err"), (101, "err")):
            z = {"lines": lines, "distinct": 1,
                 "requests": [{"rid": "r", "lines": lines, "secs": 12.0}]}
            self.assertEqual(lc.guard_verdict(z, quiet, True)[0], want, lines)

    def test_one_flood_line_is_already_a_warning(self):
        """Kills line 477 (0 -> 1): a single line means a real abandoned
        generation, and this panel exists to notice the first one."""
        z = {"lines": 1, "distinct": 1, "requests": [{"rid": "r", "lines": 1, "secs": 0.0}]}
        state, msg = lc.guard_verdict(z, lc.parse_guard(""), True)
        self.assertEqual(state, "warn")
        self.assertIn("1 flood line", msg)

    def test_the_worst_request_is_named_in_an_error_verdict(self):
        """Kills line 475 Add->Sub in the handled count and the detail clause."""
        z = {"lines": 4470, "distinct": 2,
             "requests": [{"rid": "b64416f1", "lines": 4470, "secs": 368.0},
                          {"rid": "d03744c5", "lines": 1742, "secs": 180.0}]}
        state, msg = lc.guard_verdict(z, lc.parse_guard(""), True)
        self.assertEqual(state, "err")
        self.assertIn("4,470 flood lines", msg)
        self.assertIn("2 abandoned request(s)", msg)
        self.assertIn("worst 4,470 lines over 368s", msg)

    def test_an_error_verdict_survives_a_request_with_no_span(self):
        z = {"lines": 200, "distinct": 1,
             "requests": [{"rid": "r", "lines": 200, "secs": None}]}
        state, msg = lc.guard_verdict(z, lc.parse_guard(""), True)
        self.assertEqual(state, "err")
        self.assertNotIn("worst", msg)

    def test_an_abort_and_a_drain_are_both_named_in_the_ok_verdict(self):
        """Kills line 478 And->Or and line 475 Add->Sub: both counters are
        reported, and either one alone is still an answer."""
        g = dict(lc.parse_guard(""), aborted=2, drained=3, drain_max_s=9.0)
        state, msg = lc.guard_verdict(lc.parse_zombies(""), g, True)
        self.assertEqual(state, "ok")
        self.assertIn("2 aborted", msg)
        self.assertIn("3 drained", msg)

    def test_only_aborts_reads_without_an_and(self):
        g = dict(lc.parse_guard(""), aborted=2)
        _, msg = lc.guard_verdict(lc.parse_zombies(""), g, True)
        self.assertEqual(msg, "2 aborted, no flood")

    def test_a_flood_outranks_the_work_the_proxy_did(self):
        """The order of the branches is the whole point: a leak is the finding,
        whatever the counters say next to it."""
        z = {"lines": 500, "distinct": 1,
             "requests": [{"rid": "r", "lines": 500, "secs": 60.0}]}
        g = dict(lc.parse_guard(""), aborted=9, drained=9)
        self.assertEqual(lc.guard_verdict(z, g, True)[0], "err")

    def test_a_ceiling_outranks_an_unanswered_abort(self):
        g = dict(lc.parse_guard(""), ceiling=1, abort_failed=1)
        state, msg = lc.guard_verdict(lc.parse_zombies(""), g, True)
        self.assertEqual(state, "warn")
        self.assertIn("ceiling", msg)

    def test_an_unknown_override_is_not_reported_as_a_missing_one(self):
        """Kills line 493 False->True: None means no engine is serving, which is
        not the same as an engine that refuses the proxy's rid."""
        state, msg = lc.guard_verdict(lc.parse_zombies(""), lc.parse_guard(""), None)
        self.assertEqual(state, "ok")
        self.assertIn("no abandoned request", msg)

    def test_verdict_is_clean_when_nothing_happened(self):
        state, msg = lc.guard_verdict(lc.parse_zombies(""), lc.parse_guard(""), True)
        self.assertEqual(state, "ok")
        self.assertIn("no abandoned request", msg)


class WedgeDecision(unittest.TestCase):
    def test_field_case_idle_route(self):
        # 29/08: health 200, get_load 0 requests, every completion hung
        self.assertTrue(lc.decide_wedge(health_ok=True, canary_fails=3,
                                        num_reqs=0, progress_age=None))

    def test_idle_needs_three_failures(self):
        self.assertFalse(lc.decide_wedge(health_ok=True, canary_fails=2,
                                         num_reqs=0, progress_age=None))

    def test_health_down_is_not_a_wedge(self):
        self.assertFalse(lc.decide_wedge(health_ok=False, canary_fails=9,
                                         num_reqs=0, progress_age=None))

    def test_busy_and_progressing_never_wedges_whatever_the_probes(self):
        self.assertFalse(lc.decide_wedge(health_ok=True, canary_fails=9,
                                         num_reqs=1, progress_age=12.0))

    def test_busy_long_prefill_without_lines_yet_is_not_a_wedge(self):
        self.assertFalse(lc.decide_wedge(health_ok=True, canary_fails=0,
                                         num_reqs=1, progress_age=120.0))

    def test_busy_and_stalled_wedges(self):
        self.assertTrue(lc.decide_wedge(health_ok=True, canary_fails=0,
                                        num_reqs=2, progress_age=400.0))

    def test_wedged_counts_as_busy_for_gates(self):
        self.assertIn("wedged", lc.BUSY_STATES)
        r = lc.blocked_reasons("unit", {"unit": "qwen38-sglang.service", "verb": "start"},
                               {"qwen38-flash.service": "wedged"})
        self.assertEqual(len(r), 1)


class WedgePlan(unittest.TestCase):
    def test_not_decided_does_nothing(self):
        p = lc.wedge_plan(decided=False, prev_state="ready", wedged_since=None, now=100,
                          grace=600, autoheal=True, cooldown_ok=True, job_running=False)
        self.assertEqual(p, {"state": None, "first": False, "since": None, "restart": False})

    def test_first_tick_marks_first_and_starts_the_clock(self):
        p = lc.wedge_plan(decided=True, prev_state="ready", wedged_since=None, now=100,
                          grace=600, autoheal=True, cooldown_ok=True, job_running=False)
        self.assertEqual((p["state"], p["first"], p["since"], p["restart"]), ("wedged", True, 100, False))

    def test_second_tick_is_not_first(self):
        p = lc.wedge_plan(decided=True, prev_state="wedged", wedged_since=100, now=102,
                          grace=600, autoheal=True, cooldown_ok=True, job_running=False)
        self.assertFalse(p["first"]); self.assertEqual(p["since"], 100)

    def test_restart_after_grace_only(self):
        early = lc.wedge_plan(decided=True, prev_state="wedged", wedged_since=100, now=500,
                              grace=600, autoheal=True, cooldown_ok=True, job_running=False)
        late = lc.wedge_plan(decided=True, prev_state="wedged", wedged_since=100, now=701,
                             grace=600, autoheal=True, cooldown_ok=True, job_running=False)
        self.assertFalse(early["restart"]); self.assertTrue(late["restart"])

    def test_no_restart_when_disabled_cooling_or_busy(self):
        for kw in (dict(autoheal=False), dict(cooldown_ok=False), dict(job_running=True)):
            base = dict(decided=True, prev_state="wedged", wedged_since=0, now=10_000,
                        grace=0, autoheal=True, cooldown_ok=True, job_running=False)
            base.update(kw)
            self.assertFalse(lc.wedge_plan(**base)["restart"], kw)

class MemFloor(unittest.TestCase):
    def d(self, **kw):
        base = dict(avail_gib=1.9, floor_gib=3.0, num_reqs=1, last_abort_ts=None, now=1000.0)
        base.update(kw)
        return lc.decide_mem_floor(**base)

    def test_aborts_under_floor_with_running_request(self):
        ok, why = self.d()
        self.assertTrue(ok); self.assertIn("1.9 GiB under the 3.0 GiB floor", why)

    def test_quiet_above_floor(self):
        self.assertEqual(self.d(avail_gib=3.0), (False, "above floor"))
        self.assertFalse(self.d(avail_gib=18.6)[0])

    def test_never_without_running_requests(self):
        ok, why = self.d(num_reqs=0)
        self.assertFalse(ok); self.assertIn("nothing running", why)

    def test_cooldown_and_missing_reading(self):
        self.assertEqual(self.d(last_abort_ts=970.0), (False, "cooldown"))
        self.assertTrue(self.d(last_abort_ts=900.0)[0])
        self.assertEqual(self.d(avail_gib=None), (False, "no reading"))

class ParseFeed(unittest.TestCase):
    RAW = """2026-08-29T20:47:32+02:00 gx10 python3[1]: [proxy] 127.0.0.1:50508 -> POST /v1/chat/completions body=206008b
2026-08-29T20:47:33+02:00 gx10 python3[1]: [proxy] 127.0.0.1:50508 POST /v1/chat/completions 200 ok non-sse in 0.5s
2026-08-29T20:48:08+02:00 gx10 python3[1]: [proxy] 127.0.0.1:42706 -> POST /v1/chat/completions body=478952b
2026-08-29T20:48:08+02:00 gx10 python3[1]: [proxy] 127.0.0.1:42706 REFUSED oversize (478952b, 140151 prompt tokens (counted by the engine), limit 128000)
2026-08-29T20:48:08+02:00 gx10 python3[1]: [proxy] 127.0.0.1:42706 POST /v1/chat/completions 400 oversize refused in 0.6s
2026-08-29T20:49:10+02:00 gx10 python3[1]: [proxy] 127.0.0.1:54104 -> POST /v1/chat/completions body=427000b
2026-08-29T20:49:11+02:00 gx10 python3[1]: [proxy] 127.0.0.1:54104 oversize check: 125070 tokens fit (128000 usable of pool 197760)
"""

    def test_rows_outcomes_and_details(self):
        rows = lc.parse_feed(self.RAW)
        self.assertEqual([r["peer"] for r in rows], ["127.0.0.1:50508", "127.0.0.1:42706", "127.0.0.1:54104"])
        self.assertEqual(rows[0]["outcome"], "200 ok non-sse"); self.assertIsNone(rows[0]["detail"])
        self.assertEqual(rows[1]["outcome"], "400 oversize refused")
        self.assertEqual(rows[1]["detail"], "140151 prompt tokens (counted by the engine), limit 128,000")
        self.assertEqual(rows[1]["secs"], 0.6)
        self.assertEqual(rows[2]["outcome"], "in flight")
        self.assertEqual(rows[2]["detail"], "125,070 tokens counted, fits (128,000 usable)")

    def test_last_n_and_empty(self):
        self.assertEqual(lc.parse_feed(""), [])
        self.assertEqual(len(lc.parse_feed(self.RAW, last=2)), 2)


class ParseFeedDangling(unittest.TestCase):
    L = "2026-08-30T0{h}:00:00+0200 host python3[1]: [proxy] {rest}"

    def test_old_dangling_start_is_not_in_flight(self):
        raw = "\n".join([
            self.L.format(h=1, rest="127.0.0.1:1111 -> POST /v1/chat/completions body=100b"),
            self.L.format(h=2, rest="127.0.0.1:2222 -> POST /v1/chat/completions body=200b"),
            self.L.format(h=2, rest="127.0.0.1:2222 POST /v1/chat/completions ok in 3.0s"),
        ])
        by = {r["peer"]: r for r in lc.parse_feed(raw)}
        self.assertEqual(by["127.0.0.1:1111"]["outcome"], "no end logged")
        self.assertEqual(by["127.0.0.1:2222"]["outcome"], "ok")

    def test_recent_dangling_start_stays_in_flight(self):
        raw = "\n".join([
            self.L.format(h=2, rest="127.0.0.1:2222 POST /v1/chat/completions ok in 3.0s"),
            self.L.format(h=2, rest="127.0.0.1:3333 -> POST /v1/chat/completions body=300b"),
        ])
        by = {r["peer"]: r for r in lc.parse_feed(raw)}
        self.assertEqual(by["127.0.0.1:3333"]["outcome"], "in flight")


class OpencodeDefault(unittest.TestCase):
    def test_follows(self):
        ok, why = lc.opencode_default_follows("qwen38/qwen3.8-27b", {"qwen38-sglang.service": "ready", "qwen38-flash.service": "stopped"})
        self.assertTrue(ok); self.assertIn("follows", why)

    def test_differs_and_missing(self):
        ok, why = lc.opencode_default_follows("flashnext/qwen3.8-flash-next", {"qwen38-sglang.service": "loading-weights"})
        self.assertFalse(ok); self.assertIn("qwen38", why)
        ok, _why = lc.opencode_default_follows(None, {"qwen38-flash.service": "ready"})
        self.assertFalse(ok)

    def test_nothing_serving(self):
        ok, why = lc.opencode_default_follows("qwen38/qwen3.8-27b", {"qwen38-sglang.service": "stopped", "qwen38-flash.service": "failed"})
        self.assertIsNone(ok); self.assertIn("no engine", why)


class PoolHistory(unittest.TestCase):
    """The KV pool a boot wins, kept per target.

    It is a lottery (this box measured 863,398 / 893,479 / 913,334 for one
    checkpoint) and it also depends on the checkpoint (about 863k on NVFP4
    against 778k on FP8), so one series per target is the only kind that answers
    a question. The repo carries two disagreeing 1m pool campaigns precisely
    because nobody was recording this."""

    def test_key_separates_targets_and_units(self):
        a = lc.pool_key("qwen38-sglang.service", "stock")
        b = lc.pool_key("qwen38-sglang.service", "fp8")
        c = lc.pool_key("qwen38-flash.service", "flash")
        self.assertEqual(len({a, b, c}), 3, "keys must not collide")
        self.assertEqual(lc.pool_key("u", None), "u:pool:unknown")

    def test_record_ignores_a_pool_the_engine_has_not_reported(self):
        # Before ready, get_server_info answers 0; recording it would poison the
        # spread with a floor no boot ever had.
        h = {}
        for bad in (0, -1, None):
            self.assertEqual(lc.record_pool(h, "u", "fp8", bad), {})

    def test_record_and_spread(self):
        h = {}
        for p in (863398, 893479, 913334):
            lc.record_pool(h, "qwen38-sglang.service", "stock", p)
        self.assertIsNone(lc.pool_spread(h, "qwen38-sglang.service", "fp8"),
                          "another target's series must stay empty")
        s = lc.pool_spread(h, "qwen38-sglang.service", "stock")
        self.assertEqual((s["n"], s["min"], s["max"], s["last"]),
                         (3, 863398, 913334, 913334))
        self.assertEqual(s["spread_pct"], 5.5)

    def test_one_boot_reports_no_spread_yet(self):
        h = {}
        lc.record_pool(h, "u", "fp8", 778343)
        s = lc.pool_spread(h, "u", "fp8")
        self.assertEqual(s, {"n": 1, "last": 778343})

    def test_history_is_bounded(self):
        h = {}
        for i in range(40):
            lc.record_pool(h, "u", "fp8", 700000 + i)
        self.assertEqual(len(h[lc.pool_key("u", "fp8")]), 12)
        self.assertEqual(lc.pool_spread(h, "u", "fp8")["last"], 700039)


class PoolShortfall(unittest.TestCase):
    """A pinned pool is a ceiling: a boot that profiles less serves less, in silence."""

    def test_no_pin_says_nothing(self):
        self.assertIsNone(lc.pool_shortfall(None, 189056))

    def test_no_pool_yet_says_nothing(self):
        self.assertIsNone(lc.pool_shortfall(190000, 0))
        self.assertIsNone(lc.pool_shortfall(190000, None))

    def test_at_or_above_the_pin_says_nothing(self):
        self.assertIsNone(lc.pool_shortfall(190000, 190000))
        self.assertIsNone(lc.pool_shortfall(190000, 189952))  # page alignment is not a shortfall
        self.assertIsNone(lc.pool_shortfall(189952, 249408))

    def test_below_the_pin_names_both_numbers(self):
        msg = lc.pool_shortfall(190000, 150016)
        self.assertIn("150,016", msg)
        self.assertIn("190,000", msg)
        self.assertIn("restart", msg.lower())


if __name__ == '__main__':
    unittest.main()
