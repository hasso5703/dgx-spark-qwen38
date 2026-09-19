# The GB10 unified-memory trap, in full

Why `--mem-fraction-static` is the number that decides whether this box stays alive, what each pin measured, and why the SGLang cookbook's 0.80 is not a contradiction. The README carries the rule; this carries the evidence.

SGLang's memory accounting **does not see 25-40 GB** of transient allocations on GB10 unified memory (the flashinfer fp8 autotuner and CUDA graph capture allocate outside the tracked pool). Running `--mem-fraction-static` above **0.50**, or running SGLang natively (outside Docker), can drive host available memory to **zero**: on a machine where SSH often rides on the same memory, that means a hard freeze only a power cycle fixes. We learned this the hard way.

This repo's service is safe by construction:

- Docker hard caps: `--memory 100g --memory-swap 100g` (a runaway kills the container, never the host; note the cgroup does *not* see CUDA unified allocations, so the real guard is the fraction)
- `--mem-fraction-static 0.50` (plenty for 262K context at batch ≤ 4)
- `Restart=always` + a clean `ExecStartPre docker rm -f` so even a power cut leaves nothing stale (`always` and not `on-failure`: a Triton compile crash measured on 2026-08-22 ended in `SystemExit: 0`, which `on-failure` never relaunches)

The 1m mode deliberately runs **0.76** inside the same docker caps, with the autotuner
disabled. It ran 0.70 until v1.14, and the number moved with the image, not with the appetite:
the official release claims a smaller static budget for the same fraction, so 0.70 there cost
15% of the pool (770,118 tokens against 906,524) while leaving 10 GB of GPU memory unclaimed.
0.76 hands that back and still leaves a wider margin than the old pin did, 21.30 GB free after
graph capture against 19.38. **0.80 was measured crashing** under 3 concurrent requests (2 GiB
free, Triton `CUDA operation not permitted`), and the 25-40 GB invisible-allocation bursts above
all belong to native runs and the autotuner. Treat anything past 0.80 as livelock territory. What
bounds the fraction is the unified pool, not the container: `--memory 100g` caps host RSS and the
cgroup does not see CUDA unified allocations, measured at 0.76 under a 5,623-token generation
with the container holding 7.15 GiB of its 100 GiB throughout. The number to watch is the GPU-side
headroom after graph capture (21.30 GiB at 0.76, against 19.38 at the old pin).

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

[Back to the README](../README.md)
