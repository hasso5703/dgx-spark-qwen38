# Benchmarks — methodology, full results, and how to reproduce them

Everything here was measured on one ASUS Ascent GX10 (GB10, 128 GB unified, stock DGX OS) on 2026-08-14/15, plus an independent reproduction by a second GB10 owner. Speeds are **batch-1 decode**; quality was verified identical to a Q8 reference on a deterministic battery (code, logic, language, instruction following) — speculative decoding is lossless by construction (the verify step only ever accepts tokens the target model would have emitted).

## Headline numbers (same box, same day)

| Engine / config | Code & math reasoning, French prompts (official sampling, temp 1.0) | Eval-style workloads (EN, temp 0.6) |
|---|---|---|
| llama.cpp UD-Q4_K_XL + MTP n=3 (tuned) | ~27 tok/s | 24–30 tok/s |
| vLLM 0.27 NVFP4 + MTP n=3 (official recipe) | ~24.5 tok/s | — |
| **This repo: SGLang NVFP4 + DSpark** | **~34 tok/s** | **38.0 avg, 46.7 peak (GSM8K-style)** |

The 38.0 average matches [SGLang's announced 38.28 tok/s](https://docs.sglang.io/cookbook/autoregressive/Qwen/Qwen3.8-27B) for DGX Spark (their number is an eval-suite average at temp 0.6). Those headline numbers are for what coding agents generate — code, math, structured reasoning. Free-form prose is 2–3× slower on any engine+drafter combo here; the whole story is below.

## Why this config wins (the physics)

Single-stream decode on a dense model is **memory-bandwidth-bound**. The GB10 advertises 273 GB/s; real measurable bandwidth is ~225 GB/s (DRAM refresh, bank conflicts — unrecoverable on any hardware). Every decode step must read all weights:

```
NVFP4 weights ~16.5 GB + DSpark draft ~2.7 GB + GDN states ≈ ~20 GB per step
225 GB/s ÷ 20 GB  ≈ 10–11 steps/s
× 3.3–4.7 accepted tokens per step (DSpark block speculation)
= 34–47 tok/s        ← this config runs at ~92 % of the physical ceiling
```

The two levers that matter are **bytes per step** (NVFP4 = the quality floor, don't go lower) and **accepted tokens per step** (DSpark's trained 1.36B block-drafter with confidence heads, [paper](https://arxiv.org/abs/2607.05147)). A "better engine" can only recover the last ~8 % of overhead; the rest is physics.

Config details that came out of a full tuning sweep (deterministic greedy A/B, ±0.2 tok/s reproducibility):

- `--enable-torch-compile --torch-compile-max-bs 4` → +1 tok/s
- `--num-continuous-decode-steps 2` → less scheduler overhead per token
- Checkpoints: [RadixArk/Qwen3.8-27B-NVFP4](https://huggingface.co/RadixArk/Qwen3.8-27B-NVFP4) + [RadixArk/Qwen3.8-27B-DSpark](https://huggingface.co/RadixArk/Qwen3.8-27B-DSpark) (bf16 draft — an fp8 draft measures *slower*: acceptance drops more than the bandwidth saved)
- Tested and rejected: DSpark `compact` ragged-verify + a GB10-profiled SPS cost table (needs triton attention, −8 %; the ragged scheduler only pays off at high batch sizes), draft block 9 (out-of-training-distribution), single-batch-overlap (neutral)

How many tokens each verify step accepts depends heavily on the content — that is the next section.

## Content dependence — an independent A/B

An independent GB10 owner reproduced this config from the pinned digests and A/B'd it against vLLM 0.26.1 + MTP (`num_speculative_tokens=5`, unsloth NVFP4 checkpoint) on the same machine, same day — full write-up in [the NVIDIA forum thread](https://forums.developer.nvidia.com/t/380257). Methodology: batch 1, streaming, TTFT measured separately, decode = (tokens−1)/(total−TTFT), median of 3–5 runs after warmup. One caveat they state up front: engine and checkpoint differ together, so it is not a single-variable comparison.

| Workload | SGLang + DSpark (this repo) | vLLM + MTP |
|---|---|---|
| Code probe (greedy) | **32.8 tok/s** | 25.1 tok/s |
| Reasoning probe (greedy) | 28.4 tok/s | 29.6 tok/s |
| Math probe (temp 0.6) | 30.7 tok/s | 29.8 tok/s |
| Six mixed prompts, half German | 19.5 tok/s | **24.7 tok/s** |
| TTFT, text | **0.22–0.28 s** | 0.33–0.34 s |
| Vision 1920×1200 — TTFT / total per image | **2.185 s / 6.90 s** | 2.646 s / 9.06 s |

(Their best on this repo's config was 32.8 tok/s vs the 34 measured here; their GPU clock is capped at 2200 MHz, and decode being bandwidth-bound makes that mostly negligible — close enough either way.)

The acceptance numbers explain the flip. vLLM's MTP head is conditioned on the target's hidden states at every step, so acceptance stays stable (4.35–4.77) on everything they threw at it. The DSpark drafter is a separate 1.36B model that writes whole 7-token blocks from its own distribution: 2.80–5.42 accepted on this repo's probes, but **1.25–1.52 on German prose** — the block dies at verify, and the plain MTP head wins.

Reproduced and extended on this box (greedy, fresh prompts, decode net of prefill via a two-call delta, accept length read from the server logs):

| Content | tok/s | mean accept length |
|---|---|---|
| Code, English prompt | 32.9–40.5 | 3.3 |
| Code, German prompt | 22.9 | 2.6 |
| Technical explanation, French | 18.4 | 2.2 |
| Free prose, English | 16.6 | 2.1 |
| Free prose, French | 13.7 | 1.9 |
| Free prose, German | 12.2 (reproducible ±0.1) | 1.5–1.7 |
| Math word problems, eval-style (temp 0.6) | 43–47 | ~4.9 |
| Real 56K-context agentic session (mixed FR) | 18–23 | 2.2–2.8 |

So there are two axes, and **content type dominates**: English free prose is 2× slower than English code on the same setup. Language is the second axis (EN > FR > DE at equal content). The German result above is both axes stacked. Greedy vs the official temp-1.0 sampling changes almost nothing (checked: prose stays at 12–13 either way). The headline 34–38 tok/s holds for what coding agents actually generate — code, diffs, tool calls, structured reasoning; free-form prose sits at 12–17 tok/s in any language.

Practical reading:

- The **latency** advantage is unconditional: TTFT on text and the whole vision path (17 % faster encode+prefill, 24 % faster per image). With only ~147 output tokens per image, that one is the engine, not DSpark.
- The **throughput** advantage is conditional on the draft model matching your content — and "matching" means content type first, language second. Code/agentic output: clear win regardless of prompt language. Free-form prose: acceptance collapses below 2 in any language and a plain MTP head can win.
- If a DSpark draft retrained on broader prose + multilingual data appears, this config gets better for everyone — that is the lever to watch, not the engine.

## Same battery, engine vs engine (this box, both measured with `bench-matrix.sh`)

| Workload (battery v1, greedy) | SGLang + DSpark (this repo) | SGLang + MTP (same flags, spec swapped) | llama.cpp + MTP n=3 (tuned) |
|---|---|---|---|
| Math word problems (EN) | **37–38** | 27.7 | — (too short for the delta method; eval-style runs measured 24–30) |
| Code (EN) | **28–32** | 23.3 | 25–26 |
| Code (DE) | 24–25 | 24.1 | 21–25 |
| Technical explanation (FR) | 20–23 | 22.3 | 22 |
| Reasoning (FR) | **31.6** (twice, identical) | 30.3 | 27–28 |
| Free prose (EN) | 16 | **20.1** | 17.7 |
| Free prose (FR) | 13 | **20.8** | 18.2–18.4 |
| Free prose (DE) | 12.3 | **18.8** | 17–18 |

Ranges are two independent runs each; the SGLang+MTP column uses the checkpoint's own MTP head (`--speculative-algorithm EAGLE --speculative-num-steps 3 --speculative-eagle-topk 1 --speculative-num-draft-tokens 4`, acceptance 3.2–3.6 of 4 drafted measured) with every other flag identical to this repo's service — credit to [MiaAI-Lab's repo](https://github.com/MiaAI-Lab/Qwen3.8-27B-SGLang-DGX-Spark) for surfacing that option (their repo also documents a GDN state-pool sizing recipe for 10 concurrent requests and a validated YaRN 1M-context setup — worth reading if you serve multiple users; note it ships `--mem-fraction-static 0.95`, which on GB10 unified memory is exactly the freeze trap described above — use 0.50).

Adopted from that comparison into this repo's service (validated on this box: single-stream unchanged at 27.9/13.9 code/prose, 8 truly concurrent streams confirmed in the scheduler logs, aggregate 108.9 tok/s, KV pool slightly larger): `--mamba-radix-cache-strategy extra_buffer_lazy --mamba-ssm-dtype bfloat16 --max-mamba-cache-size 96 --max-running-requests 8` — the GDN state pool no longer silently clamps concurrency at ~6, and bf16 SSM states halve the per-slot memory.

The pattern is now three-way: **DSpark = high ceiling (28–40 on structured) with a prose floor (12–16); MTP engines = stable middle; and SGLang+MTP is the best of the stable middles (18.8–20.8 on prose — beats llama.cpp there too)**. If your workload is agentic coding — code, diffs, tool calls, math, structured reasoning — DSpark wins every relevant row plus prefill (~3×) and TTFT. If you mostly generate free-form prose or heavily mixed multilingual content, run the same service with the MTP flags above instead: one flag swap, same image, same everything else.

## Concurrency — measured, not projected

With the GDN state-pool sizing this repo ships (`extra_buffer_lazy`, bf16 SSM states, 96 slots, `--max-running-requests 8`), the service runs **8 truly concurrent streams** (verified in the scheduler logs, not just accepted connections):

| Load | Aggregate | Per stream | Conditions |
|---|---|---|---|
| 1 stream | 28–40 tok/s | 28–40 | fresh coding/reasoning |
| 8 streams, 250-tok bursts | **108.9 tok/s** | ~13.6 | mixed explanations, cold |
| 7–8 streams sustained for hours | 84–107 (median **~94**) | ~12 | multilingual mixed content (hardest acceptance regime) |

KV budget at these settings: **386K tokens shared pool** (fp8 KV), 262K max per request, 96 GDN state slots. Multiple Claude Code sessions share their 36K system prompt in the radix cache, so 8 real agent sessions fit comfortably — the pool holds ~8 × 45K of *unique* context on top of the shared prefix.

## Long-prefix decode (third-party data)

Ciprian Ursu ran this repo's launch config — same flag stack, same pinned image digest, plus `--tp-size 2` — across **two** DGX Sparks over CX-7 and posted the full depth ladder on [spark-arena](https://spark-arena.com/benchmark/87a93f88-afee-4e93-89de-7cfec34c8345). Two things fall out of it:

| Prefix depth | tg128 c1 (tok/s) |
|---|---|
| fresh | 40.09 ± 3.50 |
| 4K | 36.38 |
| 8K | 39.71 |
| 16K | 33.91 |
| 32K | 34.67 |
| 65K | 35.31 |
| 100K | 28.77 |

1. **Deep context does not collapse speculative decode.** −13 % at 32K, −28 % at 100K, flat between 16K and 65K. The real 56K agentic session in the table above pointed the same way: acceptance follows content, not context length.
2. **Two Sparks at TP=2 land in the same 34–40 tok/s band as one Spark.** Batch-1 decode is latency-bound, so a second box and a CX-7 link buy concurrency headroom (c5 115, c10 97 aggregate on that run), not single-stream speed. Don't cluster for latency.

Caveat on cross-reading: that harness generates synthetic tokens (`tg128` at a set prefix depth), which is a different acceptance regime from real content — compare its *shape across depth*, not its absolute values, against this repo's battery. A controlled single-box long-prefix cell (DSpark vs MTP at 0/8K/32K) is still on this repo's list.

## Reproduce it on your box — any engine

```bash
./bench-matrix.sh                                  # this repo's service
BASE_URL=http://127.0.0.1:8000 ./bench-matrix.sh   # any OpenAI-compatible endpoint
MODEL=my-model API_KEY= ./bench-matrix.sh          # other model id / no auth
```

The battery is versioned (v1) and frozen: the prompts never change in place, so numbers posted months apart stay comparable. It warms up the server first, measures decode net of prefill with a two-call delta, and refuses to print a number when the sample is unreliable (cold start, short answer, cache artifact) instead of printing a wrong one. It drops a `bench-matrix-<label>.json` you can post alongside your numbers.

Want to A/B against another engine without installing a service? `./install.sh --no-service && ./run.sh` runs the exact pinned config in the foreground; Ctrl+C stops and removes the container. The forum A/B author also published [their own standalone launcher](https://forums.developer.nvidia.com/t/380257/10) — same pinned config, rootless container, image-ID fallback for `docker save|load` transfers — plus the `minimal` template fix now folded into this repo.

## Benchmarking traps (all hit for real)

- **Repeated images** hit the multimodal cache and skip the vision tower entirely — any image benchmark needs images the instance has never seen (found by the forum A/B author).
- **Repeated text prompts** on llama.cpp with a separate `-hfd` draft model replay at chunked-verify speed — 203 tok/s measured on a prompt whose true cold rate was 25. SGLang+DSpark did not show this effect (repeats reproduce within ±0.2 tok/s), but fresh prompts are the only safe protocol for any speculative bench.
- **The first request after a model load** pays one-time costs (mmap page-in, spec-path warmup) that poison both calls of a delta measurement — `bench-matrix.sh` burns a warmup call and flags any sample whose deltas are too small to trust (its own first version printed a 979 tok/s artifact before this guard existed).
- **Short answers** (eval-style math) end before the second call's budget and break the two-call delta — measure those with streaming TTFT-separated timing instead (`bench.sh` does).

Point-in-time note: all numbers are 2026-08-15, image `lmsysorg/sglang:qwen38-27b` (pull digest `febfb971c735…`, image ID `0076dffa60b7…`), checkpoints pinned in `install.sh`. Kernels for sm_121 are young; gaps will move with releases — that is exactly why everything here is pinned and the battery is frozen.
