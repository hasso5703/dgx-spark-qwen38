"""Offline tests for recipes.py, built on the REAL repo files (install.sh and
the two lane templates) plus synthetic registry snapshots."""
import json
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
                  "FLASH_REV", "FLASH_SERVE_IMAGE", "FLASH_IMAGE", "SERVE_IMAGE", "DRAFT2_REV"):
            self.assertIn(k, ASSIGNS, k)
        self.assertRegex(ASSIGNS["FLASH_REV"], r"^[0-9a-f]{40}$")
        self.assertTrue(ASSIGNS["FLASH_IMAGE"].startswith("lmsysorg/sglang@sha256:"))


class ProfileFromText(unittest.TestCase):
    def test_flash_template(self):
        p = rc.profile_from_text(TEMPLATES["qwen38-flash-launch.sh.template"])
        self.assertEqual(p["engine"]["image"], "__IMAGE__")
        self.assertEqual(p["model"]["repo"], "__MODEL__")
        self.assertEqual(p["serve"]["mem_fraction"], 0.81)
        self.assertEqual(p["serve"]["context_length"], 262144)
        self.assertEqual(p["serve"]["max_running_requests"], 1)
        self.assertEqual(p["serve"]["chunked_prefill"], 1024)
        self.assertEqual(p["serve"]["prefill_attention"], "triton")
        self.assertEqual(p["serve"]["decode_attention"], "trtllm_mha")
        self.assertEqual(p["drafter"]["algorithm"], "NEXTN")
        self.assertEqual(p["drafter"]["steps"], 3)
        self.assertEqual(p["drafter"]["draft_tokens"], 4)
        self.assertEqual(p["env"]["SGLANG_OPT_MAMBA_SKIP_DECODE_LOCK"], "1")
        self.assertEqual(p["env"]["SGLANG_QWEN4_PLE_TAG"], "__MODEL_REV__")

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
                         ["stock", "uncensored", "fp8", "uncensored-fp8", "flash"])
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
        self.assertEqual(f["engine"]["image"], ASSIGNS["FLASH_SERVE_IMAGE"])
        self.assertEqual(f["engine"]["base_image"], ASSIGNS["FLASH_IMAGE"])
        self.assertEqual(f["env"]["SGLANG_QWEN4_PLE_TAG"], ASSIGNS["FLASH_REV"])
        self.assertEqual(f["serve"]["mem_fraction"], 0.81)

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
                     "__MODEL_REV__": f["model"]["revision"]}.items():
            rendered = rendered.replace(k, v)
        self.assertEqual(rc.drift(f, rc.profile_from_text(rendered)), [])

    def test_changed_flag_and_env_reported(self):
        f = rc.builtin("flash", ASSIGNS, TEMPLATES)
        text = TEMPLATES["qwen38-flash-launch.sh.template"]
        text = text.replace("--mem-fraction-static 0.81", "--mem-fraction-static 0.70")
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


if __name__ == "__main__":
    unittest.main()
