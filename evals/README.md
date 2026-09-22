# Quality evals: MMLU and HumanEval, against a lane this repo serves

Two drivers, because the invocations that should work do not. In the SGLang image
this repo's flash lane serves:

- `python3 -m sglang.test.run_eval --eval-name mmlu` measures nothing itself any
  more. It shells out to an external `sgl-eval` binary (sgl-project/sgl-eval, first
  published on PyPI 2026-09-12) that the image does not ship, and dies with a
  `FileNotFoundError` before it reaches the server.
- its HumanEval path imports a `human_eval` package that is not in the image, and
  once that is mounted from the official tree it dies inside `os.fork`, because the
  image's filelock (3.32.5) installs an audit hook that refuses one.

Both failures print a traceback and then an empty score, which reads exactly like a
model that scored nothing. That is how this repo's first two runs were lost
(2026-09-18). GSM8K needs none of this: its path in the image still runs in process,
so `run_eval --eval-name gsm8k` is fine.

`mmlu.py` drives the `MMLUEval` class still vendored in the image, against the same
`openaipublic` dataset the old path used. `humaneval.py` runs the official
openai/human-eval tree with the multiprocessing start method set to `spawn`, and one
sample per task instead of the harness default of five: at temperature 0 those five
are the same answer five times.

## Running them

Both take `<served-model-name> <examples> <threads> [base-url]` and print a line
containing `'score': <value>`. The engine must already be up.

```bash
KEY="$(cat ~/.config/qwen38/api-key)"

# MMLU, 500 questions
docker run --rm --network host -e OPENAI_API_KEY="$KEY" \
  -v "$PWD/evals/mmlu.py":/d.py:ro --entrypoint python3 \
  lmsysorg/sglang:latest /d.py qwen3.8-flash-next 500 32

# HumanEval, 164 problems, pass@1. Fetch the official tree first (pinned), and
# mount it read only: it executes model-generated code, so nothing else of this
# box goes into that container.
curl -sL https://github.com/openai/human-eval/archive/6d43fb980f9fee3c892a914eda09951f772ad10d.tar.gz \
  | tar -xz && mv human-eval-6d43fb98* /tmp/human-eval
docker run --rm --network host -e OPENAI_API_KEY="$KEY" -e PYTHONPATH=/he \
  -v /tmp/human-eval:/he:ro -v "$PWD/evals/humaneval.py":/d.py:ro \
  --entrypoint python3 lmsysorg/sglang:latest /d.py qwen3.8-flash-next 164 32
```

Replace `qwen3.8-flash-next` with `qwen3.8-27b` for the 27B lane, and add a fourth
argument to point somewhere other than `http://127.0.0.1:30000/v1`.

What these measured on this box, on both flash exports:
[BENCHMARKS.md](../BENCHMARKS.md), "RadixArk against NVIDIA, head to head".

[Back to the README](../README.md)
