#!/usr/bin/env python3
"""The two eval drivers must keep the contracts that make them work at all.

`evals/mmlu.py` and `evals/humaneval.py` exist because the image's own
invocations do not run: `run_eval --eval-name mmlu` shells out to an `sgl-eval`
binary the image does not ship, and its HumanEval path needs a `human_eval`
package that is not there and then dies in `os.fork`, because the image's
filelock refuses one. Both failures print a traceback and an empty score, which
reads exactly like a model that scored nothing, and that is how two runs were
lost on 2026-09-18.

So three things have to stay true, and none of them is visible from reading a
score: the drivers print a line a caller can grep, HumanEval never forks and
asks for one sample per task (at temperature 0 the harness default of five is
the same answer five times), and MMLU pulls the dataset the published numbers
were measured against. A stub `sglang` on the path lets all three be checked
here, with no image, no network and no engine.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
EVALS = REPO / "evals"

STUB_COMMON = '''
import json, os


class ChatCompletionSampler:
    def __init__(self, **kw):
        self.kw = kw
        with open(os.environ["STUB_SAMPLER_LOG"], "w") as f:
            json.dump(kw, f, default=str)
'''

STUB_MMLU = '''
import json, os


class _Result:
    score = 0.5
    metrics = {"stem": 0.5}


class MMLUEval:
    def __init__(self, filename, num_examples, num_threads):
        with open(os.environ["STUB_LOG"], "w") as f:
            json.dump({"filename": filename, "num_examples": num_examples,
                       "num_threads": num_threads}, f)

    def __call__(self, sampler):
        return _Result()
'''

STUB_HUMANEVAL = '''
import json, multiprocessing, os


class _Result:
    score = 0.75
    metrics = {"pass@1": 0.75}


class HumanEval:
    def __init__(self, num_examples, num_threads, num_samples_per_task=5, ks_passes=(1, 2, 5)):
        with open(os.environ["STUB_LOG"], "w") as f:
            json.dump({"num_examples": num_examples, "num_threads": num_threads,
                       "samples_per_task": num_samples_per_task,
                       "ks": list(ks_passes),
                       "start_method": multiprocessing.get_start_method(allow_none=True)}, f)

    def __call__(self, sampler):
        return _Result()
'''


class EvalDrivers(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="eval-drivers-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        pkg = self.tmp / "sglang" / "test"
        pkg.mkdir(parents=True)
        (self.tmp / "sglang" / "__init__.py").write_text("")
        (pkg / "__init__.py").write_text("")
        (pkg / "simple_eval_common.py").write_text(STUB_COMMON)
        (pkg / "simple_eval_mmlu.py").write_text(STUB_MMLU)
        (pkg / "simple_eval_humaneval.py").write_text(STUB_HUMANEVAL)
        self.log = self.tmp / "call.json"
        self.sampler_log = self.tmp / "sampler.json"

    def run_driver(self, name, *args):
        r = subprocess.run([sys.executable, str(EVALS / name), *args],
                           capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, PYTHONPATH=str(self.tmp),
                                    STUB_LOG=str(self.log), STUB_SAMPLER_LOG=str(self.sampler_log),
                                    OPENAI_API_KEY="k"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout, json.loads(self.log.read_text())

    def test_mmlu_prints_a_score_a_caller_can_grep(self):
        """Every caller of these drivers reads one line: `'score': <value>`."""
        out, _ = self.run_driver("mmlu.py", "m", "7", "3")
        self.assertIn("'score': 0.5", out)

    def test_mmlu_asks_for_the_dataset_the_numbers_were_measured_against(self):
        """A different CSV is a different benchmark wearing the same name."""
        _, call = self.run_driver("mmlu.py", "m", "7", "3")
        self.assertEqual(call["filename"],
                         "https://openaipublic.blob.core.windows.net/simple-evals/mmlu.csv")
        self.assertEqual((call["num_examples"], call["num_threads"]), (7, 3))

    def test_humaneval_never_forks(self):
        """The image's filelock refuses os.fork, and human_eval forks per problem."""
        _, call = self.run_driver("humaneval.py", "m", "4", "2")
        self.assertEqual(call["start_method"], "spawn")

    def test_humaneval_asks_for_one_sample_per_task(self):
        """At temperature 0 the harness default of five measures pass@1 five times."""
        _, call = self.run_driver("humaneval.py", "m", "4", "2")
        self.assertEqual(call["samples_per_task"], 1)
        self.assertEqual(call["ks"], [1])

    def test_humaneval_prints_a_score_a_caller_can_grep(self):
        out, _ = self.run_driver("humaneval.py", "m", "4", "2")
        self.assertIn("'score': 0.75", out)

    def test_both_take_an_optional_base_url(self):
        """The fourth argument is how these point at a proxy or another box, and it has to
        reach the sampler: only the score line was checked, so a driver that ignored it
        passed (found in review, 2026-09-24)."""
        for name in ("mmlu.py", "humaneval.py"):
            with self.subTest(driver=name):
                out, _ = self.run_driver(name, "m", "2", "1", "http://127.0.0.1:1/v1")
                self.assertIn("'score':", out)
                kw = json.loads(self.sampler_log.read_text())
                self.assertEqual(kw["base_url"], "http://127.0.0.1:1/v1")
                self.run_driver(name, "m", "2", "1")
                kw = json.loads(self.sampler_log.read_text())
                self.assertEqual(kw["base_url"], "http://127.0.0.1:30000/v1")

    def test_both_sample_the_model_they_are_given_at_temperature_zero(self):
        """The published scores are greedy: a sampler at 0.7 measures something else."""
        for name in ("mmlu.py", "humaneval.py"):
            with self.subTest(driver=name):
                self.run_driver(name, "my-model", "2", "1")
                kw = json.loads(self.sampler_log.read_text())
                self.assertEqual((kw["model"], kw["temperature"]), ("my-model", 0.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
