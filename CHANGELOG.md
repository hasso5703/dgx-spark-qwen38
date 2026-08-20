# Changelog

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
