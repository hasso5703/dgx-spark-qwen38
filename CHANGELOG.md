# Changelog

## v1.5.2 (2026-08-29): flash lane correctness hotfix

**Fixes a silent long-context corruption in the v1.5 flash lane.** The v1.5
overlay widened the QSA trtllm sparse-decode gate to sm_121; on GB10 that
routes decode to FlashInfer's XQA kernel, which emits runs of token id 0 deep
in long contexts (hashd1ve, 2026-08-29: 1 of 4 requests at 120k tokens, 4 of
4 at 210k). The gate is back to upstream and sm_121 decode uses the packed
Triton varlen kernel of sglang#36845 (vendored verbatim, sha256-pinned,
attribution in flash-sglang/ATTRIBUTION.md). Serving image tag: qwen38-flash:v1.5.2.

Validation on the reference box: exact needle retrieval 12/12 at 40k, 67k and
80k real prompt tokens (the depths the probe reached), on top of the kernel
author's 4/4 at 120k/190k/210k. Not yet validated here beyond that: prompts of
~120k tokens wedged this box's scheduler twice on 2026-08-29 (prefill stalls,
`/health` keeps answering, no output), a pre-existing behavior unrelated to the
kernel route and under investigation (mamba state cache of 9 slots against one
checkpoint per 1024-token prefill chunk is the leading hypothesis). Until that
lands, treat ~100k tokens as the practical ceiling of one prompt on this lane.

Also in this release, after a real scheduler wedge on this box (two ~109k-token
requests admitted into a 159k-token KV pool, scheduler spinning at 100 % CPU
with `/health` still answering):
- flash lane: `--max-running-requests 1` (one giant context at a time; a second
  request queues instead of fighting for the pool). Small concurrent requests
  were never the use case of a single-user box.
  The boot log shows the flash lane never actually served two requests at once:
  the mamba state cache is sized to 9 slots by default and each running request
  needs 5 (`max_running_requests is capped to 1 by the mamba state cache`), so
  one active request plus four cached prompts fill it and the next prompt forces
  an eviction, which is where both hangs of 2026-08-29 happened. v1.5.3 sizes
  that cache explicitly.
- opencode config for the flash lane: context 110000 (was 226000, which let a
  conversation outgrow the pool before compaction). The converging install now
  merges the lane's limits into an EXISTING ~/.config/opencode/opencode.json too
  (targeted edit, dated backup, comments and other providers untouched), instead
  of only printing a merge reminder.
- `uninstall.sh --list` knows qwen38-flash:v1.5 as a superseded image.

## v1.5.1 (2026-08-28)

Flash lane robustness hotfix: poisoned PLE table self-healing.

- A flash boot interrupted while the 48 GB PLE mmap backing file is being
  written (power cut, manual stop, kill) left a stale table that wedged every
  later boot in a silent scheduler spin (100% of one core, empty journal, no
  IO). Root-caused and reproduced live; regenerating the table fixed it in
  one boot. The launcher now sets a `.loading` marker at start and a detached
  waiter removes it once `/health` answers; finding the marker at launch
  means the previous boot never got there, so the table is wiped and rebuilt
  automatically (one ~11 min boot instead of a wedged lane).
- No flag, image, or checkpoint changes; 27B lanes untouched.

## v1.5 (2026-08-28)

The flash lane moves to SGLang: working prefix caching, tool-loop fix, vision on.

- **Flash target now serves on SGLang** (`lmsysorg/sglang:qwen38flashnext`,
  digest-pinned), same engine family as the 27B pair. The reason: **prefix
  caching works there** and vLLM's is blocked by a GB10 GDN bug (tracked
  upstream as vllm#54173, filed with this box's exact environment). Measured on
  the reference box: a 30K-token conversation re-served in 0.5 s instead of
  18.4 s (x36); a fresh question on a known 30K prefix in ~3 s (x5.8); decode
  34-42 tok/s (vLLM lane: 31); vision validated including with large prompts;
  canaries 4/4; needle at 100K passing.
- **`flash-sglang/` vendored overlay** (MIT, by hashd1ve, over Apache-2.0
  SGLang sources): patch 1 mmaps the 51B PLE table from NVMe
  (`SGLANG_QWEN4_PLE_MMAP_DIR`, ~48 GB backing file written once at first
  boot, `PLE_DIR` env, default `~/flashnext-ple`); patch 2 fixes the QSA
  resolvers on sm_121 (+32% decode). Patch 2 is the same fix as upstream
  PR #36556, which independent reports confirm also fixes the token-ID-0
  tool-call loop (#36537); this repo carries **both** of that PR's resolver
  edits (the hashd1ve tree had one) after verifying the FA4 dispatcher module
  exists in the pinned image. Provenance is proven at vendor time: each file
  diffs against the module extracted from the pinned image in exactly the
  patched region.
- **The lane authenticates** (`--api-key`, like everything else here); early
  public recipes for this model ran open.
- The v1.4 vLLM lane is retired (git history keeps it: `git checkout v1.4`).
  Upgrades from v1.4 keep port/cache/model choices and regenerate the launch
  script on the new engine. The flash lane now also serves the Anthropic
  protocol, like the 27B lane.
- **27B: known upstream reports, not reproduced here.** sglang#36548 (DFlash2
  can attach a message to the wrong context under concurrent load) and
  sglang#35150 (speculative verify diverges from plain decode in the SSM
  transition) affect DFlash2 builds newer than this repo's pins. Reproduction
  on this repo's exact pinned build: 100 greedy ordering prompts, serial AND
  at concurrency 8, zero wrong answers. The pins stay; documented options if
  you serve many concurrent correctness-critical streams: the
  `RadixArk/Qwen3.8-27B-NVFP4-BF16-LMHead` target (`009632f`), DSpark v2, or
  `--max-running-requests 1`.
- Known upstream behavior documented and reproduced on this lane
  (sglang#35537): with chunked prefill, a long-decoding request can starve new
  requests until it completes. Single-agent use is unaffected.
- **`./uninstall.sh --list`**: read-only inventory of every artifact any
  version of this repo (v1.0 through v1.5) may have left on the box: units,
  drop-ins, unit backups, config, the oc launcher, local AND base docker
  images (matched by tag or digest: a digest pull leaves no tag), the five
  checkpoints, the PLE backing file, each with its size. The uninstall itself
  now prints reclaim commands only for what is actually present, and the
  installer points out superseded engine images after an upgrade (e.g. the
  ~40 GB of v1.4 vLLM images) without ever deleting data on its own.
- CI: flash guards moved to the SGLang launcher (radix cache, NEXTN, split
  attention backends, api-key, PLE mmap dir, no `--language-only`, revision
  lock), vendored-file gate greps, generator executed on all three shapes.

## v1.4 (2026-08-27)

Qwen3.8-Flash-Next 176B as a third switchable target, on one box.

- **New target `MODEL_CHOICE=flash`**: Qwen3.8-Flash-Next (176B hybrid MoE, 6B
  active, QSA sparse attention, multimodal) in RadixArk NVFP4, served by vLLM
  with the model's own MTP speculative head. Full native 262,144 context on a
  single GB10. Measured on the reference box: decode ~31 tok/s
  (code/reasoning), prefill ~2,280 tok/s at 60K and ~2,100 at 189K, quality
  canaries 4/4, needle passing at 190K+ depth.
- **`flash/` vendored overlay** (Apache-2.0, by blazux): the checkpoint's 51B
  N-gram (PLE) table is mmap-served from NVMe through the page cache instead of
  living in the unified pool; that single change is what makes the model fit.
  Two files, sha256-pinned, and the bit-exactness test of the gather runs
  inside the freshly built image at every install (the build refuses to tag on
  failure).
- **`qwen38-flash.service`**: same hardening as the 27B unit (docker caps
  110g, Restart=always, ExecStartPre rm, API key, serve-time --revision lock,
  HF_HUB_OFFLINE boot) plus the GB10-specific serving flags (PIECEWISE CUDA
  graphs with the PLE op split out, prefix caching off on sm_121, FlashInfer
  autotune off), each now guarded by CI.
- **Cross-engine switching**: `./switch-model.sh flash` / `stock` /
  `uncensored`. Both units publish the same port and are never enabled
  together; the switch flips which unit starts at boot, re-verifies the
  checkpoint, regenerates the target template and re-points the opencode
  default model. The installer converges on whichever target is installed,
  including flash, and never resets a served choice.
- **opencode config generation reworked**: one provider per installed engine
  (`qwen38`, `flashnext`), each with `low`/`medium`/`xhigh` reasoning-effort
  variants (xhigh added for the 27B too), written by json.dump instead of a
  heredoc; the default model follows the installed target. Flash limits:
  226,000 context / 32,000 output inside the 262,144 window.
- **Patched chat template for flash** (`chat-template-flashnext.jinja`): the
  upstream Flash-Next template ships the same two agent-hostile behaviors as
  the 27B one (500 on reasoning_effort "max"/"high", 500 on mid-conversation
  system messages); the same two surgical fixes apply, verified by rendering
  all six effort levels.
- CI: flash unit render guards (the GB10-critical flags can never silently
  regress), flash overlay manifest check, opencode generator executed on all
  three install shapes, pin contracts extended (12 for switch-model.sh).
- Uninstall now removes both engines and lists the flash reclaim paths.

## v1.3 (2026-08-27)

Release validated end to end on the reference box before tagging: a converging
upgrade over the live install (only the Description and the new `--revision`
changed in the unit, everything else preserved), stock and uncensored switches
with reboots proving the revision lock (the pinned sha served while the cache
held a newer upstream revision AND a `refs/main` pointing at it), `./run.sh`
foreground with a clean stop, a from-scratch cache simulating a fresh machine
(which caught the Xet stall and the anonymous throttling fixed below),
`./bench.sh` at 50.7 tok/s greedy median (reference: ~50), and a real opencode
session writing files through the `oc` launcher and the keepalive proxy
(which caught the `--yolo` flag-position fix below).

- The 1M context mode: `CONTEXT_MODE=1m ./install.sh` installs the preset that
  serves the reference box daily since 2026-08-22, as one converging command.
  It patches YaRN static scaling (factor 4.0) into both cached `config.json`
  files via the new `patch-yarn.py` (target AND DFlash2 draft, originals
  backed up as `config.json.pre-yarn`; the shared script also replaces
  `switch-model.sh`'s inline patcher and handles the draft's root-level
  config shape), renders a dedicated 1m unit (`--context-length 1010000`,
  `--mem-fraction-static 0.70`, `SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN=1`,
  `HF_HUB_OFFLINE=1` to shield the patched configs from Hub re-resolution,
  `Restart=always` because a Triton crash measured on 2026-08-22 exited 0 and
  `on-failure` never relaunched it), and installs the vendored keepalive proxy
  (`keepalive-proxy.py` v6.6 + `qwen38-keepalive.service` on `PORT+1`): SGLang
  buffers tool-call arguments (127 s of measured silence on a 400-line write)
  and agent CLIs abort silent streams, so the proxy injects the official
  Anthropic ping event / an authentic empty OpenAI chunk every 10 s at SSE
  event boundaries only, closes the upstream when the client leaves, and
  reports an explicit error after 3600 s of upstream silence. `./run.sh`
  refuses 1m (the proxy is a service); `uninstall.sh` removes the proxy
  service too. Native stays the default and is byte-identical to v1.2.7
  behavior.
- Converging upgrades: re-running `install.sh` (or the `get.sh` one-liner) now
  reads the installed unit and keeps the operator's choices instead of
  silently resetting them to the defaults: the target model (stock/uncensored,
  or a custom `--model-path`, kept verbatim with its download and template
  steps skipped), the context mode (native/1m, plus the proxy port), the
  port, and the HF cache location. An explicit env var (`MODEL_CHOICE=`,
  `CONTEXT_MODE=`, `PORT=`, `PROXY_PORT=`, `HF_CACHE=`) still wins. The
  previous unit is backed up to
  `~/.config/qwen38/qwen38-sglang.service.bak-preupdate` before being
  rewritten, so hand-tuned flags stay recoverable. The `oc` launcher passes
  `--yolo` after the user's arguments, not before: opencode's parser rejects
  global flags placed before a subcommand (`oc run ...` printed the help
  instead of running; caught by the release campaign's live opencode test).
- The repo's client story moves from Claude Code to opencode. `install.sh` now
  writes a complete provider config to `~/.config/qwen38/opencode.json`
  (limits sized per context mode so no request can ever 400, reasoning-effort
  variants, vision declared, key referenced via `{file:...}`), installs an
  `oc` launcher to `~/.local/bin/oc` that lifts opencode's hidden 32000
  max_tokens cap to the declared output limit (never clobbering a foreign
  `oc` binary), and no longer writes `claude-code.env` (an existing copy
  keeps working but is unmaintained). The
  Claude Code warmup is removed: `--with-claude-warmup` is now a no-op with a
  notice, `warmup-claude-code.sh` leaves the repo, and an installed
  `warmup.conf` drop-in from an earlier version is cleaned up on upgrade
  (other drop-ins are untouched). The template patches are unchanged: they
  were always server-side and client-agnostic.
- New `extras/` directory, two field-tested opt-ins from the reference box:
  `extras/opencode/auto-continue.js` (opencode plugin that resumes a session
  after a transient technical error or a stuck compaction; never after a
  deliberate abort or an auth problem; stops after 25 relaunches without
  progress) and `extras/cake-ingress/` (ingress anti-bufferbloat: CAKE shapes
  received traffic just under the measured downlink so SSH and the API stay
  at milliseconds while a model download saturates the link; interface
  auto-detected, BANDWIDTH deliberately required and validated, full
  measurement sweep in the script header, `setup.sh --uninstall` restores
  stock networking). The README's Operations section also documents
  `/abort_request` for killing abandoned generations on direct connections.
- The keepalive proxy ships with every service install, not only 1m: SGLang
  buffers tool-call arguments at any context length (127 s of measured
  silence on one 400-line write) and agent CLIs abort silent streams, so the
  proxy is the difference between a finished write and a client retry loop.
  The generated opencode config points at the proxy port on service installs
  and at the server directly with --no-service (where ./run.sh has no proxy).
  The oc launcher resolves opencode from PATH with a fallback to
  ~/.opencode/bin/opencode.
- The native unit moves to `Restart=always` (matching the 1m unit and the
  reference box): a Triton compile crash measured on 2026-08-22 terminated
  with `SystemExit: 0`, a clean exit in systemd's eyes, so `Restart=on-failure`
  never relaunched it. Documentation sweep: real disk numbers everywhere
  (~90 GB fresh, +22 GB with both targets cached), the `HF_HUB_OFFLINE`
  guidance rewritten from measurements, and a note that `claude-code.env` is
  never overwritten again.
- Serve-time revision lock: the units and `run.sh` now pass the pinned
  `--revision` to the server for the target model (the draft already had its
  own pinned revision flag). Until now the pin only governed the download:
  at boot the server could still resolve the repo's "main" and pick up an
  upstream push (RadixArk has already published two newer stock revisions).
  With the sha passed to the server, upstream changes cannot affect what is
  served, online or offline; `switch-model.sh` rewrites the revision together
  with the model path, and a kept custom model reuses its unit's existing
  revision or none. Combined with `HF_HUB_OFFLINE=1` in the 1m unit, the
  running configuration is exactly the repo's, whatever happens upstream.
- Fresh-machine hardening and small fixes, ahead of the release stress test.
  Downloads: a fresh-cache pull stalled forever at 3.3 GB during the release
  campaign; root-caused live to the hub library's Xet transfer backend (an
  established socket moving zero bytes, 0-8 MB/s when moving at all, while
  the classic CDN path measured 89 MB/s on the same box in the same second).
  The download containers now set `HF_HUB_DISABLE_XET=1`, plus
  `HF_HUB_DOWNLOAD_TIMEOUT=30` and a 5-attempt resume loop as a belt for any
  other silent stall, and `HF_TOKEN` is passed through for authenticated
  rate limits (a token already in `$HF_CACHE/token` keeps working through
  the mount). Also,
  pinned-sha downloads now also write the cache's `refs/main` when absent
  (`huggingface_hub` only writes refs for named revisions, so on a fresh
  machine the 1m unit's `HF_HUB_OFFLINE=1` boot would fail to resolve "main";
  never overwritten if present). `switch-model.sh` also regenerates the
  patched chat template from the target's own snapshot on every switch
  (byte-identical between the two known targets today, verified; the belt
  keeps the served template following the served model if one ever diverges).
  `run.sh` error messages now echo the fix-it command with the active
  `MODEL_CHOICE` prefix, so following the advice prepares the configuration
  that failed, not the default one. `uninstall.sh`'s reclaim list gains the
  uncensored checkpoint (~22 GB). The `oc` launcher passes `--yolo` (the
  reference box's way; documented, removable in the launcher file).
- Model switch option: `MODEL_CHOICE=stock|uncensored` in `install.sh` and
  `run.sh`, plus `./switch-model.sh` for surgical live installs (downloads the
  checkpoint, patches 1M YaRN config if the unit uses it, rewrites only the
  `--model-path` line, daemon-reload; no service restart by the script itself).
  Uncensored target = huihui-ai abliteration re-quantized with the identical
  RadixArk modelopt NVFP4 recipe
  (`edp1096/Huihui-RadixArk-Qwen3.8-27B-abliterated-NVFP4` @ `21565d3`);
  the DFlash2 drafter stays unchanged in both modes. `patch-template.py` now
  takes the repo as an optional fourth argument. `switch-model.sh` rewrites the
  unit via a temp file + `sudo install -m 644` (no `sudo sed`) and skips the
  download/YaRN steps idempotently when already done. See README, "Stock ↔
  Uncensored target model".

## v1.2.7 (2026-08-21)

`run.sh` now passes `--sleep-on-idle` too, matching the systemd unit which got the flag in
v1.2.6. Contributed by struxoje in [#4](https://github.com/hasso5703/dgx-spark-qwen38/pull/4),
opened before v1.2.6 was even tagged: the community caught the run.sh path while the unit
path was being A/B tested. Same behavior as measured in the v1.2.6 notes (scheduler idle
CPU 101 % -> 1.7 %, wake-up TTFT unchanged).

## v1.2.6 (2026-08-21)

The systemd unit now passes `--sleep-on-idle`. Without it, SGLang's scheduler busy-spins a
full CPU core whenever the queue is empty, which two users measured as +10-12 W at the wall
(alef204 and emX0r on the forum thread, root-caused by emX0r in MiaAI-Lab issue #4; the flag
exists in the pinned image, so no rebuild). A/B on the reference box before adopting:
scheduler CPU at idle 101 % -> 1.7 %, module power 12.1 -> 10.5 W (median over 60 s), wake-up
TTFT unchanged after 60 s and 300 s of idle (0.234-0.240 s -> 0.234-0.239 s), decode
throughput in family (41.5 tok/s code, 52.8 math, answers correct). Existing installs:
re-run the one-liner or `./install.sh --no-start`, then `sudo systemctl restart qwen38-sglang`.

## v1.2.5 (2026-08-21)

Claude Code env pair rebalanced after a real 64K truncation report and a request-capture
study on Claude Code 2.1.238. `CLAUDE_CODE_MAX_OUTPUT_TOKENS` goes 64000 -> 128000, which
is the CLI's hard ceiling for a third-party model id (any higher value, e.g. 258048, is
silently capped back to 128000). `CLAUDE_CODE_MAX_CONTEXT_TOKENS` goes 258048 -> 130048,
because the pair must satisfy CONTEXT + OUTPUT <= 258048: the server rejects any request
where input + max_tokens exceeds 262144 (a 400, no clamping), Claude Code never shrinks
max_tokens to fit, and its auto-compaction reserves at most 20000 output tokens. The
previous 64000/258048 pair had a latent dead zone (every request past ~198K input tokens
got a 400 before auto-compaction fired at ~225K); no field report yet, fixed preemptively.
Users who prefer longer context over very long single answers can set 64000/194048, as
documented in the README. Existing installs: re-run the one-liner (or `./install.sh
--no-start`) to regenerate `claude-code.env`; the serving image and unit are untouched.

Second fix from the lifecycle audit: the step-3 "container sees the GPU" line always printed
blank, because the image's entrypoint banner starts with an empty line and the check displayed
the first line of output. The GPU line comes after the banner. The check now extracts the
actual `GPU 0: ...` line, refuses to continue if no GPU line appears even when the command
exits 0, and runs one container instead of two. Verified on the reference box, including with
the GPU busy serving.

## v1.2.3 (2026-08-20)

One fix from the lifecycle audit: the systemd unit file was installed with mode 600 (a
`mktemp` + `sudo cp` interaction), so the service ran fine but `systemctl cat` and any
non-root inspection of the unit failed with permission denied. `install.sh` now writes it
with `install -m 644`, the standard mode for unit files. Re-running the one-liner (or
`./install.sh --no-start`) fixes the mode in place without restarting the service.

## v1.2.2 (2026-08-20)

Typography pass, no functional change. All prose dashes were removed from every document,
script message and comment (house style). One comment line inside the vendored overlay
(`dflash2/sglang/srt/models/dflash.py`) was reworded, so the manifest is regenerated and the
serving image tag becomes `qwen38-dflash2:v1.2.2` (rebuilt automatically by `./install.sh`,
about a minute, offline). Runtime behavior, flags, pins and performance are identical to
v1.2.1. Also validated in this release cycle, on the reference box: the full lifecycle
`get.sh` one-liner (fresh install, upgrade over a modified clone, uninstall/reinstall),
`./run.sh` end to end, and the manifest tamper guard (a modified overlay file refuses to
build).

## v1.2.1 (2026-08-20)

Correctness alignment of the vendored DFlash2 overlay with upstream, plus the quality study
that the community's reports triggered.

- **Quantized-head logits: crop → contiguous mask.** Upstream merged the official quantized
  lm_head support for the DFlash2 selector hours after this repo's v1.2 vendoring
  ([sgl-project/sglang#35496](https://github.com/sgl-project/sglang/pull/35496)): slicing the
  padded local vocab produces a non-contiguous view that flashinfer's radix top-k rejects or
  can misread; the fix keeps the logits contiguous and masks the padded tail to -inf. Both
  overlay call sites now follow that pattern. The serving image tag becomes
  `qwen38-dflash2:v1.2.1` (rebuilt automatically by `./install.sh`).
- **The quality question, measured.** Forum users reported a 2-6 point tool-eval drop and
  anecdotal hallucinations vs DSpark. We measured instead of guessing, on a deterministic
  server (reproducible to the point): tool-eval 93/93 (DSpark) vs 91/91 (DFlash2), a stable
  3-scenario delta; then GSM8K 200: **exact parity, 188/200 both**; IFEval 200: split within
  noise (prompt-level favors DSpark with 15 excluded timeouts muddying its denominator,
  instruction-level favors DFlash2). A token-identity test at temperature 0 against the pure
  autoregressive model shows **both** drafters diverge from it (10/10 prompts each, equally
  early): speculative decoding is lossless in exact arithmetic, not in floating point, and
  near-tie argmax flips cascade. The 2-3 tool-eval points are those flips landing, not a
  quality regression. Full methodology and numbers in BENCHMARKS.md, "The losslessness study".
  DFlash2 stays the default.

## v1.2 (2026-08-20)

**DFlash2 becomes the default.** The service now serves the z-lab DFlash2 drafter instead of
DSpark. Measured on the reference box against the v1.1 config, same battery, same night,
deterministic mode, thinking on: every single-stream cell improves except math (parity), with
free prose FR 20.2 vs 14.0, reasoning FR 43.5 vs 30.5, code DE 39.4 vs 25.4; aggregate
throughput 135-148 tok/s at concurrency 8 (vs 100-104) and 258 tok/s at c32
(`--max-running-requests 32`). Quality canaries pass; speculative decoding remains lossless by
construction. Validated end to end including a full machine reboot.

No official SGLang image contains DFLASH2 yet (merged upstream 2026-08-19), so `install.sh`
now builds the serving image locally: the same pinned base digest plus five sha256-verified
overlay files vendored in `dflash2/` (Apache-2.0 from sgl-project PR #35371, plus MiaAI-Lab's
MIT-licensed quantized-lm_head fix; full provenance in `dflash2/ATTRIBUTION.md`, K choice per
r0b0tlab's block sweep). The build is offline and takes about a minute. The day an official
image ships DFLASH2, the repo repins to it and `dflash2/` is retired.

Upgrade: `git pull && ./install.sh` (key and template kept; ~4 GB one-time draft download;
first boot recaptures CUDA graphs). To stay on the DSpark config instead:
`git checkout v1.1 && ./install.sh`.

Also: the DSpark draft pin moves to RadixArk's 2026-08-16 revision (identical weights and
config; the commit only fixed the transformers reference code).

## v1.1 (2026-08-20)

Two flag changes, both measured overnight on the reference box (same battery, same night,
multiple boots per verdict). Upgrade: `git pull && ./install.sh` (your key and template are
kept, the unit is rewritten, the service restarts).

- **`--disable-flashinfer-autotune` (new)**: FlashInfer re-runs its kernel autotune at every
  boot and the result varies: the identical config measured anywhere between 92 and 111 tok/s
  aggregate at concurrency 8 depending on the boot, and up to ±15 % on verify-heavy single-stream
  cells (code, math). Disabling the autotune makes boots deterministic (single-stream cells
  reproduce to the decimal across boots, c8 within ±1.6 %) and cuts about 2 minutes of boot
  time, at a cost of roughly 2 % of the lottery's average. Every published GB10 comparison that
  did not control for this contains boot noise; see BENCHMARKS.md, "The boot lottery".
- **`--cuda-graph-max-bs 4` → `8`**: with `--max-running-requests 8`, decode batches of 5 to 8
  requests were falling outside the captured CUDA graphs and running eager. Capturing up to
  batch size 8 is worth +6.5 % aggregate throughput at concurrency 8 (measured deterministic,
  reproduced across boots) for about 0.4 GB of extra capture memory and a slightly longer first
  boot.

Net effect on the reference box: concurrency-8 aggregate goes from a 92-111 lottery to a stable
100-104 tok/s, and benchmarks against this config become reproducible.

Also in this release: `bench.sh` counts vLLM ≥ 0.27's renamed `reasoning` stream field and warns
above the physical ceiling (issue #2), BENCHMARKS.md gains third-party sections (vLLM DSpark
battery, long-prefix ladder) and the renamed-field trap.

Coming next: a DFlash2-based configuration (z-lab drafter) currently measures +40 % aggregate at
c8 and wins every single-stream cell on this box; it ships as the default once an official SGLang
release image contains DFLASH2 (merged upstream 2026-08-19). Watch this repo.

## v1.0 (2026-08-15)

Initial pinned release: NVFP4 + DSpark on SGLang, one-command install, systemd unit, benchmark
battery (`bench.sh`, `bench-matrix.sh`), BENCHMARKS.md with full methodology.
