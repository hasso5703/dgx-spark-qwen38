"""Offline tests for lifecycle.py, built on REAL log lines from this box
(qwen38-flash boot of 2026-08-28 21:19 and qwen38-sglang boots of 08-28)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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
