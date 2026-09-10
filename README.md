# Qwen3.8 on DGX Spark (GB10): 27B at 50 tok/s, Flash-Next 176B on one box

One command installs a boot-persistent, hardened serving stack for the Qwen3.8 family on a single DGX Spark, with **seven switchable targets** and **zero quality loss** on each (NVFP4 is the quantization floor, Qwen's own FP8 is available above it; every speculative path is lossless by construction). Since v1.8 the flash lane serves an **official SGLang image** for this hardware with nothing added, serves **4 concurrent requests** where it used to serve one, and got **14 to 25% of its decode back from one flag** (`--speculative-token-map`, see below):

| target | model | engine | headline (measured here) |
|---|---|---|---|
| `stock` (default) | Qwen3.8-27B NVFP4 | SGLang + DFlash2 | **50 tok/s** greedy median, 148+ aggregate at 8 streams, optional 1M context |
| `uncensored` | Qwen3.8-27B abliterated NVFP4 | SGLang + DFlash2 | same speed and serving path as stock |
| `fp8` | Qwen3.8-27B FP8, Qwen's own release | SGLang + DFlash2 | the quantization reference: 108 tok/s aggregate at 8 streams, ~92K less KV pool |
| `uncensored-fp8` | Qwen3.8-27B abliterated FP8 | SGLang + DFlash2 | same serving path and same cost as `fp8` |
| `flash` (default of its lane) | **Qwen3.8-Flash-Next 176B** hybrid MoE NVFP4 | SGLang + NEXTN | **47.9 tok/s on code, 47.1 on math, 29-31 on prose, 27.0 ms/tok on an agent loop, prefix caching, vision**, 262K on ONE box |
| `flash-uncensored` | the **abliterated** build of that same tree | SGLang + NEXTN | 205 of 206 shards identical in size to stock, so the same flags: 45-46 on code, **0 refusals of 5** |
| `flash-nvda` | the same 176B from NVIDIA's mixed-precision export | SGLang + NEXTN | ties the stock export inside the spread, measured here |

The 27B path is the fastest configuration measured so far on GB10 (**SGLang + NVFP4 + DFlash2 speculative decoding with deterministic kernels**): **50 tok/s greedy median on `./bench.sh` (code 41-47, reasoning 52-57, math peak 60)**, free prose 17-23 in any language, **135-148 tok/s aggregate at 8 concurrent streams, 258 at 32**. Reproducible to the decimal across boots: see BENCHMARKS.md, "The boot lottery".

The flash path serves a model that does not otherwise fit: the 176B checkpoint's 47.7 GiB FP8 N-gram table is **served from a sparse file on NVMe**, read row by row by the gather kernel through GB10's host page tables, leaving the unified pool to the compute weights and a real KV cache. Until v1.7 that was a vendored patch of this repo's own; since v1.8 it is upstream (`--ple-offload-backend file`, [sglang#37068](https://github.com/sgl-project/sglang/pull/37068)) and the overlay is retired, along with the vendored sm_121 QSA kernel and the workaround for the GB10 MTP collapse. **Prefix caching works** (27k tokens re-served in 2.5 s against 12.0 s cold), decode is **47.9 tok/s on code and 47.1 on math** single stream (29-31 on prose), prefill ~2,250 tok/s cold, and image input stays available. Since v1.8 it also takes **`--speculative-token-map`**, which hands the speculative draft the target's `lm_head` sliced to 65,536 rows instead of all 248,320: that removes 2.6 GiB from every engine step on a lane that is memory-bandwidth bound, and it is worth 14 to 25% of decode without changing what the model can say, because the target still verifies every drafted token over the whole vocabulary.

Whatever the target, you get the same surface: an **OpenAI-compatible API** on port 30000 (both lanes also speak the Anthropic protocol), a keepalive proxy for agent CLIs on 30001, and **[opencode](https://opencode.ai) works out of the box** (the installer writes a ready-to-use provider config; the chat template ships pre-patched for agentic clients). The stack is built to grow: more targets, engines and drafters will slot into the same switch surface.

## Quickstart

Requirements: DGX Spark or other GB10 machine (128 GB unified), stock DGX OS (Docker + NVIDIA container toolkit). Free disk: **~84 GB** for a 27B target (~39 under `$HOME` for checkpoints and caches, ~45 on the Docker partition for the 39 GB image; caching the other 27B targets adds ~21 GB per NVFP4 target and ~31 GB per FP8 one), **~225 GB** for a flash target (~175 under `$HOME`: the ~126 GB checkpoint, ~124 for NVIDIA's export, plus the 47.7 GiB sparse file the N-gram table is served from and rewritten into on every boot; ~35 on the Docker partition for the 30 GB image).

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

First boot takes **~7-9 minutes** for a 27B target (CUDA graph capture + kernel compilation, cached afterwards; later boots ~5-7 min) and **~12-15 minutes** for a flash target, every boot: the server writes the whole 47.7 GiB N-gram table into its file each time (measured here: 12 min 21 s to `/health` on a fresh table). Then:

- **opencode**: ready config at `~/.config/qwen38/opencode.json`, see "opencode integration" below
- **Any OpenAI client**: `http://<host>:30000/v1/chat/completions`, model `qwen3.8-27b` (flash: `qwen3.8-flash-next`), Bearer key from `~/.config/qwen38/api-key`
- **Anthropic protocol**: `http://<host>:30000/v1/messages` (`Authorization: Bearer` only, not `x-api-key`)
- **Don't want a systemd service?** `./install.sh --no-service && ./run.sh`: same config, foreground, no sudo, Ctrl+C and it's gone (27B targets; flash is service-only in this release).
- Everything is **pinned twice** (base image digest + checkpoint revisions at download, and the same `--revision` passed to the server itself, so an upstream push to a checkpoint repo can never change what you serve; plus sha256-verified overlay files: five for DFlash2, `dflash2/ATTRIBUTION.md`, two for flash, `flash-sglang/ATTRIBUTION.md`). It still works months from now; the installer is idempotent and every failure path says how to fix itself. `MODEL_REV=main ./install.sh` overrides the pins; `git checkout v1.1 && ./install.sh` returns to the DSpark config.
- Since 2026-08-21, this same combination (DFLASH2, draft block 8) is the **official recipe in the [SGLang cookbook](https://docs.sglang.io/cookbook/autoregressive/Qwen/Qwen3.8-27B)**, and since 2026-08-22 there is an official multi-arch image that ships it (`lmsysorg/sglang:dev-qwen38-27b-dflash2`, built from `1cf2b8c`). It is **not** what this repo serves yet, for one reason: it was built on 2026-08-22 and the mrope fix this repo's 27B overlay carries ([sglang#34446](https://github.com/sgl-project/sglang/pull/34446), the fused Qwen3.5 RoPE kernel discarding mrope height and width) merged on 2026-08-30, so serving it as it stands rotates every image token as if it sat at its temporal position on all three axes. The flash lane, whose overlay is entirely upstream, does serve its official image since v1.8.
  **Where each lane stands against upstream, checked on 2026-09-10 against the GitHub API rather than against dates:** the flash lane serves `4ccff141d`, which **is** the head of the `qwen4-main-squashed` preview branch its features live on, so it is not behind anything; that branch is *diverged* from `main` (995 commits behind, 11 ahead), which is why no main-based image can serve this checkpoint at all: the file-backed PLE table 176B needs to fit one GB10 ([sglang#37068](https://github.com/sgl-project/sglang/pull/37068)) was merged into the preview branch, never into main. The 27B lane is the one with something to gain: the arm64 nightly `lmsysorg/sglang:nightly-cu134-20260909-708f51e` (main at `708f51e44`) contains **all four** of DFlash2 ([#35371](https://github.com/sgl-project/sglang/pull/35371)), the quantized-lm_head selector ([#35496](https://github.com/sgl-project/sglang/pull/35496)), the mrope fix that blocked the official image above ([#34446](https://github.com/sgl-project/sglang/pull/34446)) and the engine-side zombie fix ([#35255](https://github.com/sgl-project/sglang/pull/35255), the one the v1.8.4 proxy works around), each verified as an ancestor with 0 commits behind. The blocker is therefore gone upstream; the migration is not done here, because moving a 27B pin costs a full bench and quality cycle on this box and that has not been run yet. Until it is, the 27B lane keeps its pinned image and the proxy keeps holding the line on both lanes.

### Your choices, and how they combine

Everything below is optional and combinable. Variables ride on the `bash` side of the one-liner, flags go after `bash -s --`; with a clone, they go straight on `./install.sh`.

| Choice | How | Default | Notes |
|---|---|---|---|
| Model | `MODEL_CHOICE=stock`, `uncensored`, `fp8`, `uncensored-fp8`, `flash`, `flash-uncensored`, `flash-nvda` | `stock` | 27B NVFP4 stock or abliterated, the same pair in Qwen's FP8, or Flash-Next 176B in one of three NVFP4 exports (see the seven targets) |
| Reduced draft vocabulary | `SPEC_TOKEN_MAP_SIZE=65536`, or `0` to serve without it | `65536` | flash only: hands the speculative draft the target's `lm_head` sliced to that many rows, which is 14 to 25% of decode and cannot change what the model may say |
| Flash serving tier | `FLASH_TIER=context`, `concurrency`, `throughput` | `context` | flash only: 4 concurrent requests and a pool that takes a full 262K prompt, 8 requests at a third of the pool, or 24 without speculation |
| Context mode (27B) | `CONTEXT_MODE=native` or `1m` | `native` | `1m` = 1,010,000 window, mem-fraction 0.70, proxy required (see the 1M section) |
| systemd service | default, or `--no-service` | service | `--no-service`: foreground with `./run.sh`, no sudo, 27B native only |
| Start now | default, or `--no-start` | starts | install everything, start later with `sudo systemctl start` |
| opencode integration | default, or `--no-opencode` | on | on = ready config + `oc` launcher + default model following every switch; off = none of that, your own opencode config is never touched. `--with-opencode` turns it back on |
| Ports | `PORT=`, `PROXY_PORT=` | 30000, 30001 | agent clients use the proxy port |
| Storage | `HF_CACHE=`, `PLE_DIR=` | `~/.cache/huggingface`, `~/flashnext-ple` | checkpoints, and the 48 GB flash PLE backing file |
| Clone location | `DIR=` (one-liner only) | `~/dgx-spark-qwen38` | must be a clone of this repo on `main` |
| Cockpit dashboard | `dashboard/install-dashboard.sh`, `DASH_PORT=`, `DASH_BIND=` | not installed, loopback when installed | opt-in, never run by `install.sh`; installs a sudoers allowlist, see "The cockpit" |
| Agent tab (opencode in the cockpit) | `dashboard/install-agent.sh`, `AGENT_PORT=`, `AGENT_BIND=`, `OPENCODE_PORT=` | not installed; relay on the tailnet address when installed | opt-in, needs the cockpit and opencode on your PATH; opencode itself stays on loopback, see "The Agent tab" |

Combinations that make sense:

```bash
# one-liner forms (variables on the bash side, flags after "bash -s --")
curl -fsSL https://raw.githubusercontent.com/hasso5703/dgx-spark-qwen38/main/get.sh | bash                                  # 27B stock, native, service, opencode on
curl -fsSL https://raw.githubusercontent.com/hasso5703/dgx-spark-qwen38/main/get.sh | CONTEXT_MODE=1m bash                  # 27B stock, 1M context
curl -fsSL https://raw.githubusercontent.com/hasso5703/dgx-spark-qwen38/main/get.sh | MODEL_CHOICE=uncensored CONTEXT_MODE=1m bash
curl -fsSL https://raw.githubusercontent.com/hasso5703/dgx-spark-qwen38/main/get.sh | MODEL_CHOICE=flash bash               # Flash-Next lane (service only)
curl -fsSL https://raw.githubusercontent.com/hasso5703/dgx-spark-qwen38/main/get.sh | bash -s -- --no-opencode              # API only, no opencode files
# clone forms
./install.sh --no-service && ./run.sh                    # no systemd, foreground, Ctrl+C stops it
CONTEXT_MODE=1m ./install.sh --no-start                  # prepare the 1M unit, start it yourself later
./install.sh --with-opencode                             # turn the opencode integration back on
./switch-model.sh stock | uncensored | flash             # change model later, no reinstall (then stop/start the units it prints)
```

Re-running the installer (upgrades included) remembers what you chose: the installed model, the context mode, the port, the HF cache, and the opencode on/off choice. Pass the variable or flag again only to change something. `./uninstall.sh --list` shows everything the repo put on the box before removing anything.

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

**The SGLang cookbook pins 0.80 on DGX Spark, and that is not a contradiction.**
Its GB10 cells ran 48 configurations at ISL 8192 / OSL 1024, **concurrency 1**,
boot-and-serve only, and 0.80 served every cell on every attempt; it rejects 0.85
because 0.85 of 128 GB leaves about 8 GB for the OS, exactly DGX OS earlyoom's
SIGTERM threshold, and 15 of 48 cells were killed there (exit -15, no traceback,
visible in `journalctl -u earlyoom`). This repo's 0.80 failure was measured under
**three concurrent requests** on a box that also runs the operator's tools. One
number is a single-stream boot-and-serve bound, the other is a multi-client
operating point, and this repo optimises for the second. If you serve one stream
on a dedicated box, the cookbook's 0.80 is the better-evidenced pin.

## opencode integration

The installer writes a complete, ready-to-use [opencode](https://opencode.ai) config at `~/.config/qwen38/opencode.json` (the API key is referenced via `{file:...}`, no secret inside). It contains one provider per installed engine (`qwen38` for the 27B pair, `flashnext` for flash), each with `low` / `medium` / `xhigh` reasoning-effort variants (no variant = the template's own default, xhigh), and its default model follows the installed target (`./switch-model.sh` re-points it on every switch):

```bash
# no opencode config yet? use it as-is:
mkdir -p ~/.config/opencode && cp ~/.config/qwen38/opencode.json ~/.config/opencode/opencode.json
# already have one? merge the "qwen38" (and/or "flashnext") provider block into it
opencode
```

Do not want any of it? `./install.sh --no-opencode` (one-liner: `| bash -s -- --no-opencode`) installs the API only: no generated config, no `oc` launcher, and `switch-model.sh` never touches your opencode default model. The choice is remembered by later runs (marker `~/.config/qwen38/opencode.off`); `./install.sh --with-opencode` turns it back on. Your own `~/.config/opencode/opencode.json` is never rewritten in either mode: the installer only merges the served lane's limits into it when the integration is on.

What the shipped config gets right for you:

1. **The hidden 32K output cap**: opencode sends `max_tokens = min(limit.output, OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX or 32000)`. Without that env var, a long thinking phase hits 32000 tokens, the turn ends silently (`finish_reason: length`, no text, no tool call) and you have to re-prompt. The installer ships an **`oc` launcher** (`~/.local/bin/oc`, skipped if an unrelated `oc` binary exists) that exports the right value and execs `opencode --yolo`: launch with `oc` instead of `opencode` and the cap sits above every output limit this repo declares, on every target and in either context mode. It is deliberately a ceiling rather than the installed target's own number, because `limit.output` is what follows a model switch: a ceiling copied from the installed target survives the switch and cuts the next lane's turn in the same silence (`./oc-limits.sh --max-out` is where both installers read it). Note that `--yolo` auto-approves every tool action (how the reference box runs); remove it from the launcher file if you prefer per-action prompts.
2. **Limits that can never 400**: the server rejects any request where `input + max_tokens` exceeds the window (no clamping), so the config ships `context/input 194048, output 64000` in native mode (258048 worst case, a 4096 margin under 262144, whether the 32K cap is lifted or not) and `700000/200000` in 1m mode (worst case 880000, under the worst measured KV pool).
3. **Reasoning-effort variants**: the generated config declares `medium` and `low` variants (ctrl+t in the TUI); the default is the model's `xhigh`. This works because the patched template accepts and maps effort tiers (`max`/`high` → `xhigh`, `minimal` → `low`, [contributed by helge](https://forums.developer.nvidia.com/t/380257/10)); any client sending an unmapped tier would get a 500 on the stock template.
4. **Mid-conversation system messages**: some agent clients inject system messages after turn 1; the stock template raises `System message must be at the beginning`. Patched to render them as `<system-reminder>` blocks.
5. **Vision declared**: `attachment` + `modalities` are set, so image attachments and on-disk image reads work end to end (the model is natively multimodal).

On service installs the generated config points at the **keepalive proxy port** (`PORT+1`), not the server directly, and that is deliberate: SGLang buffers tool-call arguments while they stream (127 s of measured silence on one 400-line file write, at native context), and opencode drops a stream after roughly 140-180 s without a real chunk. The proxy (`qwen38-keepalive.service`, vendored `keepalive-proxy.py`) fills those silences with protocol-correct keepalives, at SSE event boundaries only, and makes sure a client that gives up does not leave a generation running (v6.14: it names every request with `x-override-rid` so it can abort one that has not produced anything yet, aborts before closing the socket, and drains the answer where the engine offers no rid to abort with). With `./install.sh --no-service` there is no proxy: the config then points at the server directly, and huge single-file writes may abort. One more caveat, measured: SGLang's `--api-key` only accepts `Authorization: Bearer`, **not** `x-api-key`.

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
- **`--mem-fraction-static 0.70`**: the KV pool is a boot lottery, and the two
  measurement campaigns on this box do not agree. Five boots in the v1.3 era
  reported **917K-1019K** tokens; three later ones, around 2026-08-30, reported
  **863,398, 893,479 and 913,334** for the same checkpoint. The ranges do not
  overlap, so treat **863K as the floor to plan against** until a fresh campaign
  settles it. DFlash2 acceptance is unchanged either way, and a real 690K-token
  request has been served (cold prefill 40 min, then cached).
- **Fit the limits to your own boot.** The generated opencode limits below are
  static, and their 1m worst case (compaction at ~680K plus 200K of output) sits
  **above the 863,398 floor**: on an unlucky boot a long session can meet a proxy
  refusal mid-conversation, which is the field case that produced this tool. After
  the engine is up, run `python3 oc-fit-limits.py` (or the cockpit's button): it
  reads the pool your boot actually got and rewrites the limits to fit it, up or
  down. The FP8 targets ship lower static limits already, because their pool is
  about 92,000 tokens smaller.
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
- **A corruption tripwire** (proxy v6.11). When a decode path loses its state on this
  hardware it does not stop: it emits runs of token id 0, which is `!` in the Qwen
  tokenizer, and the client reads a wall of exclamation marks as if it were an answer
  (sglang [#36537](https://github.com/sgl-project/sglang/issues/36537),
  [#36558](https://github.com/sgl-project/sglang/issues/36558),
  [#36806](https://github.com/sgl-project/sglang/pull/36806),
  [#36845](https://github.com/sgl-project/sglang/pull/36845)). The proxy counts those
  characters across the stream and, past `CORRUPTION_RUN` of them in a row (128 by
  default, `0` disables), aborts the generation upstream and sends an explicit
  `corrupted_output` error instead. It reads only the delta text it already relays,
  never tool-call arguments, so a model writing `!!!` in prose is untouched.
- **No zombie generations** (proxy v6.14). A client that gives up leaves the engine
  decoding unless the abort reaches it in time, and it cannot: `abort_request()` returns
  early once the rid has left `rid_to_state`, which the disconnect itself empties
  ([sglang#35255](https://github.com/sgl-project/sglang/pull/35255), merged upstream
  2026-09-04 and in neither image this repo serves). Measured here on 2026-09-09: 6,582
  `state was deleted in TokenizerManager` lines in one day, one request decoding 6 min for
  nobody. The proxy now names every request itself (`x-override-rid`, which the engine
  honours because the units pass `SGLANG_ENABLE_REQUEST_HEADER_OVERRIDES=1`), so a request
  abandoned during prefill still has a name, and it aborts **before** closing the socket
  rather than in a thread racing it. Where no rid can exist, on `/v1/messages` (the
  Anthropic route mints its own `msg_<uuid>` and applies no header overrides), the answer
  is drained to its end instead of being orphaned. Same request through both proxies: 223
  flood lines and a held slot before, 0 after.
- **A tool-schema guard** (proxy v6.13). The engine validates every tool's parameters
  with `jsonschema`, whose `regex` format check compiles `pattern` with Python's `re`.
  JSON Schema says `pattern` is ECMA-262, which has Unicode property escapes (`\p{Cc}`)
  that `re` rejects outright, so a single such tool makes the engine answer `400` to
  **every** request of the session (measured 2026-09-09 against Claude Code 2.1.266 and
  its `Artifact` tool). The proxy removes only the patterns Python cannot compile, only
  inside tool parameter schemas, and forwards every other body untouched and unparsed.
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

> **v1.7 and earlier users: upgrade.** v1.8 replaces this repo's vendored
> overlay with the official image the SGLang cookbook points DGX Spark at, which
> fixes at the root the failure the v1.6 proxy could only detect (every running
> MTP request collapsing to a wall of `!` at the same instant,
> [sglang#36811](https://github.com/sgl-project/sglang/pull/36811)), and serves
> **4 concurrent requests instead of 1** at the same single-stream speed. Re-run
> the one-liner (or `git pull && ./install.sh`).

Qwen's official validation environment for this model is a dual GB300 node; the
public Spark recipes run it on **two** boxes (TP2). This target runs it on
**one**, with the model's full 262K window and full NVFP4 quality, because the
47.7 GiB FP8 N-gram (PLE) table leaves memory entirely: it lives in a sparse
file on the local NVMe and the gather kernel reads its rows through GB10's host
page tables, which works because this part reports
`cudaDevAttrPageableMemoryAccessUsesHostPageTables`.

Until v1.7 that was a patch this repo vendored and built into a local image.
Since v1.8 it is upstream and the image is official:

- **`--ple-offload-embedding --ple-offload-backend file --ple-offload-dir /ple`**
  ([sglang#37068](https://github.com/sgl-project/sglang/pull/37068)), which adds
  two things the vendored patch never had: a `posix_fadvise(WILLNEED)` prefetcher
  for prefill-sized gathers, and a **resident-set trimmer**. That trimmer is the
  headline of this release for anyone who ran v1.6: a row fault maps in a whole
  page-cache folio, so the mapping's resident set climbed towards the full
  47.7 GiB while a token read a few KB of it, and on unified memory that is the
  same pool SGLang sizes the KV cache from. It is the mechanism behind the boot
  lottery v1.6.2 pinned `--max-total-tokens` against, and behind the ~9 GiB of
  host headroom a 120K prompt used to cost. Capped by
  `SGLANG_QWEN4_PLE_FILE_RSS_BUDGET_GB` (default 8; `PLE_RSS_BUDGET_GB=` at
  install). Seen at first boot here: `PLE table: trimmed resident set 10.6 ->
  0.2 GiB (budget 8.0 GiB)`.
- **QSA decode on sm_121** runs the merged kernel of
  [#36845](https://github.com/sgl-project/sglang/pull/36845) from inside the
  official image, so the two attention-backend flags this lane used to need are
  gone and the engine resolves the route itself.
- The table is **rewritten on every boot**, so the launcher deletes the previous
  `ple_table_*.bin` first: upstream measures a cold populated file rewriting at
  ~17 MB/s (~55 min) against GB/s on a fresh sparse one. One warm rewrite here
  ran at +27% of a fresh boot rather than 5x, which is the page cache, not a
  contradiction. It also retires the poisoned-table guard of v1.5 to v1.7: a
  table written from scratch cannot be half-written from a previous boot.

### Three tiers, because concurrency and context trade one for one

Each mamba state slot costs **0.206 GiB** of the same pool the KV cache comes
out of (measured here), and the hybrid GDN/QSA model reserves **5 slots per
running request** with `extra_buffer`, 4 with `extra_buffer_lazy`. The scheduler
silently caps `--max-running-requests` to what the mamba pool admits while
`/get_server_info` still reports what you asked for, so every tier pins
`--max-mamba-cache-size` to requests x slots.

| `FLASH_TIER=` | requests | KV pool | measured here |
|---|---|---|---|
| **`context`** (default) | 4 | **279,872 to 463,488 tokens** across boots, and every one of them above the 262,144-token window, so a full-context prompt always fits | **47.9 / 47.1 / 30.9 tok/s** single stream (code, math, prose FR), **71.6 tok/s aggregate at 4 streams** (17.9-18.7 each) |
| `concurrency` | 8 | 129,792 tokens on the boot measured, so prompts stop near 119K | 38.2 / 37.1 / 27.3 single and **96.5 tok/s aggregate at 8** (12.1-13.5 each), measured before the draft vocabulary |
| `throughput` | 24, no speculation | ~286K tokens (upstream) | upstream: 83 tok/s of output at 24, 15.9 single |

Both speculative tiers are the cookbook's own verified single-Spark cells, which
score **GSM8K 97.1-97.3% on the full 1,319-question set** upstream. `context` is
this repo's default because the lane exists for long context and an agent client
runs one or two streams; it is the same cell with the concurrency pinned lower.

The pool is still sized from what the host has free at the instant SGLang
profiles, so it is a range rather than a number: boots of the identical launcher
have measured 279,872, 454,016, 458,816 and 463,488 tokens at the `context`
tier. What v1.8
removed is the *creeping* half of that variance (the table's resident set), not
the boot-time half, which is why the launcher still waits for a busy box to go
quiet before it starts. At this tier both ends of the range are above the
262,144-token window, which is the property that matters.

Measured on the reference box at the `context` tier, image
`dev-qwen38-next-local` (`4ccff141db`):

| axis | measured |
|---|---|
| **prefix caching, 27K re-serve** | **12.0 s cold, 2.5 s cached (x4.8)**, 27,008 of 27,026 tokens from the cache |
| decode, single stream | **47.9 on code, 47.1 on math, 30.9 on prose FR, 29.3 on prose EN** (median of three repeats after a discarded warm-up; 38.8 / 36.7 / 26.5 on the v1.6 overlay) |
| decode, 4 streams | **71.6 tok/s aggregate**, 17.9-18.7 per stream |
| **agent loop** (`./bench-agent.py`, 8 turns on an 8K prefix, work pinned at 130 tokens) | **27.0 ms/tok** median, TTFT flat at 0.31-0.34 s, **-1 ms of TTFT per 1,000 added prompt tokens**: the prefix cache is being reused, with speculation on |
| **long-context retrieval** | **3/3 exact at ~120K** and **1/1 exact at 200,058 tokens** (`./needle.sh --mem`, fresh passphrase each), no run of token id 0 anywhere |
| quality canaries | 4/4 (merge, logic, French, primes) |
| prefill, cold | ~2,250 tok/s at 27K, ~1,960 tok/s at 200K |
| vision (image input) | works, including combined with large prompts |
| context window | 262,144 native, no YaRN |
| **context that fits** | **one prompt tops out at 200,000 tokens**, enforced by the proxy (`PROMPT_CEILING_TOKENS`), and by its share of the KV pool on the smaller tiers. This is up from 128K in v1.5.6 to v1.7, and the reason is the trimmer above: a 120K prompt now costs **0.0-0.1 GiB** of host headroom where it used to cost ~9 GiB, and a 200K prompt costs 3.5 GiB and leaves **12.6 GiB free** against the ~10 GiB livelock edge. Prompts above the ceiling get a clear 400 (`context_too_long`). Past 200K is deliberately unmeasured here: 262K would land near that edge, and on this box a livelock costs a power cycle |
| memory | fraction 0.85 + docker cap 110g (the cap does not see CUDA unified allocations). Host MemAvailable: **16.6-16.9 GiB idle** at the `context` tier whatever the pool came out at, so a larger pool inside the same static fraction costs the host nothing; 19.8 GiB at `concurrency`; 14.7-15.0 GiB through a 120K prompt, 12.6 GiB through a 200K one |
| boot to `/health` | 12 min 21 s with a fresh table, 14 min 54 s when it rewrote a populated one |

**Why the flash lane keeps a bf16 KV cache.** The 27B FP8 target asks for
`--kv-cache-dtype fp8_e4m3` and gains about half its pool for free, so the same
trick looks tempting here, and it works: an fp8 KV cache on the QSA path
([blazux, 2026-08-30](https://github.com/blazux/qwen3.8-Flash-DGX), by
@Nanetnounou) measures **x1.9 KV pool and 1M context on one box**. It also costs
**9 % of decode, 30 % of prefill, and quality**: their `b3_itinerary` check drops
to **2/6 against 6/6 in bf16**, and they keep bf16 as their own production
setting. The difference from the 27B case is calibration: the NVFP4 and FP8 27B
checkpoints carry KV scales the engine applies, while nothing calibrates the QSA
path's cache. A bigger pool is not worth a measured quality drop.

The NEXTN speculative head is the model's own next-token module (its 31
tensors ship in the checkpoint in BF16, hence `unquant` for the draft): drafts
are verified by the target, so output quality is exactly the target's.

**One thing still wedges this lane: a prompt larger than the KV pool, sent
straight to the engine port.** It is queued and never admitted, which stalls
every request after it (`/abort_request` answers `not found in rid_to_state`:
[sglang#36333](https://github.com/sgl-project/sglang/issues/36333), open
upstream). Only a restart clears it, and at 15 minutes that is worth avoiding:
**use the proxy port**, which refuses such a prompt with a 400. The `context`
tier makes this much harder to hit, since its pool (279,872) is larger than the
window it serves (262,144).

Two older failure modes are worth knowing about. The scheduler could hang under
pool pressure with a second giant request admitted at 98% usage (same family as
[sglang#30314](https://github.com/sgl-project/sglang/issues/30314)); the
frontend keeps answering `/health` while nothing is served, so a health probe is
not enough, which is why the cockpit runs a real generation probe and flushes
the prefix cache when the engine idles with a mostly-held pool. And with chunked
prefill, new requests can starve while one request decodes a long answer
([sglang#35537](https://github.com/sgl-project/sglang/issues/35537)); a single
agent client never notices, concurrent clients see bursty latency.
`/flush_cache` is refused with a 400 while anything is in flight, by design:
flush when the lane is idle.

## The seven targets, and switching between them

| choice | checkpoint | revision | engine, unit |
|---|---|---|---|
| `stock` (default) | `RadixArk/Qwen3.8-27B-NVFP4` | `52d1adc` | SGLang, `qwen38-sglang` |
| `uncensored` | `edp1096/Huihui-RadixArk-Qwen3.8-27B-abliterated-NVFP4` | `21565d3` | SGLang, `qwen38-sglang` |
| `fp8` | `Qwen/Qwen3.8-27B-FP8` | `017b9c7` | SGLang, `qwen38-sglang` |
| `uncensored-fp8` | `edp1096/Huihui-Qwen3.8-27B-abliterated-FP8` | `603028a` | SGLang, `qwen38-sglang` |
| `flash` | `RadixArk/Qwen3.8-Flash-Next-NVFP4` | `7b71922` | SGLang, `qwen38-flash` |
| `flash-uncensored` | `dealignai/Qwen3.8-Flash-Next-ABLITERATED-NVFP4` | `be794b9` | SGLang, `qwen38-flash` |
| `flash-nvda` | `nvidia/Qwen3.8-Flash-Next-NVFP4` | `fc694b5` | SGLang, `qwen38-flash` |

`flash-uncensored` is the abliterated build of the tree this lane already
serves, and it was chosen on evidence rather than on downloads: of the
abliterated Flash-Next exports published for this architecture, this one has
**206 shards with the same names as the stock export and 205 of them
byte-identical in size**, the same index, the same `hf_quant_config` and the same
chat template. It is an abliteration OF what this lane serves, so every serving
flag transfers unchanged, MTP and the vision tower included. Validated here:
boot 10 min 40 s, KV pool 473,664 tokens, decode 45.4-46.4 tok/s on code,
canaries 4/4, needle 1/1 exact at 120K, prefix caching x5.9, and **0 refusals out
of 5** deliberately blunt probes, which is the point of the variant. Safety
refusals are removed, which moves the guardrails onto you: filtering, human
review and access control are yours to supply, and the lane binds to `0.0.0.0`.
Two alternatives were rejected for stated reasons, both worth knowing if you go
looking: `orcarouter/Qwen3.8-Flash-Next-Uncensored-NVFP4` is a different
packaging (18 shards, 170.9 GiB, no `hf_quant_config`, a separate
`model-mtp.safetensors`), and the Mia/Keys splice cannot be served here at all,
because it is built on the vLLM tree
(`Qwen3_8FlashNextForConditionalGeneration`, `model_type qwen3_8_flash_next`)
while SGLang registers only `Qwen4ExpForConditionalGeneration`, with zero
mentions of the other name anywhere in the image.

`flash-nvda` is NVIDIA's own ModelOpt **MIXED_PRECISION** export of the same
176B model: NVFP4 routed experts, an FP8 N-gram table and FP8 block-scaled MTP
experts, ~124 GiB. Same lane, same tiers, one difference that matters and two
flags that carry it: its MTP draft is fp8 rather than BF16, which leaves a much
larger KV pool at identical pins (upstream measured 174K tokens against 93K on
the cookbook's 8-request cell). It is served with **no `--quantization`**, because
it resolves to `modelopt_mixed` on its own, and with **`--moe-runner-backend
flashinfer_cutlass`** pinned, because the mixed-precision auto-default picks
`flashinfer_trtllm` on GB10 and the NVFP4 MoE method rejects it at autotune.
`./switch-model.sh flash-nvda` rewrites the model path, the revision and that
flag pair together. It needs the mixed-precision loader of
[sglang#38121](https://github.com/sgl-project/sglang/pull/38121), which is in the
image this repo pins.

The uncensored target is huihui-ai's abliteration of Qwen3.8-27B re-quantized
with the identical RadixArk modelopt NVFP4 recipe (verified: same
`text_config`, same mixed 8-bit attention / 4-bit MLP quant groups, same
chat template, MTP + vision intact, ~22 GB). It refuses the least while keeping
the stock NVFP4 serving path.

**NVIDIA published its own 27B NVFP4 export on 2026-09-08**
([`nvidia/Qwen3.8-27B-NVFP4`](https://huggingface.co/nvidia/Qwen3.8-27B-NVFP4)), and it
is not a new option here, for a reason worth writing down. Its quantization map is the
**same one this repo already serves**, layer for layer: 401 quantized layers, NVFP4
group-16 on the 64 MLP triplets and on `lm_head`, FP8 on all 48 linear-attention and 16
self-attention projections, `mtp*` excluded, and the three shards match the RadixArk
export **byte for byte in size** (9,965,652,544 / 9,985,757,064 / 1,970,287,672; the
weights themselves differ, as two calibrations do). One field differs and it is the one
that costs something: RadixArk declares `kv_cache_quant_algo: FP8`, NVIDIA's declares
`null`, so SGLang's `--kv-cache-dtype auto` gives it a **bf16 KV cache and about half the
pool** unless you pass `--kv-cache-dtype fp8_e4m3` yourself, which is what NVIDIA's own
card tells you to do (and what this repo already does for the FP8 pair). Two things on
that card are worth knowing: it says the checkpoint was produced with **modelopt v0.48.0**
while the checkpoint's own `hf_quant_config.json` names `0.47.0.dev80+g913f5e224`, and its
accuracy table, measured by NVIDIA on GB300 under vLLM, prices this recipe against BF16 at
**GPQA Diamond 88.01 against 88.92, Terminal-Bench 74.02 against 75.56, IFBench 78.93
against 80.07, MMMU-Pro 74.86 against 75.14**, with **AA-LCR 73.38 against 72.63** and
**SciCode 48.41 against 47.93** landing the other way. That is the size of the NVFP4
question on this model, from the people who quantized it. Adding an eighth target for a
recipe-identical export would cost 21 GB and a validation cycle for, at best, a tie: say
so in an issue if you want it, the switch surface has room.

The FP8 pair is Qwen's own release and huihui-ai's abliteration of it in the
same format. SGLang reads the weight scheme from the checkpoint's config, but
one flag does change: **the FP8 targets are served with `--kv-cache-dtype
fp8_e4m3`**. The whole 27B lane runs an fp8 KV cache; the NVFP4 checkpoints
carry their own KV scales so SGLang picks it up on its own, while Qwen's FP8
release does not and would otherwise fall back to a bf16 KV cache costing about
half the pool (measured on the same 1m unit: 771,139 tokens with the flag,
382,706 without). `install.sh` and `run.sh` add it for these two targets only.

The [SGLang cookbook](https://docs.sglang.io/cookbook/autoregressive/Qwen/Qwen3.8-27B)
states the mechanism directly: "Both NVFP4 checkpoints declare
`kv_cache_quant_algo: FP8`; SGLang's default `--kv-cache-dtype auto` honors it,
so the KV pool runs in fp8_e4m3 with the checkpoint's calibration scales
automatically." Qwen's FP8 release carries no such declaration, which is why it
is the one target that has to ask. The cookbook also prices the difference at
32.8 KB per token in fp8 against 65.5 KB in bf16, exactly the factor of two this
box measures between a flagged and an unflagged FP8 pool. Take it when you want the quantization question off the table, and know
what it costs: the weights are **30.9 GB against 21 GB** for NVFP4, SGLang takes
that out of the KV pool: the same 1m unit measures **863,398 tokens on NVFP4 and
771,139 on FP8**, about 92,000 fewer, and decode is bandwidth bound on GB10 so it
is also slower (**108 tok/s aggregate at
8 streams** against 135-148 for the NVFP4 default). The abliterated FP8 was
checked against Qwen's official FP8 file by file before pinning: 66 weight names
in common and not one shared hash, so the abliteration is real and not a rename,
and `lm_head` is left out of the quantization, which is what keeps an FP8
checkpoint loadable by SGLang.

Two honest gaps on the FP8 pair, both being closed: it does not yet carry a full
`./bench-matrix.sh` run the way the other three targets do (the 108 tok/s figure
is a single aggregate probe), and **the memory ceiling of FP8 combined with
`CONTEXT_MODE=1m` is not measured**. The "27B lane measured flat at 100K, 200K
and 300K" result quoted elsewhere in this README was measured on NVFP4, and FP8
puts about 10 GB more in residence. Until that curve exists, run the FP8 pair at
native context, or watch host `MemAvailable` if you run it at 1M.

The flash target is Qwen3.8-Flash-Next (Qwen4-generation preview: 176B hybrid
MoE, 6B active, QSA sparse attention, multimodal) in RadixArk's NVFP4, served
by SGLang with the model's own NEXTN/MTP speculative head and a working radix
(prefix) cache. Both units publish the same port and are never enabled
together: switching targets flips which unit starts at boot, the API surface
and the keepalive proxy stay put.

- Fresh install: `MODEL_CHOICE=uncensored ./install.sh` or
  `MODEL_CHOICE=flash ./install.sh` (one-liner: `curl -fsSL .../get.sh |
  MODEL_CHOICE=flash bash`). Upgrades keep the installed choice.
- Existing install, within the 27B lane (`stock`, `uncensored`, `fp8`,
  `uncensored-fp8`, in any direction): `./switch-model.sh uncensored` (or
  `stock`, `fp8`, `uncensored-fp8`), as many times as you like. It downloads the
  checkpoint (cached after the first time), applies the 1M YaRN config patch if
  the installed unit uses `--context-length 1010000`, regenerates the patched
  chat template from the target's own snapshot, rewrites **only** the
  `--model-path` line of `/etc/systemd/system/qwen38-sglang.service` and
  daemon-reloads.
- Existing install, across lanes (`flash` ↔ any 27B target): install each
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

## Three tools worth knowing about

```bash
./bench-agent.py                  # the agent-loop shape: ms/tok on a growing conversation
./bench-agent.py --turns 6 --prefix-tokens 30000
./build-token-map.py --help       # the reduced draft vocabulary (install.sh runs it for you)
./check-pins.sh                   # does every pinned revision and digest still resolve?
```

`bench-agent.py` measures the shape an agent client actually has, which none of
the tok/s numbers above capture: it resends a growing conversation and asks for a
short answer, with the work pinned so ms/tok compares engines and not answer
lengths. The number to read is **TTFT per 1,000 added prompt tokens**: near zero
means the prefix cache is being reused, and a figure that tracks the whole prompt
means it is not, which is what to check before blaming decode. That distinction
is not academic. On vLLM a third party measured the drafter ranking *invert*
between the two shapes: MTP had the best decode on their box and the worst agent
loop, worse than no speculation at all, because their scheduler drops a cacheable
block per request when a drafter is configured. This lane does not pay that, and
`bench-agent.py` is how you check yours.

`check-pins.sh` is deliberately not in CI: a green build must not depend on
Hugging Face being up. Run it before a release, or when an install fails on a
fresh box.

## Operations

```bash
systemctl status qwen38-sglang          # 27B server state (any of the four 27B targets)
systemctl status qwen38-flash           # Flash-Next server state (target flash)
systemctl status qwen38-keepalive       # keepalive proxy state
systemctl status qwen38-dashboard       # cockpit state, if you installed it
python3 conc-check.py                   # does this lane still answer correctly at concurrency 8
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

**Idle power**: without `--sleep-on-idle`, SGLang's scheduler busy-spins a full CPU core while doing nothing (reported as +10-12 W at the wall by [alef204 and emX0r](https://forums.developer.nvidia.com/t/380257/56), diagnosed in [MiaAI-Lab issue #4](https://github.com/MiaAI-Lab/Qwen3.8-27B-SGLang-DGX-Spark/issues/4)). Every serving lane ships the flag, and a CI gate requires it: the 27B units and `run.sh` since v1.2.6, the flash launcher since v1.8.5, where the lane was measured holding a core at 101 % for 12 h 21 min of idle because the launcher had been written without it. A/B on the reference box: scheduler CPU 101 % -> 1.7 % at idle, module power 12.1 -> 10.5 W, and wake-up TTFT unchanged (0.234-0.240 s before, 0.234-0.239 s after, measured after 60 s and 300 s of idle), throughput in family (41.5 tok/s code, 52.8 math).

## The cockpit (opt-in web dashboard)

A local dashboard for this stack: what is served right now, whether it is
healthy for real, and the handful of actions you would otherwise type by hand.
Single-file stdlib backend, no pip and no venv. It is **not** part of
`install.sh`; you install it on purpose:

```bash
dashboard/install-dashboard.sh          # DASH_PORT=30090 by default
# open http://127.0.0.1:30090, the login is the API key from ~/.config/qwen38/api-key
```

It binds to `127.0.0.1` by default. To open it on your laptop instead of on the
box, set `DASH_BIND` at install time:

```bash
DASH_BIND=0.0.0.0 dashboard/install-dashboard.sh        # every interface
DASH_BIND=$(tailscale ip -4) dashboard/install-dashboard.sh   # that interface only
```

Then browse `http://<the box's tailnet or LAN address>:30090`. The login is the
same API key, over plain HTTP: the session cookie is `HttpOnly` and
`SameSite=Strict` and every mutating POST carries a CSRF token, but there is no
TLS, so this belongs on a tailnet or a LAN you trust and never on the open
internet. On an already installed cockpit, change it in place:

```bash
sudo systemctl edit qwen38-dashboard    # [Service] Environment=COCKPIT_BIND=0.0.0.0
sudo systemctl restart qwen38-dashboard
```

What it shows and does:

- **Lane state that is not a lie.** A wedged SGLang still answers `/health`, so
  the cockpit runs a real generation canary and reports `ready`, `loading`,
  `wedged` or `stopped` from that, with the served checkpoint named from the
  unit rather than guessed.
- **Belts.** A host `MemAvailable` floor that aborts generations before the box
  reaches the memory edge, and a counter of the kernel's `NVRM` allocation
  refusals, which is how the memory-edge behaviour in the flash section was
  found in the first place.
- **Actions, one at a time.** Unit start/stop/restart, lane switch (the same
  `switch-model.sh` you would run), cache flush, abort-all, smoke probe. Every
  action is audited to `~/.config/qwen38/cockpit-audit.log` with its exact argv.
- **Registry.** Which pinned checkpoints are actually on disk, which are stray,
  which are missing, and what each costs you in bytes.
- **Recipes and drift.** Every target as data, derived from `install.sh` and the lane
  templates so a recipe cannot drift from what the installer renders, compared flag by
  flag against the invocation actually running on the box. Since v1.8.5 that comparison
  covers value-less flags too (`switch.--sleep-on-idle: recipe true, installed false` is
  what the panel said the morning the flash lane was caught spinning a core).
- **Zombie guard.** A client that gives up leaves the engine decoding unless something
  stops it, so the Requests tab reads both sides of the wire: the engine's own flood
  lines grouped by request, worst first, with the span between a request's first and
  last line, which is the dead decode; what the proxy did about it over the same window
  (aborted, drained, the longest drain, aborts the engine never answered); the version
  of the **running** proxy from its startup banner; and whether the engine accepts the
  proxy's request id at all, because without that an abandoned answer can only be
  drained. See the v1.8.4 and v1.8.5 changelog entries.

**The privileged surface, stated plainly.** The unit actions need root, so the
installer writes `/etc/sudoers.d/qwen38-cockpit`: an exact argv allowlist,
nothing wildcarded except the rendered unit path, validated with `visudo -c`
from a temp file before it lands so a bad render can never brick sudo. It covers
start/stop/restart and enable/disable of this repo's units, `daemon-reload`, the
writes `switch-model.sh` performs, one read-only forensics wrapper and kernel
journal reads. `./uninstall.sh` removes it along with the unit and the wrapper.
If that surface is more than you want, do not install the cockpit: nothing else
in this repo depends on it.

**Self-restart is off by default** (`COCKPIT_AUTOHEAL=0` in the unit). This repo
spends a whole section on how a GB10 box freezes, so an engine that restarts
itself is not something an install should decide for you. Arm it once you know
what a wedge looks like on your box:

```bash
sudo systemctl edit qwen38-dashboard    # [Service] Environment=COCKPIT_AUTOHEAL=1
                                        # and Environment=COCKPIT_AUTOHEAL_GRACE=600
                                        # to keep a wedge up for forensics first
```

Remove it with `./uninstall.sh` (which takes the whole stack) or on its own:

```bash
sudo systemctl disable --now qwen38-dashboard
sudo rm -f /etc/systemd/system/qwen38-dashboard.service \
           /etc/sudoers.d/qwen38-cockpit /usr/local/bin/qwen38-pyspy-scheduler
sudo systemctl daemon-reload
```

### On a phone

The cockpit is built for a hand as well as for a desk, and the layout is checked
rather than assumed: `dashboard/tests/mobile-check.mjs` drives a headless
Chromium through four real iPhone geometries (SE, 15, 15 Pro Max, and 15 in
landscape) on all eight tabs and asserts what a phone actually gets.

Below 980 px the top bar keeps one identity row and gives the actions a row of
their own that scrolls sideways, the section rail becomes one swipeable row of
pills with the current section scrolled into view, and the cards stack. Controls
are at least 44x44 CSS px, and every form control is 16 px or larger, because
iOS Safari zooms the whole page when a smaller one takes focus and never zooms
back. The heights use `dvh`, not `vh`: on iOS the browser's own chrome counts
inside `100vh`, so a full-height panel written that way overflows by exactly the
toolbar.

The Agent tab opens **fullscreen on a phone**, because there the tab is the
frame: opencode gets the whole screen and the corner chip brings the cockpit
back. That choice is remembered per device, so exiting once makes the embedded
frame the default from then on. It never opens fullscreen when the panel cannot
load (a relay bound to another address, a stopped server): covering the
explanation with a blank frame would leave nothing to act on.

Run it against the address the phone uses, so the Agent tab is exercised the way
it behaves in a hand:

```bash
node dashboard/tests/mobile-check.mjs http://<the box's tailnet address>:30090
```

### The Agent tab: opencode in the browser, behind the cockpit login

Since v1.7.0 the cockpit can hold opencode's own web interface, so a session on the
box runs from the laptop without a terminal: sessions, the project picker, file
diffs, the terminal panel, the same config, plugins, skills and MCP servers as the
`oc` command. It is opt-in and needs the cockpit installed and opencode 1.18 or
newer on your PATH:

```bash
dashboard/install-agent.sh
```

Two pieces land, and the shape is the security model:

- **`opencode-web.service`** runs `opencode serve` as you on `127.0.0.1:4096`
  (`OPENCODE_PORT=`), with Basic credentials generated once into
  `~/.config/qwen38/opencode-web.env` (mode 0600). Nothing else ever reaches it,
  and the password never leaves the box. The unit carries your PATH and the same
  output-token cap as the `oc` launcher, so long thinking is not cut at 32,000.
- **The relay** inside the cockpit process listens on ONE address, the tailnet
  address by default (`AGENT_BIND=`, port `AGENT_PORT=30091`), never on the LAN.
  It answers a request only with a valid cockpit session cookie, refuses any
  foreign `Origin`, strips the cookie before forwarding, adds the credentials,
  and streams everything back: plain answers, the event stream, the WebSocket of
  the terminal panel. Its responses carry `frame-ancestors` naming the cockpit,
  so no other page can frame the interface.

The browser therefore sees one host for the cockpit and the relay (cookies ignore
ports): the Agent tab frames the interface with no second login and no Basic-auth
prompt, which Chrome would block inside a cross-origin frame anyway. That is also
the one rule: open the cockpit through the address the relay binds. With the
cockpit on `0.0.0.0` or on the tailnet address, that is
`http://<tailnet address>:30090/#agent`; the tab says so when you arrive by
another name. A cockpit bound to `127.0.0.1` gets a loopback relay, usable on the
box itself.

What the tab shows: the state of the server and the relay, the served opencode
version and, after `opencode upgrade`, that a newer binary is installed with a
**Restart server** button (systemd restart, through the same exact-argv sudoers
allowlist as the other units, three more lines). **Fullscreen** makes the
interface cover the whole browser window; the corner button or Escape brings the
cockpit back, and a reload on the tab comes back the way it was left. Prefer it
to opening more browser tabs: every tab of the interface holds one permanent
event stream, browsers allow six connections per origin, and a sixth tab freezes
them all (the interface handles any number of sessions in one tab). **Open in a
tab** still exists for a second screen. The Logs tab reads the server's journal.

Permissions are opencode's own. Its defaults (opencode 1.18) allow most tool
calls and ask before a tool touches a path outside the session's project and
when the same call repeats three times; the interface shows those prompts. For
the autonomy of the `oc` launcher's `--yolo` (a flag `opencode serve` rejects),
install with `AGENT_AUTO=1`: the unit then carries `OPENCODE_PERMISSION` set to
allow everything, which opencode honours (verified: the served config reads
`{"*": "allow"}`), explicit `deny` rules of your config still apply, and your
`opencode.json` is not touched. The tab says which mode the running server
applies. Re-runs remember the choice; `AGENT_AUTO=0` turns it back off.

```bash
AGENT_AUTO=1 dashboard/install-agent.sh
```

Variables: `OPENCODE_PORT` (4096), `AGENT_PORT` (30091), `AGENT_BIND` (an address,
or `tailscale`), `AGENT_AUTO` (0 or 1), `AGENT_OUTPUT_TOKEN_MAX` (the output
ceiling; by default `./oc-limits.sh --max-out`, the largest limit any target asks for), `AGENT_PATH` (the PATH the service gets; yours by
default). Re-running
`dashboard/install-dashboard.sh` alone keeps the relay settings, the bind and the
port it finds in the installed unit. Remove with:

```bash
sudo systemctl disable --now opencode-web.service
sudo rm -f /etc/systemd/system/opencode-web.service
DASH_AGENT_PORT=0 dashboard/install-dashboard.sh       # the cockpit without the relay
```

## Extras (opt-in)

Three field-tested pieces from the reference box, deliberately not part of the default
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

**`extras/gguf/`**: a note, not a lane. What llama.cpp measured on this box against the
SGLang path (25.6 tok/s on code against 32-40, prose 17.7-18.2 against 17-22, prefill
about a third), the four traps that make a GGUF benchmark on GB10 lie to you, and what
evidence would make a GGUF target worth adding. Written for
[issue #12](https://github.com/hasso5703/dgx-spark-qwen38/issues/12).

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
under `/etc/systemd/system/qwen38-sglang.service.d/`, the opencode on/off choice (v1.5.9), and (since v1.3) the installed target
model, port and HF cache location, which are read from the installed unit (v1.4: units; a box
serving the flash target keeps it, exactly like a 27B choice). The unit itself is
rewritten on the repo's current flags (the previous one is backed up to
`~/.config/qwen38/<unit>.bak-preupdate`) and the service restarts on the new
config. v1.3 -> v1.4 changes nothing by itself for a 27B box: the flash stack is only
downloaded and installed when you ask for it (`MODEL_CHOICE=flash`), and the regenerated
`opencode.json` gains an `xhigh` reasoning-effort variant. v1.4 -> v1.5 moves the flash
lane from vLLM to SGLang (working prefix caching; the upgrade keeps your port, cache and
model choices and regenerates the launch script on the new engine; the first boot writes
the 48 GB PLE backing file). **v1.7 -> v1.8 moves the flash lane onto the official image
and off this repo's overlay**: the upgrade pulls one 15 GB image, builds nothing, deletes
the previous N-gram table file so the next boot writes a fresh one (~12 min), and serves 4
concurrent requests instead of 1. It also raises that lane's one-prompt ceiling from 128,000
to 200,000 tokens and its opencode limits with it, so an agent client will start sending
longer conversations: that is measured, not assumed (needle 3/3 at 120K and 1/1 at 200K, host
memory floor 12.6 GiB). Rollback is `OVERLAY_FLASH=1 ./install.sh`, which rebuilds the v1.7
image; the v1.7 image is kept on the box for exactly that. **27B boxes are untouched by
v1.8**, on purpose: see `dflash2/ATTRIBUTION.md`, "The 27B migration, and what blocks it". Upgrading from v1.2.x also removes the deprecated Claude Code warmup drop-in if you had
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
