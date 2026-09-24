# The flash lane: Qwen3.8-Flash-Next 176B on one Spark

How a 176B hybrid MoE serves on a single GB10, the three serving tiers, and what each one measured on this box. Moved out of the README in v1.14.2.

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
| **`context`** (default) | 4 | **279,872 to 463,488 tokens** across boots, and every one of them above the 262,144-token window, so a full-context prompt always fits | **47.9 / 47.1 / 30.9 tok/s** single stream (code, math, prose FR), **71.6 tok/s aggregate at 4 streams** (17.9-18.7 each) |
| `concurrency` | 8 | **468,480 tokens** measured 2026-09-12 with replayssm-spec (was 129,792 before it), so a full 262K prompt fits at 8 requests too | **43.7 / 44.2 / 29.5 tok/s** single stream (code, reasoning, prose FR, warmed, uncensored), **90.4 tok/s aggregate at 4 streams**, needle 8/8 exact to 140K at 13.5 GiB floor |
| `throughput` | 24, no speculation | ~286K tokens (upstream) | upstream: 83 tok/s of output at 24, 15.9 single |

Both speculative tiers are the cookbook's own verified single-Spark cells, which
score **GSM8K 97.1-97.3% on the full 1,319-question set** upstream. `context` stays
this repo's default after measuring 8 requests too: the 8-request pool came out
larger than the 4-request pool used to (replayssm-spec frees the draft depth
out of the state budget), but the per-session limits that make long agent
sessions work (175K/64K) are sized for one or two streams, and concurrent-load
memory is not measured yet. Same cell, concurrency pinned lower, longer sessions.

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
| **agent loop** (`./bench-agent.py`, 8 turns on an 8K prefix, work pinned at 130 tokens) | **27.0 ms/tok** median, TTFT flat at 0.31-0.34 s, **within ±35 ms of TTFT per 1,000 added prompt tokens** (where a cache that is not reused reads about 580): the prefix cache is being reused, with speculation on |
| **long-context retrieval** | **3/3 exact at ~120K** and **1/1 exact at 200,058 tokens** (`./needle.sh --mem`, fresh passphrase each), no run of token id 0 anywhere |
| quality canaries | 4/4 (merge, logic, French, primes) |
| prefill, cold | ~2,250 tok/s at 27K, ~1,960 tok/s at 200K |
| vision (image input) | works, including combined with large prompts |
| context window | 262,144 native, no YaRN |
| **context that fits** | **one prompt tops out at 250,000 tokens**, enforced by the proxy (`PROMPT_CEILING_TOKENS`), and by its share of the KV pool on the smaller tiers. This was 128K in v1.5.6 to v1.7 and 200K in v1.8 to v1.10, because on the v1.5 engine the prefill of a long prompt grew the footprint by ~0.27 GiB per 1k tokens past ~90k. v1.8's engine trims that, and v1.10.2 measured how far rather than assuming: **195,784 tokens cost 1.16 GiB of host headroom, 225,051 cost 1.57 GiB, 249,500 cost 1.52 GiB** (MemAvailable sampled throughout, floor 6.9 GiB), with **needle 3/3 exact** at 200,058 / 230,231 / 249,838. So memory is no longer what caps this lane; the engine's own `max_req_input_len` (262,138) is, and 250,000 leaves 12,138 tokens of slack under it. Prompts above the ceiling get a clear 400 (`context_too_long`, `code: context_length_exceeded`) |
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

[Back to the README](../README.md)
