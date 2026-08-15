# Qwen3.8-27B at 34–38 tok/s on DGX Spark (GB10)

**One-command setup for the fastest known Qwen3.8-27B config on a single DGX Spark / ASUS Ascent GX10**: SGLang + NVFP4 (W4A4) + DSpark block-speculative decoding, hardened for the GB10's unified memory. Boots automatically with the machine via systemd, serves an OpenAI **and** Anthropic-compatible API, and works with Claude Code out of the box.

If you are stuck around **20–27 tok/s** with llama.cpp or vLLM — this gets you to **34–38 tok/s** with **zero quality loss** (same NVFP4 quantization floor, speculative decoding is lossless by construction).

## Measured numbers (single DGX Spark, decode, batch 1)

| Engine / config | French agentic workload (official sampling, temp 1.0) | Eval-style workloads (EN, temp 0.6) |
|---|---|---|
| llama.cpp UD-Q4_K_XL + MTP n=3 (tuned) | ~27 tok/s | 24–30 tok/s |
| vLLM 0.27 NVFP4 + MTP n=3 (official recipe) | ~24.5 tok/s | — |
| **This repo: SGLang NVFP4 + DSpark** | **~34 tok/s** | **38.0 avg, 46.7 peak (GSM8K-style)** |

The 38.0 average matches [SGLang's announced 38.28 tok/s](https://docs.sglang.io/cookbook/autoregressive/Qwen/Qwen3.8-27B) for DGX Spark (their number is an eval-suite average at temp 0.6). Quality was verified identical to a Q8 reference on a deterministic battery (code, logic, language, instruction following) — see *Why this is lossless* below.

## Quickstart

Requirements: DGX Spark or other GB10 machine (128 GB unified), DGX OS with Docker + NVIDIA container toolkit (stock), ~85 GB free disk.

```bash
git clone https://github.com/hasso5703/dgx-spark-qwen38.git
cd dgx-spark-qwen38
./install.sh            # pulls image, downloads checkpoints, installs the systemd service
./bench.sh              # verify your tok/s
```

First boot takes **~9 minutes** (torch.compile + CUDA graph capture; a persistent compile cache makes later boots faster). The service then starts itself at every boot. Add `--with-claude-warmup` to `install.sh` if you use Claude Code and want the server to pre-warm your system prompt after each boot.

**Built to still work months from now**: the installer pins the exact Docker image digest and HuggingFace checkpoint revisions that were validated. It is idempotent (re-run it anytime; existing downloads/keys are reused, interrupted downloads resume) and every failure path prints what went wrong and how to fix it. Want to try newer builds instead of the pinned ones?

```bash
IMAGE=lmsysorg/sglang:qwen38-27b MODEL_REV=main DRAFT_REV=main ./install.sh
```

Endpoints (default port 30000, API key generated at `~/.config/qwen38/api-key`):

- OpenAI: `http://<host>:30000/v1/chat/completions`
- Anthropic (Claude Code): `http://<host>:30000/v1/messages`
- Model id: `qwen3.8-27b`

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

How many tokens each verify step accepts depends heavily on the content — see the independent A/B below.

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

Practical reading:

- The **latency** advantage held in every one of their measurements: TTFT on text and the whole vision path (17 % faster encode+prefill, 24 % faster per image). With only ~147 output tokens per image, that one is the engine, not DSpark.
- The **throughput** advantage is conditional on the draft model matching your content. English code and French agentic work: clear win. Mixed or non-English prose: it can lose to a plain MTP head.
- If a DSpark draft retrained on broader multilingual data appears, this config gets better for everyone — that is the lever to watch, not the engine.

One bench trap worth stealing from their write-up: repeated images hit the multimodal cache and skip the vision tower entirely, so any image benchmark needs images the instance has never seen. The text-side twin of that trap (measured on this box): llama.cpp with a separate `-hfd` draft model keeps the draft's own KV cache, and a repeated identical prompt replays at chunked-verify speed — 203 tok/s on a prompt whose true cold rate was 25. SGLang+DSpark did not show this effect in testing (repeats reproduce within ±0.2 tok/s), but fresh prompts are the only safe protocol for any speculative bench.

## ⚠️ The GB10 unified-memory trap (read this before changing anything)

SGLang's memory accounting **does not see 25–40 GB** of transient allocations on GB10 unified memory (the flashinfer fp8 autotuner and CUDA graph capture allocate outside the tracked pool). Running `--mem-fraction-static` above **0.50**, or running SGLang natively (outside Docker), can drive host available memory to **zero** — on a machine where SSH often rides on the same memory, that means a hard freeze only a power cycle fixes. We learned this the hard way.

This repo's service is safe by construction:

- Docker hard caps: `--memory 100g --memory-swap 100g` (a runaway kills the container, never the host — note the cgroup does *not* see CUDA unified allocations, so the real guard is the fraction)
- `--mem-fraction-static 0.50` (plenty for 262K context at batch ≤ 4)
- `Restart=on-failure` + a clean `ExecStartPre docker rm -f` so even a power cut leaves nothing stale

## Claude Code integration

The installer writes a ready-to-source env file at `~/.config/qwen38/claude-code.env`:

```bash
source ~/.config/qwen38/claude-code.env && claude --model qwen3.8-27b
```

Three integration bugs are already fixed for you:

1. **`reasoning_effort` 500s** — Claude Code sessions set to *max* effort send `reasoning_effort: "max"`, which the stock chat template rejects (only `xhigh/medium/low`). The installer patches the template to map `max`/`high` → `xhigh` (the actual ceiling — no behavior change).
2. **Mid-conversation system messages** — Claude Code injects system-reminders after turn 1; the stock template raises `System message must be at the beginning`. Patched to render them as `<system-reminder>` blocks (their exact semantics).
3. **5-minute stream aborts** — on a custom `ANTHROPIC_BASE_URL`, Claude Code arms 300 s stream-idle watchdogs; a cold 36K-token prefill or a queued request can trip them while the server is still working. The env file raises them to their 30-minute maximum (`CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS`, `CLAUDE_STREAM_IDLE_TIMEOUT_MS`) plus `API_TIMEOUT_MS=3600000`.

Also: SGLang's `--api-key` only accepts `Authorization: Bearer` (Claude Code's `ANTHROPIC_AUTH_TOKEN`), **not** `x-api-key`. Another 90 %-slowdown killer, `CLAUDE_CODE_ATTRIBUTION_HEADER`, is disabled in the env file per [Unsloth's guide](https://unsloth.ai/docs/basics/claude-code).

## Operations

```bash
systemctl status qwen38-sglang          # state
sudo systemctl restart qwen38-sglang    # ~9 min boot; radix cache is wiped, warmup (if installed) re-heats it
journalctl -u qwen38-sglang -f          # logs
./bench.sh                              # re-measure
./uninstall.sh                          # removes service + config (keeps downloaded models)
```

Notes: the server's own `watchdog_timeout=300` is a *hang* detector (kills a genuinely stuck forward so systemd restarts it) — it does not limit generation length. Two concurrent generations share the memory bus (~half speed each): the GB10 is a batch-1-per-moment machine.

## Credits

All the heavy lifting belongs to the [SGLang](https://github.com/sgl-project/sglang) team (day-0 Qwen3.8 support, the DSpark implementation, the `lmsysorg/sglang:qwen38-27b` image), [RadixArk](https://huggingface.co/RadixArk) for the NVFP4 + DSpark checkpoints, [DeepSeek](https://arxiv.org/abs/2607.05147) for the DSpark method, [Qwen](https://huggingface.co/Qwen/Qwen3.8-27B) for the model, and [Unsloth](https://unsloth.ai/docs/models/qwen3.8) for their guides. This repo just packages a validated, hardened configuration of their work for GB10 machines — the SGLang cookbook's DGX Spark cell was marked "not yet validated" at the time; consider this an independent field validation (2026-08-15, image `lmsysorg/sglang:qwen38-27b`, digest `0076dffa60b7`).

## License

MIT — see [LICENSE](LICENSE). Performance numbers are point-in-time measurements on one machine; your acceptance lengths (and therefore tok/s) vary with workload and language.
