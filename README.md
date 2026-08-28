# Qwen3.8 on DGX Spark (GB10): 27B at 50 tok/s, Flash-Next 176B on one box

One command installs a boot-persistent, hardened serving stack for the Qwen3.8 family on a single DGX Spark, with **three switchable targets** and **zero quality loss** on each (NVFP4 quantization floor everywhere; every speculative path is lossless by construction):

| target | model | engine | headline (measured here) |
|---|---|---|---|
| `stock` (default) | Qwen3.8-27B NVFP4 | SGLang + DFlash2 | **50 tok/s** greedy median, 148+ aggregate at 8 streams, optional 1M context |
| `uncensored` | Qwen3.8-27B abliterated NVFP4 | SGLang + DFlash2 | same speed and serving path as stock |
| `flash` | **Qwen3.8-Flash-Next 176B** hybrid MoE NVFP4 | SGLang + NEXTN | **34-42 tok/s decode, working prefix caching (30K re-serve: 0.5 s), vision**, 262K on ONE box |

The 27B path is the fastest configuration measured so far on GB10 (**SGLang + NVFP4 + DFlash2 speculative decoding with deterministic kernels**): **50 tok/s greedy median on `./bench.sh` (code 41-47, reasoning 52-57, math peak 60)**, free prose 17-23 in any language, **135-148 tok/s aggregate at 8 concurrent streams, 258 at 32**. Reproducible to the decimal across boots: see BENCHMARKS.md, "The boot lottery".

The flash path serves a model that does not otherwise fit: the 176B checkpoint's 51B N-gram table is **mmap-served from NVMe through the page cache** (a two-file, sha256-verified overlay on the official SGLang image; see `flash-sglang/ATTRIBUTION.md`), leaving the unified pool to the compute weights and a real 262K KV cache. Since v1.5 the lane runs on SGLang: **prefix caching works** (a 30K-token conversation is re-served in 0.5 s instead of 18.4 s, x36; an agent turn with a fresh question on a known prefix lands in ~3 s), decode is 34-42 tok/s with the model's own NEXTN/MTP head, prefill ~1,500-2,000 tok/s cold, and image input stays available.

Whatever the target, you get the same surface: an **OpenAI-compatible API** on port 30000 (both lanes also speak the Anthropic protocol), a keepalive proxy for agent CLIs on 30001, and **[opencode](https://opencode.ai) works out of the box** (the installer writes a ready-to-use provider config; the chat template ships pre-patched for agentic clients). The stack is built to grow: more targets, engines and drafters will slot into the same switch surface.

## Quickstart

Requirements: DGX Spark or other GB10 machine (128 GB unified), stock DGX OS (Docker + NVIDIA container toolkit). Free disk: **~90 GB** for a 27B target (~45 under `$HOME` for checkpoints and caches, ~45 on the Docker partition for the 39 GB image; keeping both 27B targets cached adds ~22 GB), **~230 GB** for the flash target (~195 under `$HOME`: the ~136 GB checkpoint plus a ~48 GB mmap backing file for the N-gram table, written once at first boot; ~35 on the Docker partition for the 30 GB image).

One command, first install and updates alike (clones or updates `~/dgx-spark-qwen38`, then runs the pinned installer):

```bash
curl -fsSL https://raw.githubusercontent.com/hasso5703/dgx-spark-qwen38/main/get.sh | bash
```

Options ride on the **bash side** of the pipe (an env prefix on `curl` would not reach the installer):

```bash
curl -fsSL https://raw.githubusercontent.com/hasso5703/dgx-spark-qwen38/main/get.sh | MODEL_CHOICE=uncensored CONTEXT_MODE=1m bash
curl -fsSL https://raw.githubusercontent.com/hasso5703/dgx-spark-qwen38/main/get.sh | MODEL_CHOICE=flash bash
```

Or the explicit way:

```bash
git clone https://github.com/hasso5703/dgx-spark-qwen38.git
cd dgx-spark-qwen38
./install.sh            # image + checkpoints + systemd service, starts at every boot
./bench.sh              # verify your tok/s
```

First boot takes **~7-9 minutes** for a 27B target (CUDA graph capture + kernel compilation, cached afterwards; later boots ~5-7 min) and **~15 minutes** for flash (the first boot also writes the 48 GB PLE backing file; later boots ~10 min). Then:

- **opencode**: ready config at `~/.config/qwen38/opencode.json`, see "opencode integration" below
- **Any OpenAI client**: `http://<host>:30000/v1/chat/completions`, model `qwen3.8-27b` (flash: `qwen3.8-flash-next`), Bearer key from `~/.config/qwen38/api-key`
- **Anthropic protocol**: `http://<host>:30000/v1/messages` (`Authorization: Bearer` only, not `x-api-key`)
- **Don't want a systemd service?** `./install.sh --no-service && ./run.sh`: same config, foreground, no sudo, Ctrl+C and it's gone (27B targets; flash is service-only in this release).
- Everything is **pinned twice** (base image digest + checkpoint revisions at download, and the same `--revision` passed to the server itself, so an upstream push to a checkpoint repo can never change what you serve; plus sha256-verified overlay files: five for DFlash2, `dflash2/ATTRIBUTION.md`, two for flash, `flash-sglang/ATTRIBUTION.md`). It still works months from now; the installer is idempotent and every failure path says how to fix itself. `MODEL_REV=main ./install.sh` overrides the pins; `git checkout v1.1 && ./install.sh` returns to the DSpark config.
- Since 2026-08-21, this same combination (DFLASH2, draft block 8) is the **official recipe in the [SGLang cookbook](https://docs.sglang.io/cookbook/autoregressive/Qwen/Qwen3.8-27B)**. No official image ships it yet (the cookbook has you build from source at a pinned commit), so this repo's prebuilt overlay stays the no-build path until one does.

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

v1.2 wins every row of the frozen battery except eval-style math (parity with stock), including free prose, historically the weak spot of block drafters. Every number above is deterministic across boots (`--disable-flashinfer-autotune`, see BENCHMARKS.md "The boot lottery") and was re-verified after a full machine reboot, with output-quality canaries passing. This machine serves its own opencode sessions daily on this config (stretched to the 1M preset from the field report below): if something breaks, it breaks here first.

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
- `Restart=always` + a clean `ExecStartPre docker rm -f` so even a power cut leaves nothing stale (`always` and not `on-failure`: a Triton compile crash measured on 2026-08-22 ended in `SystemExit: 0`, which `on-failure` never relaunches)

The 1m mode deliberately runs **0.70** inside the same docker caps, with the autotuner
disabled: field-tested continuously on the reference box (~17 GiB host headroom). **0.80 was
measured crashing** under 3 concurrent requests (2 GiB free, Triton `CUDA operation not
permitted`), and the 25-40 GB invisible-allocation bursts above all belong to native runs and
the autotuner. Treat anything past 0.70 as livelock territory.

## opencode integration

The installer writes a complete, ready-to-use [opencode](https://opencode.ai) config at `~/.config/qwen38/opencode.json` (the API key is referenced via `{file:...}`, no secret inside). It contains one provider per installed engine (`qwen38` for the 27B pair, `flashnext` for flash), each with `low` / `medium` / `xhigh` reasoning-effort variants (no variant = the template's own default, xhigh), and its default model follows the installed target (`./switch-model.sh` re-points it on every switch):

```bash
# no opencode config yet? use it as-is:
mkdir -p ~/.config/opencode && cp ~/.config/qwen38/opencode.json ~/.config/opencode/opencode.json
# already have one? merge the "qwen38" (and/or "flashnext") provider block into it
opencode
```

What the shipped config gets right for you:

1. **The hidden 32K output cap**: opencode sends `max_tokens = min(limit.output, OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX or 32000)`. Without that env var, a long thinking phase hits 32000 tokens, the turn ends silently (`finish_reason: length`, no text, no tool call) and you have to re-prompt. The installer ships an **`oc` launcher** (`~/.local/bin/oc`, skipped if an unrelated `oc` binary exists) that exports the right value and execs `opencode --yolo`: launch with `oc` instead of `opencode` and the cap matches the declared output limit in either context mode. Note that `--yolo` auto-approves every tool action (how the reference box runs); remove it from the launcher file if you prefer per-action prompts.
2. **Limits that can never 400**: the server rejects any request where `input + max_tokens` exceeds the window (no clamping), so the config ships `context/input 194048, output 64000` in native mode (258048 worst case, a 4096 margin under 262144, whether the 32K cap is lifted or not) and `700000/200000` in 1m mode (worst case 880000, under the worst measured KV pool).
3. **Reasoning-effort variants**: the generated config declares `medium` and `low` variants (ctrl+t in the TUI); the default is the model's `xhigh`. This works because the patched template accepts and maps effort tiers (`max`/`high` → `xhigh`, `minimal` → `low`, [contributed by helge](https://forums.developer.nvidia.com/t/380257/10)); any client sending an unmapped tier would get a 500 on the stock template.
4. **Mid-conversation system messages**: some agent clients inject system messages after turn 1; the stock template raises `System message must be at the beginning`. Patched to render them as `<system-reminder>` blocks.
5. **Vision declared**: `attachment` + `modalities` are set, so image attachments and on-disk image reads work end to end (the model is natively multimodal).

On service installs the generated config points at the **keepalive proxy port** (`PORT+1`), not the server directly, and that is deliberate: SGLang buffers tool-call arguments while they stream (127 s of measured silence on one 400-line file write, at native context), and opencode drops a stream after roughly 140-180 s without a real chunk. The proxy (`qwen38-keepalive.service`, vendored `keepalive-proxy.py`) fills those silences with protocol-correct keepalives, at SSE event boundaries only, and aborts the generation server-side the moment the client disconnects (no zombie generations). With `./install.sh --no-service` there is no proxy: the config then points at the server directly, and huge single-file writes may abort. One more caveat, measured: SGLang's `--api-key` only accepts `Authorization: Bearer`, **not** `x-api-key`.

## The 1M context mode

```bash
CONTEXT_MODE=1m ./install.sh        # combines freely with MODEL_CHOICE=uncensored
# one-liner: curl -fsSL .../get.sh | CONTEXT_MODE=1m bash
```

This installs, as one converging command, the exact preset that serves the reference box
daily since 2026-08-22:

- **1,010,000-token window** via YaRN static scaling (factor 4.0,
  `original_max_position_embeddings: 262144`) patched into **both** cached `config.json`
  files by `patch-yarn.py` (target model AND DFlash2 draft, or the draft crashes at load;
  originals backed up next to them as `config.json.pre-yarn`), plus
  `--context-length 1010000` and `SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN=1`.
- **`--mem-fraction-static 0.70`**: 917K-1019K tokens of KV pool depending on the boot
  (a lottery; 5 measured boots), DFlash2 acceptance unchanged; a real 690K-token request
  has been served (cold prefill 40 min, then cached).
- **The keepalive proxy becomes load-bearing.** Every service install ships it (see
  "opencode integration"), but at 1M it is not optional: a cold 690K-token prefill can
  keep the wire silent for tens of minutes. The proxy injects the official Anthropic
  `ping` event on `/v1/messages` and an authentic empty chunk on the OpenAI dialect,
  every 10 s, only at SSE event boundaries (a keepalive inside an event corrupts the
  JSON, measured); it closes the upstream the moment the client disconnects, and
  reports an explicit SSE error after 3600 s of true upstream silence (above the worst
  legitimate prefill). **Agent clients must use the proxy port**; the direct server
  port stays for curl and benches.
- **`HF_HUB_OFFLINE=1`** in the unit, so no Hub metadata check can re-resolve a
  checkpoint and silently undo the YaRN-patched configs (see "Operations" below).
- **`Restart=always`**: a crash that exits 0 (a Triton compile crash measured 2026-08-22
  ended in `SystemExit: 0`) still gets relaunched; `on-failure` would not.
- The generated opencode config switches to `context/input 700000, output 200000`
  (compaction fires at 680000; worst case 880000, under the worst measured pool).

Quality past the native 262144 window is not formally evaluated here: treat it as an
experimental preset. Proof it holds up operationally, one continuous **opencode** session
(reasoning effort `xhigh`, output cap lifted) built a playable 3D zombie FPS from a single
prompt by YouTuber Bijan Bowen:

- **535,361 tokens** of context reached in one session, twice the native window, zero compaction
- **~360K tokens generated**, 239 agent steps, 274 tool calls, no retry, no manual rescue
- Result, single HTML file: **https://subway-fps.vercel.app**

Back to native: `CONTEXT_MODE=native ./install.sh` (removes the proxy service; the
`config.json.pre-yarn` backups let you undo the YaRN patches, though a 1010000
`max_position_embeddings` is harmless at native context length).

## The flash target: Qwen3.8-Flash-Next 176B on one Spark

Qwen's official validation environment for this model is a dual GB300 node;
on Sparks, the public recipes run it on **two** boxes (TP2). This target runs
it on **one**, at full 262K native context and full NVFP4 quality, on SGLang
(since v1.5; the same engine as the 27B pair), because of one structural trick
and two kernel-resolver fixes, all vendored, sha256-verified and pinned in
`flash-sglang/`:

- **The 51B N-gram (PLE) table never sits in RAM.** A three-line patch makes
  its backing store a file-backed mmap on NVMe (`torch.from_file`), with
  `madvise(MADV_RANDOM)` so a cold row costs one 4K page instead of a
  readahead window. On GB10's coherent memory the gather kernel dereferences
  the pageable pointer directly. Measured overhead: under 3%.
- **QSA decode unblocked on sm_121** (the resolver gates exclude GB10 upstream,
  landing decode on a kernel that does not compile there): +32% decode, and
  the same two edits fix the token-ID-0 tool-call loop tracked upstream
  ([#36537](https://github.com/sgl-project/sglang/issues/36537) /
  [PR #36556](https://github.com/sgl-project/sglang/pull/36556)).

Measured on the reference box (two-call wall-clock, quality canaries 4/4,
needle-in-haystack passing at 100K with fresh content):

| axis | measured |
|---|---|
| **prefix caching, 30K re-serve** | **18.4 s cold, 0.5 s cached (x36)** |
| **known 30K prefix + fresh question** | **~3 s (x5.8)**: the agentic turn shape |
| decode, reasoning | 34.2 tok/s (up to ~42 reported on short-context profiles) |
| decode, free prose | 20.3 tok/s |
| prefill, cold | ~1,500-2,000 tok/s (real QSA sparse kernels) |
| vision (image input) | works, including combined with large prompts |
| context | 262,144 native, no YaRN |
| memory | fraction 0.79 + docker cap 110g, ~23 GB host headroom |

The NEXTN speculative head is the model's own next-token module (its 31
tensors ship in the checkpoint in BF16, hence `unquant` for the draft): drafts
are verified by the target, so output quality is exactly the target's.

Known upstream behavior, reproduced here
([sglang #35537](https://github.com/sgl-project/sglang/issues/35537)): with
chunked prefill, while one request is decoding a long answer, NEW requests can
starve until it finishes, even with room in every pool. A single agent client
(opencode sends one request at a time) never notices; concurrent clients see
bursty latency. The keepalive proxy cleans up after dead clients either way. Prefix
caching is why this lane exists: an agent client resends the whole
conversation every turn, and the radix cache turns that from a full recompute
into an incremental one. Note `--max-running-requests 4` is the validated
shape, and the 48 GB mmap backing file (`PLE_DIR`, default `~/flashnext-ple`)
is written once at first boot.

## The three targets, and switching between them

| choice | checkpoint | revision | engine, unit |
|---|---|---|---|
| `stock` (default) | `RadixArk/Qwen3.8-27B-NVFP4` | `52d1adc` | SGLang, `qwen38-sglang` |
| `uncensored` | `edp1096/Huihui-RadixArk-Qwen3.8-27B-abliterated-NVFP4` | `21565d3` | SGLang, `qwen38-sglang` |
| `flash` | `RadixArk/Qwen3.8-Flash-Next-NVFP4` | `7b71922` | SGLang, `qwen38-flash` |

The uncensored target is huihui-ai's abliteration of Qwen3.8-27B re-quantized
with the identical RadixArk modelopt NVFP4 recipe (verified: same
`text_config`, same mixed 8-bit attention / 4-bit MLP quant groups, same
chat template, MTP + vision intact, ~22 GB). It refuses the least while keeping
the stock NVFP4 serving path.

The flash target is Qwen3.8-Flash-Next (Qwen4-generation preview: 176B hybrid
MoE, 6B active, QSA sparse attention, multimodal) in RadixArk's NVFP4, served
by SGLang with the model's own NEXTN/MTP speculative head and a working radix
(prefix) cache. Both units publish the same port and are never enabled
together: switching targets flips which unit starts at boot, the API surface
and the keepalive proxy stay put.

- Fresh install: `MODEL_CHOICE=uncensored ./install.sh` or
  `MODEL_CHOICE=flash ./install.sh` (one-liner: `curl -fsSL .../get.sh |
  MODEL_CHOICE=flash bash`). Upgrades keep the installed choice.
- Existing install, 27B pair (`stock` ↔ `uncensored`): `./switch-model.sh
  uncensored` (or `stock`), as many times as you like. It downloads the
  checkpoint (cached after the first time), applies the 1M YaRN config patch if
  the installed unit uses `--context-length 1010000`, regenerates the patched
  chat template from the target's own snapshot, rewrites **only** the
  `--model-path` line of `/etc/systemd/system/qwen38-sglang.service` and
  daemon-reloads.
- Existing install, across lanes (`flash` ↔ either 27B target): install each
  stack once (`MODEL_CHOICE=flash ./install.sh` downloads the image and
  checkpoint and builds the overlay); after that `./switch-model.sh flash` /
  `./switch-model.sh stock` is surgical too: it re-verifies the checkpoint,
  regenerates the target's template, flips which unit is enabled at boot, and
  points the opencode default model at the target.
- `switch-model.sh` never restarts a service itself: every switch takes effect
  on the next restart or reboot, and the script prints the exact stop/start
  commands for the engine pair it just queued.
- Speculation stays lossless with every target (DFlash2 drafts and MTP drafts
  are verified against the target model); only acceptance rates vary.

## Operations

```bash
systemctl status qwen38-sglang          # 27B server state (target stock/uncensored)
systemctl status qwen38-flash           # Flash-Next server state (target flash)
systemctl status qwen38-keepalive       # keepalive proxy state
sudo systemctl restart qwen38-sglang    # 27B: ~5-7 min boot; the radix (prefix) cache starts empty
sudo systemctl restart qwen38-flash     # flash: ~10 min boot (weight load + PLE prewarm)
journalctl -u qwen38-sglang -f          # server logs (qwen38-flash for the flash target)
journalctl -u qwen38-keepalive -f       # one line per proxied request (bytes, first/last event, outcome)
./bench.sh                              # re-measure this config
./bench-matrix.sh                       # per-workload profile, works on any engine
./uninstall.sh --list                   # inventory: everything any version of this repo left here, with sizes
./uninstall.sh                          # removes services + config; prints reclaim commands for data it found
```

**Killing an abandoned generation.** If a client dies mid-generation the server keeps
decoding for nothing (symptom: power draw and GPU busy with no active session). Behind
the keepalive proxy this heals itself: the proxy aborts the upstream the moment the
client disconnects. For direct connections (`./run.sh`, curl, custom clients), abort
everything in flight with:

```bash
curl -X POST -H "Authorization: Bearer $(cat ~/.config/qwen38/api-key)" \
  -H 'Content-Type: application/json' -d '{"abort_all": true}' http://127.0.0.1:30000/abort_request
```

The server keeps running; use it only when you know the in-flight work is abandoned,
because it aborts EVERY request currently decoding, yours included.

`HF_HUB_OFFLINE=1` is fine **once every pinned checkpoint is cached**: the 1m unit sets it on
purpose (it protects the YaRN-patched configs from any Hub re-resolution) and the reference
box serves that way across reboots; the LongCat metadata probe
(`srt/utils/hf_transformers/config.py`) reads from the cache, verified in the pinned image.
Do **not** set it on a first install or over an incomplete cache: the probe's harmless online
miss then becomes a hard `LocalEntryNotFoundError` at startup
([reported by helge](https://forums.developer.nvidia.com/t/380257/10)). With the pinned
revisions cached, that metadata probe is the only network call.

Notes: the server's own `watchdog_timeout=300` is a *hang* detector (kills a genuinely stuck forward so systemd restarts it); it does not limit generation length. Two concurrent generations share the memory bus (~half speed each): the GB10 is a batch-1-per-moment machine.

**Idle power**: without `--sleep-on-idle`, SGLang's scheduler busy-spins a full CPU core while doing nothing (reported as +10-12 W at the wall by [alef204 and emX0r](https://forums.developer.nvidia.com/t/380257/56), diagnosed in [MiaAI-Lab issue #4](https://github.com/MiaAI-Lab/Qwen3.8-27B-SGLang-DGX-Spark/issues/4)). The unit ships the flag since v1.2.6. A/B on the reference box: scheduler CPU 101 % -> 1.7 % at idle, module power 12.1 -> 10.5 W, and wake-up TTFT unchanged (0.234-0.240 s before, 0.234-0.239 s after, measured after 60 s and 300 s of idle), throughput in family (41.5 tok/s code, 52.8 math).

## Extras (opt-in)

Two field-tested pieces from the reference box, deliberately not part of the default
install because they touch things beyond the serving stack:

**`extras/opencode/auto-continue.js`**: an opencode plugin that automatically resumes a
session interrupted by a transient technical error (tool-call delta without id, timeout,
network reset) or left stuck right after a context compaction, so a one-off incident no
longer freezes an overnight run. It never resumes after a deliberate abort, a permission
prompt, or an auth/quota problem, and stops after 25 relaunches without progress. Install:

```bash
mkdir -p ~/.config/opencode/plugins && cp extras/opencode/auto-continue.js ~/.config/opencode/plugins/
```

Plugins load when opencode starts (a running session never picks it up). Log at
`~/.config/qwen38/auto-continue.log`; tune with `AC_THROTTLE_MS`, `AC_IDLE_DELAY_MS`,
`AC_MAX_CONSECUTIVE`, `AC_LOG`.

**`extras/cake-ingress/`**: ingress anti-bufferbloat. While a model download saturates
your link, the queue builds up inside the ISP box and everything else drowns (measured on
the reference box: 1 ms ping became a 4797 ms average and the tunnel in front of the API
answered 502). The fix shapes RECEIVED traffic just under your real link capacity with
CAKE, so the queue forms on the Spark where it is scheduled fairly; SSH and the API stay
at a few milliseconds while the download still runs at ~97 % speed. You must pass your
own measured downlink (never the NIC speed; the interface is auto-detected):

```bash
BANDWIDTH=950Mbit  extras/cake-ingress/setup.sh    # 1 Gb/s link (the reference box)
BANDWIDTH=475Mbit  extras/cake-ingress/setup.sh    # 500 Mb/s link
BANDWIDTH=2350Mbit extras/cake-ingress/setup.sh    # 2.5 Gb/s link
extras/cake-ingress/setup.sh --uninstall           # back to stock networking
```

Boot-persistent (`cake-ingress.service`). Verify with a `ping 1.1.1.1` kept running
during a big download. The full bandwidth sweep and the reasoning are in the script's
header; setting BANDWIDTH too high is the one mistake that silently does nothing.

## Upgrading from an earlier version

```bash
cd dgx-spark-qwen38 && git pull && ./install.sh
```

Your choices survive the upgrade: the API key, the patched template, your own systemd drop-ins
under `/etc/systemd/system/qwen38-sglang.service.d/`, and (since v1.3) the installed target
model, port and HF cache location, which are read from the installed unit (v1.4: units; a box
serving the flash target keeps it, exactly like a 27B choice). The unit itself is
rewritten on the repo's current flags (the previous one is backed up to
`~/.config/qwen38/<unit>.bak-preupdate`) and the service restarts on the new
config. v1.3 -> v1.4 changes nothing by itself for a 27B box: the flash stack is only
downloaded and installed when you ask for it (`MODEL_CHOICE=flash`), and the regenerated
`opencode.json` gains an `xhigh` reasoning-effort variant. v1.4 -> v1.5 moves the flash
lane from vLLM to SGLang (working prefix caching; the upgrade keeps your port, cache and
model choices and regenerates the launch script on the new engine; the first boot writes
the 48 GB PLE backing file). 27B boxes are again untouched. Upgrading from v1.2.x also removes the deprecated Claude Code warmup drop-in if you had
installed it, and no longer writes `claude-code.env`: an existing copy keeps working and will
never be overwritten again (earlier versions regenerated it on every install, losing any
customization), but it is unmaintained; the supported client config is `opencode.json`. v1.1 → v1.2 downloads the ~4 GB
DFlash2 draft and builds the serving image locally (~1 min, offline, sha256-verified, see
`dflash2/ATTRIBUTION.md`). To return to the DSpark config: `git checkout v1.1 && ./install.sh`.
Change history: [CHANGELOG.md](CHANGELOG.md).

## Credits

All the heavy lifting belongs to the [SGLang](https://github.com/sgl-project/sglang) team (day-0 Qwen3.8 support, the DSPARK and DFLASH implementations, the `lmsysorg/sglang:qwen38-27b` image), [z-lab / Inco AI](https://huggingface.co/z-lab/Qwen3.8-27B-DFlash2) for the DFlash2 drafter, [MiaAI-Lab](https://github.com/MiaAI-Lab/Qwen3.8-27B-SGLang-DGX-Spark) for the quantized-lm_head fix that makes DFlash2 safe on GB10, [r0b0tlab](https://github.com/r0b0tlab/qwen38-27b-nvfp4-sm121-sglang) for the draft-block sweep, [RadixArk](https://huggingface.co/RadixArk) for the NVFP4 + DSpark checkpoints, [DeepSeek](https://arxiv.org/abs/2607.05147) for the DSpark method, [Qwen](https://huggingface.co/Qwen/Qwen3.8-27B) for the model, and [Unsloth](https://unsloth.ai/docs/models/qwen3.8) for their guides. This repo just packages a validated, hardened configuration of their work for GB10 machines. The SGLang cookbook's DGX Spark cell was marked "not yet validated" at the time; consider this an independent field validation (2026-08-15). On 2026-08-21 the cookbook made DFlash2 the official recipe for this model (same algorithm, same draft block 8; its `incoai/Qwen3.8-27B-DFlash2` draft path is byte-identical to the z-lab checkpoint pinned here), with the DGX Spark cell marked "Final Verification In Progress": this repo's validation data is submitted upstream in [sgl-project/sglang#35860](https://github.com/sgl-project/sglang/issues/35860).

## License

MIT, see [LICENSE](LICENSE). Performance numbers are point-in-time measurements on one machine; your acceptance lengths (and therefore tok/s) vary with workload and language, see [BENCHMARKS.md](BENCHMARKS.md).
