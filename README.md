# Qwen3.8-27B at 30–40 tok/s on DGX Spark (GB10)

Most GB10 setups serve Qwen3.8-27B at 20–27 tok/s. This repo installs the fastest configuration measured so far — **SGLang + NVFP4 + DSpark speculative decoding** — as a one-command, boot-persistent service, with **zero quality loss** (same NVFP4 quantization floor; speculative decoding is lossless by construction, verified against a Q8 reference).

You get an **OpenAI and Anthropic-compatible API** on port 30000. **Claude Code works out of the box** — three integration bugs are pre-fixed.

## Quickstart

Requirements: DGX Spark or other GB10 machine (128 GB unified), stock DGX OS (Docker + NVIDIA container toolkit), ~85 GB free disk.

```bash
git clone https://github.com/hasso5703/dgx-spark-qwen38.git
cd dgx-spark-qwen38
./install.sh            # image + checkpoints + systemd service, starts at every boot
./bench.sh              # verify your tok/s
```

First boot takes **~9 minutes** (kernel compilation, cached afterwards). Then:

- **Claude Code**: `source ~/.config/qwen38/claude-code.env && claude --model qwen3.8-27b`
- **Any OpenAI client**: `http://<host>:30000/v1/chat/completions`, model `qwen3.8-27b`, Bearer key from `~/.config/qwen38/api-key`
- **Anthropic protocol** (Claude Code & co): `http://<host>:30000/v1/messages` — `Authorization: Bearer` only, not `x-api-key`
- **Don't want a systemd service?** `./install.sh --no-service && ./run.sh` — same config, foreground, no sudo, Ctrl+C and it's gone.
- Everything is **pinned** (image digest + checkpoint revisions validated 2026-08-15) so it still works months from now; the installer is idempotent and every failure path says how to fix itself. `IMAGE=… MODEL_REV=main ./install.sh` overrides the pins.

## What speed to expect

Speculative decoding accepts *predictable* tokens, so speed depends on **what the model generates** — not on one magic number:

| What you generate | This config | Stable-MTP engines (llama.cpp / vLLM) |
|---|---|---|
| Agentic coding — code, diffs, tool calls | **28–40 tok/s** | 24–28 |
| Math & structured reasoning | **31–47** | 24–30 |
| Technical explanations | 20–23 | ~22 |
| Free-form prose (any language) | 12–16 | **17–18** |

If you're here for coding agents (Claude Code, agentic workflows), this config wins every relevant row, plus ~3× faster prefill and the best time-to-first-token. It also serves **8 truly concurrent streams (~94–109 tok/s aggregate measured)** — see BENCHMARKS.md. If you mostly generate free prose, llama.cpp+MTP is ~40 % faster on that one workload.

Full study — methodology, engine-vs-engine matrix, an independent reproduction, the physics of the GB10 ceiling, and a frozen benchmark battery you can run against **any** engine (`./bench-matrix.sh`) — in **[BENCHMARKS.md](BENCHMARKS.md)**.

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

Four integration bugs are already fixed for you:

1. **`reasoning_effort` 500s** — Claude Code sessions set to *max* effort send `reasoning_effort: "max"`, which the stock chat template rejects (only `xhigh/medium/low`). The installer patches the template to map `max`/`high` → `xhigh` (the actual ceiling — no behavior change) and `minimal` → `low` (an OpenAI tier, [contributed by helge](https://forums.developer.nvidia.com/t/380257/10)). This is not a Claude Code quirk: any OpenAI-compatible client sending a `reasoning_effort` outside `xhigh/medium/low` gets the same 400.
2. **Mid-conversation system messages** — Claude Code injects system-reminders after turn 1; the stock template raises `System message must be at the beginning`. Patched to render them as `<system-reminder>` blocks (their exact semantics).
3. **5-minute stream aborts** — on a custom `ANTHROPIC_BASE_URL`, Claude Code arms 300 s stream-idle watchdogs; a cold 36K-token prefill or a queued request can trip them while the server is still working. The env file raises them to their 30-minute maximum (`CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS`, `CLAUDE_STREAM_IDLE_TIMEOUT_MS`) plus `API_TIMEOUT_MS=3600000`.
4. **Truncated long answers** — Claude Code sends `max_tokens: 32000` per request by default (verified by request capture on Claude Code 2.1.235), and reasoning tokens count against that budget, so long turns ended mid-sentence. The env file raises it with `CLAUDE_CODE_MAX_OUTPUT_TOKENS=64000`. It also declares the context window with a safety margin (`CLAUDE_CODE_MAX_CONTEXT_TOKENS=258048`, i.e. 262144 − 4096) so auto-compaction fires *before* the server's hard limit — client-side token counting is approximate (see [#2](https://github.com/hasso5703/dgx-spark-qwen38/issues/2)).

Also: SGLang's `--api-key` only accepts `Authorization: Bearer` (Claude Code's `ANTHROPIC_AUTH_TOKEN`), **not** `x-api-key`. Another 90 %-slowdown killer, `CLAUDE_CODE_ATTRIBUTION_HEADER`, is disabled in the env file per [Unsloth's guide](https://unsloth.ai/docs/basics/claude-code).

## Operations

```bash
systemctl status qwen38-sglang          # state
sudo systemctl restart qwen38-sglang    # ~9 min boot; radix cache is wiped, warmup (if installed) re-heats it
journalctl -u qwen38-sglang -f          # logs
./bench.sh                              # re-measure this config
./bench-matrix.sh                       # per-workload profile, works on any engine
./uninstall.sh                          # removes service + config (keeps downloaded models)
```

Do **not** set `HF_HUB_OFFLINE=1`: SGLang probes for a LongCat config that doesn't exist in these repos (`srt/utils/hf_transformers/config.py`, `_try_load_longcat_config`) and offline mode turns that harmless miss into a hard `LocalEntryNotFoundError` at startup ([reported by helge](https://forums.developer.nvidia.com/t/380257/10); the function is verified present in the pinned image). With the pinned revisions cached, that metadata probe is the only network call.

Notes: the server's own `watchdog_timeout=300` is a *hang* detector (kills a genuinely stuck forward so systemd restarts it) — it does not limit generation length. Two concurrent generations share the memory bus (~half speed each): the GB10 is a batch-1-per-moment machine.

## Credits

All the heavy lifting belongs to the [SGLang](https://github.com/sgl-project/sglang) team (day-0 Qwen3.8 support, the DSpark implementation, the `lmsysorg/sglang:qwen38-27b` image), [RadixArk](https://huggingface.co/RadixArk) for the NVFP4 + DSpark checkpoints, [DeepSeek](https://arxiv.org/abs/2607.05147) for the DSpark method, [Qwen](https://huggingface.co/Qwen/Qwen3.8-27B) for the model, and [Unsloth](https://unsloth.ai/docs/models/qwen3.8) for their guides. This repo just packages a validated, hardened configuration of their work for GB10 machines — the SGLang cookbook's DGX Spark cell was marked "not yet validated" at the time; consider this an independent field validation (2026-08-15).

## License

MIT — see [LICENSE](LICENSE). Performance numbers are point-in-time measurements on one machine; your acceptance lengths (and therefore tok/s) vary with workload and language — see [BENCHMARKS.md](BENCHMARKS.md).
