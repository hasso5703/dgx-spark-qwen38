#!/usr/bin/env python3
# Drives the MMLU eval that ships inside the SGLang image.
#
# Why this file exists: in the image this lane serves, `python3 -m
# sglang.test.run_eval --eval-name mmlu` no longer runs anything itself. It
# shells out to an external `sgl-eval` binary (sgl-project/sgl-eval, first
# published on PyPI 2026-09-12) that the image does not ship, so it dies with
# FileNotFoundError before touching the server. Measured 2026-09-18.
#
# The MMLUEval class is still vendored in the image, so this calls it directly
# with the same dataset and the same sampler the old path used.
# Usage: see evals/README.md (it runs inside the serving image, not on the host).
import sys
import time

from sglang.test.simple_eval_common import ChatCompletionSampler
from sglang.test.simple_eval_mmlu import MMLUEval

URL = "https://openaipublic.blob.core.windows.net/simple-evals/mmlu.csv"

model = sys.argv[1]
num_examples = int(sys.argv[2])
num_threads = int(sys.argv[3])
base_url = sys.argv[4] if len(sys.argv) > 4 else "http://127.0.0.1:30000/v1"

sampler = ChatCompletionSampler(
    model=model,
    max_tokens=2048,
    top_p=1.0,
    base_url=base_url,
    temperature=0.0,
    record_meta_info=True,
)
eval_obj = MMLUEval(URL, num_examples, num_threads)

tic = time.perf_counter()
result = eval_obj(sampler)
latency = time.perf_counter() - tic

print(f"Total latency: {latency:.3f} s")
print(f"examples: {num_examples}, threads: {num_threads}")
print(f"'score': {result.score}")
print(f"metrics: {dict(result.metrics or {})}")
