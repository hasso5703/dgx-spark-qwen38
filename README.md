# Qwen3.8-27B at 50 tok/s greedy median and 148+ tok/s aggregate on DGX Spark (GB10)

Most GB10 setups serve Qwen3.8-27B at 20-27 tok/s single-stream with boot-to-boot variance nobody controls for. This repo installs the fastest configuration measured so far, **SGLang + NVFP4 + DFlash2 speculative decoding with deterministic kernels**, as a one-command, boot-persistent service, with **zero quality loss** (same NVFP4 quantization floor; speculative decoding is lossless by construction, verified against a Q8 reference and live canaries). Measured on the reference box, thinking on: **50 tok/s greedy median on `./bench.sh` (code 41-47, reasoning 52-57, math peak 60)**, free prose 17-23 in any language (2-3x the stock drafter, its historical weak spot), and **135-148 tok/s aggregate at 8 concurrent streams, 258 tok/s at 32** (`--max-running-requests 32`). Per-workload profile on a frozen battery below. Reproducible to the decimal across boots: see BENCHMARKS.md, "The boot lottery".

You get an **OpenAI and Anthropic-compatible API** on port 30000. **Claude Code works out of the box**: three integration bugs are pre-fixed.

## Quickstart

Requirements: DGX Spark or other GB10 machine (128 GB unified), stock DGX OS (Docker + NVIDIA container toolkit), ~85 GB free disk.

One command, first install and updates alike (clones or updates `~/dgx-spark-qwen38`, then runs the pinned installer):

```bash
curl -fsSL https://raw.githubusercontent.com/hasso5703/dgx-spark-qwen38/main/get.sh | bash
```

Or the explicit way:

```bash
git clone https://github.com/hasso5703/dgx-spark-qwen38.git
cd dgx-spark-qwen38
./install.sh            # image + checkpoints + systemd service, starts at every boot
./bench.sh              # verify your tok/s
```

First boot takes **~7-9 minutes** (CUDA graph capture + kernel compilation, cached afterwards; later boots ~5-7 min). Then:

- **Claude Code**: `source ~/.config/qwen38/claude-code.env && claude --model qwen3.8-27b`
- **Any OpenAI client**: `http://<host>:30000/v1/chat/completions`, model `qwen3.8-27b`, Bearer key from `~/.config/qwen38/api-key`
- **Anthropic protocol** (Claude Code & co): `http://<host>:30000/v1/messages` (`Authorization: Bearer` only, not `x-api-key`)
- **Don't want a systemd service?** `./install.sh --no-service && ./run.sh`: same config, foreground, no sudo, Ctrl+C and it's gone.
- Everything is **pinned** (base image digest + checkpoint revisions + five sha256-verified DFlash2 overlay files, see `dflash2/ATTRIBUTION.md`) so it still works months from now; the installer is idempotent and every failure path says how to fix itself. `MODEL_REV=main ./install.sh` overrides the pins; `git checkout v1.1 && ./install.sh` returns to the DSpark config.

## What speed to expect

Speculative decoding accepts *predictable* tokens, so speed depends on **what the model generates**, not on one magic number:

Two instruments, both in the box, both reproducible. The headline **50 tok/s greedy median** is `./bench.sh` (streaming decode rate net of TTFT, the repo's historical headline instrument: v1.0-v1.1 measured ~36-40 on it, v1.2 measures 41-57 per probe). The table below is the harsher one: the frozen battery `./bench-matrix.sh` (two-call wall-clock delta, comparable across engines and boxes):

| What you generate (thinking on, battery v1) | v1.2 (DFlash2, this repo) | v1.1 (DSpark) | Stable-MTP engines |
|---|---|---|---|
| Agentic coding (code, diffs, tool calls) | **32-40 tok/s** | 28-36 | 24-28 |
| Math & structured reasoning | **41-44** | 38-42 | 24-33 |
| Technical explanations (FR) | **26** | 23-25 | ~22 |
| Free-form prose EN / FR / DE | **22 / 20 / 17** | 17 / 14 / 13 | 17-20 |
| **8 concurrent streams, aggregate** | **135-148** | 100-104 | ~92 |
| **32 concurrent streams, aggregate** | **258** | not measured | not measured |

v1.2 wins every row of the frozen battery except eval-style math (parity with stock), including free prose, historically the weak spot of block drafters. Every number above is deterministic across boots (`--disable-flashinfer-autotune`, see BENCHMARKS.md "The boot lottery") and was re-verified after a full machine reboot, with output-quality canaries passing. This machine serves its own Claude Code sessions with this exact repo, unmodified: if something breaks, it breaks here first.

**Quality, measured (not claimed).** Same box, v1.2.1, thinking on:

| Quality check | Result |
|---|---|
| GSM8K, 200 problems | **94.0%** (188/200), exact parity with the DSpark profile |
| IFEval, 200 prompts | **81.4%** prompt-level / **87.4%** instruction-level |
| tool-eval-bench, 69 scenarios | **91/100** (Excellent), reproducible to the point across seeds |
| Independent users on this config | **92-94/100** tool-calling ([forum thread](https://forums.developer.nvidia.com/t/380257)) |
| Losslessness | token-identity study vs the pure model in BENCHMARKS.md ("The losslessness study") |


Full study (methodology, engine-vs-engine matrix, an independent reproduction, the physics of the GB10 ceiling, and a frozen benchmark battery you can run against **any** engine, `./bench-matrix.sh`): in **[BENCHMARKS.md](BENCHMARKS.md)**.

## ⚠️ The GB10 unified-memory trap (read this before changing anything)

SGLang's memory accounting **does not see 25-40 GB** of transient allocations on GB10 unified memory (the flashinfer fp8 autotuner and CUDA graph capture allocate outside the tracked pool). Running `--mem-fraction-static` above **0.50**, or running SGLang natively (outside Docker), can drive host available memory to **zero**: on a machine where SSH often rides on the same memory, that means a hard freeze only a power cycle fixes. We learned this the hard way.

This repo's service is safe by construction:

- Docker hard caps: `--memory 100g --memory-swap 100g` (a runaway kills the container, never the host; note the cgroup does *not* see CUDA unified allocations, so the real guard is the fraction)
- `--mem-fraction-static 0.50` (plenty for 262K context at batch ≤ 4)
- `Restart=on-failure` + a clean `ExecStartPre docker rm -f` so even a power cut leaves nothing stale

## Claude Code integration

The installer writes a ready-to-source env file at `~/.config/qwen38/claude-code.env`:

```bash
source ~/.config/qwen38/claude-code.env && claude --model qwen3.8-27b
```

Four integration bugs are already fixed for you:

1. **`reasoning_effort` 500s**: Claude Code sessions set to *max* effort send `reasoning_effort: "max"`, which the stock chat template rejects (only `xhigh/medium/low`). The installer patches the template to map `max`/`high` → `xhigh` (the actual ceiling, no behavior change) and `minimal` → `low` (an OpenAI tier, [contributed by helge](https://forums.developer.nvidia.com/t/380257/10)). This is not a Claude Code quirk: any OpenAI-compatible client sending a `reasoning_effort` outside `xhigh/medium/low` gets the same 400.
2. **Mid-conversation system messages**: Claude Code injects system-reminders after turn 1; the stock template raises `System message must be at the beginning`. Patched to render them as `<system-reminder>` blocks (their exact semantics).
3. **5-minute stream aborts**: on a custom `ANTHROPIC_BASE_URL`, Claude Code arms 300 s stream-idle watchdogs; a cold 36K-token prefill or a queued request can trip them while the server is still working. The env file raises them to their 30-minute maximum (`CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS`, `CLAUDE_STREAM_IDLE_TIMEOUT_MS`) plus `API_TIMEOUT_MS=3600000`.
4. **Truncated long answers, and a long-context dead zone**: Claude Code sends `max_tokens: 32000` per request by default, and reasoning tokens count against that budget, so long turns ended mid-sentence. The env file raises it to the real ceiling: `CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000`. For a third-party model id, any higher value (129000, 258048, ...) is silently capped back to 128000, verified by request capture on Claude Code 2.1.238. The context declaration is the subtle half of the pair: the server rejects any request where `input + max_tokens > 262144` (a 400, no clamping), Claude Code never shrinks `max_tokens` to fit (verified by capture up to ~200K input tokens), and its auto-compaction reserves at most 20000 output tokens no matter what you set. So the two variables must satisfy `CONTEXT + OUTPUT <= 258048` (262144 minus a 4096 margin: client-side token counting is approximate, see [#2](https://github.com/hasso5703/dgx-spark-qwen38/issues/2)), otherwise every request landing between `262144 - OUTPUT` input tokens and the auto-compaction threshold gets a 400 before compaction can rescue it (the previous 64000/258048 pair had exactly that dead zone from ~198K on). The env file ships `CLAUDE_CODE_MAX_CONTEXT_TOKENS=130048` (262144 - 128000 - 4096); if you prefer longer context over very long single answers, use `OUTPUT=64000` with `CONTEXT=194048`.

Also: SGLang's `--api-key` only accepts `Authorization: Bearer` (Claude Code's `ANTHROPIC_AUTH_TOKEN`), **not** `x-api-key`. Another 90 %-slowdown killer, `CLAUDE_CODE_ATTRIBUTION_HEADER`, is disabled in the env file per [Unsloth's guide](https://unsloth.ai/docs/basics/claude-code).

## Operations

```bash
systemctl status qwen38-sglang          # state
sudo systemctl restart qwen38-sglang    # ~5-7 min boot; radix cache is wiped, warmup (if installed) re-heats it
journalctl -u qwen38-sglang -f          # logs
./bench.sh                              # re-measure this config
./bench-matrix.sh                       # per-workload profile, works on any engine
./uninstall.sh                          # removes service + config (keeps downloaded models)
```

Do **not** set `HF_HUB_OFFLINE=1`: SGLang probes for a LongCat config that doesn't exist in these repos (`srt/utils/hf_transformers/config.py`, `_try_load_longcat_config`) and offline mode turns that harmless miss into a hard `LocalEntryNotFoundError` at startup ([reported by helge](https://forums.developer.nvidia.com/t/380257/10); the function is verified present in the pinned image). With the pinned revisions cached, that metadata probe is the only network call.

Notes: the server's own `watchdog_timeout=300` is a *hang* detector (kills a genuinely stuck forward so systemd restarts it); it does not limit generation length. Two concurrent generations share the memory bus (~half speed each): the GB10 is a batch-1-per-moment machine.

## Upgrading from an earlier version

```bash
cd dgx-spark-qwen38 && git pull && ./install.sh
```

Your API key, patched template, and any systemd drop-ins under
`/etc/systemd/system/qwen38-sglang.service.d/` are kept; the unit is rewritten and the service
restarts on the new config. v1.1 → v1.2 downloads the ~4 GB DFlash2 draft and builds the serving
image locally (~1 min, offline, sha256-verified, see `dflash2/ATTRIBUTION.md`). To return to
the DSpark config: `git checkout v1.1 && ./install.sh`. Change history: [CHANGELOG.md](CHANGELOG.md).

## Credits

All the heavy lifting belongs to the [SGLang](https://github.com/sgl-project/sglang) team (day-0 Qwen3.8 support, the DSPARK and DFLASH implementations, the `lmsysorg/sglang:qwen38-27b` image), [z-lab / Inco AI](https://huggingface.co/z-lab/Qwen3.8-27B-DFlash2) for the DFlash2 drafter, [MiaAI-Lab](https://github.com/MiaAI-Lab/Qwen3.8-27B-SGLang-DGX-Spark) for the quantized-lm_head fix that makes DFlash2 safe on GB10, [r0b0tlab](https://github.com/r0b0tlab/qwen38-27b-nvfp4-sm121-sglang) for the draft-block sweep, [RadixArk](https://huggingface.co/RadixArk) for the NVFP4 + DSpark checkpoints, [DeepSeek](https://arxiv.org/abs/2607.05147) for the DSpark method, [Qwen](https://huggingface.co/Qwen/Qwen3.8-27B) for the model, and [Unsloth](https://unsloth.ai/docs/models/qwen3.8) for their guides. This repo just packages a validated, hardened configuration of their work for GB10 machines. The SGLang cookbook's DGX Spark cell was marked "not yet validated" at the time; consider this an independent field validation (2026-08-15).

## License

MIT, see [LICENSE](LICENSE). Performance numbers are point-in-time measurements on one machine; your acceptance lengths (and therefore tok/s) vary with workload and language, see [BENCHMARKS.md](BENCHMARKS.md).
