# Benchmarks: methodology, full results, and how to reproduce them

Everything here was measured on one ASUS Ascent GX10 (GB10, 128 GB unified, stock DGX OS) on 2026-08-14/15 (v1.0-v1.1 sections) and 2026-08-19/20 (v1.2, the boot-lottery campaign), plus independent reproductions by other GB10 owners. Speeds are **batch-1 decode**; quality was verified identical to a Q8 reference on a deterministic battery (code, logic, language, instruction following); speculative decoding is lossless by construction (the verify step only ever accepts tokens the target model would have emitted).

## Headline numbers (same box, same day)

| Engine / config | Code & math reasoning, French prompts (official sampling, temp 1.0) | Eval-style workloads (EN, temp 0.6) |
|---|---|---|
| llama.cpp UD-Q4_K_XL + MTP n=3 (tuned) | ~27 tok/s | 24-30 tok/s |
| vLLM 0.27 NVFP4 + MTP n=3 (official recipe) | ~24.5 tok/s | n/a |
| **This repo v1.0-v1.1: SGLang NVFP4 + DSpark** | **~34 tok/s** | **38.0 avg, 46.7 peak (GSM8K-style)** |

**v1.2 (DFlash2, 2026-08-20) supersedes this table's repo row**: bench.sh greedy median 50.0 on the same instrument that measured ~36-40 above, with the full same-night three-way comparison in "The boot lottery" section below and the workload table in the README. The historical rows stay for context. The 38.0 average matches [SGLang's announced 38.28 tok/s](https://docs.sglang.io/cookbook/autoregressive/Qwen/Qwen3.8-27B) for DGX Spark (their number is an eval-suite average at temp 0.6). Those headline numbers are for what coding agents generate: code, math, structured reasoning. Free-form prose is 2-3× slower on any engine+drafter combo here; the whole story is below.

## Why this config wins (the physics)

Single-stream decode on a dense model is **memory-bandwidth-bound**. The GB10 advertises 273 GB/s; real measurable bandwidth is ~225 GB/s (DRAM refresh, bank conflicts: unrecoverable on any hardware). Every decode step must read all weights:

```
NVFP4 weights ~16.5 GB + DSpark draft ~2.7 GB + GDN states ≈ ~20 GB per step
225 GB/s ÷ 20 GB  ≈ 10-11 steps/s
× 3.3-4.7 accepted tokens per step (DSpark block speculation)
= 34-47 tok/s        ← this config runs at ~92 % of the physical ceiling
```

The two levers that matter are **bytes per step** (NVFP4 = the quality floor, don't go lower) and **accepted tokens per step** (DSpark's trained 1.36B block-drafter with confidence heads, [paper](https://arxiv.org/abs/2607.05147)). A "better engine" can only recover the last ~8 % of overhead; the rest is physics.

Config details that came out of a full tuning sweep (deterministic greedy A/B, ±0.2 tok/s reproducibility):

- `--enable-torch-compile --torch-compile-max-bs 4` → +1 tok/s
- `--num-continuous-decode-steps 2` → less scheduler overhead per token
- Checkpoints: [RadixArk/Qwen3.8-27B-NVFP4](https://huggingface.co/RadixArk/Qwen3.8-27B-NVFP4) + [RadixArk/Qwen3.8-27B-DSpark](https://huggingface.co/RadixArk/Qwen3.8-27B-DSpark) (bf16 draft; an fp8 draft measures *slower*: acceptance drops more than the bandwidth saved)
- Tested and rejected: DSpark `compact` ragged-verify + a GB10-profiled SPS cost table (needs triton attention, -8 %; the ragged scheduler only pays off at high batch sizes), draft block 9 (out-of-training-distribution), single-batch-overlap (neutral)

How many tokens each verify step accepts depends heavily on the content; that is the next section.

## Content dependence: an independent A/B

An independent GB10 owner reproduced this config from the pinned digests and A/B'd it against vLLM 0.26.1 + MTP (`num_speculative_tokens=5`, unsloth NVFP4 checkpoint) on the same machine, same day; full write-up in [the NVIDIA forum thread](https://forums.developer.nvidia.com/t/380257). Methodology: batch 1, streaming, TTFT measured separately, decode = (tokens-1)/(total-TTFT), median of 3-5 runs after warmup. One caveat they state up front: engine and checkpoint differ together, so it is not a single-variable comparison.

| Workload | SGLang + DSpark (this repo) | vLLM + MTP |
|---|---|---|
| Code probe (greedy) | **32.8 tok/s** | 25.1 tok/s |
| Reasoning probe (greedy) | 28.4 tok/s | 29.6 tok/s |
| Math probe (temp 0.6) | 30.7 tok/s | 29.8 tok/s |
| Six mixed prompts, half German | 19.5 tok/s | **24.7 tok/s** |
| TTFT, text | **0.22-0.28 s** | 0.33-0.34 s |
| Vision 1920×1200, TTFT / total per image | **2.185 s / 6.90 s** | 2.646 s / 9.06 s |

(Their best on this repo's config was 32.8 tok/s vs the 34 measured here; their GPU clock is capped at 2200 MHz, and decode being bandwidth-bound makes that mostly negligible; close enough either way.)

The acceptance numbers explain the flip. vLLM's MTP head is conditioned on the target's hidden states at every step, so acceptance stays stable (4.35-4.77) on everything they threw at it. The DSpark drafter is a separate 1.36B model that writes whole 7-token blocks from its own distribution: 2.80-5.42 accepted on this repo's probes, but **1.25-1.52 on German prose**: the block dies at verify, and the plain MTP head wins.

Reproduced and extended on this box (greedy, fresh prompts, decode net of prefill via a two-call delta, accept length read from the server logs):

| Content | tok/s | mean accept length |
|---|---|---|
| Code, English prompt | 32.9-40.5 | 3.3 |
| Code, German prompt | 22.9 | 2.6 |
| Technical explanation, French | 18.4 | 2.2 |
| Free prose, English | 16.6 | 2.1 |
| Free prose, French | 13.7 | 1.9 |
| Free prose, German | 12.2 (reproducible ±0.1) | 1.5-1.7 |
| Math word problems, eval-style (temp 0.6) | 43-47 | ~4.9 |
| Real 56K-context agentic session (mixed FR) | 18-23 | 2.2-2.8 |

So there are two axes, and **content type dominates**: English free prose is 2× slower than English code on the same setup. Language is the second axis (EN > FR > DE at equal content). The German result above is both axes stacked. Greedy vs the official temp-1.0 sampling changes almost nothing (checked: prose stays at 12-13 either way). The headline 34-38 tok/s holds for what coding agents actually generate: code, diffs, tool calls, structured reasoning; free-form prose sits at 12-17 tok/s in any language.

Practical reading:

- The **latency** advantage is unconditional: TTFT on text and the whole vision path (17 % faster encode+prefill, 24 % faster per image). With only ~147 output tokens per image, that one is the engine, not DSpark.
- The **throughput** advantage is conditional on the draft model matching your content, and "matching" means content type first, language second. Code/agentic output: clear win regardless of prompt language. Free-form prose: acceptance collapses below 2 in any language and a plain MTP head can win.
- If a DSpark draft retrained on broader prose + multilingual data appears, this config gets better for everyone; that is the lever to watch, not the engine.

## Same battery, engine vs engine (this box, both measured with `bench-matrix.sh`)

| Workload (battery v1, greedy) | SGLang + DSpark (this repo) | SGLang + MTP (same flags, spec swapped) | llama.cpp + MTP n=3 (tuned) |
|---|---|---|---|
| Math word problems (EN) | **37-38** | 27.7 | n/a (too short for the delta method; eval-style runs measured 24-30) |
| Code (EN) | **28-32** | 23.3 | 25-26 |
| Code (DE) | 24-25 | 24.1 | 21-25 |
| Technical explanation (FR) | 20-23 | 22.3 | 22 |
| Reasoning (FR) | **31.6** (twice, identical) | 30.3 | 27-28 |
| Free prose (EN) | 16 | **20.1** | 17.7 |
| Free prose (FR) | 13 | **20.8** | 18.2-18.4 |
| Free prose (DE) | 12.3 | **18.8** | 17-18 |

Ranges are two independent runs each; the SGLang+MTP column uses the checkpoint's own MTP head (`--speculative-algorithm EAGLE --speculative-num-steps 3 --speculative-eagle-topk 1 --speculative-num-draft-tokens 4`, acceptance 3.2-3.6 of 4 drafted measured) with every other flag identical to this repo's service; credit to [MiaAI-Lab's repo](https://github.com/MiaAI-Lab/Qwen3.8-27B-SGLang-DGX-Spark) for surfacing that option (their repo also documents a GDN state-pool sizing recipe for 10 concurrent requests and a validated YaRN 1M-context setup, worth reading if you serve multiple users; note it ships `--mem-fraction-static 0.95`, which on GB10 unified memory is exactly the freeze trap described above: use 0.50).

Adopted from that comparison into this repo's service (validated on this box: single-stream unchanged at 27.9/13.9 code/prose, 8 truly concurrent streams confirmed in the scheduler logs, aggregate 108.9 tok/s, KV pool slightly larger): `--mamba-radix-cache-strategy extra_buffer_lazy --mamba-ssm-dtype bfloat16 --max-mamba-cache-size 96 --max-running-requests 8`: the GDN state pool no longer silently clamps concurrency at ~6, and bf16 SSM states halve the per-slot memory.

The pattern is now three-way: **DSpark = high ceiling (28-40 on structured) with a prose floor (12-16); MTP engines = stable middle; and SGLang+MTP is the best of the stable middles (18.8-20.8 on prose, beats llama.cpp there too)**. If your workload is agentic coding (code, diffs, tool calls, math, structured reasoning), DSpark wins every relevant row plus prefill (~3×) and TTFT. If you mostly generate free-form prose or heavily mixed multilingual content, run the same service with the MTP flags above instead: one flag swap, same image, same everything else.

## Concurrency: measured, not projected

With the GDN state-pool sizing this repo ships (`extra_buffer_lazy`, bf16 SSM states, 96 slots, `--max-running-requests 8`), the service runs **8 truly concurrent streams** (verified in the scheduler logs, not just accepted connections):

| Load | Aggregate | Per stream | Conditions |
|---|---|---|---|
| 1 stream | 28-40 tok/s | 28-40 | fresh coding/reasoning |
| 8 streams, 250-tok bursts | **108.9 tok/s** | ~13.6 | mixed explanations, cold |
| 7-8 streams sustained for hours | 84-107 (median **~94**) | ~12 | multilingual mixed content (hardest acceptance regime) |

KV budget at these settings: **386K tokens shared pool** (fp8 KV), 262K max per request, 96 GDN state slots. Multiple sessions of the same agent CLI share their system prompt in the radix cache (measured with a 36K prompt), so 8 real agent sessions fit comfortably: the pool holds ~8 × 45K of *unique* context on top of the shared prefix.

## Long-prefix decode (third-party data)

Ciprian Ursu ran this repo's launch config (same flag stack, same pinned image digest, plus `--tp-size 2`) across **two** DGX Sparks over CX-7 and posted the full depth ladder on [spark-arena](https://spark-arena.com/benchmark/87a93f88-afee-4e93-89de-7cfec34c8345). Two things fall out of it:

| Prefix depth | tg128 c1 (tok/s) |
|---|---|
| fresh | 40.09 ± 3.50 |
| 4K | 36.38 |
| 8K | 39.71 |
| 16K | 33.91 |
| 32K | 34.67 |
| 65K | 35.31 |
| 100K | 28.77 |

1. **Deep context does not collapse speculative decode.** -13 % at 32K, -28 % at 100K, flat between 16K and 65K. The real 56K agentic session in the table above pointed the same way: acceptance follows content, not context length.
2. **Two Sparks at TP=2 land in the same 34-40 tok/s band as one Spark.** Batch-1 decode is latency-bound, so a second box and a CX-7 link buy concurrency headroom (c5 115, c10 97 aggregate on that run), not single-stream speed. Don't cluster for latency.

Caveat on cross-reading: that harness generates synthetic tokens (`tg128` at a set prefix depth), which is a different acceptance regime from real content: compare its *shape across depth*, not its absolute values, against this repo's battery. A controlled single-box long-prefix cell (DSpark vs MTP at 0/8K/32K) is still on this repo's list.

## vLLM + DSpark, same battery (third-party data)

[erikvullings](https://github.com/hasso5703/dgx-spark-qwen38/issues/2) ran battery v1 (`bench-matrix.sh`, two-call wall-clock delta, temperature 0) against vLLM 0.27-dev serving the **same pinned checkpoints as this repo** (`RadixArk/Qwen3.8-27B-NVFP4` plus the RadixArk DSpark drafter, wired into vLLM via [eugr's radixark-dspark mod](https://github.com/eugr/spark-vllm-docker/tree/main/mods/radixark-dspark)) on a freshly rebooted, otherwise idle GB10 Spark:

| Workload (battery v1, greedy) | vLLM + DSpark v1 (idle box) | vLLM + DSpark v2 (idle box, 2026-08-29) | SGLang + DSpark v1 (this repo, loaded box) |
|---|---|---|---|
| Math word problems (EN) | guard refused the sample (answer too short) | guard refused the sample | 37-38 |
| Code (EN) | 38.1 | 36.1 | 28-32 |
| Code (DE) | 25.3 | 31.0 | 24-25 |
| Technical explanation (FR) | 36.8 | 32.7 | 20-23 |
| Reasoning (FR) | 39.2 | 44.7 | 31.6 |
| Free prose (EN) | 16.9 | 20.4 | 16 |
| Free prose (FR) | 13.1 | 16.3 | 13 |
| Free prose (DE) | 12.8 | 15.1 | 12.3 |

The v2 column (RadixArk DSpark v2 weights, same box and battery, erikvullings 2026-08-29) is mixed rather than a step change: prose +20 to +25 %, code DE +23 %, reasoning FR +14 %, but code EN -5 % and technical FR -11 %. Acceptance went 25.2 to 54.7 % on his setup; on this bandwidth-bound decode that does not translate into uniform speed.

Read it carefully before concluding "vLLM is faster": the prose floor is identical (the drafter's low-acceptance signature, quant and drafter being the same), and the structured cells sit +15-25 % above this repo's reference numbers, measured on an **idle, freshly rebooted box**, where this repo's reference cells are measured on a box that concurrently runs the very agent sessions it serves. Independent reproducers on the NVIDIA forum thread (pontostroy, Schnabulator) report the same +8-30 % offset on quiet boxes with this exact config. The honest conclusion: **on identical hardware, quant and drafter, eugr's vLLM path and SGLang land in the same band; engine choice is not the lever, box load and content are.** A controlled idle-box re-baseline of this repo's config (benched from a second machine, zero local sessions) is on the list and will get its own column.

## DFlash2 vs DSpark v2 on this box, same battery (2026-08-29)

RadixArk republished the DSpark drafter as v2 on 2026-08-28 (commit `d0755f9`;
acceptance up sharply on vLLM per issue #2). Measured here on the 27B lane, same
day, same box, both drafters on the SGLang image this repo ships
(`qwen38-dflash2:v1.2.2`), the native unit template plus an explicit
`--context-length 262144` for both (the cached stock checkpoint is YaRN-patched
by the 1M install and the DSpark draft refuses the 1010000 target length), idle
box, `bench-matrix.sh` battery v1, one run per configuration:

| Workload (battery v1, greedy) | DFlash2 run 1 | DFlash2 run 2 | DSpark v2 |
|---|---|---|---|
| Math word problems (EN) | 39.8 | 41.9 | 39.5 |
| Code (EN) | 42.0 | 27.8 | 35.7 |
| Code (DE) | 32.9 | 31.0 | 28.1 |
| Technical explanation (FR) | 27.8 | 32.5 | 30.8 |
| Reasoning (FR) | 46.1 | 46.7 | 38.2 |
| Free prose (EN) | 21.5 | 21.8 | 19.6 |
| Free prose (FR) | 20.3 | 20.3 | 16.9 |
| Free prose (DE) | 19.7 | 18.8 | 17.7 |
| **median** | **30.4** | **29.4** | **29.5** |

Reading: a tie on the median, and DSpark v2 behind on the cells that are stable
across the two DFlash2 runs (reasoning FR -18 %, prose -10 to -17 %). The two
DFlash2 runs, 30 minutes apart, show the noise floor of single cells (code EN
42.0 vs 27.8); only medians and cells that agree across runs are readable.
DFlash2 stays this repo's 27B drafter, which also keeps the prompt-injection
resistance scenario it won in the tool-eval reproduction (issue #6).

## The flash target on SGLang (v1.5), measured (2026-08-28)

Same box, same two-call instrument. Serving config: official SGLang image +
the `flash-sglang/` overlay, NVFP4, NEXTN 3/1/4, 262,144 context, mem-fraction
0.79, chunked prefill 1024, radix cache `extra_buffer`, PLE mmap on NVMe.

| probe | measured |
|---|---|
| prefix caching, 30K identical re-serve | 18.4 s -> 0.5 s (x36) |
| known 30K prefix, fresh question | 3.2 s (x5.8) |
| decode, reasoning | 34.2 tok/s |
| decode, free prose | 20.3 tok/s |
| frozen battery, code EN (long-ctx profile) | 31.7 tok/s |
| cold prefill | ~1,480-1,930 tok/s |
| vision (two-color probe, alone and with an 8K prompt) | exact both times |
| needle at 100K, fresh content | found (92K tok ingested in 58 s) |
| quality canaries | 4/4 |
| NEXTN acceptance | 2.0-2.7 tokens/step (up to 3.95-4.0 reported with the full resolver fix) |

Short-context profiles (32K, mem-fraction 0.85) measure up to 41.5-42.2 tok/s
on code upstream; this repo ships the full-context profile.


Independent reproduction (helge, NVIDIA forum thread 381228, 2026-08-29, own installer variant without systemd, `bench.sh` from this repo): about 37 tok/s on coding tasks and about 24 tok/s on prose, in line with the reference box.

## Host memory vs prompt length on the flash lane (2026-08-29)

The KV pool says how many tokens the cache can hold (184K at fraction 0.81); it says
nothing about what a long prefill costs the box. Measured on the reference box with a
fresh boot, `MemAvailable` sampled every 2 s during a needle probe sent straight to the
engine, kernel log watched for GPU driver allocation refusals (`NVRM: NV_ERR_NO_MEMORY`):

| prompt (tokens) | MemAvailable floor | growth over idle | driver refusals | memory returned after |
|---|---|---|---|---|
| idle after boot | 23.6 to 24.2 GiB | 0 | 0 | |
| 30K / 60K | 23.0 / 23.1 GiB | +0.9 | 0 | yes |
| 120K | 16.6 GiB | +7.4 | 0 | no (16.5 after 60 s) |
| 125K (after the v1.5.6 install, pool 197K) | 14.8 GiB | +8.0 | 11 | not measured |
| 135K | 12.6 GiB | +11.4 | 0 | no |
| 150K | 8.9 GiB | +15.3 | 3 | yes (24.0) |
| 150K, second run | 6.7 GiB | +17.1 | 13 | no (5.8) |
| 177K | 0.8 GiB | +22.8 | 15 | not measured |

About 0.27 GiB per 1k tokens beyond ~90K, linear. The process RSS stays at 5.7 GiB the
whole time (unified CUDA allocations do not show in RSS or in the docker cgroup: the
`--memory 110g` cap does not see them). The 177K prompt still answered the needle
correctly; the box was one allocation away from the livelock class documented in the
README. The driver refusals are recoverable (torch frees its cache and retries) and follow
the page cache state rather than a clean threshold (0 at 135K in one run, 11 at 125K in
another); the host floor is the hard limit. This is why v1.5.6 caps one prompt at 128K on
the flash lane (about 14 GiB left at the peak) and why the README no longer says 262K fits.

Repeated use under the ceiling (same evening, v1.5.6 deployed, through the proxy, no cache
flush): 24 consecutive prompts cycling 100K, 120K and 125K tokens, 40 minutes, every one
with exact needle retrieval, prefill 60 / 73 / 77 s (no prefix reuse between them), zero
driver refusals. MemAvailable went 23.1 GiB idle to 21.4, 19.6, 18.6 after the first three
prompts and then stayed at 18.4 to 18.5 for the remaining 21: the retained footprint
plateaus at the largest prompt's peak, it does not accumulate.

The 27B lane does not have this problem. Same method on the installed 1M unit (fraction
0.70, pool 1,008,429 tokens, 18.0 GiB available idle), same evening:

| prompt (tokens) | MemAvailable floor | growth over idle | driver refusals | prefill time |
|---|---|---|---|---|
| 100K | 15.3 GiB | +2.9 | 1 (boot or prompt) | 85.6 s |
| 200K | 14.7 GiB | +3.5 | 0 | 249 s |
| 300K | 14.5 GiB | +3.3 | 0 | 488 s |

Flat from 100K to 300K: the growth is specific to the flash lane's prefill path (QSA sparse
attention layers), and the 27B lane's long-context limit stays the KV pool. Prefill
throughput on the 27B at depth: 1,170 tok/s at 100K, 800 at 200K, 615 at 300K.

Tested and rejected: `--chunked-prefill-size 512` (120K floor 20.4 GiB, better; 150K floor
6.7 GiB with 13 refusals, worse; cold prefill 30 percent slower: 96 s vs 74 s at 120K).

Method: `needle.sh` at each depth (exact retrieval, real token count from the response),
`awk` on `/proc/meminfo` every 2 s, `journalctl -k` for the driver lines, one depth at a time,
cache flushed between depths.

## The flash target on vLLM (v1.4, historical), measured (2026-08-27)

Same box, same instruments (two-call wall-clock delta for decode, single-shot
usage/wall for prefill). Serving config: vLLM official image + the PLE-mmap
overlay, NVFP4, MTP `num_speculative_tokens=2`, 262,144 context, GPU fraction
0.78, PLE prewarm on.

| probe | measured |
|---|---|
| decode, code | 31.0 tok/s |
| decode, reasoning | 31.1 tok/s |
| decode, free prose | 20.8 tok/s |
| prefill, 60K prompt | 2,284 tok/s (24.3 s) |
| prefill, 120K prompt | 2,073 tok/s (54.2 s) |
| prefill, 189K prompt | 2,099 tok/s (90.0 s) |
| MTP acceptance | ~2.2 tokens/step (rate ~0.69) |
| quality canaries (merge/logic/fr/primes) | 4/4 |
| needle at 190K depth | found |

Notes from the sweep that produced this config:

- MTP=2 is the optimum on this box: MTP=3 lowers mean acceptance (2.2 -> 1.9)
  with no speed gain. The official GB300 recipe uses 3; GB10 pays more per
  rejected draft.
- FlashInfer autotune ON and `--max-num-batched-tokens 16384` were measured:
  no gain over the pinned config (30.6/30.7/20.4 decode, 2,240 prefill), so
  the repo keeps autotune off, like every official recipe for this model.
- The ~31 tok/s decode ceiling is kernel-launch overhead in the 512-expert
  MoE at batch 1, not bandwidth and not the PLE table (PLE placement measured
  irrelevant to decode; it only affects long-context prefill). The same
  ceiling shows on llama.cpp with the same checkpoint quantized to Q4.
- For reference, the public single-Spark alternatives measured/reported at the
  time of writing: llama.cpp GGUF recipes 22-27 tok/s decode with prefill in
  the low hundreds (and ~8 tok/s decode at 185K depth), dual-Spark SGLang TP2
  64 tok/s. This target keeps single-box, full NVFP4 quality, native 262K.

## Reproduce it on your box, any engine

```bash
./bench-matrix.sh                                  # this repo's service
BASE_URL=http://127.0.0.1:8000 ./bench-matrix.sh   # any OpenAI-compatible endpoint
MODEL=my-model API_KEY= ./bench-matrix.sh          # other model id / no auth
```

The battery is versioned (v1) and frozen: the prompts never change in place, so numbers posted months apart stay comparable. It warms up the server first, measures decode net of prefill with a two-call delta, and refuses to print a number when the sample is unreliable (cold start, short answer, cache artifact) instead of printing a wrong one. It drops a `bench-matrix-<label>.json` you can post alongside your numbers.

Want to A/B against another engine without installing a service? `./install.sh --no-service && ./run.sh` runs the exact pinned config in the foreground; Ctrl+C stops and removes the container. The forum A/B author also published [their own standalone launcher](https://forums.developer.nvidia.com/t/380257/10) (same pinned config, rootless container, image-ID fallback for `docker save|load` transfers) plus the `minimal` template fix now folded into this repo.

## The boot lottery, and how we killed it (2026-08-20)

The single most consequential finding of this repo's overnight flag campaign: **on GB10,
the identical SGLang config does not perform identically across boots.** Measured on this box,
same config, same load, same battery: concurrency-8 aggregate ranged from 92 to 111 tok/s
across boots, and verify-heavy single-stream cells (code, math) swung up to ±15 % while prose
cells stayed stable to the decimal. Root cause, isolated by A/B: FlashInfer's kernel autotune
re-measures and re-picks kernels at every boot (its on-disk cache turns out to be advisory;
we verified the cache file is read but a different draw can still land). Any cross-box or
cross-config comparison that did not control for this contains boot noise, including earlier
numbers in this file and every third-party GB10 table we have seen.

The fix shipped in v1.1: `--disable-flashinfer-autotune`. With it, single-stream cells
reproduce to the decimal across boots, c8 lands within ±1.6 %, and boots get about 2 minutes
faster, for roughly 2 % of the lottery's average throughput. Additionally
`--cuda-graph-max-bs 8` captures decode batches 5-8 that previously ran eager: +6.5 % at c8,
reproduced across boots. Net: a stable 100-104 tok/s c8 instead of a 92-111 roll of the dice.

Two more sizing facts from the same campaign, both measured multi-boot:

- **GDN slot headroom is throughput.** `--max-mamba-cache-size 32` (the sizing rule's exact
  minimum for 8 requests) costs ~14 % of c8 aggregate vs 96; 64 keeps full c8 and frees
  ~55K KV tokens. The cookbook's memory-saving settings (fewer slots,
  `SGLANG_OPT_MAMBA_SKIP_DECODE_LOCK`) both cost c8 on this hardware.
- **Chunked-prefill 4096 and `--num-continuous-decode-steps 3` both cost ~16 % of c8** on this
  box despite plausible single-stream stories; their apparent c1 gains did not survive the
  boot-lottery control.

### DFlash2: now the default (v1.2)

With the same deterministic stack, the z-lab DFlash2 drafter (merged into SGLang main
2026-08-19) measured on this box, thinking on, quality canaries passing: **wins every
single-stream cell of the battery** (prose FR 20.2 vs 14.0 stock, reasoning FR 43.5 vs 30.5,
code DE 39.4 vs 25.4, math at parity) and lifts aggregate throughput to **135-148 tok/s at c8
and 258 tok/s at c32** (max-running-requests 32), still climbing at c32. It ships as this repo's default in v1.2 via a locally built, sha256-pinned overlay image (see dflash2/ATTRIBUTION.md; credit MiaAI-Lab for the quantized lm_head fix and r0b0tlab for the K sweep: block 8 optimal, 9 collapses). The repo repins to the official image the day one ships DFLASH2. An FP8-target variant (zero quantization-quality questions) measured
108 tok/s c8 with the same drafter: above the old DSpark default, and the fallback if
NVFP4-target quality evaluations ever demand it.


## The losslessness study (2026-08-20)

Community reports after v1.2 (a 2-6 point tool-eval drop vs DSpark, anecdotal hallucination
reports) triggered the deepest measurement pass of this repo. Everything below ran on the
deterministic stack (`--disable-flashinfer-autotune`), which turns out to make even quality
benchmarks reproducible to the point across seeds.

**Step 1, reproduce.** tool-eval-bench (69 scenarios, two seeds each): DSpark **93 / 93**
(identical points per seed), DFlash2 **91 / 91**. The deficit is real, stable, and lives in a
core of 4 long-agentic-chain scenarios. Greedy (temperature 0) does not remove it. The
community's inter-run "variance" (88-92 for the same config) does not exist on a deterministic
server: it was the boot lottery again.

**Step 2, ground truth.** At temperature 0, a lossless speculative decoder should reproduce the
pure autoregressive model token for token. Measured (10 diverse prompts, sequential, full
content+reasoning compared): **DSpark diverges from the AR ground truth on 10/10 prompts, and
so does DFlash2, equally early** (2-33% into the reasoning). Speculative decoding is lossless
in exact arithmetic, not in floating point: block verification changes reduction orders, a
near-tie argmax flips, and the chain cascades. Neither drafter's text is "the model's true
output"; both are equally legitimate samples of its numeric neighborhood. (Escha Labs'
runtime docs independently document the same effect: "never A/B two configurations by diffing
one generation".)

**Step 3, does it cost real quality?** Large-n standard evals, same box, same night, both
drafters: GSM8K 200 → **exact parity, 188/200 vs 188/200**. IFEval 200 → split within noise:
prompt-level 86.5 (DSpark, with 15 server timeouts excluded from its denominator; it is the
slower config under a fixed budget) vs 81.4 (DFlash2, 1 timeout); instruction-level flips the
other way, 87.4 (DFlash2) vs 83.7 (DSpark). At n=200 the +-1 sigma band is ~3 points: no
consistent direction survives.

**Verdict:** no measurable real-quality loss; the 2-3 tool-eval points are floating-point
near-tie flips landing unfavorably on a handful of long scenarios of one benchmark, made
visible (and stable) by determinism. DFlash2 stays the default: it wins every speed lane by
20-40% and ties the quality battery. What the tool-eval score of "the model itself" would be (a pure-AR run, ~3x slower to
produce) is left as an open question; the token-identity test above already establishes that
neither drafter reproduces it exactly, and the large-n evals settle the practical one.

## Benchmarking traps (all hit for real)

- **Repeated images** hit the multimodal cache and skip the vision tower entirely: any image benchmark needs images the instance has never seen (found by the forum A/B author).
- **Repeated text prompts** on llama.cpp with a separate `-hfd` draft model replay at chunked-verify speed: 203 tok/s measured on a prompt whose true cold rate was 25. SGLang+DSpark did not show this effect (repeats reproduce within ±0.2 tok/s), but fresh prompts are the only safe protocol for any speculative bench.
- **The first request after a model load** pays one-time costs (mmap page-in, spec-path warmup) that poison both calls of a delta measurement; `bench-matrix.sh` burns a warmup call and flags any sample whose deltas are too small to trust (its own first version printed a 979 tok/s artifact before this guard existed).
- **Short answers** (eval-style math) end before the second call's budget and break the two-call delta: measure those with streaming TTFT-separated timing instead (`bench.sh` does).
- **Renamed streaming fields** silently start a streaming benchmark's clock late: vLLM ≥ 0.27 streams thinking tokens as `delta.reasoning` (SGLang: `reasoning_content`); a client that only recognizes one name times just the visible answer while `usage.completion_tokens` counts the whole generation, inflating tok/s by the think-to-answer ratio. Produced a reported 97 tok/s on a box whose wall-clock rate was 19.9 (issue #2); `bench.sh` fixed in `9cf6b20`, plus a warning above the DSpark block-7 physical ceiling (~90 tok/s here). Wall-clock delta methods (`bench-matrix.sh`) are immune.
- **Prefill benchmarks whose prompts share a prefix** (each shorter prompt being a prefix of the longer) replay cached KV instead of prefilling: RadixAttention reported 20,262 tokens in 0.202 s, roughly 100,000 tok/s, against a true cold rate of ~2,100 on the same box, a ~50x overstatement from a protocol that looks careful (measured and reported in issue #6). Prefill probes need mutually unrelated prompts.
- **`eugr/llama-benchy` cannot drive this server**: it sends `return_token_ids=true` with `stream=true`, which SGLang rejects (HTTP 400), so every run fails and writes an empty result file (issue #6). `bench.sh` and `bench-matrix.sh` are the supported instruments.
- **Omitting sampling parameters does not give you greedy.** When a client sends no temperature/top_p/top_k, both SGLang and vLLM silently adopt the checkpoint's `generation_config.json`; for both checkpoints this repo pins, that means temperature 1.0, top_k 20, top_p 0.95. Verified live here: two parameter-free requests diverge within 15 tokens. Pin sampling explicitly in every benchmark or comparison; `bench.sh` and `bench-matrix.sh` always send `temperature` (surfaced by issue #6).
- **Multi-turn runs are not reproducible with RadixAttention on.** With the radix cache enabled, identical greedy multi-turn agentic runs diverged on 6 of 34 tasks in a third-party harness; speculation amplifies but does not cause it (3 of 34 without the drafter, 0 of 34 with radix off, either way). `--enable-deterministic-inference` removes the variance for about 11% throughput, and on GB10 it necessarily disables the radix cache as well: the radix-preserving deterministic backends do not run on sm121 (fa4 asserts out, triton fails during CUDA-graph capture). Reproducible multi-turn evals: use the flag. Prefix caching: accept run-to-run variance. Measured and reported by TravisMeyers in issue #6.

Point-in-time note: all numbers are 2026-08-15, image `lmsysorg/sglang:qwen38-27b` (pull digest `febfb971c735…`, image ID `0076dffa60b7…`), checkpoints pinned in `install.sh`. Kernels for sm_121 are young; gaps will move with releases; that is exactly why everything here is pinned and the battery is frozen.
