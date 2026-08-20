# Changelog

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
