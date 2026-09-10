"""The measurement and build tools, which had no tests at all.

coverage.py on 2026-09-10 read 0% for build-token-map.py, conc-check.py and
bench-agent.py. They are not the serving path, which is why they were skipped,
and they are exactly the wrong things to leave untested: one of them decides
what the engine drafts with, and the other two produce the numbers this repo
makes decisions from. A benchmark that lies is worse than no benchmark, and this
repo has the scar to prove it (a renamed streaming field once reported 97 tok/s
on a box whose real rate was 19.9).

Only the pure logic is exercised: nothing here reaches an engine, a tokenizer or
the network.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[1]


def load(name, stub_env=None):
    """Import a hyphenated top-level script as a module."""
    for k, v in (stub_env or {}).items():
        os.environ.setdefault(k, v)
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), REPO / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ConstructionRank(unittest.TestCase):
    """build-token-map ranks the vocabulary tail by the tokenizer's own merge
    order, because a BPE merge table IS a frequency ranking. Get this wrong and
    the draft head is sliced to the wrong 65,536 rows, which costs speed
    silently: the target still verifies, so nothing looks broken."""

    @classmethod
    def setUpClass(cls):
        cls.btm = load("build-token-map.py")

    def _snapshot(self, merges_lines):
        d = Path(tempfile.mkdtemp(prefix="btm-"))
        if merges_lines is not None:
            (d / "merges.txt").write_text("\n".join(merges_lines) + "\n")
        return str(d)

    def test_a_base_token_ranks_zero_and_a_merge_ranks_its_order(self):
        snap = self._snapshot(["#version: 0.2", "a b", "ab c", "x y"])
        vocab = {"a": 0, "b": 1, "ab": 2, "abc": 3, "xy": 4, "z": 5}
        rank = self.btm.construction_rank(snap, vocab, 6)
        self.assertEqual(rank[0], 0, "a base token must rank 0")
        self.assertEqual(rank[1], 0)
        self.assertEqual(rank[2], 1, "the first merge must rank 1")
        self.assertEqual(rank[3], 2, "the second merge must rank 2")
        self.assertEqual(rank[4], 3)
        self.assertEqual(rank[5], 0, "a token no merge produces is a base token")

    def test_every_id_in_the_vocabulary_gets_a_rank(self):
        snap = self._snapshot(["a b"])
        rank = self.btm.construction_rank(snap, {"a": 0, "b": 1, "ab": 2}, 200)
        self.assertEqual(len(rank), 200)
        self.assertTrue(all(isinstance(v, int) and v >= 0 for v in rank.values()))

    def test_the_version_header_is_not_a_merge(self):
        snap = self._snapshot(["#version: 0.2", "a b"])
        rank = self.btm.construction_rank(snap, {"a": 0, "b": 1, "ab": 2}, 3)
        self.assertEqual(rank[2], 1, "the header was counted as a merge")

    def test_a_malformed_merge_line_is_skipped_not_counted(self):
        """A three-token or empty line must not shift every later rank by one."""
        snap = self._snapshot(["a b", "", "one two three", "ab c"])
        rank = self.btm.construction_rank(snap, {"a": 0, "b": 1, "ab": 2, "abc": 3}, 4)
        self.assertEqual(rank[2], 1)
        self.assertEqual(rank[3], 2, "a skipped line still advanced the index")

    def test_the_first_merge_producing_a_token_is_the_one_that_counts(self):
        snap = self._snapshot(["a b", "ab c", "a bc"])
        rank = self.btm.construction_rank(snap, {"abc": 9}, 10)
        self.assertEqual(rank[9], 2, "a later merge overwrote an earlier rank")

    def test_a_merge_of_a_token_outside_the_vocabulary_is_ignored(self):
        snap = self._snapshot(["q r"])
        rank = self.btm.construction_rank(snap, {"a": 0}, 1)
        self.assertEqual(rank, {0: 0})

    def test_no_merges_file_falls_back_to_id_order(self):
        snap = self._snapshot(None)
        rank = self.btm.construction_rank(snap, {"a": 0}, 5)
        self.assertEqual(rank, {i: i for i in range(5)})


class CorpusReader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.btm = load("build-token-map.py")

    def _write(self, name, text):
        d = Path(tempfile.mkdtemp(prefix="corpus-"))
        p = d / name
        p.write_text(text)
        return str(p)

    def test_a_jsonl_corpus_yields_its_text_fields(self):
        p = self._write("c.jsonl", json.dumps({"text": "one"}) + "\n"
                        + json.dumps({"text": "two"}) + "\n")
        self.assertEqual(list(self.btm.iter_texts(p)), ["one", "two"])

    def test_a_broken_jsonl_line_is_skipped_not_fatal(self):
        p = self._write("c.jsonl", json.dumps({"text": "one"}) + "\n{not json\n"
                        + json.dumps({"text": "two"}) + "\n")
        self.assertEqual(list(self.btm.iter_texts(p)), ["one", "two"])

    def test_a_jsonl_line_without_a_text_field_yields_empty(self):
        p = self._write("c.jsonl", json.dumps({"other": "x"}) + "\n")
        self.assertEqual(list(self.btm.iter_texts(p)), [""])

    def test_a_repeat_weight_repeats_the_corpus(self):
        p = self._write("c.jsonl", json.dumps({"text": "one"}) + "\n")
        self.assertEqual(list(self.btm.iter_texts(p + ":3")), ["one"] * 3)

    def test_a_plain_text_corpus_is_read_in_chunks(self):
        p = self._write("c.txt", "x" * (self.btm.CHUNK * 2 + 5))
        chunks = list(self.btm.iter_texts(p))
        self.assertEqual(sum(len(c) for c in chunks), self.btm.CHUNK * 2 + 5)
        self.assertGreaterEqual(len(chunks), 3)

    def test_a_colon_in_a_filename_is_not_a_repeat_weight(self):
        """Only a trailing :<digits> is a weight; a path with a colon in it is a
        path, and reading it as a weight would silently drop the corpus."""
        p = self._write("odd:name.txt", "hello")
        self.assertEqual(list(self.btm.iter_texts(p)), ["hello"])


class Grading(unittest.TestCase):
    """conc-check decides whether a concurrent answer was correct, verbose,
    wrong, or CONTAMINATED by another request. That last verdict is the one that
    accuses the engine (sglang#36548), so a false positive there would send this
    repo chasing an upstream bug it does not have."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("SGLANG_API_KEY", "test")
        cls.cc = load("conc-check.py")
        cls.tasks = cls.cc.TASKS
        cls.name, _, cls.want = cls.tasks[0]
        cls.other_name, _, cls.other_want = cls.tasks[1]

    def test_the_exact_answer_is_exact(self):
        self.assertEqual(self.cc.grade(self.want, self.want, self.name), "exact")

    def test_case_punctuation_and_spacing_do_not_matter(self):
        noisy = f"  {self.want.upper()},  "
        self.assertEqual(self.cc.grade(noisy, self.want, self.name), "exact")

    def test_a_right_answer_inside_a_rambling_one_is_verbose(self):
        self.assertEqual(
            self.cc.grade(f"Well, let me think. The answer is {self.want}.",
                          self.want, self.name), "verbose")

    def test_a_wrong_answer_is_wrong(self):
        self.assertEqual(self.cc.grade("something else entirely",
                                       self.want, self.name), "wrong")

    def test_another_task_s_answer_is_contamination_named_by_its_task(self):
        verdict = self.cc.grade(self.other_want, self.want, self.name)
        self.assertTrue(verdict.startswith("contaminated:"), verdict)
        self.assertEqual(verdict.split(":", 1)[1], self.other_name)

    def test_a_task_is_never_contaminated_by_itself(self):
        for name, _, want in self.tasks:
            self.assertEqual(self.cc.grade(want, want, name), "exact", name)

    def test_an_empty_answer_is_wrong_not_contaminated(self):
        self.assertEqual(self.cc.grade("", self.want, self.name), "wrong")

    def test_norm_is_idempotent(self):
        for s in ("A, b.", "  x  ", "", "Multi,  Word.  Answer.."):
            once = self.cc.norm(s)
            self.assertEqual(self.cc.norm(once), once, repr(s))

    def test_filler_is_deterministic_for_a_seed(self):
        """The filler exists to give each request its own context. Two runs of
        the same seed must produce the same probe, or a rerun is not a rerun."""
        import random
        a = self.cc.filler(random.Random(7), 200)
        b = self.cc.filler(random.Random(7), 200)
        self.assertEqual(a, b)
        self.assertNotEqual(a, self.cc.filler(random.Random(8), 200))

    def test_filler_length_tracks_the_token_budget(self):
        import random
        short = self.cc.filler(random.Random(1), 100)
        long = self.cc.filler(random.Random(1), 1000)
        self.assertLess(len(short), len(long))
        self.assertEqual(len(long.split()), 750)


class BenchAgentTiming(unittest.TestCase):
    """bench-agent measures an agent loop. Its only job is to not lie."""

    @classmethod
    def setUpClass(cls):
        cls.ba = load("bench-agent.py")

    def test_the_api_key_is_read_from_the_file_not_the_environment(self):
        d = Path(tempfile.mkdtemp(prefix="ba-"))
        (d / "api-key").write_text("from-the-file\n")
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(d)
        try:
            # api_key() resolves $HOME at call time in this tool
            got = self.ba.api_key()
        finally:
            if old is not None:
                os.environ["HOME"] = old
        self.assertIsInstance(got, str)

    def test_die_exits_with_the_code_it_is_given(self):
        with self.assertRaises(SystemExit) as cm:
            self.ba.die("nope", 3)
        self.assertEqual(cm.exception.code, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
