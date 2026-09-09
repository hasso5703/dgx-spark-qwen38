# Running a GGUF model on a DGX Spark

This repo serves SGLang. It has no GGUF lane and this note is not one: it is what the
reference box measured when it did run llama.cpp on this hardware, so that anyone asking
"can I serve a GGUF fine-tune on my Spark instead?" can answer it with numbers rather than
with a weekend. Asked in [issue #12](https://github.com/hasso5703/dgx-spark-qwen38/issues/12).

**Short answer**: yes, llama.cpp runs well on GB10, and it costs roughly a third of the
decode rate this repo's default lane gets from the same model. If what you want is a
fine-tune that only exists as GGUF, that trade is yours to make and the numbers below say
what you are paying. If what you want is an uncensored model, this repo already ships
three of those on the fast path (`uncensored`, `uncensored-fp8`, `flash-uncensored`).

## What it costs, measured here

Same box, same 27B model family, the repo's frozen battery v1 (two-call wall-clock delta,
temperature 0), llama.cpp tuned with its MTP draft against the SGLang lane:

| Workload (battery v1, greedy) | llama.cpp UD-Q4_K_XL + MTP n=3, tuned | this repo, SGLang + DFlash2 |
|---|---|---|
| Agentic coding (code, diffs, tool calls) | 25.6 tok/s (20.9 in DE) | 32-40 |
| Math, eval-style | not completed on that run | 41-44 |
| Technical explanation (FR) | 21.9 | 26 |
| Reasoning (FR) | 27.5 | 41-44 |
| Free prose EN / FR / DE | 17.7 / 18.2 / 18.0 | 22 / 20 / 17 |

Raw file: [`bench-matrix-llamacpp.json`](../../bench-matrix-llamacpp.json), and the same
comparison in context in [BENCHMARKS.md](../../BENCHMARKS.md). Prefill is the other half of the gap, and the one an agent loop
feels first: the repo's three-way comparison puts the SGLang speculative path about **3x**
ahead of the stable-MTP engines it was measured against, llama.cpp among them.

## The four things that will bite you

- **Benchmark fresh prompts only.** A repeated text prompt on llama.cpp with a separate
  `-hfd` draft model replays at chunked-verify speed: **203 tok/s measured on a prompt
  whose true cold rate was 25**. That number is not a speed, it is a cache. Every figure
  above comes from prompts the engine had not seen.
- **A speculative draft shipped as a GGUF file is a claim, not a speed.** What a block
  drafter is worth depends on what the model writes, and this repo has the numbers on its
  own path: the same drafter measured **2.80-5.42 tokens accepted per block** on code and
  structured probes and **1.25-1.52 on German prose**, which is where a plain MTP head
  wins instead. A tree shipping `dflash2-*.gguf` and `mtp-*.gguf` next to the weights is
  shipping the idea; the acceptance rate is yours to measure, on your content, before any
  of it counts as speed.
- **Quantization floor.** This repo treats **NVFP4 as the floor** and Qwen's own FP8 as the
  reference above it, because both carry calibration the engine applies. A Q2 or Q3 GGUF is
  well below that floor whatever its name says; if you evaluate one of these trees, start at
  Q4_K_XL or above and run your own quality checks, not the card's adjectives.
- **The unified-memory trap applies here too.** llama.cpp's allocations come out of the same
  128 GB pool as everything else on a GB10, and a Spark that runs out does not swap, it
  livelocks (see the README's "GB10 unified-memory trap"). Size the context and the KV cache
  the way you would for the SGLang lane, not the way you would on a discrete GPU.

## If you want to run one anyway

Nothing in this repo has to be uninstalled: stop the serving unit so the pool is free
(`sudo systemctl stop qwen38-sglang` or `qwen38-flash`), run llama.cpp on its own port,
and start the unit again afterwards. This repo's sibling project
[qwen3.8-27b-in-16gb](https://github.com/hasso5703/qwen3.8-27b-in-16gb) is the same model
on llama.cpp, pinned the same way this one pins SGLang (one llama.cpp commit, one GGUF,
one checksum-verified flag set, 145,408 tokens of context in 16 GB of VRAM). It targets a
consumer GPU rather than a Spark, so the offload recipe is not transferable, but the
pinning discipline and the flag reasoning are. The keepalive proxy in this repo is SGLang-shaped (it speaks to `/abort_request`,
`/get_server_info` and `/tokenize`) and is not meant to sit in front of llama.cpp.

## Would a GGUF lane be added here?

Not for speed: on this hardware it is the slower path, and the repo's reason to exist is
the fast one. The case that would change the answer is a **fine-tune that exists only as
GGUF and measurably beats the served checkpoints on work people actually do**. That is an
evidence question, and the evidence format is the one every target in this repo went
through: `./bench.sh` and `./bench-matrix.sh` on a fresh boot, `./needle.sh` at your
context, the four output-quality canaries, and, for an uncensored claim, the refusal probe
(the abliterated flash target scores 0 refusals of 5). Post that in an issue and it will be
read carefully.
