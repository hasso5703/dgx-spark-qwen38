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
