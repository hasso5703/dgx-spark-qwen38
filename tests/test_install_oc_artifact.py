#!/usr/bin/env python3
"""The opencode config install.sh generates gives each lane the limits it serves.

oc and the Agent tab load this file over the user's own (OPENCODE_CONFIG), so its
numbers are the ones they run on. A flash install runs in native mode and used that mode
for the 27B block too: on a box whose 27B serves 1M, the block came out at the native
194048/64000 (reference box, 2026-09-23). These run the installer's own lines, as
written, in a throwaway config dir."""
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


def generate(lane, mode, unit_ctx=None, flash_unit=False):
    t = pathlib.Path(tempfile.mkdtemp(prefix="oc-art-"))
    sgl = t / "qwen38-sglang.service"
    if unit_ctx:
        sgl.write_text(f"ExecStart=... --context-length {unit_ctx} ...\n")
    flash = t / "qwen38-flash.service"
    if flash_unit:
        flash.write_text("x\n")
    ctx, out = ("225000", "32000") if lane == "flash" else ("700000", "200000")
    script = ("set -euo pipefail\ndie(){ echo \"DIE: $*\"; exit 1; }\n"
              f'LANE={lane}; CONTEXT_MODE={mode}; SGL_UNIT_PATH="{sgl}"; FLASH_UNIT_PATH="{flash}"\n'
              f'REPO_DIR="{REPO}"; CONFIG_DIR="{t}"; OC_PORT=30001; OC_CTX={ctx}; OC_OUT={out}\n'
              'OC_LABEL=local; OPENCODE_PIN=1\n' + block())
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(t)})
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads((t / "opencode.json").read_text())
    return {p: b["models"][next(iter(b["models"]))]["limit"] for p, b in doc["provider"].items()}


class EachLaneKeepsItsOwnLimits(unittest.TestCase):
    def test_a_flash_install_keeps_a_1m_27b_at_its_1m_limits(self):
        lim = generate("flash", "native", unit_ctx=1010000)
        self.assertEqual(lim["qwen38"], {"context": 700000, "input": 700000, "output": 200000})
        self.assertEqual(lim["flashnext"]["context"], 225000)

    def test_a_flash_install_keeps_a_native_27b_native(self):
        lim = generate("flash", "native", unit_ctx=262144)
        self.assertEqual(lim["qwen38"], {"context": 194048, "input": 194048, "output": 64000})

    def test_a_flash_only_box_lists_no_27b(self):
        self.assertNotIn("qwen38", generate("flash", "native"))

    def test_a_27b_install_uses_its_own_numbers(self):
        lim = generate("27b", "1m", unit_ctx=1010000, flash_unit=True)
        self.assertEqual(lim["qwen38"]["context"], 700000)
        self.assertIn("flashnext", lim)


if __name__ == "__main__":
    unittest.main(verbosity=2)
