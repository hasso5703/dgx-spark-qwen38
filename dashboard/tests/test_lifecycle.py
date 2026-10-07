"""Offline tests for lifecycle.py, built on REAL log lines from this box
(qwen38-flash boot of 2026-08-28 21:19 and qwen38-sglang boots of 08-28)."""
import json
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


class DeriveState(unittest.TestCase):
    def s(self, **kw):
        base = dict(unit_active="active", unit_sub="running",
                    container_running=True, healthy=False,
                    boot={"stage": None, "fired_up": False})
        base.update(kw)
        return lc.derive_state(**base)

    def test_stopped_failed_stopping(self):
        self.assertEqual(self.s(unit_active="inactive", container_running=False)["state"], "stopped")
        self.assertEqual(self.s(unit_active="failed", container_running=False)["state"], "failed")
        self.assertEqual(self.s(unit_active="deactivating")["state"], "stopping")

    def test_a_unit_waiting_to_be_relaunched_after_a_crash_is_failed(self):
        """Restart=always with RestartSec=15 never reaches systemd's start limit, so a unit
        that dies at load sits in activating/auto-restart between attempts (read on the
        reference box's systemd 255, 2026-09-24) and never in failed: the page drew a boot
        in its first stage forever."""
        got = self.s(unit_active="activating", unit_sub="auto-restart", container_running=False)
        self.assertEqual(got["state"], "failed")
        self.assertTrue(got.get("restarting"))
        self.assertNotEqual(self.s(unit_active="activating", unit_sub="start", container_running=False)["state"],
                            "failed", "an ordinary start is still a start")

    def test_a_container_that_outlives_its_unit_is_an_orphan(self):
        """A stop past its timeout: systemd kills the docker client and marks the unit
        failed (base.js already tells that case apart), while the container, which the
        docker daemon owns, keeps its whole pool. Also a container started by hand. It
        read "failed" or "stopped", and neither was busy for the gate."""
        for active in ("failed", "inactive", "dead"):
            with self.subTest(active=active):
                self.assertEqual(self.s(unit_active=active, container_running=True)["state"], "orphan")

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

    def test_no_rebuild_flag_is_left(self):
        """Nothing announces a PLE table rebuild since v1.8 (every flash boot writes it
        whole), so the flag it raised never rose (found in review, 2026-09-24)."""
        self.assertNotIn("rebuild", self.s())
        self.assertFalse(hasattr(lc, "journal_flags"))


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

    def test_an_orphan_container_blocks_every_other_engine(self):
        # the pool is held whatever systemd says about the unit
        states = {"qwen38-sglang.service": "orphan", "qwen38-flash.service": "stopped"}
        for other in ("qwen38-flash.service", "qwen38-image.service"):
            with self.subTest(start=other):
                r = lc.blocked_reasons("unit", {"unit": other, "verb": "start"}, states)
                self.assertEqual(len(r), 1)
                self.assertIn("outside systemd", r[0])

    def test_an_orphan_can_be_replaced_by_its_own_unit_and_switched(self):
        # ExecStartPre=-docker rm -f removes the orphan before the unit's own container
        # starts, which is how the page's banner tells the operator to recover; and a
        # switch only rewrites files, so it waits for nothing an orphan could settle.
        states = {"qwen38-sglang.service": "orphan", "qwen38-flash.service": "stopped"}
        self.assertEqual(lc.blocked_reasons("unit", {"unit": "qwen38-sglang.service", "verb": "start"},
                                            states), [])
        self.assertEqual(lc.blocked_reasons("switch", {"target": "stock"}, states), [])

    def test_a_wedged_engine_does_not_hold_a_switch_back(self):
        """A wedge does not settle by waiting (autoheal is off by default), and a switch only
        rewrites files: "wait for it to settle" refused it for good (found in review,
        2026-09-24). It still counts as busy for starting a second engine."""
        states = {"qwen38-sglang.service": "wedged", "qwen38-flash.service": "stopped"}
        self.assertEqual(lc.blocked_reasons("switch", {"target": "stock"}, states), [])
        self.assertTrue(lc.blocked_reasons("unit", {"unit": "qwen38-flash.service", "verb": "start"}, states))
        self.assertEqual(lc.warn_reasons("unit", {"unit": "qwen38-flash.service", "verb": "stop"},
                                         {"qwen38-flash.service": "wedged"}), [],
                         "a wedged lane has booted: no mid-boot warning")

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
        # the launcher deletes the table before every boot (since v1.8): nothing is left
        # dirty, and what a stop costs is the boot under way
        self.assertIn("from scratch", w[0])
        self.assertNotIn("dirty", w[0])


class ThreeEngines(unittest.TestCase):
    """The image lane is a third engine. The rule it has to obey is the one the two text
    lanes already do, and the gate used to pick the other engine with [0], which with
    three of them checked one neighbour of two and let the third through."""
    IMG = "qwen38-image.service"

    def test_image_is_blocked_while_the_27b_serves(self):
        r = lc.blocked_reasons("unit", {"unit": self.IMG, "verb": "start"},
                               {"qwen38-sglang.service": "ready", "qwen38-flash.service": "stopped"})
        self.assertEqual(len(r), 1)
        self.assertIn("qwen38-sglang.service", r[0])

    def test_image_is_blocked_while_flash_boots(self):
        """[0] would have been the 27B here, stopped, and let the start through."""
        r = lc.blocked_reasons("unit", {"unit": self.IMG, "verb": "start"},
                               {"qwen38-sglang.service": "stopped",
                                "qwen38-flash.service": "loading-weights"})
        self.assertEqual(len(r), 1)
        self.assertIn("qwen38-flash.service", r[0])

    def test_a_text_lane_is_blocked_while_the_image_lane_runs(self):
        for text in ("qwen38-sglang.service", "qwen38-flash.service"):
            with self.subTest(text=text):
                r = lc.blocked_reasons("unit", {"unit": text, "verb": "start"},
                                       {self.IMG: "ready"})
                self.assertEqual(len(r), 1)
                self.assertIn(self.IMG, r[0])

    def test_image_starts_when_nothing_else_runs(self):
        self.assertEqual(lc.blocked_reasons("unit", {"unit": self.IMG, "verb": "start"},
                                            {"qwen38-sglang.service": "stopped",
                                             "qwen38-flash.service": "stopped"}), [])

    def test_the_switch_waits_for_an_image_boot_too(self):
        self.assertEqual(len(lc.blocked_reasons("switch", {"target": "stock"},
                                                {self.IMG: "warming-up"})), 1)

    def test_stopping_a_ready_image_lane_warns_about_the_image_not_the_proxy(self):
        """The proxy on :30001 is a text door; nothing reaches this lane through it."""
        w = lc.warn_reasons("unit", {"unit": self.IMG, "verb": "stop"}, {self.IMG: "ready"})
        self.assertEqual(len(w), 1)
        self.assertIn("image", w[0])
        self.assertNotIn(":30001", w[0])


class ImageBootLog(unittest.TestCase):
    """A real boot of the image lane, captured from journald on the reference box:
    64 s from process start to fired up. The engine and uvicorn write two different
    timestamp formats, so the parser matches what a line says, never when."""

    @classmethod
    def setUpClass(cls):
        cls.lines = (HERE.parent / "fixtures" / "image-boot.log").read_text().splitlines()

    def at(self, needle):
        """The boot as it looked the moment this line was written."""
        for i, ln in enumerate(self.lines):
            if needle in ln:
                return lc.parse_image_boot_log(self.lines[:i + 1])
        self.fail(f"the fixture has no line with {needle!r}")

    def test_a_complete_boot_is_fired_up_with_every_stage_done(self):
        b = lc.parse_image_boot_log(self.lines)
        self.assertTrue(b["fired_up"])
        self.assertEqual(b["done"], list(lc.IMAGE_STAGES))

    def test_each_milestone_names_what_is_being_loaded(self):
        """"loading weights" for 33 s says less than "the 13.3 GB DiT"."""
        for needle, stage, detail in (
                ("Starting server", "init", "starting the server"),
                ("Loading text_encoder from", "loading-weights", "Qwen3-VL encoder"),
                ("Loading transformer from", "loading-weights", "13.3 GB DiT"),
                ("Loading vae from", "loading-weights", "the VAE"),
                ("Starting FastAPI server", "warming-up", "throwaway image")):
            with self.subTest(line=needle):
                b = self.at(needle)
                self.assertEqual(b["stage"], stage)
                self.assertIn(detail, b["detail"])
                self.assertFalse(b["fired_up"])

    def test_only_the_latest_boot_counts(self):
        """journald keeps every previous life of the unit. A "fired up" from an earlier
        boot must not make the one in progress read as ready."""
        restarted = self.lines + ["[09-22 18:00:00] Starting server...",
                                  "[09-22 18:00:07] Loading pipeline modules..."]
        b = lc.parse_image_boot_log(restarted)
        self.assertFalse(b["fired_up"])
        self.assertEqual(b["stage"], "loading-weights")

    def test_an_empty_journal_is_not_a_boot(self):
        b = lc.parse_image_boot_log([])
        self.assertIsNone(b["stage"])
        self.assertFalse(b["fired_up"])

    def test_a_marker_behind_a_tqdm_prefix_is_still_found(self):
        """journald keeps tqdm's carriage returns inside a newline-terminated line: this
        fixture holds 17 of them in 52 lines, so "Loading transformer from" arrives glued
        behind "Loading required modules: 20%|...". The parser matches by substring, so
        that prefix changes nothing, whether the bytes are split on \\r or not."""
        raw = (HERE.parent / "fixtures" / "image-boot.log").read_bytes()
        self.assertGreater(raw.count(b"\r"), 0, "the fixture lost its carriage returns")
        glued = raw.replace(b"\r", b" ").decode().split("\n")      # the worst case: no \r split
        b = lc.parse_image_boot_log([ln for ln in glued if "Loaded transformer" not in ln
                                     and "Loading vae" not in ln and "Loaded vae" not in ln
                                     and "Pipeline instantiated" not in ln
                                     and "FastAPI" not in ln and "fired up" not in ln])
        self.assertIn("13.3 GB DiT", b["detail"])


class EtaHistory(unittest.TestCase):
    def test_record_and_median(self):
        h = {}
        for v in (698, 755, 966):   # real Started->fired-up durations, s
            h = lc.record_boot(h, "qwen38-flash.service", v)
        self.assertEqual(lc.eta_for(h, "qwen38-flash.service"), 755)
        self.assertEqual(list(h), ["qwen38-flash.service"], "a boot went to a series of its own")

    def test_bounded_history(self):
        h = {}
        for i in range(30):
            h = lc.record_boot(h, "u", 100 + i)
        self.assertEqual(len(h["u"]), 12)

    def test_no_history_gives_none(self):
        self.assertIsNone(lc.eta_for({}, "u"))


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

    # The proxy's outcome vocabulary, CLOSED, with the kind each one must read as.
    # A closed set is the point: outcome_kind falls back to "fail", so a new
    # outcome that should have read "gone" or "ok" would be silently mis-coloured
    # and a test that only checked "the kind is one of five" would pass. Verified
    # by adding a bogus outcome to the proxy: the earlier version of this class
    # did not notice, this one does. Adding an outcome means adding it here and
    # deciding what it is.
    EXPECTED = {
        "ok": "ok",
        "ok get": "ok",
        "ok non-sse": "ok",
        "ok non-sse CORRUPTED": "ok",
        "CLIENT GONE on write": "gone",
        "CLIENT GONE during keepalive": "gone",
        # v6.25: the caller of a non-streamed answer left while the engine worked; the
        # proxy ended the upstream and the engine dropped the request itself
        "CLIENT GONE during non-sse wait": "gone",
        # v6.28: the caller left while its request waited for the engine to come back
        "CLIENT GONE during hold": "gone",
        # v6.29: the caller left before its body arrived whole, and the engine was not asked
        "CLIENT GONE before its body": "gone",
        "no outcome (client vanished mid-request)": "gone",
        "DROPPED upstream silent": "fail",
        "UPSTREAM CUT": "fail",
        "UPSTREAM CUT non-sse": "fail",    # v6.25: the engine ended a non-streamed body early
        "REFUSED corrupted output": "fail",
        "400 oversize refused": "fail",
        "400 bad Content-Length": "fail",
        "413 body over cap": "fail",
        "401 unknown client key": "fail",   # the identity wall's refusal (v6.16)
        "503 monster held during warmup": "fail",
        "503 engine unreachable": "fail",
        "503 engine unreachable (upstream 502)": "fail",
        "502 upstream": "fail",
        # v6.20: the two fields SGLang leaves unbounded and dies on rather than
        # refusing (sglang#40076, #31597). A client bug, like the oversize refusal.
        "400 logprob width over ceiling": "fail",
        "400 sampling field out of range": "fail",
        # v6.33: the request named a model this engine does not serve. SGLang would have
        # answered it with the loaded model and nothing would have shown the swap.
        "400 model not served": "fail",
        # v6.19, POST /v1/systemone: the answer was delivered; the request was
        # refused with the field named (a client bug, not the box's); the engine
        # answered a branch with something that is not a one-token distribution.
        "ok systemone": "ok",
        "400 systemone refused": "fail",   # a request that parses and cannot be served
        "400 suspect path": "fail",        # a path the proxy cannot relay as it is
        "422 systemone refused": "fail",
        "500 systemone failed": "fail",    # the catch-all: nothing leaves the route unanswered
        "CLIENT GONE mid-systemone": "gone",
        "CLIENT GONE before the engine answered": "gone",   # v6.31: a stream's caller left while it was queued
        "502 systemone upstream": "fail",
        "529 systemone overloaded": "fail",   # admission: the caller past SYSTEMONE_MAX_CALLS got no answer
    }

    @classmethod
    def setUpClass(cls):
        """Read the vocabulary out of the proxy's AST, not with a regex.

        Two wrong versions before this one, both worth naming. A regex on
        `self._done(` catches the first string literal only, so
        `self._done("ok" if kind == "f" else "UPSTREAM CUT")` reported one
        outcome and hid the other. Then an ast.walk collecting every string
        Constant picked up the PIECES of the f-strings ("503 engine unreachable
        (upstream " and ")") as if they were outcomes. Resolving each argument by
        node type is exact: a literal is itself, an f-string is its parts with a
        stand-in for the hole, and a conditional is both of its branches.
        """
        import ast

        def values(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return {node.value}
            if isinstance(node, ast.JoinedStr):
                # every hole in this proxy's _done() f-strings is an HTTP status
                return {"".join(v.value if isinstance(v, ast.Constant) else "502"
                                for v in node.values)}
            if isinstance(node, ast.IfExp):
                return values(node.body) | values(node.orelse)
            return set()

        tree = ast.parse((REPO / "keepalive-proxy.py").read_text())
        found = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_done"):
                for arg in node.args:
                    found |= values(arg)
        cls.outcomes = sorted(found)
        assert len(cls.outcomes) >= 12, cls.outcomes

    def test_the_proxy_writes_exactly_the_outcomes_this_file_knows(self):
        """The gate. A new _done() string in the proxy fails here until someone
        says which of the five kinds it is."""
        self.assertEqual(set(self.outcomes), set(self.EXPECTED),
                         "the proxy's outcome vocabulary changed: add the new "
                         "string to EXPECTED with the kind it must read as")

    def test_each_outcome_reads_as_the_kind_it_was_given(self):
        for outcome, kind in self.EXPECTED.items():
            self.assertEqual(lc.parse_feed(self._line(outcome))[0]["kind"], kind, outcome)

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
        for outcome in ("CLIENT GONE on write", "CLIENT GONE during non-sse wait",
                        "CLIENT GONE during keepalive",
                        "no outcome (client vanished mid-request)"):
            self.assertIn(outcome, self.outcomes, f"{outcome} is no longer in the proxy")
            self.assertEqual(lc.parse_feed(self._line(outcome))[0]["kind"], "gone", outcome)

    def test_a_real_failure_still_reads_as_one(self):
        for outcome in ("503 engine unreachable", "400 oversize refused", "400 bad Content-Length",
                        "413 body over cap", "503 monster held during warmup", "UPSTREAM CUT",
                        "DROPPED upstream silent", "REFUSED corrupted output"):
            self.assertEqual(lc.parse_feed(self._line(outcome))[0]["kind"], "fail", outcome)

    def test_a_delivered_answer_reads_as_ok(self):
        for outcome in ("ok", "ok get", "ok non-sse", "ok non-sse CORRUPTED"):
            self.assertEqual(lc.parse_feed(self._line(outcome))[0]["kind"], "ok", outcome)

    def test_a_request_labelled_by_the_identity_wall_is_read_to_its_end(self):
        """With the wall on, the proxy names the client after the peer on every line:
        "127.0.0.1:5555 key=alice". No end line matched, so every such request stayed "in
        flight", then "no end logged" (found in review, 2026-09-24)."""
        raw = "\n".join([
            "2026-09-24T12:00:00+02:00 gx10 python3[1]: [proxy] 127.0.0.1:5555 key=alice -> POST /v1/messages body=900000b",
            "2026-09-24T12:00:01+02:00 gx10 python3[1]: [proxy] 127.0.0.1:5555 key=alice oversize check: 28458 tokens fit (829020 usable of pool 901109)",
            "2026-09-24T12:00:09+02:00 gx10 python3[1]: [proxy] 127.0.0.1:5555 key=alice POST /v1/messages ok in 9.0s [300b relayed, first event at 8.1s, last at 9.0s]"])
        rows = lc.parse_feed(raw)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["outcome"], rows[0]["kind"], rows[0]["peer"]), ("ok", "ok", "127.0.0.1:5555"))
        self.assertIn("28,458 tokens counted", rows[0]["detail"])

    START = ("2026-10-01T12:49:31+02:00 gx10 python3[1]: [proxy] 127.0.0.1:5555 -> "
             "POST /v1/chat/completions body=502315b")

    def test_an_unfinished_request_stays_live_however_long_it_waits(self):
        """A generation has no deadline. On 2026-10-01 eight long prompts went in at once:
        three answered after 14 minutes, the last after 22, and the 10-minute rule this
        replaced called the five still queued "no end logged" while the engine ran three
        and queued two."""
        self.assertEqual(lc.parse_feed(self.START)[0]["kind"], "live")
        later = self.START + ("\n2026-10-01T13:03:29+02:00 gx10 python3[1]: [proxy] 127.0.0.1:6666 -> "
                              "POST /v1/chat/completions body=42b"
                              "\n2026-10-01T13:11:29+02:00 gx10 python3[1]: [proxy] 127.0.0.1:6666 POST "
                              "/v1/chat/completions ok in 480.0s")
        rows = lc.parse_feed(later)
        self.assertEqual([(r["outcome"], r["kind"]) for r in rows],
                         [("in flight", "live"), ("ok", "ok")])

    def test_it_has_no_end_once_the_proxy_that_took_it_is_gone(self):
        """Every way this box's journal says the proxy process is gone, copied from it: the
        stop (both shapes of systemd's line), a crash, and the next proxy's banner (both
        shapes, before and since PROXY_BIND)."""
        for gone in ("systemd[1]: qwen38-keepalive.service: Deactivated successfully.",
                     "systemd[1]: Stopped qwen38-keepalive.service - Keepalive proxy in front of the serving engine :30000 (agent clients connect to :30001).",
                     "systemd[1]: Stopped Keepalive proxy in front of the serving engine :30000.",
                     "systemd[1]: qwen38-keepalive.service: Main process exited, code=killed, status=9/KILL",
                     "systemd[1]: qwen38-keepalive.service: Failed with result 'signal'.",
                     "python3[2]: [proxy] v6.27 on 0.0.0.0:30001 -> http://127.0.0.1:30000 (keepalive 15s, max silence 900s)",
                     "python3[2]: [proxy] v6.14 on :30001 -> http://127.0.0.1:30000"):
            with self.subTest(gone=gone):
                rows = lc.parse_feed(self.START + "\n2026-10-01T12:50:00+02:00 gx10 " + gone)
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0]["outcome"], rows[0]["kind"]), ("no end logged", "unknown"))
                self.assertEqual(rows[0]["detail"], "the proxy that took it stopped")

    def test_a_proxy_still_stopping_can_still_end_it(self):
        """systemd writes "Stopping" before it signals the process: an end line can still
        follow, and it must land."""
        raw = "\n".join([self.START,
                         "2026-10-01T12:50:00+02:00 gx10 systemd[1]: Stopping qwen38-keepalive.service - Keepalive proxy...",
                         "2026-10-01T12:50:00+02:00 gx10 python3[1]: [proxy] 127.0.0.1:5555 POST /v1/chat/completions "
                         "CLIENT GONE on write in 29.0s"])
        self.assertEqual(lc.parse_feed(raw)[0]["kind"], "gone")

    def test_a_restart_leaves_finished_requests_alone_and_new_ones_live(self):
        """Only what was still open when the proxy went is orphaned: a delivered answer keeps
        its outcome, a request the next proxy took is live, and an end line with nothing
        open on its peer is dropped instead of reviving the orphan."""
        def line(ts, text):
            return f"2026-10-01T12:{ts}+02:00 gx10 {text}"
        raw = "\n".join([
            line("49:00", "python3[1]: [proxy] 127.0.0.1:1111 -> POST /v1/chat/completions body=10b"),
            line("49:05", "python3[1]: [proxy] 127.0.0.1:1111 POST /v1/chat/completions ok in 5.0s"),
            self.START,
            line("49:40", "python3[1]: [proxy] 127.0.0.1:5555 oversize check: 107156 tokens fit (436830 usable of pool 474816)"),
            line("50:00", "systemd[1]: qwen38-keepalive.service: Deactivated successfully."),
            line("50:01", "python3[2]: [proxy] v6.27 on 0.0.0.0:30001 -> http://127.0.0.1:30000"),
            line("50:02", "python3[2]: [proxy] 127.0.0.1:5555 POST /v1/chat/completions ok in 31.0s"),
            line("50:03", "python3[2]: [proxy] 127.0.0.1:7777 -> POST /v1/chat/completions body=10b"),
        ])
        rows = lc.parse_feed(raw)
        self.assertEqual([(r["peer"], r["outcome"], r["kind"]) for r in rows],
                         [("127.0.0.1:1111", "ok", "ok"),
                          ("127.0.0.1:5555", "no end logged", "unknown"),
                          ("127.0.0.1:7777", "in flight", "live")])
        self.assertIsNone(rows[0]["detail"])
        self.assertEqual(rows[1]["detail"], "107,156 tokens counted, fits (436,830 usable); the proxy that took it stopped")
        self.assertIsNone(rows[1]["secs"])

    def test_a_client_line_cannot_pass_for_the_proxy_going(self):
        """Request lines carry a peer and a path, never the banner's place: the banner
        alternative is anchored on the journal's "]: " so a peer, a label or a path that
        read like one leaves the record alone."""
        raw = "\n".join([self.START,
                         "2026-10-01T12:50:00+02:00 gx10 python3[1]: [proxy] 127.0.0.1:5555 key=v6.27_on_x "
                         "oversize check: 10 tokens fit (100 usable of pool 200)",
                         "2026-10-01T12:50:01+02:00 gx10 python3[1]: [proxy] 127.0.0.1:8888 -> GET "
                         "/v1/Stopped body=0b"])
        self.assertEqual(lc.parse_feed(raw)[0]["outcome"], "in flight")

    def test_two_requests_sharing_a_peer_stay_two_requests(self):
        """A reused keep-alive connection serves turns one after the other under
        the same ip:port. The old single record per peer let the second start
        overwrite the first, then let the first end mark the second as done:
        the feed showed 6 in flight for 7 requests on the engine (found live,
        2026-09-29, eight reviewers on one proxy)."""
        def line(ts, text):
            return f"2026-09-29T10:{ts}+02:00 gx10 python3[1]: [proxy] 127.0.0.1:5555 {text}"
        raw = "\n".join([
            line("00:00", "-> POST /v1/chat/completions body=100b"),
            line("00:01", "POST /v1/chat/completions ok in 1.0s"),
            line("00:02", "-> POST /v1/chat/completions body=100b"),
        ])
        rows = lc.parse_feed(raw)
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["outcome"], rows[0]["kind"]), ("ok", "ok"))
        self.assertEqual((rows[1]["outcome"], rows[1]["kind"]), ("in flight", "live"))
        # the first end must not touch the second request: end lines name the
        # newest still-open record, and an end with nothing open is ignored
        rows = lc.parse_feed(raw + "\n" + line("00:03", "POST /v1/chat/completions ok in 1.0s"))
        self.assertEqual([(r["outcome"], r["kind"]) for r in rows],
                         [("ok", "ok"), ("ok", "ok")])

    def test_an_end_with_nothing_open_is_ignored(self):
        """A duplicate end line (or one for a start the journal window lost)
        must leave the completed record alone instead of rewriting it."""
        def line(ts, text):
            return f"2026-09-29T10:{ts}+02:00 gx10 python3[1]: [proxy] 127.0.0.1:5555 {text}"
        raw = "\n".join([
            line("00:00", "-> POST /v1/chat/completions body=100b"),
            line("00:01", "POST /v1/chat/completions ok in 1.0s"),
            line("00:02", "POST /v1/chat/completions ok in 1.0s"),
        ])
        rows = lc.parse_feed(raw)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["outcome"], rows[0]["kind"], rows[0]["secs"]),
                         ("ok", "ok", 1.0))

    def test_an_end_naming_another_route_takes_the_open_one(self):
        """Two requests multiplexed on one peer: the end line still closes the
        newest still-open record when no open record has its route."""
        def line(ts, text):
            return f"2026-09-29T10:{ts}+02:00 gx10 python3[1]: [proxy] 127.0.0.1:5555 {text}"
        raw = "\n".join([
            line("00:00", "-> POST /v1/chat/completions body=100b"),
            line("00:01", "-> GET /v1/models body=0b"),
            line("00:02", "POST /v1/other ok in 1.0s"),
        ])
        rows = lc.parse_feed(raw)
        self.assertEqual([(r["path"], r["outcome"]) for r in rows],
                         [("/v1/chat/completions", "in flight"),
                          ("/v1/models", "ok")])

    def test_an_end_closes_the_request_it_names_not_the_newest(self):
        """Route matching is load-bearing, not decoration: swapping the method
        and path groups, flipping == to !=, or turning either or into and must
        all close the wrong record here (mutation walk, 2026-09-29)."""
        def line(ts, text):
            return f"2026-09-29T10:{ts}+02:00 gx10 python3[1]: [proxy] 127.0.0.1:5555 {text}"
        raw = "\n".join([
            line("00:00", "-> POST /v1/a body=100b"),
            line("00:01", "-> GET /v1/b body=100b"),
            line("00:02", "POST /v1/a ok in 1.0s"),
        ])
        rows = lc.parse_feed(raw)
        self.assertEqual([(r["path"], r["outcome"]) for r in rows],
                         [("/v1/a", "ok"), ("/v1/b", "in flight")])

    def test_feed_open_takes_the_route_parts_separately(self):
        """_feed_open answers method-only and path-only calls from the parts
        given: collapsing either or into and falls back to the newest record
        instead (mutation walk, 2026-09-29). NOTE: `method is not None` flipped
        to `is None` is genuinely equivalent and untestable: with both parts
        missing the block and the fallback both answer the newest open record,
        and parse_feed never calls it mixed any other way."""
        a = {"outcome": "in flight", "method": "POST", "path": "/v1/a"}
        b = {"outcome": "in flight", "method": "GET", "path": "/v1/b"}
        recs = [a, b]
        self.assertIs(lc._feed_open(recs, path="/v1/a"), a)
        self.assertIs(lc._feed_open(recs, "POST"), a)
        self.assertIs(lc._feed_open(recs), b)

    def test_details_land_on_the_open_request_not_the_finished_one(self):
        """A refused/fit line names only the peer: with an older request still
        open beside a finished one, the detail belongs to the open one. `or`
        turned into `and` would file it on the finished record."""
        def line(ts, text):
            return f"2026-09-29T10:{ts}+02:00 gx10 python3[1]: [proxy] 127.0.0.1:5555 {text}"
        raw = "\n".join([
            line("00:00", "-> POST /v1/a body=100b"),
            line("00:01", "-> POST /v1/b body=100b"),
            line("00:02", "POST /v1/b ok in 1.0s"),
            line("00:03", "REFUSED oversize (9b, 8 prompt tokens (counted by the engine), limit 7)"),
            line("00:04", "oversize check: 6 tokens fit (7 usable of pool 9)"),
        ])
        rows = lc.parse_feed(raw)
        self.assertIn("6 tokens counted", rows[0]["detail"])
        self.assertIsNone(rows[1]["detail"])

    def test_details_land_on_the_last_record_when_nothing_is_open(self):
        """The `or [-1]` fallback is the only thing a late detail line has when
        every record for the peer is finished: it attaches to the last one
        instead of crashing."""
        def line(ts, text):
            return f"2026-09-29T10:{ts}+02:00 gx10 python3[1]: [proxy] 127.0.0.1:5555 {text}"
        raw = "\n".join([
            line("00:00", "-> POST /v1/a body=100b"),
            line("00:01", "POST /v1/a ok in 1.0s"),
            line("00:02", "oversize check: 6 tokens fit (7 usable of pool 9)"),
        ])
        rows = lc.parse_feed(raw)
        self.assertEqual(len(rows), 1)
        self.assertIn("6 tokens counted", rows[0]["detail"])


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

    def test_restarting_a_ready_lane_says_requests_wait_for_it(self):
        """Since proxy v6.28 a request that reaches :30001 while a lane's unit comes back is
        held for it: "will get errors" was true of a stop only."""
        warns = lc.warn_reasons("unit", {"unit": "qwen38-sglang.service", "verb": "restart"},
                                {"qwen38-sglang.service": "ready"})
        self.assertEqual(len(warns), 1, warns)
        self.assertIn("wait for it while it boots", warns[0])
        self.assertNotIn("errors", warns[0])
        stop = lc.warn_reasons("unit", {"unit": "qwen38-sglang.service", "verb": "stop"},
                               {"qwen38-sglang.service": "ready"})
        self.assertIn("will get errors", stop[0])

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

    # ---- parse_feed's timestamp, and an orphan without a clock ---------------
    def test_only_an_iso_timestamp_is_read_as_one(self):
        raw = ("2026-09-10T09:00:00+02:00 h p[1]: [proxy] 1.2.3.4:5 -> "
               "POST /v1/chat/completions body=10b")
        self.assertEqual(lc.parse_feed(raw)[0]["ts"], "2026-09-10T09:00:00")

    def test_neither_a_line_without_a_date_nor_hours_of_traffic_orphan_a_start(self):
        """The clock this replaced read the newest dated line and turned every start 600 s
        older into "no end logged". Nothing reads a clock now: an undated line, and twenty
        hours of newer traffic, leave a start in flight; the proxy going is what ends it."""
        raw = ("2026-09-10T09:00:00+02:00 h p[1]: [proxy] 1.2.3.4:5 -> POST /v1/chat/completions body=10b\n"
               "2026-09-11T05:00:00+02:00 h p[1]: [proxy] 1.2.3.4:6 -> POST /v1/chat/completions body=10b\n"
               "[proxy] 1.2.3.4:7 -> POST /v1/chat/completions body=10b\n")
        self.assertEqual([(r["peer"], r["outcome"]) for r in lc.parse_feed(raw)],
                         [("1.2.3.4:5", "in flight"), ("1.2.3.4:6", "in flight"), ("1.2.3.4:7", "in flight")])


class TheLastTenLines(unittest.TestCase):
    """Written to answer one question: can a module like this reach 100%?

    coverage.py put lifecycle.py at 96% with exactly ten statements left, and
    every one of them turned out reachable. Two were real gaps rather than
    plumbing: outcome_kind's own "in flight" and "no end logged" branches were
    never called directly (parse_feed sets those kinds itself), and the drain
    ceiling was only ever tested through a hand-built dict, so the log line that
    is supposed to produce it had never been parsed. The other eight are the
    malformed-timestamp paths, which is exactly the input a journal produces when
    a line is truncated.
    """

    def test_outcome_kind_answers_the_two_states_parse_feed_sets_itself(self):
        self.assertEqual(lc.outcome_kind("in flight"), "live")
        self.assertEqual(lc.outcome_kind("no end logged"), "unknown")

    def test_a_real_drain_ceiling_line_is_counted(self):
        """The line the proxy writes when a drain runs past DRAIN_MAX_S. The
        verdict for ceiling > 0 was tested; the parse that produces it was not."""
        raw = ("[proxy] drain ceiling reached after 900s, dropping the socket "
               "for /v1/chat/completions")
        g = lc.parse_guard(raw)
        self.assertEqual(g["ceiling"], 1)
        self.assertEqual(g["drained"], 0, "a ceiling is not a completed drain")
        state, msg = lc.guard_verdict(lc.parse_zombies(""), g, True)
        self.assertEqual(state, "warn")
        self.assertIn("ceiling", msg)

    def test_a_boot_with_only_a_server_args_line_is_the_init_stage(self):
        b = lc.parse_boot_log(["[2026-09-10 10:00:00] server_args=ServerArgs(x)"])
        self.assertEqual(b["stage"], "init")
        self.assertEqual(b["done"], [])

    def test_a_tail_with_no_boot_evidence_has_no_stage(self):
        b = lc.parse_boot_log(["nothing about a boot in here", ""])
        self.assertIsNone(b["stage"])

    def test_an_unparsable_newest_timestamp_leaves_requests_in_flight(self):
        """The feed's orphan window needs a clock. If the newest line's timestamp
        is not a date, there is no clock, and a request must stay 'in flight'
        rather than be called unaccounted for on no evidence."""
        start = ("2026-09-10T09:00:00+02:00 h p[1]: [proxy] 1.2.3.4:5 -> "
                 "POST /v1/chat/completions body=10b")
        # 19 characters with a T in position 10, and not a date
        bogus = "2026-99-99T99:99:99 h p[1]: [proxy] 9.9.9.9:9 -> POST /x body=1b"
        rows = lc.parse_feed(start + "\n" + bogus)
        first = [r for r in rows if r["peer"] == "1.2.3.4:5"][0]
        self.assertEqual(first["kind"], "live")
        self.assertEqual(first["outcome"], "in flight")

    def test_a_request_whose_own_timestamp_is_unparsable_is_left_alone(self):
        """The start line carries the bogus stamp this time, and a later good
        line provides the clock: the row must survive rather than raise."""
        bogus = "2026-13-45T99:00:00 h p[1]: [proxy] 1.2.3.4:5 -> POST /x body=10b"
        good = ("2026-09-10T09:20:00+02:00 h p[1]: [proxy] 9.9.9.9:9 -> "
                "POST /v1/chat/completions body=10b")
        rows = lc.parse_feed(bogus + "\n" + good)
        row = [r for r in rows if r["peer"] == "1.2.3.4:5"][0]
        self.assertEqual(row["outcome"], "in flight")

    def test_a_zombie_request_with_an_unparsable_span_reports_no_seconds(self):
        """parse_zombies measures the span between a request's first and last
        flood line. A truncated timestamp must give secs=None, not a crash and
        not a made-up duration."""
        raw = ("[2026-09-10 10:00:00] Received output for rid='a' but the state "
               "was deleted in TokenizerManager.\n"
               "[2026-13-45 99:00:00] Received output for rid='a' but the state "
               "was deleted in TokenizerManager.")
        z = lc.parse_zombies(raw)
        self.assertEqual(z["lines"], 2)
        self.assertEqual(z["requests"][0]["rid"], "a")
        self.assertIsNone(z["requests"][0]["secs"])
        # and a verdict over it must not raise on the missing span
        state, msg = lc.guard_verdict(z, lc.parse_guard(""), True)
        self.assertEqual(state, "warn")


class TheLastFourBranches(unittest.TestCase):
    """After every statement was covered, four partial BRANCHES remained. Two
    were reachable and are tested here. The other two cannot be taken by any
    input, and rather than silence them with a pragma alone, the structural
    test below keeps the reason true:

      * `parse_boot_log`'s elif chain ends in a defensive `break`. Reaching it
        needs a marker name the chain does not handle, and the chain handles all
        of MARKERS. `test_every_marker_is_handled_by_the_chain` is what keeps
        that so, and is strictly stronger than covering the branch would be: it
        fails the day someone adds a marker without an arm.
      * `if stage in STAGES:` then `for s in STAGES: if s == stage: break` cannot
        finish the loop without breaking, because the guard in front of it
        guarantees a match.
    """

    def test_every_marker_is_handled_by_the_chain(self):
        """The guard on the unreachable break: every name in MARKERS must have an
        arm in parse_boot_log, or the break stops being dead code and starts
        being a silently ignored marker."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(lc.parse_boot_log))
        handled = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                    and node.left.id == "name"
                    and isinstance(node.comparators[0], ast.Constant)):
                handled.add(node.comparators[0].value)
        declared = {name for name, _ in lc.MARKERS}
        self.assertEqual(declared - handled, set(),
                         "a marker is declared but never handled: the defensive "
                         "break in parse_boot_log would swallow it")

    def test_an_action_with_no_lifecycle_gate_is_never_blocked(self):
        """Covers 162->166: the elif is false, so nothing is blocked. smoke,
        flush_cache and abort_all are safe at any engine state."""
        states = {u: "loading-weights" for u in lc.ENGINE_UNITS}
        for action in ("smoke", "flush_cache", "abort_all", "diag_bundle", ""):
            self.assertEqual(lc.blocked_reasons(action, {}, states), [], action)

    def test_a_window_with_drains_but_no_aborts_reads_as_drains(self):
        """Covers 488->490: handled is truthy with aborted at zero."""
        g = dict(lc.parse_guard(""), drained=4, drain_max_s=12.0)
        state, msg = lc.guard_verdict(lc.parse_zombies(""), g, True)
        self.assertEqual(state, "ok")
        self.assertEqual(msg, "4 drained, no flood")


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

    def test_the_banner_is_read_whatever_address_the_proxy_listens_on(self):
        """Since PROXY_BIND the banner names the address: "v6.24 on 0.0.0.0:30001". The
        pattern knew only "on :30001", so the page showed the last banner of that era,
        v6.20 on the reference box while v6.24 ran (found in review, 2026-09-24)."""
        for line, want in (("[proxy] v6.25 on 0.0.0.0:30001 -> http://127.0.0.1:30000", ("6.25", 30001)),
                           ("[proxy] v6.25 on 127.0.0.1:30071 -> http://127.0.0.1:30000", ("6.25", 30071)),
                           ("[proxy] v6.25 on [::]:30001 -> http://127.0.0.1:30000", ("6.25", 30001)),
                           ("[proxy] v6.14 on :30001 -> http://127.0.0.1:30000", ("6.14", 30001))):
            g = lc.parse_guard(line)
            self.assertEqual((g["version"], g["port"]), want, line)
            self.assertRegex(line, lc.GUARD_BANNER_GREP)

    def test_versions_compare_part_by_part(self):
        """As floats "6.9" is above "6.14": the page took v6.2 to v6.9 for proxies that
        abort, which only v6.14 and later do."""
        for v, older in (("6.2", True), ("6.9", True), ("6.13", True), ("6.14", False),
                         ("6.20", False), ("6.25", False), ("7.0", False)):
            self.assertIs(lc.version_before(v, "6.14"), older, v)
        for v in (None, "", "six", "6.x"):
            self.assertIsNone(lc.version_before(v, "6.14"), v)

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

    def test_old_dangling_start_is_in_flight_until_the_proxy_restarts(self):
        raw = "\n".join([
            self.L.format(h=1, rest="127.0.0.1:1111 -> POST /v1/chat/completions body=100b"),
            self.L.format(h=2, rest="127.0.0.1:2222 -> POST /v1/chat/completions body=200b"),
            self.L.format(h=2, rest="127.0.0.1:2222 POST /v1/chat/completions ok in 3.0s"),
        ])
        by = {r["peer"]: r for r in lc.parse_feed(raw)}
        self.assertEqual(by["127.0.0.1:1111"]["outcome"], "in flight")
        self.assertEqual(by["127.0.0.1:2222"]["outcome"], "ok")
        raw += "\n" + self.L.format(h=3, rest="v6.27 on 0.0.0.0:30001 -> http://127.0.0.1:30000")
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



class AFeedKeepsEveryRequestInFlight(unittest.TestCase):
    """The rail's badge and the table count the rows in flight, and the rows were the 25
    newest requests: with eight agents, a generation that outlasted 25 newer requests (34
    minutes on 2026-10-02) left both while the engine still ran it, the badge falling from
    8 to 2. Older requests still in flight stay in the rows, and what was in flight at one
    read is carried into the next, whose window no longer holds its start."""

    L = "2026-10-02T23:{m:02d}:{s:02d}+02:00 gx10 python3[1]: [proxy] {rest}"

    def short(self, n, minute):
        out = []
        for i in range(n):
            port = 40000 + minute * 100 + i
            out.append(self.L.format(m=minute, s=i % 60, rest=f"127.0.0.1:{port} -> POST /v1/chat/completions body=100b"))
            out.append(self.L.format(m=minute, s=i % 60, rest=f"127.0.0.1:{port} POST /v1/chat/completions ok in 1.0s"))
        return out

    def long_start(self, port=49398, minute=16):
        return self.L.format(m=minute, s=1, rest=f"127.0.0.1:{port} -> POST /v1/chat/completions body=111922b")

    def test_an_older_request_in_flight_stays_in_the_rows(self):
        raw = "\n".join([self.long_start()] + self.short(30, 20))
        rows = lc.parse_feed(raw)
        self.assertEqual(len(rows), 26)
        self.assertEqual((rows[0]["peer"], rows[0]["outcome"]), ("127.0.0.1:49398", "in flight"))
        self.assertEqual(sum(r["outcome"] == "in flight" for r in rows), 1)
        self.assertEqual([r["outcome"] for r in rows[1:]], ["ok"] * 25)

    def test_an_older_request_that_ended_leaves_the_rows_as_before(self):
        end = self.L.format(m=21, s=59, rest="127.0.0.1:49398 POST /v1/chat/completions ok in 358.0s")
        rows = lc.parse_feed("\n".join([self.long_start()] + self.short(30, 20) + [end]))
        self.assertEqual(len(rows), 25)
        self.assertNotIn("127.0.0.1:49398", [r["peer"] for r in rows[:-1]])

    def test_a_request_carried_from_the_last_read_ends_in_this_one(self):
        first = lc.parse_feed("\n".join([self.long_start()] + self.short(3, 20)))
        carry = [r for r in first if r["outcome"] == "in flight"]
        self.assertEqual(len(carry), 1)
        later = self.short(3, 40) + [self.L.format(m=49, s=49, rest="127.0.0.1:49398 POST /v1/chat/completions ok in 2028.0s")]
        rows = lc.parse_feed("\n".join(later), carry=carry)
        by = {r["peer"]: r for r in rows}
        self.assertEqual((by["127.0.0.1:49398"]["outcome"], by["127.0.0.1:49398"]["secs"]), ("ok", 2028.0))
        self.assertNotIn("127.0.0.1:49398", {r["peer"] for r in lc.parse_feed("\n".join(later))},
                         "without the carry, the end has no start to land on")

    def test_a_carried_request_still_in_its_window_is_not_counted_twice(self):
        raw = "\n".join([self.long_start()] + self.short(3, 20))
        carry = [r for r in lc.parse_feed(raw) if r["outcome"] == "in flight"]
        rows = lc.parse_feed(raw, carry=carry)
        self.assertEqual(sum(r["peer"] == "127.0.0.1:49398" for r in rows), 1)

    def test_a_carried_request_stays_in_flight_until_it_ends_or_the_proxy_goes(self):
        carry = [r for r in lc.parse_feed(self.long_start()) if r["outcome"] == "in flight"]
        rows = lc.parse_feed("\n".join(self.short(30, 40)), carry=carry)
        self.assertEqual(sum(r["outcome"] == "in flight" for r in rows), 1)
        gone = self.L.format(m=50, s=0, rest="v6.30 on 0.0.0.0:30001 -> http://127.0.0.1:30000")
        rows = lc.parse_feed("\n".join(self.short(2, 41) + [gone]), carry=carry)
        by = {r["peer"]: r for r in rows}
        self.assertEqual(by["127.0.0.1:49398"]["outcome"], "no end logged")

    def test_a_read_that_returned_nothing_keeps_what_was_in_flight(self):
        carry = [r for r in lc.parse_feed(self.long_start()) if r["outcome"] == "in flight"]
        self.assertEqual([r["peer"] for r in lc.parse_feed("", carry=carry)], ["127.0.0.1:49398"])

    def test_the_carry_is_not_changed_by_the_read(self):
        carry = [r for r in lc.parse_feed(self.long_start()) if r["outcome"] == "in flight"]
        lc.parse_feed(self.L.format(m=49, s=49, rest="127.0.0.1:49398 POST /v1/chat/completions ok in 2028.0s"), carry=carry)
        self.assertEqual(carry[0]["outcome"], "in flight")

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

    def test_a_folded_provider_is_not_reported_as_stranded(self):
        # GB_10 on the reference box, 2026-10-07: both lanes under one provider of the
        # operator's own naming. Judging by the generated names alone, the panel insisted a
        # default that did follow the lane did not.
        prov = {"qwen3.8-flash-next": ["GB_10"], "qwen3.8-27b": ["GB_10"]}
        ok, why = lc.opencode_default_follows("GB_10/qwen3.8-flash-next",
                                              {"qwen38-flash.service": "ready"}, prov)
        self.assertTrue(ok); self.assertIn("follows", why)
        ok, why = lc.opencode_default_follows("GB_10/qwen3.8-27b",
                                              {"qwen38-flash.service": "ready"}, prov)
        self.assertFalse(ok); self.assertIn("qwen3.8-flash-next", why)

    def test_a_config_with_no_entry_for_the_served_model_says_so(self):
        ok, why = lc.opencode_default_follows("GB_10/qwen3.8-flash-next",
                                              {"qwen38-flash.service": "ready"},
                                              {"qwen3.8-flash-next": []})
        self.assertFalse(ok); self.assertIn("no provider", why)

    def test_the_generated_names_are_the_fallback(self):
        ok, _why = lc.opencode_default_follows("flashnext/qwen3.8-flash-next", {"qwen38-flash.service": "ready"})
        self.assertTrue(ok)


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


class FourEngines(unittest.TestCase):
    """The video lane is a fourth engine, under the same one-at-a-time rule as the
    other three: about 108 GB of weights do not fit beside any serving lane."""
    VID = "qwen38-video.service"

    def test_video_is_blocked_while_the_27b_serves(self):
        r = lc.blocked_reasons("unit", {"unit": self.VID, "verb": "start"},
                               {"qwen38-sglang.service": "ready", "qwen38-flash.service": "stopped",
                                "qwen38-image.service": "stopped"})
        self.assertEqual(len(r), 1)
        self.assertIn("qwen38-sglang.service", r[0])

    def test_video_is_blocked_while_the_image_lane_runs(self):
        """With four lanes, checking one neighbour of three would let a start through."""
        r = lc.blocked_reasons("unit", {"unit": self.VID, "verb": "start"},
                               {"qwen38-sglang.service": "stopped",
                                "qwen38-flash.service": "stopped",
                                "qwen38-image.service": "ready"})
        self.assertEqual(len(r), 1)
        self.assertIn("qwen38-image.service", r[0])

    def test_a_text_lane_is_blocked_while_the_video_lane_runs(self):
        for text in ("qwen38-sglang.service", "qwen38-flash.service"):
            with self.subTest(text=text):
                r = lc.blocked_reasons("unit", {"unit": text, "verb": "start"},
                                       {self.VID: "ready"})
                self.assertEqual(len(r), 1)
                self.assertIn(self.VID, r[0])

    def test_an_image_start_is_blocked_while_the_video_lane_runs(self):
        r = lc.blocked_reasons("unit", {"unit": "qwen38-image.service", "verb": "start"},
                               {self.VID: "warming-up"})
        self.assertEqual(len(r), 1)
        self.assertIn(self.VID, r[0])

    def test_video_starts_when_nothing_else_runs(self):
        self.assertEqual(lc.blocked_reasons("unit", {"unit": self.VID, "verb": "start"},
                                            {"qwen38-sglang.service": "stopped",
                                             "qwen38-flash.service": "stopped",
                                             "qwen38-image.service": "stopped"}), [])

    def test_the_switch_waits_for_a_video_boot_too(self):
        self.assertEqual(len(lc.blocked_reasons("switch", {"target": "stock"},
                                                {self.VID: "loading-weights"})), 1)

    def test_stopping_a_ready_video_lane_warns_about_the_video_not_the_proxy(self):
        """The proxy on :30001 is a text door; nothing reaches this lane through it."""
        w = lc.warn_reasons("unit", {"unit": self.VID, "verb": "stop"}, {self.VID: "ready"})
        self.assertEqual(len(w), 1)
        self.assertIn("video", w[0])
        self.assertIn("12 min", w[0])
        self.assertNotIn(":30001", w[0])

    def test_the_video_lane_is_an_engine_but_not_a_text_engine(self):
        self.assertIn(self.VID, lc.ENGINE_UNITS)
        self.assertNotIn(self.VID, lc.TEXT_UNITS)
        self.assertEqual(lc.VIDEO_UNIT, self.VID)


class VideoBootLog(unittest.TestCase):
    """The MiniMax-H3 boot lines, captured from journald on the reference box on
    2026-09-25 (boot 23:08:50): DiT 61.73 GB in 13 shards, text encoder 48.09 GB,
    audio VAE 0.56 GB, video VAE 5.2 GB, then three warmup requests before ready."""

    @classmethod
    def setUpClass(cls):
        cls.lines = (HERE.parent / "fixtures" / "video-boot.log").read_text().splitlines()

    def at(self, needle):
        """The boot as it looked the moment this line was written."""
        for i, ln in enumerate(self.lines):
            if needle in ln:
                return lc.parse_video_boot_log(self.lines[:i + 1])
        self.fail(f"the fixture has no line with {needle!r}")

    def test_a_complete_boot_is_fired_up_with_every_stage_done(self):
        b = lc.parse_video_boot_log(self.lines)
        self.assertTrue(b["fired_up"])
        self.assertEqual(b["done"], list(lc.VIDEO_STAGES))

    def test_each_shared_milestone_opens_its_stage(self):
        for needle, stage in (
                ("Starting server", "init"),
                ("Loading pipeline modules", "loading-weights"),
                ("Pipeline instantiated", "loading-weights"),
                ("Starting FastAPI server", "warming-up")):
            with self.subTest(line=needle):
                b = self.at(needle)
                self.assertEqual(b["stage"], stage)
                self.assertFalse(b["fired_up"])

    def test_each_component_line_names_what_is_being_loaded(self):
        """"loading weights" for a minute says less than "the 61.7 GB DiT"."""
        for needle, detail in (
                ("Loading text_encoder from", "48 GB text encoder"),
                ("Loading MiniMaxH3DiTModel from", "61.7 GB DiT"),
                ("Loading audio_vae from", "audio VAE"),
                ("Loading video_vae from", "5.2 GB video VAE")):
            with self.subTest(line=needle):
                b = self.at(needle)
                self.assertEqual(b["stage"], "loading-weights")
                self.assertIn(detail, b["detail"])

    def test_only_the_latest_boot_counts(self):
        """journald keeps every previous life of the unit. A "fired up" from an earlier
        boot must not make the one in progress read as ready."""
        restarted = self.lines + ["[2026-09-25 22:00:00] Starting server...",
                                  "[2026-09-25 22:00:07] Loading pipeline modules..."]
        b = lc.parse_video_boot_log(restarted)
        self.assertFalse(b["fired_up"])
        self.assertEqual(b["stage"], "loading-weights")

    def test_an_empty_journal_is_not_a_boot(self):
        b = lc.parse_video_boot_log([])
        self.assertIsNone(b["stage"])
        self.assertFalse(b["fired_up"])




class TheVideoRunComesFromItsOwnJournal(unittest.TestCase):
    """The lane's own API says "queued, 0%" from the POST until the video is done
    (measured 2026-09-29), so the phase and the step come from its journal. A wrong
    operator here is a progress bar that lies while a 60 GB call burns the box."""

    def run_of(self, *messages):
        return lc.parse_video_run(list(messages))

    def test_nothing_ran_reads_empty(self):
        self.assertEqual(self.run_of(), {})
        self.assertEqual(self.run_of("serving, some unrelated line"), {})

    def test_a_step_line_alone_proves_a_request_runs(self):
        r = self.run_of("denoise:  40%|#### | 20/50 [03:40<05:30, 11.02s/it]")
        self.assertEqual(r["phase"], "denoise")
        self.assertEqual((r["step"], r["steps"]), (20, 50))
        self.assertEqual(r["elapsed_s"], 220.0)
        self.assertEqual(r["left_s"], 330.0)
        self.assertEqual(r["s_per_step"], 11.02)
        self.assertEqual(r["label"], "denoising")

    def test_a_h_m_s_clock_reads_and_a_question_mark_is_none(self):
        r = self.run_of("denoise:  20%|# | 10/50 [1:02:03<??,  ?it/s]")
        self.assertEqual(r["elapsed_s"], 3723.0)
        self.assertIsNone(r["left_s"])
        self.assertIsNone(r["s_per_step"])

    def test_it_per_second_is_read_as_seconds_per_step(self):
        r = self.run_of("denoise:  10%| | 5/50 [00:10<00:10, 1.52it/s]")
        self.assertAlmostEqual(r["s_per_step"], 1 / 1.52, places=6)

    def test_the_rate_helper_reads_both_arms_and_garbage(self):
        self.assertEqual(lc._rate_s_per_step("14.78s/it"), 14.78)
        self.assertEqual(lc._rate_s_per_step("2it/s"), 0.5)
        for junk in ("?it/s", "0it/s", "x s/it", ""):
            self.assertIsNone(lc._rate_s_per_step(junk), junk)

    def test_a_zero_rate_reads_none_and_not_a_crash(self):
        # 0it/s is tqdm's own before the first step completes: inverting it
        # would crash the parser inside a live progress read
        r = self.run_of("denoise:  0%| | 0/50 [00:00<?, 0it/s]")
        self.assertIsNone(r["s_per_step"])
        self.assertEqual(r["step"], 0)
        self.assertEqual(r["steps"], 50)

    def test_pipeline_start_is_encode_until_a_stage_names_otherwise(self):
        r = self.run_of("Running pipeline stages for request abc")
        self.assertEqual(r["phase"], "encode")
        self.assertEqual(r["label"], "encoding the prompt")
        r2 = self.run_of("Running pipeline stages", "[MiniMaxH3DenoisingStage] started")
        self.assertEqual(r2["phase"], "denoise")

    def test_a_stage_the_phase_map_does_not_know_leaves_encode(self):
        r = self.run_of("Running pipeline stages", "[MiniMaxH3EncodingStage] started")
        self.assertEqual(r["phase"], "encode")

    def test_a_lone_finished_stage_is_not_a_run(self):
        self.assertEqual(self.run_of("[MiniMaxH3DenoisingStage] finished"), {})

    def test_a_finished_stage_moves_nothing_by_itself(self):
        r = self.run_of("[MiniMaxH3DenoisingStage] finished", "[MiniMaxH3DecodingStage] started")
        self.assertEqual(r["phase"], "decode")
        self.assertEqual(r["label"], "decoding video and audio")

    def test_denoise_stays_denoise_before_the_decode(self):
        r = self.run_of("[MiniMaxH3DenoisingStage] started",
                        "denoise:  60%|#### | 30/50 [05:30<03:40, 11.02s/it]")
        self.assertEqual(r["phase"], "denoise")
        self.assertEqual(r["step"], 30)

    def test_the_100_percent_line_closes_the_denoise(self):
        r = self.run_of("[MiniMaxH3DecodingStage] started",
                        "denoise: 100%|####| 50/50 [09:52<00:00, 11.85s/it]")
        self.assertEqual(r["phase"], "decode")
        self.assertEqual(r["step"], 50)

    def test_the_run_ends_at_its_outcome_line(self):
        for end in ("Pixel data generated for request abc",
                    "Error executing request abc",
                    "Failed to generate video"):
            r = self.run_of("Running pipeline stages", end)
            self.assertEqual(r, {}, end)

    def test_ansi_and_carriage_returns_do_not_hide_the_step(self):
        r = self.run_of("\x1b[2B\x1b[1Gdenoise:  80%|#### | 40/50 [07:21<01:51, 11.12s/it]\r")
        self.assertEqual(r["step"], 40)
        self.assertEqual(r["s_per_step"], 11.12)


class TheVideoRunFixtureIsReadEndToEnd(unittest.TestCase):
    """One real 12-minute call, captured from journald on the reference box on
    2026-09-29: 49 denoise steps at ~14.8 s each, then the pipeline's own "Pixel data
    generated" line closes it. The tests above feed the parser hand-written lines;
    this one feeds the whole journal, so a parser that reads them out of order or
    loses the close is caught against what the lane really emitted."""

    @classmethod
    def setUpClass(cls):
        raw = (HERE.parent / "fixtures" / "video-run.log").read_text().splitlines()
        cls.msgs = [json.loads(ln) for ln in raw if ln.strip()]

    def at(self, needle, last=False):
        """The run as it looked the moment this line was written."""
        idx = [i for i, m in enumerate(self.msgs) if needle in m]
        self.assertTrue(idx, needle)
        return lc.parse_video_run(self.msgs[:(idx[-1] if last else idx[0]) + 1])

    def test_the_journal_opens_in_encode(self):
        r = self.at("Running pipeline")
        self.assertEqual(r["phase"], "encode")
        self.assertIsNone(r["step"])

    def test_the_first_step_line_switches_to_denoise(self):
        r = self.at("denoise:")
        self.assertEqual(r["phase"], "denoise")
        self.assertEqual((r["step"], r["steps"]), (0, 49))

    def test_the_last_step_line_carries_the_measured_rate(self):
        r = self.at("denoise:", last=True)
        self.assertEqual((r["step"], r["steps"]), (49, 49))
        self.assertAlmostEqual(r["s_per_step"], 14.78, places=2)
        self.assertEqual(r["elapsed_s"], 724.0)

    def test_the_outcome_line_closes_the_run(self):
        self.assertEqual(self.at("Pixel data"), {})

    def test_the_journal_ends_in_the_next_request_encode(self):
        # the tail of the window is the next call starting: the close of one run must
        # not leak a stale denoise step into the read of the following one
        r = lc.parse_video_run(self.msgs)
        self.assertEqual(r["phase"], "encode")
        self.assertIsNone(r["step"])



class TheCockpitsRouteProbeIsNotAFailure(unittest.TestCase):
    """The Decide view asks the proxy whether it serves /v1/systemone with a body that has no
    state, which the proxy refuses at the schema without running a model. The request log
    painted each of those a red failure, once a minute while the view was open. These are
    the proxy's own lines for that probe, from the reference box on 2026-09-30."""
    PROBE = ("2026-09-30T17:04:05+02:00 gx10-eff9 python3[4726]: [proxy] {peer} -> POST {path} body={size}b\n"
             "2026-09-30T17:04:05+02:00 gx10-eff9 python3[4726]: [proxy] {peer} systemone INVALID: ['body', 'state'] Field required\n"
             "2026-09-30T17:04:05+02:00 gx10-eff9 python3[4726]: [proxy] {peer} POST {path} {outcome} in 0.0s\n")

    def row(self, peer="127.0.0.1:56836", path="/v1/systemone", size=40, outcome="422 systemone refused"):
        rows = lc.parse_feed(self.PROBE.format(peer=peer, path=path, size=size, outcome=outcome))
        self.assertEqual(len(rows), 1)
        return lc.mark_probes(rows, "/v1/systemone", 40)[0]

    def test_the_probe_reads_as_the_cockpits_own(self):
        r = self.row()
        self.assertEqual(r["kind"], "probe")
        self.assertTrue(r["outcome"].startswith("422"), "the status stays in the words")

    def test_the_same_refusal_from_a_client_is_still_a_failure(self):
        self.assertEqual(self.row(peer="100.78.198.77:51000")["kind"], "fail")

    def test_a_request_from_this_box_that_is_not_the_probe_body_is_still_a_failure(self):
        self.assertEqual(self.row(size=57)["kind"], "fail")

    def test_another_route_is_never_taken_for_the_probe(self):
        self.assertEqual(self.row(path="/v1/chat/completions", outcome="422 bad request")["kind"], "fail")

    def test_an_answered_request_keeps_its_own_outcome(self):
        r = self.row(outcome="ok non-sse")
        self.assertEqual((r["kind"], r["outcome"]), ("ok", "ok non-sse"))


class TheStartGuardsWords(unittest.TestCase):
    """engine-preflight.sh's lines, as the cockpit reads them from a unit's journal. Without
    them a lane the guard held read "starting", and one it refused "keeps crashing"."""
    W = ("preflight: qwen38-sglang.service waits: what is left of an engine still holds 16 GiB of GPU "
         "memory: 1756018 sglang::stuck 16578 MiB")
    R = ("preflight: NOT starting qwen38-sglang.service: what is left of an engine still holds 16 GiB of "
         "GPU memory: 1756018 sglang::stuck 16578 MiB, still after 60 s. Starting into memory the driver "
         "has not given back is how sglang#40948 froze a DGX Spark until a power cycle. Stop what holds "
         "it, or reboot if nothing does; systemd tries again in 15 s.")
    S = "preflight: qwen38-sglang.service starts: no GPU memory held from before, 115.7 GiB available (0 s)"
    HELD = "what is left of an engine still holds 16 GiB of GPU memory: 1756018 sglang::stuck 16578 MiB"

    def test_each_word_and_its_reason(self):
        self.assertEqual(lc.parse_preflight([self.W]), {"verdict": "waits", "reason": self.HELD})
        self.assertEqual(lc.parse_preflight([self.R]), {"verdict": "refused", "reason": self.HELD})
        self.assertEqual(lc.parse_preflight([self.S])["verdict"], "starts")

    def test_the_last_word_wins(self):
        self.assertEqual(lc.parse_preflight([self.W, self.S])["verdict"], "starts")
        self.assertEqual(lc.parse_preflight([self.S, self.W])["verdict"], "waits")
        self.assertEqual(lc.parse_preflight([self.W, self.R])["verdict"], "refused")
        self.assertEqual(lc.parse_preflight([self.W, "Started qwen38-sglang.service"])["verdict"], "waits")

    def test_silence_and_noise_are_none(self):
        for lines in ([], ["Starting qwen38-sglang.service..."], ["preflight: half a line"], [""]):
            self.assertIsNone(lc.parse_preflight(lines), lines)

    def test_a_journal_prefix_does_not_hide_it(self):
        line = "2026-10-01T14:47:50+02:00 gx10-eff9 bash[1757029]: " + self.W
        self.assertEqual(lc.parse_preflight([line])["verdict"], "waits")


class TheGuardsFlags(unittest.TestCase):
    W = {"verdict": "waits", "reason": "the GPU driver does not answer nvidia-smi"}
    R = {"verdict": "refused", "reason": "the GPU driver does not answer nvidia-smi"}

    def test_held_only_while_the_unit_runs_its_start_step(self):
        st = {"state": "starting"}
        self.assertEqual(lc.preflight_flags(st, unit_sub="start-pre", preflight=self.W)["held"], self.W["reason"])
        # the guard passed and the engine's own command runs: its last word is history
        self.assertNotIn("held", lc.preflight_flags(st, unit_sub="start", preflight=self.W))
        self.assertNotIn("held", lc.preflight_flags({"state": "loading-weights"}, unit_sub="start-pre", preflight=self.W))

    def test_refused_only_on_a_failed_unit(self):
        st = {"state": "failed", "restarting": True}
        out = lc.preflight_flags(st, unit_sub="auto-restart", preflight=self.R)
        self.assertEqual((out["refused"], out["restarting"]), (self.R["reason"], True))
        self.assertNotIn("refused", lc.preflight_flags({"state": "starting"}, unit_sub="start-pre", preflight=self.R))
        self.assertNotIn("held", lc.preflight_flags(st, unit_sub="auto-restart", preflight=self.W))

    def test_a_guard_that_let_it_start_or_said_nothing_adds_nothing(self):
        st = {"state": "starting"}
        self.assertIs(lc.preflight_flags(st, unit_sub="start-pre", preflight=None), st)
        out = lc.preflight_flags(st, unit_sub="start-pre", preflight={"verdict": "starts", "reason": "x"})
        self.assertEqual(out, st)
        self.assertIsNot(out, st, "the state it was handed is not changed in place")


class TheZombie(unittest.TestCase):
    """A server alive without its scheduler: nothing restarts it unless the cockpit does."""

    def plan(self, **kw):
        args = dict(state="degraded", scheduler_alive=False, since=None, now=1000.0, after_s=120.0,
                    enabled=True, cooldown_ok=True, job_running=False)
        args.update(kw)
        return lc.zombie_plan(**args)

    def test_only_a_degraded_engine_with_no_scheduler_is_one(self):
        for kw in ({"state": "ready"}, {"state": "warming-up"}, {"state": "wedged"},
                   {"scheduler_alive": True}, {"scheduler_alive": None}):
            with self.subTest(**kw):
                self.assertEqual(self.plan(**kw), {"zombie": False, "since": None, "restart": False, "in": 0.0})

    def test_it_waits_its_delay_from_the_first_sighting(self):
        first = self.plan()
        self.assertEqual(first, {"zombie": True, "since": 1000.0, "restart": False, "in": 120.0})
        self.assertEqual(self.plan(since=1000.0, now=1060.0)["in"], 60.0)
        self.assertFalse(self.plan(since=1000.0, now=1119.9)["restart"])
        due = self.plan(since=1000.0, now=1120.0)
        self.assertEqual((due["restart"], due["in"], due["since"]), (True, 0.0, 1000.0))

    def test_it_restarts_only_when_allowed(self):
        for kw in ({"enabled": False}, {"cooldown_ok": False}, {"job_running": True}):
            with self.subTest(**kw):
                out = self.plan(since=0.0, now=1000.0, **kw)
                self.assertEqual((out["zombie"], out["restart"]), (True, False))

if __name__ == '__main__':
    unittest.main()
