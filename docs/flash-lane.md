# The flash lane: Qwen3.8-Flash-Next 176B on one Spark

How a 176B hybrid MoE serves on a single GB10, the three serving tiers, and what each one measured on this box.

Since v1.20 every plain install puts this lane on the box beside the 27B, installed and not
enabled at boot, by the same installer started again for this lane (its target and tier are
kept across updates), and now on demand: a plain run writes its unit and launcher, and its
first Load from the cockpit pulls its image, fetches its checkpoint and builds its draft
vocabulary from the target it loads (`./prepare-lane.sh flash <target>`, as the user, no sudo),
then switches to it and starts it. `--all-weights` or `--with-flash` installs it whole at once and proves it with a boot and
a real generation, as every run did before. `MODEL_CHOICE=flash` makes it the lane that serves instead, with
the 27B beside it on demand the same way (its first Load runs `./prepare-lane.sh 27b <target>`: its image,
its DFlash2 draft and its checkpoint); `--no-flash` leaves it out of a 27B box (223 GB), and later
runs remember that.

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

That attribute holds in the GPU's ATS addressing mode only. Outside it SGLang
refuses the file backend when it loads the weights ("needs a device whose
pageable host memory accesses go through the host page tables"), as on the GB10
of [issue #54](https://github.com/hasso5703/dgx-spark-qwen38/issues/54), whose
mode reads None. `nvidia-smi --query-gpu=addressing_mode --format=csv,noheader`
names the mode: ATS, HMM or None. The reference box runs in ATS. The box of
issue #54 boots with the same `iommu.passthrough=0` and the same `nvidia_uvm`
settings, so those alone do not decide it.

Since v1.36.4 the lane asks before it starts (`flash-ats-check.sh`), and a box
in another mode fetches nothing for it:

- A new flash install stops before its first download, with the reason. An
  update gives the flash lane its units only, beside the 27B lane or as the
  box's own text lane, starts nothing for it, updates the rest of the box, and
  says the lane cannot start here.
- Its first Load, a Load of another flash checkpoint and the Library's Download
  (`prepare-lane.sh`, `switch-model.sh`) refuse it before the first byte, and
  before the serving lane is stopped.
- The launcher stops its start with the reason, which the cockpit shows as
  **cannot start on this GPU**, instead of a new start every 15 s. The
  FP4-Local config's own launcher asks the same way (below).

A mode nvidia-smi does not name (an older driver, `[N/A]`), or a check that
cannot run, leaves the engine to decide, as before. Serving the table on a GPU without ATS would take a
host-staging path in SGLang, which
[sglang#40235](https://github.com/sgl-project/sglang/pull/40235) proposes.

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

**One boot in thirteen failed, once.** On 2026-09-18 this lane was started
thirteen times in one day for a measurement campaign. Twelve came up. The one
that did not died during CUDA graph capture of the speculative draft, with
`Exception: Capture cuda graph failed: CUDA error: an illegal instruction was
encountered` out of `eagle_draft_cuda_graph_runner`, and the unit exited cleanly
rather than serving anything wrong. The GPU was idle and healthy before and
after (10.7 W, 2411 MHz), the next boot of the same target with the same flags
came up normally, and it is the only occurrence in seven days of journal. It is
recorded here because a boot that fails this way costs twelve minutes and says
nothing useful in the unit status, not because anything about it is understood.

## Three tiers, because concurrency and context trade one for one

Each mamba state slot costs **0.206 GiB** of the same pool the KV cache comes
out of (measured here), and the hybrid GDN/QSA model reserves **5 slots per
running request** with `extra_buffer`, 4 with `extra_buffer_lazy`. The scheduler
silently caps `--max-running-requests` to what the mamba pool admits while
`/get_server_info` still reports what you asked for, so every tier pins
`--max-mamba-cache-size` to requests x slots.

| `FLASH_TIER=` | requests | KV pool | measured here |
|---|---|---|---|
| `context` | 4 | **279,872 to 463,488 tokens** across boots, and every one of them above the 262,144-token window, so a full-context prompt always fits | **47.9 / 47.1 / 30.9 tok/s** single stream (code, math, prose FR), **71.6 tok/s aggregate at 4 streams** (17.9-18.7 each) |
| **`concurrency`** (default since 2026-09-30) | 8, 2 states per path | **477,056 tokens** measured 2026-09-29 with the per-path cap (468,480 without it, 129,792 before replayssm-spec), so a full 262K prompt fits at 8 requests too | **43.7 / 44.2 / 29.5 tok/s** single stream (code, reasoning, prose FR, warmed, uncensored), **90.4 tok/s aggregate at 4 streams**, needle 8/8 exact to 140K at 13.5 GiB floor |
| `throughput` | 24, no speculation | ~286K tokens (upstream) | upstream: 83 tok/s of output at 24, 15.9 single |

Both speculative tiers are the cookbook's own verified single-Spark cells, which
score **GSM8K 97.1-97.3% on the full 1,319-question set** upstream. `concurrency`
became the default on 2026-09-30 with `--mamba-max-states-per-path 2` added,
after the campaign measured it on this box (2026-09-29, on the nightly it was pinned to:
canaries 4/4, needle 120k/200k 2/2, prefix cache 99.5 to 99.9% on repeated
prefixes, the pool never saturated, mamba peaked at 22 of 40). The per-path cap
halves the state each speculative path holds, which is what makes 8 requests fit
in 40 slots while a full 262K prompt still fits. `context` stays one
`FLASH_TIER=context ./install.sh` away, and a re-run keeps the tier a box
chose, in both directions (the detection now reads a 4-request launcher too,
which the old default never needed). Since v1.36.0 the context tier that an
install before v1.19.0 got by default, and that nothing records as chosen, moves
to concurrency at the next update, which says so before its first step.

The pool is still sized from what the host has free at the instant SGLang
profiles, so it is a range rather than a number: boots of the identical launcher
have measured 279,872, 454,016, 458,816 and 463,488 tokens at the `context`
tier. What v1.8
removed is the *creeping* half of that variance (the table's resident set), not
the boot-time half, which is why the launcher still waits for a busy box to go
quiet before it starts. At this tier both ends of the range are above the
262,144-token window, which is the property that matters.

## Several agents at once: their contexts share one pool

The tier caps how many requests run together; the pool caps how much context they hold
between them, and with agents that is the limit reached first. An agent keeps its whole
conversation in the prefix cache from one turn to the next, which is why a turn costs only
its new tokens (99.5 to 99.9% of a repeated prefix comes from the cache). When the agents'
contexts add up to more than the pool, each turn evicts another agent's prefix, and that
agent's next turn computes its whole prompt again.

Measured on 2026-10-03 at the `concurrency` tier, pool 437,184 tokens: an opencode session
that started eight sub-agents at once. Each sub-agent's first request held about 28,500
tokens there (opencode's system prompt, that box's tools and the task), and every file it
read added to it; at 01:00 the eight held 66,103 to 116,159 tokens each, 771,301 in all
(opencode's own count, in its database). The
engine's counters, per half hour:

| | 00:15-00:45 | 00:45-01:15 | 01:15-02:15 |
|---|---|---|---|
| prompt tokens served from the cache | 94.5% | 50.5% | 27.6% |
| prompt tokens computed | 303/s | 1,074/s | 1,041 to 1,320/s |
| tokens decoded, all requests together | 69.3/s | 15.7/s | 2.9 to 3.0/s |
| requests running (mean, max) | 5.5, 8 | 4.1, 6 | 3.0 to 3.5, 5 |

The rest of that night was another failure, fixed in v1.22.5: requests waited in the queue
past opencode's 300 s wait for headers, opencode sent them again, and from 02:15 to 11:36
the engine computed 47,939,516 prompt tokens and decoded 18,807: prompts for callers that
had already gone (CHANGELOG, v1.22.5). The proxy now keeps such a stream open and ends the
request of a caller that leaves. What the table shows is left, and it is arithmetic, not a
bug.

**Keep the sum of the agents' contexts under the pool.** The pool of the running boot is on
the cockpit's Lanes page (and in the engine's log, `max_total_num_tokens`). An agent that
reads a lot of files reaches 100,000 to 140,000 tokens (that night's eight peaked at 66,103
to 142,638), and opencode's compaction keeps each agent under the lane's limit (205,000
here), not their sum. On a 437,184-token pool that is three or four such agents at once,
and two at their full limit. More agents still run, but each of their turns costs a whole
prefill, at about 1,000 to 1,500 tokens a second.

Measured on the reference box at the `context` tier, image
`dev-qwen38-next-local` (`4ccff141db`):

| axis | measured |
|---|---|
| **prefix caching, 27K re-serve** | **12.0 s cold, 2.5 s cached (x4.8)**, 27,008 of 27,026 tokens from the cache |
| decode, single stream | **47.9 on code, 47.1 on math, 30.9 on prose FR, 29.3 on prose EN** (median of three repeats after a discarded warm-up; 38.8 / 36.7 / 26.5 on the v1.6 overlay) |
| decode, 4 streams | **71.6 tok/s aggregate**, 17.9-18.7 per stream |
| **agent loop** (`./bench-agent.py`, 8 turns on an 8K prefix, work pinned at 130 tokens) | **27.0 ms/tok** median, TTFT flat at 0.31-0.34 s, **within ±35 ms of TTFT per 1,000 added prompt tokens** (where a cache that is not reused reads about 580): the prefix cache is being reused, with speculation on |
| **long-context retrieval** | **3/3 exact at ~120K** and **1/1 exact at 200,058 tokens** (`./needle.sh --mem`, fresh passphrase each), no run of token id 0 anywhere |
| quality canaries | 4/4 (merge, logic, French, primes) |
| prefill, cold | ~2,250 tok/s at 27K, ~1,960 tok/s at 200K |
| vision (image input) | works, including combined with large prompts |
| context window | 262,144 native, no YaRN |
| **context that fits** | **one prompt tops out at 250,000 tokens**, enforced by the proxy while this lane serves (`FLASH_PROMPT_CEILING_TOKENS`, since v1.18.7 whatever lane was installed or switched to last; `PROMPT_CEILING_TOKENS` is a ceiling on any lane), and by its share of the KV pool on the smaller tiers. This was 128K in v1.5.6 to v1.7 and 200K in v1.8 to v1.10, because on the v1.5 engine the prefill of a long prompt grew the footprint by ~0.27 GiB per 1k tokens past ~90k. v1.8's engine trims that, and v1.10.2 measured how far rather than assuming: **195,784 tokens cost 1.16 GiB of host headroom, 225,051 cost 1.57 GiB, 249,500 cost 1.52 GiB** (MemAvailable sampled throughout, floor 6.9 GiB), with **needle 3/3 exact** at 200,058 / 230,231 / 249,838. So memory is no longer what caps this lane; the engine's own `max_req_input_len` (262,138) is, and 250,000 leaves 12,138 tokens of slack under it. Prompts above the ceiling get a clear 400 (`context_too_long`, `code: context_length_exceeded`) |
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
Two independent parties have since measured the same thing: blazux score fp8 KV as a
"measurable quality cost" on their 17-scenario agentic tournament and keep bf16 in
production, and the poster who announced MiaAI Lab's 1M recipe on the NVIDIA forum came
back the same evening reporting the fp8-KV build "significantly more degraded" in real
use with coding agents. **A 1M window on one GB10 is reachable today and an fp8 KV cache
is what buys it**, so this lane serves 262,144 with a bf16 cache and the 1M mode this
repo ships is the 27B one, where the checkpoints carry their own calibrated KV scales.
The full survey, including the one idea from those stacks worth taking (blazux's `hybrid`
side layers: +20% decode and +8% pool at an identical tournament score) is in
BENCHMARKS.md, "What the other one-Spark stacks measured about QUALITY".

The NEXTN speculative head is the model's own next-token module (its 31
tensors ship in the checkpoint in BF16, hence `unquant` for the draft): drafts
are verified by the target, so output quality is exactly the target's.

**One thing still wedges this lane: a prompt larger than the KV pool, sent
straight to the engine port.** It is queued and never admitted, which stalls
every request after it, and aborting it does not bring the scheduler back (the
engine logs `not found in rid_to_state`, its answer to the scheduler's abort
echo). This is this box's own measurement, not
[sglang#36333](https://github.com/sgl-project/sglang/issues/36333) (a request
left decoding after its client disconnects, closed on 2026-10-15 as fixed by
[#35255](https://github.com/sgl-project/sglang/pull/35255), which every image
this repo pins carries). The closest upstream report is
[sglang#41841](https://github.com/sgl-project/sglang/issues/41841), fixed by
[#41872](https://github.com/sgl-project/sglang/pull/41872) on 2026-10-12, which
this lane's image does not carry; whether it ends this wedge is not measured
here. Only a restart clears it, and at 15 minutes that is worth avoiding:
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

## The lane's other config: FP4-Local, on vLLM

`nvidia/Qwen3.8-Flash-Next-FP4-Local` is NVIDIA's QAD (quantization-aware
distillation) FP4 build of the same model: **102,262,310,289 bytes measured at
the pinned `b9ce18a` (95.24 GiB, what the card rounds to ~96 GiB), against the
176B NVFP4 tree's 136 GB** (units noted: the card counts GiB, this page's disk
numbers are GB of 10^9). Its scale factors are in a new lossless compression
format that upstream vLLM does not read yet: the NVIDIA card is explicit that
a community container is required, so this config runs the pinned
`eugr/spark-vllm-b12x` image, not SGLang. That makes it a *config* of this lane
rather than just another target: the serving engine changes, and only the
installer renders the unit and its launcher around it, so `switch-model.sh` can
switch onto that render, never into one it has to make (below).

```
MODEL_CHOICE=flash-fp4-local ./install.sh      # make it the serving config
./switch-model.sh flash-fp4-local              # lane-level bring-back once its launcher is the unit's
./switch-model.sh flash-fp4-local --fetch      # only the checkpoint, once the b12x image is here
./prepare-lane.sh flash flash-fp4-local        # image and checkpoint, no serving
MODEL_CHOICE=flash ./install.sh                # back to the SGLang config above
```

It keeps the lane's whole serving surface: the same unit (`qwen38-flash.service`),
the same launcher pattern (`launch-flash-fp4local.sh` beside `launch-flash.sh`;
the unit's `ExecStart` says which one is installed), the same served name
(`qwen3.8-flash-next`, so the proxy, the cockpit and opencode keep working with no
new mapping), and the same patched-template and media-hardening steps: this
build's own patched template, served from `chat-template-flashnext-local.jinja`.
The cockpit lists it as the eighth target: its Download runs
`./prepare-lane.sh flash flash-fp4-local` (image and checkpoint, no launcher on
the box needed) and its Load runs `./switch-model.sh flash-fp4-local`. That
switch is a lane-level bring-back, and it claims only what `install.sh` has
already installed: when the unit's `ExecStart` names `launch-flash-fp4local.sh`
it fetches the checkpoint, regenerates the served chat template from that
patched file, enables `qwen38-flash.service`, disables the other lane units,
and updates the keepalive ceiling, opencode's default and limits and the
conformity target. While the unit still names the SGLang launcher it refuses,
with the exact install command; `--fetch` works in either config and with no
flash unit installed at all, but fetches the checkpoint only and needs the b12x
image already on the box (`./prepare-lane.sh flash flash-fp4-local` fetches
both without serving), and like every flash target it is refused on a GPU
outside ATS. The reverse direction is the same rule: an SGLang
flash target on a box running this config is refused, and `MODEL_CHOICE=<target> ./install.sh`
is the answer it says. Checkpoint switches inside each config work as before.

What differs:

* **No PLE table.** Nothing generates one here and no backing file is ever
  written: with `VLLM_PLE_TABLE_MEMORY=disk` (the card's own DGX Spark setting)
  the b12x runtime reads the 47.7 GiB n-gram table's rows *directly from the
  checkpoint's own safetensors*, over io_uring inside the container. So there is
  no rewrite at every boot and no 52 GB file beside the checkpoint: the disk
  ask for this config is 114 GB (106 GiB) where the SGLang flash configs ask
  248 GB (180 GiB for the checkpoint and its caches, plus 50 GiB for the PLE
  table), and the launcher's `seccomp=unconfined` is load-bearing: docker's
  default seccomp profile blocks io_uring.
* **The ATS guard, inherited.** The lane's guard covers this config whole:
  install, `prepare-lane.sh`, the switch and `--fetch` refuse it on a GPU
  outside ATS, and its launcher asks `flash-ats-check.sh` first and stops with
  exit 78 (the unit's `RestartPreventExitStatus=78`), so the cockpit shows
  **cannot start on this GPU**. This engine reads no table file through host
  page tables; whether its io_uring path needs ATS is not measured, so the
  guard stays until a box outside ATS shows it serving.
* **The key, on the repo's terms.** This build's own guard puts `/metrics`
  behind the server key (its `UNGUARDED_PATHS` lost the path and the upstream
  toggle for it, read in the pinned image, 2026-10-19), which would blind the
  cockpit's five-minute counter window and the installer's probes, readers
  the repo's contract keeps key-free on every lane. So the FP4-Local launcher
  hands the engine no `--config` with a key at all: `vllm_engine_auth.RequireKey`
  is the only gate, reading the box's key from the mounted file and exempting
  exactly `/health` and `/metrics`, as the SGLang lanes do. The config leaves
  `VLLM_SERVER_DEV_MODE` off, and that is load-bearing: dev mode mounts vLLM's
  own `GET /server_info`, which the proxy reads as SGLang's info route; it finds
  no `max_total_num_tokens` in it, never falls back to the `/metrics` pool, and
  refuses every big prompt as "unknown pool, engine warming up" (found on the
  box, 2026-10-19). "vLLM has no `/server_info`" is how the proxy and the
  cockpit tell the two dialects apart. The price is no `/reset_prefix_cache`,
  so `bench-agent.py --flush` cannot make a cold-shape run on this config.
* **Boots.** vLLM's AOT-compiled kernels live in the mounted caches under
  `~/.config/qwen38/flashnext-local-cache/`: the cold first boot loads the
  checkpoint (95.24 GiB) and builds them, later boots load the cache. The
  installer waits 48 min for it where the SGLang configs get 20, and the
  cockpit gives the target a 45-minute boot budget. Those walls are this repo's
  own boot budgets, set with margin; they are not measured load times, and
  they are not the card's either: the card publishes no load times to take
  margin from. One boot of this config has been timed on the reference box
  (2026-10-19, a restart on warm caches): 4 min 44 s to "Application startup
  complete", of which the weights took 184 s and the engine's own init 69 s;
  the cold first boot that builds the AOT kernels is not timed yet. Its measured
  decode (59 tok/s greedy median, 3.1 tokens per MTP step) is in BENCHMARKS.md.
* **The engine's knobs** are vLLM's (`--gpu-memory-utilization 0.8` where SGLang
  takes the `FLASH_MEM_FRACTION` share; `--max-num-seqs 8`, MTP x4 through
  `--speculative-config`). Its recipe card and drift check read the file at the
  unit's `ExecStart` with vLLM's flag spellings, so the config the box does not
  run reports nothing: cross-engine comparison would read every flag of the
  other dialect as drift.
* **Quality**, per the card: GPQA Diamond **91.5 %** against the BF16 base's
  92.2 %, ToolEvalBench **88.7 %** against 89.4 %. That is NVIDIA's measurement,
  not this box's, and the lane's canaries do not close the gap: they are SGLang
  probes, and the cockpit does not run them against this config: a vLLM engine
  is judged by vLLM facts (`/health`, the served model), as every vLLM lane on
  this box is. Until this box probes the checkpoint itself, no quality number of
  ours is claimed for it.

[Back to the README](../README.md)
