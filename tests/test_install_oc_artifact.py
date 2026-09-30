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
    start = text.index("OC_27B=0; OC_FLASH=0\n")
    end = text.index("\nPYEOF\n", start) + len("\nPYEOF\n")
    return text[start:end]


PINS = "\n".join(line for line in INSTALL.read_text().splitlines()
                 if line.startswith(("STOCK_REPO=", "UNC_REPO=", "FP8_REPO=", "UNCFP8_REPO=",
                                     "FLASH_REPO=", "FLASH_UNC_REPO=", "FLASH_NVDA_REPO=")))


def generate(lane, mode, unit_ctx=None, flash_unit=False, model="RadixArk/Qwen3.8-27B-NVFP4",
             pair=None, names=False):
    t = pathlib.Path(tempfile.mkdtemp(prefix="oc-art-"))
    sgl = t / "qwen38-sglang.service"
    if unit_ctx:
        sgl.write_text(f"ExecStart=... --model-path {model} --context-length {unit_ctx} ...\n")
    flash = t / "qwen38-flash.service"
    if flash_unit:
        flash.write_text("x\n")
    ctx, out = pair or (("205000", "32000") if lane == "flash" else ("700000", "200000"))
    script = ("set -euo pipefail\ndie(){ echo \"DIE: $*\"; exit 1; }\n" + PINS + "\n"
              f'LANE={lane}; CONTEXT_MODE={mode}; SGL_UNIT_PATH="{sgl}"; FLASH_UNIT_PATH="{flash}"\n'
              f'MODEL_CHOICE={"flash" if lane == "flash" else "stock"}\n'
              f'REPO_DIR="{REPO}"; CONFIG_DIR="{t}"; OC_PORT=30001; OC_CTX={ctx}; OC_OUT={out}\n'
              'OC_LABEL=local; OPENCODE_PIN=1\n' + block())
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(t)})
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads((t / "opencode.json").read_text())
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

if __name__ == "__main__":
    unittest.main(verbosity=2)
