"""Offline tests for recipes.py, built on the REAL repo files (install.sh and
the two lane templates) plus synthetic registry snapshots."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
import recipes as rc  # noqa: E402

REPO = HERE.parents[2]
ASSIGNS = rc.parse_assignments((REPO / "install.sh").read_text())
TEMPLATES = rc.load_templates(REPO)


class ParseAssignments(unittest.TestCase):
    def test_literal_default_and_comment(self):
        text = ('STOCK_REV="52d1adc5f38aa5ebf099c29ed7025ba34cfbb854"\n'
                'FLASH_IMAGE="${FLASH_IMAGE:-lmsysorg/sglang@sha256:' + "a" * 64 + '}"  # = tag, date\n'
                'SERVE_IMAGE="${SERVE_IMAGE:-qwen38-dflash2:v1.2.2}"\n'
                'MODEL_CHOICE="${MODEL_CHOICE:-stock}"\n'
                '  INDENTED="no"  # inside a function is fine too\n'
                'MODEL_REPO="$STOCK_REPO"\n')
        a = rc.parse_assignments(text)
        self.assertEqual(a["STOCK_REV"], "52d1adc5f38aa5ebf099c29ed7025ba34cfbb854")
        self.assertEqual(a["FLASH_IMAGE"], "lmsysorg/sglang@sha256:" + "a" * 64)
        self.assertEqual(a["SERVE_IMAGE"], "qwen38-dflash2:v1.2.2")
        self.assertEqual(a["MODEL_CHOICE"], "stock")
        self.assertEqual(a["INDENTED"], "no")
        self.assertNotIn("MODEL_REPO", a)  # a reference, not a literal

    def test_real_install_pins(self):
        for k in ("STOCK_REPO", "STOCK_REV", "UNC_REPO", "UNC_REV", "FLASH_REPO",
                  "FLASH_REV", "FLASH_NVDA_REPO", "FLASH_NVDA_REV", "FLASH_TIER",
                  "FLASH_MEM_FRACTION", "PLE_RSS_BUDGET_GB", "OVERLAY_FLASH_SERVE_IMAGE",
                  "FLASH_SERVE_IMAGE", "FLASH_IMAGE", "SERVE_IMAGE", "DRAFT2_REV"):
            self.assertIn(k, ASSIGNS, k)
        self.assertRegex(ASSIGNS["FLASH_REV"], r"^[0-9a-f]{40}$")
        self.assertTrue(ASSIGNS["FLASH_IMAGE"].startswith("lmsysorg/sglang@sha256:"))


class ProfileFromText(unittest.TestCase):
    def test_flash_template(self):
        # The launcher carries placeholders for the tier, the checkpoint's own
        # flag pair and two numbers, so the raw template profiles as strings
        # where install.sh substitutes; builtin() is what resolves them.
        p = rc.profile_from_text(TEMPLATES["qwen38-flash-launch.sh.template"])
        self.assertEqual(p["engine"]["image"], "__IMAGE__")
        self.assertEqual(p["model"]["repo"], "__MODEL__")
        self.assertEqual(p["serve"]["mem_fraction"], "__FLASH_MEM_FRACTION__")
        self.assertEqual(p["serve"]["context_length"], 262144)
        self.assertEqual(p["serve"]["chunked_prefill"], 4096)
        self.assertEqual(p["serve"]["page_size"], 64)
        self.assertEqual(p["serve"]["fp4_gemm_backend"], "flashinfer_cutlass")
        self.assertEqual(p["serve"]["ple_offload_backend"], "file")
        self.assertEqual(p["env"]["SGLANG_OPT_MAMBA_SKIP_DECODE_LOCK"], "1")
        self.assertEqual(p["env"]["PYTORCH_CUDA_ALLOC_CONF"], "expandable_segments:True")

    def test_flash_tiers_resolve(self):
        lat = rc.builtin("flash", ASSIGNS, TEMPLATES)
        self.assertEqual(lat["serve"]["max_running_requests"], 4)
        self.assertEqual(lat["serve"]["max_mamba_cache_size"], 20)
        self.assertEqual(lat["serve"]["mamba_cache_strategy"], "extra_buffer")
        self.assertEqual(lat["serve"]["quantization"], "modelopt_fp4")
        self.assertEqual(lat["drafter"]["algorithm"], "NEXTN")
        self.assertEqual(lat["drafter"]["steps"], 3)
        self.assertEqual(lat["drafter"]["draft_tokens"], 4)
        con = rc.builtin("flash", {**ASSIGNS, "FLASH_TIER": "concurrency"}, TEMPLATES)
        self.assertEqual(con["serve"]["max_running_requests"], 8)
        self.assertEqual(con["serve"]["max_mamba_cache_size"], 40)
        thr = rc.builtin("flash", {**ASSIGNS, "FLASH_TIER": "throughput"}, TEMPLATES)
        self.assertEqual(thr["serve"]["max_running_requests"], 24)
        self.assertEqual(thr["serve"]["max_mamba_cache_size"], 96)
        self.assertEqual(thr["serve"]["mamba_cache_strategy"], "extra_buffer_lazy")
        self.assertEqual(thr["drafter"]["algorithm"], "none")

    def test_nvda_export_swaps_the_quantization_flags(self):
        # The mixed-precision export is served with no --quantization and a
        # pinned MoE runner; the RadixArk one the other way round.
        nv = rc.builtin("flash-nvda", ASSIGNS, TEMPLATES)
        self.assertEqual(nv["model"]["repo"], ASSIGNS["FLASH_NVDA_REPO"])
        self.assertEqual(nv["serve"]["moe_runner_backend"], "flashinfer_cutlass")
        self.assertNotIn("quantization", nv["serve"])
        rd = rc.builtin("flash", ASSIGNS, TEMPLATES)
        self.assertNotIn("moe_runner_backend", rd["serve"])
        self.assertEqual(rd["lane"], nv["lane"])

    def test_a_flag_named_in_a_comment_is_not_a_flag(self):
        # 2026-09-03: a comment saying "--max-total-tokens below pins the ceiling" was
        # parsed as max_total_tokens="below" and failed the recipe as invalid
        text = ("# the --max-total-tokens below pins the ceiling\n"
                "  # --mem-fraction-static 0.99 is what NOT to do\n"
                "python3 -m sglang.launch_server --max-total-tokens 190000 "
                "--mem-fraction-static 0.81 --port 1")
        p = rc.profile_from_text(text)
        self.assertEqual(p["serve"]["max_total_tokens"], 190000)
        self.assertEqual(p["serve"]["mem_fraction"], 0.81)

    def test_27b_template(self):
        p = rc.profile_from_text(TEMPLATES["qwen38-sglang.service.template"])
        self.assertEqual(p["serve"]["mem_fraction"], 0.50)
        self.assertEqual(p["serve"]["max_running_requests"], 8)
        self.assertEqual(p["serve"]["attention_backend"], "flashinfer")
        self.assertEqual(p["serve"]["max_mamba_cache_size"], 96)
        self.assertEqual(p["drafter"]["algorithm"], "DFLASH")
        self.assertEqual(p["drafter"]["repo"], "z-lab/Qwen3.8-27B-DFlash2")
        self.assertEqual(p["drafter"]["revision"], "__DRAFT2_REV__")
        self.assertEqual(p["drafter"]["draft_tokens"], 8)
        self.assertNotIn("context_length", p["serve"])  # native default, no flag


class Builtins(unittest.TestCase):
    def test_every_builtin_recipe_is_valid(self):
        recs = rc.builtins(ASSIGNS, TEMPLATES)
        self.assertEqual([r["id"] for r in recs],
                         ["stock", "uncensored", "fp8", "uncensored-fp8", "flash",
                          "flash-nvda", "flash-uncensored"])
        for r in recs:
            errs = rc.validate(r) if r["lane"] == "flash" else rc.validate(
                {**r, "serve": {**r["serve"], "context_length": 262144}})
            self.assertEqual(errs, [], r["id"])

    def test_fp8_is_the_27b_lane_with_qwens_own_checkpoint(self):
        f = rc.builtin("fp8", ASSIGNS, TEMPLATES)
        self.assertEqual(f["lane"], "27b")
        self.assertEqual(f["model"]["repo"], ASSIGNS["FP8_REPO"])
        self.assertEqual(f["model"]["revision"], ASSIGNS["FP8_REV"])
        # same engine image and same drafter as the NVFP4 targets: the weights
        # differ, and so does exactly one serve flag. Qwen's FP8 checkpoint has no
        # KV scales, so it must ask for the fp8 KV cache the NVFP4 checkpoints get
        # from their own quant config; without it the pool roughly halves.
        s = rc.builtin("stock", ASSIGNS, TEMPLATES)
        self.assertEqual(f["engine"], s["engine"])
        self.assertEqual(f["drafter"], s["drafter"])
        self.assertEqual(f["serve"].get("kv_cache_dtype"), "fp8_e4m3")
        self.assertIsNone(s["serve"].get("kv_cache_dtype"))
        self.assertEqual({k: v for k, v in f["serve"].items() if k != "kv_cache_dtype"},
                         s["serve"], "fp8 must differ from stock by the KV cache alone")

    def test_uncensored_fp8_differs_from_fp8_only_by_model(self):
        a, b = rc.builtin("fp8", ASSIGNS, TEMPLATES), rc.builtin("uncensored-fp8", ASSIGNS, TEMPLATES)
        self.assertEqual(b["lane"], "27b")
        self.assertEqual(b["model"]["repo"], ASSIGNS["UNCFP8_REPO"])
        self.assertEqual(b["model"]["revision"], ASSIGNS["UNCFP8_REV"])
        self.assertNotEqual(a["model"]["repo"], b["model"]["repo"])
        for k in ("engine", "drafter", "serve"):
            self.assertEqual(a[k], b[k], k)

    def test_flash_pins_substituted(self):
        f = rc.builtin("flash", ASSIGNS, TEMPLATES)
        self.assertEqual(f["model"]["repo"], ASSIGNS["FLASH_REPO"])
        self.assertEqual(f["model"]["revision"], ASSIGNS["FLASH_REV"])
        # The default pin is a reference to the base, resolved one level: the
        # served image and the base are the same official image since v1.8.
        self.assertEqual(f["engine"]["image"], ASSIGNS["FLASH_IMAGE"])
        self.assertEqual(f["engine"]["base_image"], ASSIGNS["FLASH_IMAGE"])
        self.assertEqual(f["env"]["SGLANG_QWEN4_PLE_FILE_RSS_BUDGET_GB"], ASSIGNS["PLE_RSS_BUDGET_GB"])
        self.assertEqual(f["serve"]["mem_fraction"], float(ASSIGNS["FLASH_MEM_FRACTION"]))
        # Since v1.8 the served image IS the pinned official one, so there is no
        # overlay to name; an OVERLAY=1 box is what puts a local tag back here.
        self.assertIsNone(f["engine"]["overlay"])

    def test_stock_vs_uncensored_differ_only_by_model(self):
        s, u = rc.builtin("stock", ASSIGNS, TEMPLATES), rc.builtin("uncensored", ASSIGNS, TEMPLATES)
        self.assertNotEqual(s["model"], u["model"])
        self.assertEqual(s["drafter"]["revision"], ASSIGNS["DRAFT2_REV"])
        for k in ("engine", "drafter", "serve", "env"):
            self.assertEqual(s[k], u[k], k)

    def test_unknown_id(self):
        with self.assertRaises(KeyError):
            rc.builtin("banana", ASSIGNS, TEMPLATES)


def good():
    return {
        "id": "my-flash", "lane": "flash",
        "engine": {"family": "sglang", "image": "qwen38-flash:v1.5.3"},
        "model": {"repo": "RadixArk/Qwen3.8-Flash-Next-NVFP4", "revision": "7" * 40},
        "drafter": {"algorithm": "NEXTN", "repo": None, "revision": None, "steps": 3, "draft_tokens": 4},
        "serve": {"context_length": 262144, "mem_fraction": 0.81, "max_running_requests": 1},
        "env": {"SGLANG_OPT_MAMBA_SKIP_DECODE_LOCK": "1"},
    }


class Validate(unittest.TestCase):
    def test_good(self):
        self.assertEqual(rc.validate(good()), [])

    def check(self, mutate, needle):
        r = good()
        mutate(r)
        errs = rc.validate(r, reserved_ids=rc.BUILTIN_IDS)
        self.assertTrue(any(needle in e for e in errs), (needle, errs))

    def test_rejections(self):
        self.check(lambda r: r.update(id="Flash!"), "id:")
        self.check(lambda r: r.update(id="flash"), "reserved")
        self.check(lambda r: r.update(lane="vllm"), "lane:")
        self.check(lambda r: r["engine"].update(family="vllm"), "engine.family")
        self.check(lambda r: r["engine"].update(image="qwen38-flash:latest"), "moving tag")
        self.check(lambda r: r["engine"].update(image="qwen38-flash"), "engine.image")
        self.check(lambda r: r["model"].update(revision="main"), "model.revision")
        self.check(lambda r: r["model"].update(repo="no-owner"), "model.repo")
        self.check(lambda r: r["drafter"].update(algorithm="EAGLE9"), "drafter.algorithm")
        self.check(lambda r: r["drafter"].update(repo="z-lab/x"), "own head")
        self.check(lambda r: r["drafter"].update(algorithm="DFLASH"), "drafter.repo")
        self.check(lambda r: r["drafter"].update(draft_tokens=99), "drafter.draft_tokens")
        self.check(lambda r: r["serve"].update(mem_fraction=0.99), "0.3 to 0.95")
        self.check(lambda r: r["serve"].update(mem_fraction=True), "number expected")
        self.check(lambda r: r["serve"].update(extra_flag=1), "unknown key")
        self.check(lambda r: r["serve"].pop("context_length"), "serve.context_length")
        self.check(lambda r: r["serve"].update(attention_backend="magic"), "serve.attention_backend")
        self.check(lambda r: r.update(env={"lower": "1"}), "NAME must be")
        self.check(lambda r: r.update(env={"X": "a b"}), "without whitespace")
        self.check(lambda r: r.update(env="X=1"), "env:")

    def test_not_a_dict(self):
        self.assertEqual(rc.validate([]), ["recipe must be an object"])


class Drift(unittest.TestCase):
    def test_no_drift_against_own_template_render(self):
        f = rc.builtin("flash", ASSIGNS, TEMPLATES)
        rendered = TEMPLATES["qwen38-flash-launch.sh.template"]
        for k, v in {"__IMAGE__": f["engine"]["image"], "__MODEL__": f["model"]["repo"],
                     "__MODEL_REV_ARGS__": "--revision " + f["model"]["revision"],
                     "__MODEL_REV__": f["model"]["revision"],
                     "__FLASH_QUANT_ARGS__": "--quantization modelopt_fp4 ",
                     "__FLASH_TIER_ARGS__": rc.TIER_ARGS["context"],
                     "__FLASH_MEM_FRACTION__": ASSIGNS["FLASH_MEM_FRACTION"],
                     "__PLE_RSS_BUDGET_GB__": ASSIGNS["PLE_RSS_BUDGET_GB"],
                     "__SPEC_TOKEN_MAP_LINE__":
                         "TIER+=(--speculative-token-map /out/token-map-"
                         + ASSIGNS["SPEC_TOKEN_MAP_SIZE"] + ".pt)"}.items():
            rendered = rendered.replace(k, v)
        self.assertEqual(rc.drift(f, rc.profile_from_text(rendered)), [])

    def test_changed_flag_and_env_reported(self):
        f = rc.builtin("flash", ASSIGNS, TEMPLATES)
        text = TEMPLATES["qwen38-flash-launch.sh.template"]
        text = text.replace("--mem-fraction-static __FLASH_MEM_FRACTION__",
                            "--mem-fraction-static 0.70")
        text = text.replace("-e SGLANG_OPT_MAMBA_SKIP_DECODE_LOCK=1 \\\n", "")
        rows = {r["key"]: r for r in rc.drift(f, rc.profile_from_text(text))}
        self.assertEqual(rows["serve.mem_fraction"]["installed"], 0.70)
        self.assertIsNone(rows["env.SGLANG_OPT_MAMBA_SKIP_DECODE_LOCK"]["installed"])
        self.assertNotIn("engine.image", rows)  # placeholder side skipped

    def test_1m_unit_vs_stock_recipe(self):
        s = rc.builtin("stock", ASSIGNS, TEMPLATES)
        unit = (REPO / "qwen38-sglang-1m.service.template").read_text()
        keys = {r["key"] for r in rc.drift(s, rc.profile_from_text(unit))}
        self.assertIn("serve.context_length", keys)
        self.assertIn("serve.mem_fraction", keys)


class Presence(unittest.TestCase):
    REG = {"images": [{"ref": "qwen38-flash:v1.5.3"}],
           "models": [{"repo_id": "RadixArk/Qwen3.8-Flash-Next-NVFP4",
                       "revisions": [{"rev": "7" * 40}]},
                      {"repo_id": "z-lab/Qwen3.8-27B-DFlash2", "revisions": []}]}

    def test_present(self):
        p = rc.presence(good(), self.REG)
        self.assertEqual(p, {"image": True, "downloading": False, "model": True, "drafter": None})

    def test_missing_and_unknown(self):
        r = good()
        r["engine"]["image"] = "qwen38-flash:v9"
        r["model"]["revision"] = "8" * 40
        r["drafter"] = {"algorithm": "DFLASH", "repo": "z-lab/Qwen3.8-27B-DFlash2", "revision": "9" * 40}
        self.assertEqual(rc.presence(r, self.REG),
                         {"image": False, "downloading": False, "model": False, "drafter": False})
        r["model"]["repo"] = "someone/else"
        self.assertIsNone(rc.presence(r, self.REG)["model"])


class LoadCustom(unittest.TestCase):
    def test_directory(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "ok.json").write_text(json.dumps(good()))
            bad = good(); bad["id"] = "stock"
            (p / "bad.json").write_text(json.dumps(bad))
            (p / "broken.json").write_text("{not json")
            (p / "notes.txt").write_text("ignored")
            items = {i["file"]: i for i in rc.load_custom(p)}
            self.assertEqual(set(items), {"ok.json", "bad.json", "broken.json"})
            self.assertEqual(items["ok.json"]["errors"], [])
            self.assertFalse(items["ok.json"]["recipe"]["builtin"])
            self.assertTrue(any("reserved" in e for e in items["bad.json"]["errors"]))
            self.assertIsNone(items["broken.json"]["recipe"])
        self.assertEqual(rc.load_custom(Path("/nonexistent/dir")), [])



class PresenceDuringDownload(unittest.TestCase):
    """A snapshot directory exists long before the model can be served: while
    huggingface_hub still has .incomplete blobs, presence must not say 'here'."""

    def rec(self):
        return rc.builtin("fp8", ASSIGNS, TEMPLATES)

    def reg(self, incomplete):
        r = self.rec()
        return {"images": [{"ref": r["engine"]["image"]}],
                "models": [{"repo_id": r["model"]["repo"], "incomplete": incomplete,
                            "revisions": [{"rev": r["model"]["revision"]}]}]}

    def test_complete_model_is_present(self):
        p = rc.presence(self.rec(), self.reg(0))
        self.assertIs(p["model"], True)
        self.assertFalse(p["downloading"])

    def test_model_with_incomplete_blobs_is_not_present(self):
        p = rc.presence(self.rec(), self.reg(8))
        self.assertIs(p["model"], False)
        self.assertTrue(p["downloading"])


class PresenceOfANeverDownloadedTarget(unittest.TestCase):
    """A pinned checkpoint that is not in the cache is missing, not unknown: the
    difference decides whether switching to it works or downloads 31 GB first."""

    def test_pinned_and_absent_reads_missing(self):
        r = rc.builtin("uncensored-fp8", ASSIGNS, TEMPLATES)
        reg = {"images": [{"ref": r["engine"]["image"]}], "models": [],
               "managed_repos": [r["model"]["repo"]]}
        self.assertIs(rc.presence(r, reg)["model"], False)

    def test_unpinned_and_absent_stays_unknown(self):
        r = rc.builtin("uncensored-fp8", ASSIGNS, TEMPLATES)
        reg = {"images": [], "models": [], "managed_repos": []}
        self.assertIsNone(rc.presence(r, reg)["model"])


class KvCacheDtype(unittest.TestCase):
    """Only the FP8 pair asks for an fp8 KV cache, and it must reach the recipe.

    Qwen's FP8 checkpoint carries no KV scales, so without the flag SGLang falls
    back to a bf16 KV cache worth about half the pool (measured, same 1m unit:
    771,139 tokens with, 382,706 without). A recipe that omits it is not the
    recipe anyone measured, and the placeholder must never survive into one."""

    def setUp(self):
        self.built = {r["id"]: r for r in rc.builtins(ASSIGNS, TEMPLATES)}

    def test_fp8_pair_requests_fp8_kv(self):
        for rid in ("fp8", "uncensored-fp8"):
            self.assertEqual(self.built[rid]["serve"].get("kv_cache_dtype"), "fp8_e4m3",
                             f"{rid} lost the fp8 KV cache that its pool figures assume")

    def test_other_targets_do_not_force_a_kv_dtype(self):
        for rid in ("stock", "uncensored", "flash"):
            self.assertIsNone(self.built[rid]["serve"].get("kv_cache_dtype"),
                              f"{rid} must take the KV dtype from its checkpoint")

    def test_no_placeholder_survives_into_a_recipe(self):
        for rid, rec in self.built.items():
            blob = json.dumps(rec)
            self.assertNotIn("__KV_CACHE_ARGS__", blob, f"{rid} kept the KV placeholder")



class ContextModeRecipes(unittest.TestCase):
    """The 27B lane has two unit templates and a box runs one. A recipe derived from
    the wrong one reports the other mode's own settings as drift, forever."""

    @classmethod
    def setUpClass(cls):
        cls.assigns = rc.parse_assignments((REPO / "install.sh").read_text())
        cls.templates = rc.load_templates(REPO)

    def test_both_27b_templates_are_loaded(self):
        self.assertIn("qwen38-sglang.service.template", self.templates)
        self.assertIn("qwen38-sglang-1m.service.template", self.templates)

    def test_native_mode_is_the_native_template(self):
        # the native unit sets no --context-length at all: the checkpoint's own window
        r = rc.builtin("stock", self.assigns, self.templates, "native")
        self.assertNotIn("context_length", r["serve"])
        self.assertNotIn("SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN", r["env"])

    def test_1m_mode_carries_the_1m_settings(self):
        r = rc.builtin("stock", self.assigns, self.templates, "1m")
        self.assertEqual(r["serve"]["context_length"], 1010000)
        self.assertEqual(r["serve"]["mem_fraction"], 0.70)
        self.assertEqual(r["env"].get("SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN"), "1")

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            rc.builtin("stock", self.assigns, self.templates, "enormous")

    def test_the_flash_lane_ignores_the_mode(self):
        a = rc.builtin("flash", self.assigns, self.templates, "native")
        b = rc.builtin("flash", self.assigns, self.templates, "1m")
        self.assertEqual(a, b)

    def test_mode_is_read_off_the_installed_invocation(self):
        self.assertEqual(rc.context_mode_of({"serve": {"context_length": 1010000}}), "1m")
        self.assertEqual(rc.context_mode_of({"serve": {"context_length": 262144}}), "native")
        self.assertEqual(rc.context_mode_of({"serve": {}}), "native")
        self.assertEqual(rc.context_mode_of(None), "native")

    def test_a_1m_box_shows_no_drift_on_those_three_keys(self):
        # the exact false alarm this fixes: the served lane on the reference box
        installed = rc.profile_from_text((REPO / "qwen38-sglang-1m.service.template").read_text())
        rec = rc.builtin("uncensored-fp8", self.assigns, self.templates, "1m")
        keys = {d["key"] for d in rc.drift(rec, installed)}
        for k in ("serve.context_length", "serve.mem_fraction",
                  "env.SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN"):
            self.assertNotIn(k, keys)


class FlashFamily(unittest.TestCase):
    """The three flash targets share one lane, one launcher and one set of
    serving flags. What may differ between them is the checkpoint, and for the
    mixed-precision export the one flag pair its own quantization forces."""

    def test_abliterated_differs_only_by_checkpoint(self):
        a = rc.builtin("flash", ASSIGNS, TEMPLATES)
        b = rc.builtin("flash-uncensored", ASSIGNS, TEMPLATES)
        self.assertNotEqual(a["model"], b["model"])
        self.assertEqual(b["model"]["repo"], ASSIGNS["FLASH_UNC_REPO"])
        for key in ("lane", "engine", "drafter", "serve", "env"):
            self.assertEqual(a[key], b[key], key)

    def test_nvda_differs_only_by_checkpoint_and_its_quant_flags(self):
        a = rc.builtin("flash", ASSIGNS, TEMPLATES)
        n = rc.builtin("flash-nvda", ASSIGNS, TEMPLATES)
        for key in ("lane", "engine", "env"):
            self.assertEqual(a[key], n[key], key)
        differing = {k for k in set(a["serve"]) | set(n["serve"])
                     if a["serve"].get(k) != n["serve"].get(k)}
        self.assertEqual(differing, {"quantization", "moe_runner_backend"})
        # The draft's quantization is the third checkpoint-owned difference, and
        # the one whose failure mode is silent: the RadixArk tree's MTP tensors
        # are BF16 inside an NVFP4 export, NVIDIA's are fp8 block-scaled.
        self.assertEqual(a["drafter"]["quantization"], "unquant")
        self.assertIsNone(n["drafter"].get("quantization"))
        self.assertEqual(a["drafter"]["algorithm"], n["drafter"]["algorithm"])

    def test_abliterated_keeps_the_bf16_draft_quantization(self):
        b = rc.builtin("flash-uncensored", ASSIGNS, TEMPLATES)
        self.assertEqual(b["drafter"]["quantization"], "unquant")

    def test_every_flash_target_is_on_the_flash_lane(self):
        for rid in ("flash", "flash-nvda", "flash-uncensored"):
            self.assertEqual(rc.builtin(rid, ASSIGNS, TEMPLATES)["lane"], "flash", rid)

    def test_no_recipe_leaves_a_placeholder_behind(self):
        # builtin() renders a lane template; a placeholder it forgets becomes a
        # flag the recipe does not know about, and every box then reports a drift
        # it does not have. Live smoke caught exactly that for the draft
        # vocabulary, so the invariant is asserted for every id and every mode.
        for rid in rc.BUILTIN_IDS:
            for mode in rc.CONTEXT_MODES:
                with self.subTest(f"{rid}/{mode}"):
                    rc.builtin(rid, ASSIGNS, TEMPLATES, mode)   # raises if any is left
        for tier in rc.TIER_ARGS:
            rc.builtin("flash", {**ASSIGNS, "FLASH_TIER": tier}, TEMPLATES)

    def test_the_draft_vocabulary_follows_the_tier(self):
        on = rc.builtin("flash", ASSIGNS, TEMPLATES)
        self.assertEqual(on["serve"]["speculative_token_map"],
                         f"/out/token-map-{ASSIGNS['SPEC_TOKEN_MAP_SIZE']}.pt")
        off = rc.builtin("flash", {**ASSIGNS, "SPEC_TOKEN_MAP_SIZE": "0"}, TEMPLATES)
        self.assertNotIn("speculative_token_map", off["serve"])
        thr = rc.builtin("flash", {**ASSIGNS, "FLASH_TIER": "throughput"}, TEMPLATES)
        self.assertNotIn("speculative_token_map", thr["serve"],
                         "a tier that does not speculate must not carry a draft vocabulary")

    def test_every_flash_target_validates(self):
        for rid in ("flash", "flash-nvda", "flash-uncensored"):
            self.assertEqual(rc.validate(rc.builtin(rid, ASSIGNS, TEMPLATES)), [], rid)


class TokenMap(unittest.TestCase):
    """The reduced draft vocabulary is a speculative-path flag. A tier that does
    not speculate must not receive it, and a rendered launcher must carry it
    exactly when install.sh built one."""

    LINE = "TIER+=(--speculative-token-map /out/token-map-65536.pt)"

    def render(self, tier, line):
        text = TEMPLATES["qwen38-flash-launch.sh.template"]
        for k, v in {"__FLASH_TIER_ARGS__": rc.TIER_ARGS[tier],
                     "__SPEC_TOKEN_MAP_LINE__": line,
                     "__FLASH_QUANT_ARGS__": "--quantization modelopt_fp4 ",
                     "__FLASH_MEM_FRACTION__": "0.85",
                     "__PLE_RSS_BUDGET_GB__": "8",
                     "__IMAGE__": "img:v", "__MODEL__": "org/repo",
                     "__MODEL_REV_ARGS__": "--revision " + "c" * 40,
                     "__MODEL_REV__": "c" * 40, "__PLE_DIR__": "/ple",
                     "__HF_CACHE__": "/hf", "__HOME__": "/home/u",
                     "__PORT__": "30000"}.items():
            text = text.replace(k, v)
        return text

    def test_flag_is_read_when_present(self):
        prof = rc.profile_from_text(self.render("context", self.LINE))
        self.assertEqual(prof["serve"]["speculative_token_map"],
                         "/out/token-map-65536.pt")

    def test_absent_when_off(self):
        prof = rc.profile_from_text(self.render("context", ""))
        self.assertNotIn("speculative_token_map", prof["serve"])

    def test_render_still_parses_either_way(self):
        import subprocess
        for line in ("", self.LINE):
            text = self.render("context", line)
            r = subprocess.run(["bash", "-n"], input=text, text=True,
                               capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_path_shape_is_validated(self):
        base = rc.builtin("flash", ASSIGNS, TEMPLATES)
        for bad in ("/out/a b.pt", "/out/x;rm -rf /", "/out/$(id).pt", "", "x" * 513):
            r = {**base, "serve": {**base["serve"], "speculative_token_map": bad}}
            self.assertTrue(any("speculative_token_map" in e for e in rc.validate(r)),
                            repr(bad))
        good = {**base, "serve": {**base["serve"],
                                  "speculative_token_map": "/out/token-map-65536.pt"}}
        self.assertEqual(rc.validate(good), [])


class ImageParserParity(unittest.TestCase):
    """switch-model.sh gates a switch on the image the installed invocation will
    run, and it reads it with awk. The cockpit reads the same thing in Python.
    If the two ever disagree, one of them is gating on the wrong image.

    The shell side is not copied here: it is sourced out of switch-model.sh, so
    this test exercises the code that actually ships. A copy would drift, and a
    drifting copy of a gate is worse than no gate."""

    @classmethod
    def setUpClass(cls):
        text = (REPO / "switch-model.sh").read_text()
        m = re.search(r"^unit_image\(\) \{[^\n]*\n(.*?)^\}$", text, re.M | re.S)
        if not m:
            raise AssertionError("switch-model.sh no longer defines unit_image()")
        cls.fn = "unit_image() {\n" + m.group(1) + "}\n"

    def shell_image(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
            fh.write(text)
            path = fh.name
        try:
            r = subprocess.run(["bash", "-c", self.fn + 'unit_image "$1"', "sh", path],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout.strip()
        finally:
            os.unlink(path)

    def test_both_readers_agree_on_both_invocations(self):
        cases = {
            "flash": TokenMap().render("context", TokenMap.LINE),
            "flash, no token map": TokenMap().render("throughput", ""),
            "27b": (REPO / "qwen38-sglang.service.template").read_text()
                   .replace("__IMAGE__", "qwen38-dflash2:v1.2.3")
                   .replace("__KV_CACHE_ARGS__", ""),
            "27b 1m": (REPO / "qwen38-sglang-1m.service.template").read_text()
                      .replace("__IMAGE__", "qwen38-dflash2:v1.2.3")
                      .replace("__KV_CACHE_ARGS__", ""),
        }
        for name, text in cases.items():
            py = rc.profile_from_text(text)["engine"]["image"]
            sh = self.shell_image(text)
            self.assertEqual(py, sh, f"{name}: python {py!r} vs shell {sh!r}")
            self.assertIn(":", sh, name)

    def test_shell_reader_is_quiet_on_a_missing_file(self):
        r = subprocess.run(["bash", "-c", self.fn + 'unit_image /nope/nothing.sh'],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_digest_pinned_image_survives_both_readers(self):
        digest = "lmsysorg/sglang@sha256:" + "a" * 64
        text = TokenMap().render("context", TokenMap.LINE).replace("img:v", digest)
        self.assertEqual(rc.profile_from_text(text)["engine"]["image"], digest)
        self.assertEqual(self.shell_image(text), digest)


class SwitchRewrite(unittest.TestCase):
    """switch-model.sh points the flash launcher at another checkpoint of the same
    lane. The function is sourced out of the script, not copied, and exercised in
    both directions on both input shapes, because this rewrite broke twice on
    2026-09-08: once leaving the previous checkpoint's draft quantization behind
    (fragments patched instead of the line rebuilt) and once on sed escaping."""

    QUANT = {
        "flash": "--quantization modelopt_fp4 --speculative-draft-model-quantization unquant ",
        "flash-nvda": "--moe-runner-backend flashinfer_cutlass ",
    }
    REPO_OF = {
        "flash": "RadixArk/Qwen3.8-Flash-Next-NVFP4",
        "flash-nvda": "nvidia/Qwen3.8-Flash-Next-NVFP4",
    }

    @classmethod
    def setUpClass(cls):
        text = (REPO / "switch-model.sh").read_text()
        m = re.search(r"^rewrite_flash_launcher\(\) \{[^\n]*\n(.*?)^\}$", text, re.M | re.S)
        if not m:
            raise AssertionError("switch-model.sh no longer defines rewrite_flash_launcher()")
        cls.fn = "rewrite_flash_launcher() {\n" + m.group(1) + "}\n"
        # And the two strings it switches between must be install.sh's own.
        inst = (REPO / "install.sh").read_text()
        for choice, want in cls.QUANT.items():
            if f'NEW_QUANT="{want}"' not in text:
                raise AssertionError(f"switch-model.sh lost the {choice} flags")
            if f'FLASH_QUANT_ARGS="{want}"' not in inst:
                raise AssertionError(f"install.sh lost the {choice} flags")

    def rewrite(self, launcher_text, choice):
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
            fh.write(launcher_text)
            path = fh.name
        try:
            rev = "d" * 40
            r = subprocess.run(
                ["bash", "-c", self.fn + 'rewrite_flash_launcher "$1" "$2" "$3" "$4"',
                 "sh", path, self.REPO_OF[choice], rev, self.QUANT[choice]],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout
        finally:
            os.unlink(path)

    def launcher(self, choice):
        text = TokenMap().render("context", TokenMap.LINE)
        return (text.replace("--quantization modelopt_fp4 ", self.QUANT[choice])
                if choice == "flash-nvda" else text)

    def test_every_direction_lands_exactly(self):
        for source in ("flash", "flash-nvda"):
            for target in ("flash", "flash-nvda"):
                out = self.rewrite(self.launcher(source), target)
                with self.subTest(f"{source} -> {target}"):
                    self.assertIn(f"--model-path {self.REPO_OF[target]} ", out)
                    self.assertIn("--revision " + "d" * 40, out)
                    self.assertIn(self.QUANT[target] + "--fp4-gemm-backend flashinfer_cutlass", out)
                    other = self.QUANT["flash" if target == "flash-nvda" else "flash-nvda"]
                    self.assertNotIn(other.strip(), out)
                    # and the result is still a launcher
                    r = subprocess.run(["bash", "-n"], input=out, text=True,
                                       capture_output=True)
                    self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_rewrite_is_idempotent(self):
        once = self.rewrite(self.launcher("flash"), "flash-nvda")
        twice = self.rewrite(once, "flash-nvda")
        self.assertEqual(once, twice)

    def test_nothing_else_in_the_launcher_moves(self):
        before = self.launcher("flash").splitlines()
        after = self.rewrite(self.launcher("flash"), "flash-nvda").splitlines()
        self.assertEqual(len(before), len(after))
        changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        # exactly two lines: the model path plus revision line, and the flag line
        self.assertEqual(len(changed), 2, [before[i] for i in changed])


class SwitchRewrite27B(unittest.TestCase):
    """The 27B unit's KV cache dtype belongs to the checkpoint, and until
    2026-09-08 the switch did not rewrite it: NVFP4 -> FP8 left the unit with no
    --kv-cache-dtype, which on Qwen's FP8 release means a bf16 KV cache and half
    the pool (771,139 tokens against 382,706, measured by this repo). The
    function is sourced out of switch-model.sh, and both real unit templates are
    exercised in both directions."""

    KV = {"stock": "", "uncensored": "", "fp8": "--kv-cache-dtype fp8_e4m3 ",
          "uncensored-fp8": "--kv-cache-dtype fp8_e4m3 "}

    @classmethod
    def setUpClass(cls):
        text = (REPO / "switch-model.sh").read_text()
        m = re.search(r"^rewrite_27b_unit\(\) \{[^\n]*\n(.*?)^\}$", text, re.M | re.S)
        if not m:
            raise AssertionError("switch-model.sh no longer defines rewrite_27b_unit")
        cls.fn = "rewrite_27b_unit() {\n" + m.group(1) + "}\n"
        inst = (REPO / "install.sh").read_text()
        if 'fp8|uncensored-fp8) KV_CACHE_ARGS="--kv-cache-dtype fp8_e4m3 "' not in inst:
            raise AssertionError("install.sh no longer sets the fp8 KV args the same way")
        if 'fp8|uncensored-fp8) NEW_KV="--kv-cache-dtype fp8_e4m3 "' not in text:
            raise AssertionError("switch-model.sh does not carry install.sh's KV args")

    def rendered_unit(self, template, kv):
        text = (REPO / template).read_text()
        for k, v in {"__IMAGE__": "qwen38-dflash2:v1.2.3", "__KV_CACHE_ARGS__": kv,
                     "__MODEL__": "RadixArk/Qwen3.8-27B-NVFP4",
                     "__MODEL_REV_ARGS__": "--revision " + "a" * 40,
                     "__MODEL_REV__": "a" * 40, "__DRAFT2_REV__": "b" * 40}.items():
            text = text.replace(k, v)
        return text

    def rewrite(self, unit_text, kv):
        with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as fh:
            fh.write(unit_text)
            path = fh.name
        try:
            r = subprocess.run(
                ["bash", "-c", self.fn + 'rewrite_27b_unit "$1" "$2" "$3" "$4"',
                 "sh", path, "Qwen/Qwen3.8-27B-FP8", "c" * 40, kv],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout
        finally:
            os.unlink(path)

    def test_the_kv_dtype_follows_the_target_both_ways(self):
        for template, frac in (("qwen38-sglang.service.template", "0.50"),
                               ("qwen38-sglang-1m.service.template", "0.70")):
            for src in ("", "--kv-cache-dtype fp8_e4m3 "):
                for dst_choice, dst in self.KV.items():
                    with self.subTest(f"{template} {src!r}->{dst_choice}"):
                        out = self.rewrite(self.rendered_unit(template, src), dst)
                        n = out.count("--kv-cache-dtype fp8_e4m3")
                        self.assertEqual(n, 1 if dst else 0, out)
                        # the box's own memory fraction survives a switch
                        self.assertIn(f"--mem-fraction-static {frac}", out)
                        self.assertIn("--model-path Qwen/Qwen3.8-27B-FP8 ", out)
                        self.assertIn("--revision " + "c" * 40, out)

    def test_only_two_lines_move(self):
        base = self.rendered_unit("qwen38-sglang.service.template", "")
        out = self.rewrite(base, "--kv-cache-dtype fp8_e4m3 ")
        a, b = base.splitlines(), out.splitlines()
        self.assertEqual(len(a), len(b))
        changed = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        self.assertEqual(len(changed), 2, [a[i] for i in changed])

    def test_the_1m_context_survives(self):
        out = self.rewrite(self.rendered_unit("qwen38-sglang-1m.service.template", ""), "")
        self.assertIn("--context-length 1010000", out)
        self.assertIn("HF_HUB_OFFLINE=1", out)

    def test_idempotent(self):
        once = self.rewrite(self.rendered_unit("qwen38-sglang.service.template", ""), "")
        self.assertEqual(once, self.rewrite(once, ""))

    def test_the_drafters_own_revision_is_not_touched(self):
        # --speculative-draft-model-revision holds the word "revision" but not the
        # token "--revision", and the rewrite must leave it alone: pointing the
        # draft at the target's commit would fail to load, ten minutes in.
        base = self.rendered_unit("qwen38-sglang.service.template", "")
        out = self.rewrite(base, "")
        self.assertIn("--speculative-draft-model-revision " + "b" * 40, out)
        self.assertIn("--revision " + "c" * 40, out)
        self.assertNotIn("--speculative-draft-model-revision " + "c" * 40, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
