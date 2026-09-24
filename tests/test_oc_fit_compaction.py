#!/usr/bin/env python3
"""oc-fit-limits.py fits the compaction block with the context, and says when it fitted nothing.

The compaction block's preserve_recent_tokens is a quarter of the context's threshold, and
install.sh sizes it from the table's context: a fit that set a smaller context left the
block at the table's, up to 53% of the new threshold (found in review, 2026-09-24). And
with no config holding the lane's entry, the fit said opencode "already asks for no more
than this engine can serve", having compared nothing. HOME is a throwaway here, and every
merge is recorded instead of run."""
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess as sp
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("oc_fit_compaction", REPO / "oc-fit-limits.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Base(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp(prefix="oc-fit-home-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.saved = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", self.saved) if self.saved else None)
        self.cfg = self.home / ".config" / "qwen38"
        self.cfg.mkdir(parents=True)
        self.m = load()
        self.m.CONFIG_DIR = self.cfg
        self.m.engine_info = lambda base: {"max_total_num_tokens": 600_000, "context_length": 1_010_000,
                                           "served_model_name": "qwen3.8-27b", "model_path": "x"}
        self.calls = []
        real = self.m.subprocess.run
        # self.m.subprocess is the process's own subprocess module: put run() back, or every
        # test after this one gets these fakes instead of real processes
        self.addCleanup(setattr, self.m.subprocess, "run", real)

        def fake(argv, **kw):
            joined = " ".join(map(str, argv))
            if "systemctl show" in joined:
                return sp.CompletedProcess(argv, 0, stdout="Environment=\n", stderr="")
            if "oc-merge-limits.py" in joined:
                self.calls.append(list(map(str, argv)))
                rc, said = self.merge_answer(argv)
                return sp.CompletedProcess(argv, rc, stdout=said, stderr="")
            return real(argv, **kw)           # oc-limits.sh only: read-only
        self.m.subprocess.run = fake

    def merge_answer(self, argv):
        return 0, "written"


class TheCompactionFollowsTheFit(Base):
    def test_the_block_is_merged_with_a_quarter_of_the_new_threshold(self):
        (self.cfg / "opencode.json").write_text("{}")
        self.assertEqual(self.m.main([]), 0)
        limits = [c for c in self.calls if "--compaction" not in c]
        compaction = [c for c in self.calls if "--compaction" in c]
        self.assertEqual(len(limits), 1)
        context = int(limits[0][-2])
        self.assertEqual([c[2:] for c in compaction],
                         [[str(self.cfg / "opencode.json"), "--compaction", str((context - 20000) // 4 // 1000 * 1000)]])


class NothingFittedIsNotAFit(Base):
    def merge_answer(self, argv):
        return 3, "qwen38/qwen3.8-27b not in x: nothing to merge"

    def test_no_entry_anywhere(self):
        (self.cfg / "opencode.json").write_text(json.dumps({"provider": {}}))
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(self.m.main([]), 0)
        out = buf.getvalue()
        self.assertIn("no opencode config here has an entry for qwen38/qwen3.8-27b", out)
        self.assertNotIn("already asks", out)
        self.assertFalse([c for c in self.calls if "--compaction" in c], "a compaction merged with no entry")


if __name__ == "__main__":
    unittest.main(verbosity=2)
