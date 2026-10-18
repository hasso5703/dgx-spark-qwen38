#!/usr/bin/env python3
"""The 27B lane does not offer to run a checkpoint's own code (2026-10-18).

Its two units and run.sh passed --trust-remote-code, which lets the engine import Python a
checkpoint names in its config (auto_map), as root in its container. None of what the lane
loads has any: the four targets (RadixArk/Qwen3.8-27B-NVFP4, Qwen/Qwen3.8-27B-FP8 and the two
abliterated builds) and the DFlash2 drafter it serves name no auto_map at their pinned revisions
and ship no .py file; their architectures are SGLang's own (Qwen3_5ForConditionalGeneration,
DFlash2DraftModel). Read on the Hub in a cloud review; the lane was not started without the
flag there. So the flag ran nothing, and would have run whatever a later pin or a target
picked by hand brought. The one checkpoint of this lane with code, the DSpark drafter
(dspark.py, dflash.py), has not been served since v1.2.

The Nemotron lanes keep theirs: their checkpoints need it, and their units and docs say so.
"""
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
LANE = ("qwen38-sglang.service.template", "qwen38-sglang-1m.service.template", "run.sh")


def code(name):
    """The file without its comment lines."""
    return "\n".join(ln for ln in (REPO / name).read_text().splitlines() if not ln.lstrip().startswith("#"))


class The27BRunsNoCheckpointCode(unittest.TestCase):
    def test_its_units_and_run_sh_do_not_pass_the_flag(self):
        for name in LANE:
            self.assertFalse("--trust-remote-code" in code(name).split(), f"{name} passes --trust-remote-code")

    def test_the_lanes_that_need_it_keep_it(self):
        for name in ("qwen38-nemotron.service.template", "qwen38-nemotron-super.service.template"):
            self.assertIn("--trust-remote-code", code(name).split(), name)


if __name__ == "__main__":
    unittest.main()
