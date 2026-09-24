#!/usr/bin/env python3
"""oc-merge-limits.py --keep-lower leaves a fitted pair under the bounds it is given.

On a 1m box the limits table gives bounds and the fit to the engine's pool sets the pair.
Step 7 merged the bounds over the fitted pair, and the fit at the end merged it back: two
writes and two backups per run (see test_install_restarts.py).
"""
import json
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TOOL = REPO / "oc-merge-limits.py"


def cfg(ctx, out):
    d = pathlib.Path(tempfile.mkdtemp(prefix="keep-lower-"))
    p = d / "opencode.json"
    p.write_text(json.dumps({"provider": {"qwen38": {"models": {"qwen3.8-27b": {
        "limit": {"context": ctx, "input": ctx, "output": out}}}}}}, indent=2) + "\n")
    return p


def merge(p, ctx, out, *extra):
    r = subprocess.run(["python3", str(TOOL), str(p), "qwen38", "qwen3.8-27b", str(ctx), str(out), *extra],
                       capture_output=True, text=True, timeout=30)
    lim = json.loads(p.read_text())["provider"]["qwen38"]["models"]["qwen3.8-27b"]["limit"]
    backups = list(p.parent.glob("opencode.json.bak-*"))
    return r.returncode, (lim["context"], lim["output"]), backups


class KeepLower(unittest.TestCase):
    def test_a_fitted_pair_under_the_bounds_is_kept_without_a_backup(self):
        rc, pair, backups = merge(cfg(559000, 186000), 700000, 200000, "--keep-lower")
        self.assertEqual((rc, pair, backups), (0, (559000, 186000), []))

    def test_a_pair_above_the_bounds_is_brought_down(self):
        rc, pair, backups = merge(cfg(559000, 186000), 173000, 64000, "--keep-lower")
        self.assertEqual((rc, pair), (0, (173000, 64000)))
        self.assertEqual(len(backups), 1)

    def test_without_the_flag_the_bounds_are_written_as_before(self):
        rc, pair, _ = merge(cfg(559000, 186000), 700000, 200000)
        self.assertEqual((rc, pair), (0, (700000, 200000)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
