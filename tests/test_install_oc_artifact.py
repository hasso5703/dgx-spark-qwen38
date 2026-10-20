#!/usr/bin/env python3
"""The opencode config install.sh generates gives each lane the limits it serves.

oc and the Agent tab load this file over the user's own (OPENCODE_CONFIG), so its
numbers are the ones they run on. A flash install runs in native mode and used that mode
for the 27B block too: on a box whose 27B serves 1M, the block came out at the native
pair (reference box, 2026-09-23). The 27B block now comes from oc-limits.sh, for the
checkpoint and mode its unit serves: a copy of the numbers here had drifted from the
table and gave an FP8 box the NVFP4 1M pair (found in review, 2026-09-24). These run the
installer's own lines, as written, in a throwaway config dir.""" 
import json
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
INSTALL = REPO / "install.sh"


def block() -> str:
    text = INSTALL.read_text()
    start = text.index("OC_27B=0; OC_FLASH=0; OC_KOLIBRI=0; OC_NEMOTRON=0; OC_NEMOTRON_SUPER=0\n")
    end = text.index("\nPYEOF\n", start) + len("\nPYEOF\n")
    return text[start:end]


PINS = "\n".join(line for line in INSTALL.read_text().splitlines()
                 if line.startswith(("STOCK_REPO=", "UNC_REPO=", "FP8_REPO=", "UNCFP8_REPO=",
                                     "FLASH_REPO=", "FLASH_UNC_REPO=", "FLASH_NVDA_REPO=",
                                     "FLASH_LOCAL_REPO=")))


def generate(lane, mode, unit_ctx=None, flash_unit=False, model="RadixArk/Qwen3.8-27B-NVFP4",
             pair=None, names=False, flash_running=None, kolibri_unit=False, kolibri_boot=False, doc=False,
             nemotron_unit=False, nemotron_boot=False, super_unit=False, super_boot=False):
    t = pathlib.Path(tempfile.mkdtemp(prefix="oc-art-"))
    kolibri = t / "qwen38-kolibri.service"
    if kolibri_unit:
        kolibri.write_text("ExecStart=... Aleph-Alpha/Kolibri-1 --revision 258cae8f ...\n")
    nemotron = t / "qwen38-nemotron.service"
    if nemotron_unit:
        nemotron.write_text("ExecStart=... nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB --revision c6cc2e8f ...\n")
    nsuper = t / "qwen38-nemotron-super.service"
    if super_unit:
        nsuper.write_text("ExecStart=... nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4 --revision 8a115b9d ...\n")
    if flash_running:
        (t / "launch-flash.sh").write_text(
            "docker run img python3 -m sglang.launch_server --model-path RadixArk/Qwen3.8-Flash-Next-NVFP4 "
            f"--max-running-requests {flash_running} --context-length 262144\n")
    sgl = t / "qwen38-sglang.service"
    if unit_ctx:
        sgl.write_text(f"ExecStart=... --model-path {model} --context-length {unit_ctx} ...\n")
    flash = t / "qwen38-flash.service"
    if flash_unit:
        flash.write_text("x\n")
    ctx, out = pair or (("205000", "32000") if lane == "flash" else ("700000", "200000"))
    script = ("set -euo pipefail\ndie(){ echo \"DIE: $*\"; exit 1; }\n" + PINS + "\n"
              f'LANE={lane}; CONTEXT_MODE={mode}; SGL_UNIT_PATH="{sgl}"; FLASH_UNIT_PATH="{flash}"\n'
              f'KOLIBRI_UNIT_PATH="{kolibri}"; KOLIBRI_BOOT={1 if kolibri_boot else 0}\n'
              f'NEMOTRON_UNIT_PATH="{nemotron}"; NEMOTRON_BOOT={1 if nemotron_boot else 0}\n'
              f'NEMOTRON_SUPER_UNIT_PATH="{nsuper}"; NEMOTRON_SUPER_BOOT={1 if super_boot else 0}\n'
              f'MODEL_CHOICE={"flash" if lane == "flash" else "stock"}\n'
              f'REPO_DIR="{REPO}"; CONFIG_DIR="{t}"; OC_PORT=30001; OC_CTX={ctx}; OC_OUT={out}\n'
              'OC_LABEL=local; OPENCODE_PIN=1\n' + block())
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(t)})
    assert r.returncode == 0, r.stdout + r.stderr
    doc_ = json.loads((t / "opencode.json").read_text())
    if doc:
        return doc_
    doc = doc_
    if names:
        return {p: b["models"][next(iter(b["models"]))]["name"] for p, b in doc["provider"].items()}
    return {p: b["models"][next(iter(b["models"]))]["limit"] for p, b in doc["provider"].items()}


class EachLaneKeepsItsOwnLimits(unittest.TestCase):
    def test_a_flash_install_keeps_a_1m_27b_at_its_1m_limits(self):
        lim = generate("flash", "native", unit_ctx=1010000)
        self.assertEqual(lim["qwen38"], {"context": 700000, "input": 700000, "output": 200000})
        self.assertEqual(lim["flashnext"]["context"], 205000)

    def test_a_flash_install_keeps_a_native_27b_native(self):
        lim = generate("flash", "native", unit_ctx=262144)
        self.assertEqual(lim["qwen38"], {"context": 173000, "input": 173000, "output": 64000})

    def test_a_flash_install_keeps_an_fp8_27b_at_the_fp8_pair(self):
        lim = generate("flash", "native", unit_ctx=1010000, model="Qwen/Qwen3.8-27B-FP8")
        self.assertEqual(lim["qwen38"], {"context": 480000, "input": 480000, "output": 160000})

    def test_the_27b_block_is_read_from_the_table(self):
        # the numbers themselves live in oc-limits.sh, once
        self.assertNotIn("(194048, 64000", INSTALL.read_text())
        self.assertNotIn("(700000, 200000", INSTALL.read_text())

    def test_the_flash_block_beside_a_27b_is_read_from_the_table_too(self):
        """A copy of one pair here, 110000/32000 (the throughput tier's), gave the flash its
        205,000 window cut to 110,000 at every run that served the 27B (2026-10-06)."""
        self.assertFalse("(110000, 32000)" in INSTALL.read_text(), "a flash pair is copied in install.sh")
        lim = generate("27b", "1m", unit_ctx=1010000, flash_unit=True, flash_running=8)
        self.assertEqual(lim["flashnext"], {"context": 205000, "input": 205000, "output": 32000})

    def test_the_flash_block_follows_the_tier_its_launcher_runs(self):
        lim = generate("27b", "1m", unit_ctx=1010000, flash_unit=True, flash_running=24)
        self.assertEqual(lim["flashnext"], {"context": 110000, "input": 110000, "output": 32000})

    def test_a_flash_only_box_lists_no_27b(self):
        self.assertNotIn("qwen38", generate("flash", "native"))

    def test_a_27b_install_uses_its_own_numbers(self):
        # This run's target, not the unit on disk, which step 8 rewrites after this block:
        # an update from the NVFP4 checkpoint to FP8 still finds the NVFP4 unit here. The
        # pair must differ from that unit's own for the test to see which one was used
        # (700000/200000 here was both, found in review, 2026-09-24).
        lim = generate("27b", "1m", unit_ctx=1010000, flash_unit=True, pair=("480000", "160000"))
        self.assertEqual(lim["qwen38"], {"context": 480000, "input": 480000, "output": 160000})
        self.assertIn("flashnext", lim)



class TheLaneNotInstalledKeepsItsCheckpointsName(unittest.TestCase):
    """A flash install named the 27B entry generically, whatever its unit serves; the next
    switch back to the 27B renamed it, so the first update after every switch rewrote the
    file and restarted opencode-web for a name (the reference box, 2026-09-30)."""

    def test_an_abliterated_27b_keeps_its_name_under_a_flash_install(self):
        names = generate("flash", "native", unit_ctx=1010000,
                         model="edp1096/Huihui-RadixArk-Qwen3.8-27B-abliterated-NVFP4", names=True)
        self.assertEqual(names["qwen38"], "Qwen3.8-27B NVFP4 abliterated + DFlash2 (local, 1M)")

    def test_an_fp8_27b_is_named_fp8_and_a_native_one_says_its_window(self):
        names = generate("flash", "native", unit_ctx=262144, model="Qwen/Qwen3.8-27B-FP8", names=True)
        self.assertEqual(names["qwen38"], "Qwen3.8-27B FP8 official + DFlash2 (local, 262K)")


class ABoxBootingKolibri(unittest.TestCase):
    """Kolibri serving at boot: opencode's default is Kolibri, and the compaction block, global,
    is sized from its window (46,000 kept of its 185,000 threshold), not from the 27B's (132,000
    on a 1M 27B, and opencode compacted again every step or two: found in review, 2026-10-14)."""

    def test_the_default_and_the_compaction_are_kolibris(self):
        d = generate("27b", "1m", unit_ctx=1010000, flash_unit=True, pair=("551000", "160000"),
                     kolibri_unit=True, kolibri_boot=True, doc=True)
        self.assertEqual(d["model"], "kolibri/kolibri-1")
        self.assertIn("kolibri", d["provider"])
        self.assertEqual(d["compaction"]["preserve_recent_tokens"], 46000)

    def test_kolibris_pair_compacts_late_and_still_fits_its_window(self):
        # 173,000 + 64,000 compacted at 153,000 tokens, its 64,000 of answer unused (the longest
        # reply of a session on the reference box was 1,742 tokens): 205,000 + 32,000, the same
        # rule, compacts at 185,000 (Hasan, 2026-10-14)
        for target in ("kolibri", "kolibri-nvfp4"):
            ctx, out, *_ = subprocess.run([str(REPO / "oc-limits.sh"), target], capture_output=True,
                                          text=True, check=True).stdout.split()
            ctx, out = int(ctx), int(out)
            self.assertEqual((ctx, out), (205000, 32000), target)
            self.assertLessEqual(ctx - 20000 + 43863 + out, 262144, "threshold + worst step + answer fit the window")
            self.assertGreaterEqual(ctx - 20000, 185000)

    def test_with_kolibri_installed_but_not_at_boot_the_27b_keeps_its_own(self):
        d = generate("27b", "1m", unit_ctx=1010000, pair=("551000", "160000"), kolibri_unit=True, doc=True)
        self.assertEqual(d["model"], "qwen38/qwen3.8-27b")
        self.assertIn("kolibri", d["provider"], "the installed lane is offered")
        self.assertEqual(d["compaction"]["preserve_recent_tokens"], 132000)

    def test_without_its_unit_there_is_no_kolibri_provider(self):
        d = generate("27b", "1m", unit_ctx=1010000, pair=("551000", "160000"), doc=True)
        self.assertNotIn("kolibri", d["provider"])

class ABoxBootingNemotron(unittest.TestCase):
    """The Nemotron lane, the same way: serving at boot it is opencode's default, its compaction
    sized from its window; installed beside another lane it is offered; with no unit, absent."""

    def test_the_default_the_compaction_and_the_provider_are_nemotrons(self):
        d = generate("27b", "1m", unit_ctx=1010000, flash_unit=True, pair=("551000", "160000"),
                     nemotron_unit=True, nemotron_boot=True, doc=True)
        self.assertEqual(d["model"], "nemotron/nemotron-3.5-bolt")
        self.assertEqual(d["compaction"]["preserve_recent_tokens"], 46000)
        m = d["provider"]["nemotron"]["models"]["nemotron-3.5-bolt"]
        self.assertEqual(m["limit"], {"context": 205000, "input": 205000, "output": 32000})
        self.assertEqual(m["name"], "Nemotron 3.5 Bolt (local, 262K)")
        # it reads images, and its levels are its template's own words
        self.assertTrue(m["attachment"])
        self.assertEqual(m["modalities"]["input"], ["text", "image"])
        self.assertEqual(m["variants"]["none"], {"chat_template_kwargs": {"enable_thinking": False}})
        self.assertEqual(m["variants"]["medium"], {"chat_template_kwargs": {"enable_thinking": True, "medium_effort": True}})
        self.assertEqual(m["variants"]["high"], {"chat_template_kwargs": {"enable_thinking": True}})
        self.assertEqual(d["provider"]["nemotron"]["options"]["baseURL"], "http://127.0.0.1:30001/v1")

    def test_its_pair_compacts_late_and_still_fits_its_window(self):
        ctx, out, *_ = subprocess.run([str(REPO / "oc-limits.sh"), "nemotron"], capture_output=True,
                                      text=True, check=True).stdout.split()
        ctx, out = int(ctx), int(out)
        self.assertEqual((ctx, out), (205000, 32000))
        self.assertLessEqual(ctx - 20000 + 43863 + out, 262144, "threshold + worst step + answer fit the window")

    def test_installed_but_not_at_boot_it_is_offered_and_the_27b_keeps_its_own(self):
        d = generate("27b", "1m", unit_ctx=1010000, pair=("551000", "160000"), nemotron_unit=True, doc=True)
        self.assertEqual(d["model"], "qwen38/qwen3.8-27b")
        self.assertIn("nemotron", d["provider"])
        self.assertEqual(d["compaction"]["preserve_recent_tokens"], 132000)

    def test_without_its_unit_there_is_no_nemotron_provider(self):
        d = generate("27b", "1m", unit_ctx=1010000, pair=("551000", "160000"), kolibri_unit=True, doc=True)
        self.assertNotIn("nemotron", d["provider"])


class ABoxBootingNemotronSuper(unittest.TestCase):
    """The Nemotron Super lane, its own provider (its own model and window): serving at boot it is
    opencode's default, its compaction sized from its window; installed beside another lane it is
    offered, Bolt's provider beside it; with no unit, absent."""

    def test_the_default_the_compaction_and_the_provider_are_super_vls(self):
        d = generate("27b", "1m", unit_ctx=1010000, flash_unit=True, pair=("551000", "160000"),
                     nemotron_unit=True, super_unit=True, super_boot=True, doc=True)
        self.assertEqual(d["model"], "nemotron-super/nemotron-3.5-super-vl")
        keep = int(subprocess.run([str(REPO / "oc-limits.sh"), "--preserve", "990000"], capture_output=True,
                                  text=True, check=True).stdout)
        self.assertEqual(d["compaction"]["preserve_recent_tokens"], keep)
        m = d["provider"]["nemotron-super"]["models"]["nemotron-3.5-super-vl"]
        self.assertEqual(m["limit"], {"context": 990000, "input": 990000, "output": 32000})
        self.assertEqual(m["name"], "Nemotron 3.5 Super VL (local, 1M)")
        self.assertTrue(m["attachment"])
        self.assertEqual(m["modalities"]["input"], ["text", "image"])
        self.assertEqual(m["variants"]["none"], {"chat_template_kwargs": {"enable_thinking": False}})
        self.assertEqual(m["variants"]["low"], {"chat_template_kwargs": {"enable_thinking": True, "medium_effort": True}})
        self.assertEqual(m["variants"]["high"], {"chat_template_kwargs": {"enable_thinking": True}})
        self.assertEqual(d["provider"]["nemotron-super"]["name"], "Nemotron 3.5 Super VL (DGX Spark)")
        # Bolt's provider beside it, its own model and window
        self.assertIn("nemotron-3.5-bolt", d["provider"]["nemotron"]["models"])

    def test_its_pair_compacts_late_and_still_fits_its_window(self):
        ctx, out, *_ = subprocess.run([str(REPO / "oc-limits.sh"), "nemotron-super"], capture_output=True,
                                      text=True, check=True).stdout.split()
        ctx, out = int(ctx), int(out)
        self.assertEqual((ctx, out), (990000, 32000))
        self.assertLessEqual(ctx - 20000 + 43863 + out, 1048576, "threshold + worst step + answer fit the window")

    def test_installed_but_not_at_boot_it_is_offered_and_the_27b_keeps_its_own(self):
        d = generate("27b", "1m", unit_ctx=1010000, pair=("551000", "160000"), super_unit=True, doc=True)
        self.assertEqual(d["model"], "qwen38/qwen3.8-27b")
        self.assertIn("nemotron-super", d["provider"])
        self.assertNotIn("nemotron", d["provider"])
        self.assertEqual(d["compaction"]["preserve_recent_tokens"], 132000)

    def test_without_its_unit_there_is_no_super_provider(self):
        d = generate("27b", "1m", unit_ctx=1010000, pair=("551000", "160000"), nemotron_unit=True, doc=True)
        self.assertNotIn("nemotron-super", d["provider"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
