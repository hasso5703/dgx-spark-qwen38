#!/usr/bin/env python3
"""Drives the HumanEval eval that ships inside the SGLang image.

Two reasons this file exists rather than `run_eval --eval-name humaneval`:

1. The image ships no `human_eval` package. The official tree (openai/human-eval,
   pinned at 6d43fb98) is mounted at /he and reached through PYTHONPATH.
2. human_eval runs every completion in a child process, and the image's filelock
   (3.32.5) installs an audit hook that refuses os.fork outright:
   "os.fork is unsafe while filelock is changing descriptor ownership".
   The start method is spawn here, which never forks. unsafe_execute is a module
   level function, so it pickles.

Also one sample per task instead of the harness default of five: at temperature 0
the five are the same answer, so they cost 5x the generations and measure pass@1
anyway.
"""
import multiprocessing
# Usage: see evals/README.md (it runs inside the serving image, not on the host).
import sys
import time


def main():
    from sglang.test.simple_eval_common import ChatCompletionSampler
    from sglang.test.simple_eval_humaneval import HumanEval

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
    eval_obj = HumanEval(num_examples, num_threads, num_samples_per_task=1, ks_passes=[1])

    tic = time.perf_counter()
    result = eval_obj(sampler)
    latency = time.perf_counter() - tic

    print(f"Total latency: {latency:.3f} s")
    print(f"examples: {num_examples}, threads: {num_threads}, samples/task: 1")
    print(f"'score': {result.score}")
    print(f"metrics: {dict(result.metrics or {})}")


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn", force=True)
    main()
