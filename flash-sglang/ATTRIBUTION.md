# Flash-Next SGLang overlay: provenance and licenses

Since v1.5 the flash target serves on SGLang (the same engine as the 27B pair).
The official `lmsysorg/sglang:qwen38flashnext` image cannot serve the model on a
single GB10 box as shipped: the 51B N-gram (PLE) table wants ~48 GiB of pinned
host RAM the box cannot spare, and the QSA sparse-decode resolver rejects
sm_121, which sends decode to a fallback kernel that does not compile there.
`install.sh` (MODEL_CHOICE=flash) builds the serving image locally: the pinned
official base plus the two sha256-verified files in this directory.
`MANIFEST.sha256` is checked before every build.

The two files are the image's own modules with two small patches applied, by
[hashd1ve](https://github.com/hashd1ve/qwen38-flash-next-one-dgx-spark)
(`qwen38-flash-next-one-dgx-spark`, vendored 2026-08-28). License: MIT (their
patches) over Apache-2.0 (the underlying SGLang sources). Verified locally:
each vendored file diffs against the module extracted from the pinned image in
exactly the patched region, nothing else (4 em/en dashes in comments replaced
with house punctuation, AST-verified; 2 more in the 2026-09-03 KDA revision of
`qwen_sparse_attn_backend.py`, same treatment).

- `qwen4_exp.py` (patch 1, "PLE table to NVMe"): with `SGLANG_QWEN4_PLE_MMAP_DIR`
  set, the PLE table's backing store becomes a file-backed mmap
  (`torch.from_file(shared=True)`) instead of pinned host RAM, with
  `madvise(MADV_RANDOM)` so a cold row costs one 4K page instead of a readahead
  window (measured upstream: ~560x less disk traffic per token). On GB10's
  coherent CPU-GPU memory the gather kernel dereferences the pageable pointer
  directly; the table never has to be resident. Without the env var the file
  behaves exactly like upstream. The ~48 GiB backing file is written once at
  first boot and reused afterwards.
- `qwen_sparse_attn_backend.py` (patch 2, "QSA decode on sm_121"): two resolver
  edits. The decode gate widens from `is_sm100_supported()` to
  `is_sm100_supported() or is_sm120_supported()`, unblocking FlashInfer's
  working `trtllm_batch_decode_with_kv_cache` on GB10 (+32% decode measured
  upstream, reproduced here); the packed sparse-decode fallback routes to
  SGLang's architecture-owned FA4 dispatcher on SM120. This is the same fix later proposed upstream as
  [sglang PR #36556](https://github.com/sgl-project/sglang/pull/36556), which
  three independent reports confirm ALSO fixes the token-ID-0 tool-call loop
  ([sglang #36537](https://github.com/sgl-project/sglang/issues/36537)): the
  broken fallback path was poisoning speculative draft-verify batches. The
  hashd1ve tree carried only the first of the PR's two resolver edits; this
  repo applies the second one (routing the packed sparse-decode fallback to
  SGLang's architecture-owned FA4 dispatcher on SM120) verbatim from the PR,
  after verifying the dispatcher module exists in the pinned image. When an
  official image ships with that PR merged, this overlay can shrink to patch 1.

Why the serving flags look the way they do (carried in
`qwen38-flash-launch.sh.template`, guarded by CI):

- `--prefill-attention-backend triton --decode-attention-backend trtllm_mha`:
  the combined `--attention-backend trtllm_mha` is refused (prefill side is
  gated to SM100); splitting the phases is allowed and is where the +32% comes
  from. FlashInfer prefill hits a CUTLASS kernel that does not compile on SM120.
- `--mamba-radix-cache-strategy extra_buffer`: prefix caching for the hybrid
  GDN layers; the reason this lane exists (a 30K-token re-serve measured 0.5 s
  vs 18.4 s cold on the reference box, x36).
- `--speculative-algorithm NEXTN` (steps 3, topk 1, draft 4, `unquant`): the
  model's own in-checkpoint MTP head. Its 31 tensors are BF16 (RadixArk
  quantized only the routed experts), hence `unquant` for the draft. PLE
  requires topk=1, which is exactly what NEXTN uses.
- `--chunked-prefill-size 1024` and `--mem-fraction-static 0.79`: long-prefill
  sequences at 262K without flushing can starve the host (unified memory);
  these are the values validated at full context on the reference box.
- NO `--language-only`: the vision tower stays loaded (multimodal requests
  work; validated with image inputs on the reference box, ~23 GB host headroom).
- `--api-key`: unlike the 27B lane, early public recipes for this model ran
  open; the lane authenticates like everything else in this repo.

Upstream model: [RadixArk/Qwen3.8-Flash-Next-NVFP4](https://huggingface.co/RadixArk/Qwen3.8-Flash-Next-NVFP4),
served with the same serve-time `--revision` lock as every other checkpoint here.


## v1.5.2 correction (2026-08-29)

The v1.5 overlay widened the QSA trtllm sparse-decode gate to sm_120/121.
hashd1ve established on 2026-08-29 (commit bd16dce, "Patch 2 was wrong on
GB10") that this gate routes GB10 decode to FlashInfer's XQA kernel, which
silently corrupts long-context output (runs of token id 0: 1 of 4 requests at
120k tokens, 4 of 4 at 210k). The gate is back to upstream (sm100 only) and
sm_121 decode now uses a dedicated Triton packed-varlen kernel:

- `qwen_sparse_attn_backend.py`: pristine file from the pinned image plus the
  `patches/qsa_sm121_triton.py` patcher of
  https://github.com/hashd1ve/qwen38-flash-next-one-dgx-spark (MIT), which
  adds the `is_sm121()` route to `qsa.sm121_varlen`.
- `sm121_varlen.py`: verbatim `patches/qsa_sm121_varlen.py` from the same repo,
  itself a verbatim copy of `sglang/srt/layers/attention/qsa/sm121_varlen.py`
  from upstream PR sgl-project/sglang#36845 (BBuf, 2026-08-28, open at the time
  of vendoring). Validated there by exact needle retrieval 4/4 at 120k, 190k and
  210k tokens; re-validated on this repo's box before release (CHANGELOG v1.5.2).
  The PR later moved to a KDA-optimized kernel package (`kda_kernels/qwen38_qsa_sm121`,
  2026-08-29); our copy is the Triton varlen revision of 2026-08-28, sha256-pinned in
  MANIFEST.sha256, field-validated on the reference box (BENCHMARKS, 29/08).

**Upstream status, checked 2026-09-02.** PR #36845 **merged** on 2026-08-30
(merge commit `78c5024e`), in its restructured form: the kernel now lives at
`python/sglang/kernels/kda_kernels/qwen38_qsa_sm121/kernel.py` (274 lines, a
split-K `_qsa_split_kernel` with a scratch-buffer helper), not at the
`srt/layers/attention/qsa/sm121_varlen.py` path we vendored, and it arrived with
a registered test (`test/registered/kernels/test_kda_qsa_sm121.py`). It is a
reimplementation rather than a revision of what is in this directory.

We keep the 2026-08-28 copy on purpose, for now. It is the version that was
field-validated here by exact needle retrieval at 120k, 190k and 210k, and
swapping a kernel whose whole job is long-context correctness is not something
to do on the strength of an upstream merge alone. Two conditions gate the
change, in this order:

1. An official image ships it. Our flash base is pinned to the
   `qwen38flashnext` digest of 2026-08-26, which predates the merge, and the
   only tags carrying post-merge code today are `nightly-*` and `dev-*`, which
   this repo does not pin. When a stable tag contains it, the overlay drops
   `sm121_varlen.py` entirely and the backend patch with it, exactly as this
   repo plans to drop the DFlash2 overlay once an official image ships DFLASH2.
2. Failing that, a swap has to be re-validated here the way the current copy
   was: `./needle.sh --mem` at 120k, 190k and 210k, plus the quality canaries.
   That needs the flash lane installed (~230 GB free) and one giant-context run,
   so it is a deliberate campaign, not a drive-by bump.

**Update, 2026-09-03.** The evidence moved, and it is now strong enough that this
should be the next thing done to the flash lane rather than a someday item.
hashd1ve, whose patch set this directory vendors, adopted the merged kernel on
2026-08-30 (`4f425ca5`): the KDA implementation measures **4 to 5 times the
2026-08-28 Triton revision on a GB10 at decode batch 1-4, with identical
numerics**, and it is routed inside its exact contract with the 2026-08-28 kernel
kept as the fallback for anything outside it. Their validation is on one DGX
Spark: needle retrieval **9/9 exact at 120k, 190k and 210k**, decode after those
prompts **46-92 tok/s where it was 30-48**, short-context decode unchanged.

That is the same hardware, the same depths and the same author whose kernel is
already in this directory. What is still missing is our own run: the flash lane
needs about 230 GB free and this box does not have it today, and a
correctness-critical kernel does not get swapped on someone else's needle test,
however good. So the file is unchanged and the upgrade is now a named, costed
task rather than an open question: install the flash lane, vendor
`patches/kda_kernels/` and `patches/qsa_sm121_kda.py` at their pinned revision,
re-run `./needle.sh --mem` at 120k/190k/210k plus the quality canaries, and keep
the 2026-08-28 kernel as the documented fallback exactly as they do.

**Done, 2026-09-03.** The swap was made and validated here, and this directory now
ships the merged kernel:

- `kda_kernels/` is the KDA package of sglang#36845 as vendored by hashd1ve at
  `4f425ca5`, verbatim, sha256-pinned in MANIFEST.sha256. It mounts at
  `sglang/kernels/kda_kernels/`, which is where the merge put it.
- `qwen_sparse_attn_backend.py` is the pristine file of the pinned image with
  `patches/qsa_sm121_kda.py` applied, so sm_121 decode calls the KDA kernel inside
  its exact contract (bf16, head dim 256, 24Q/2KV or 12Q/1KV, batch <= 128,
  selected KV <= 2055) and the 2026-08-28 Triton kernel everywhere else.
- `sm121_varlen.py` is unchanged: it is now the documented fallback, not the route.
- The image build imports both routes for real, so a missing symbol fails the build
  instead of surfacing as a decode crash nine minutes into a boot.

Validation on the reference box, image `qwen38-flash:v1.6.0-kda`:

| check | result |
|---|---|
| needle retrieval at 120k prompt tokens | **11/11 exact** (9 on the build under test, then 2 more on the shipped image after a comments-only edit; fresh passphrase each) |
| host memory floor during those prompts | 14.6 GiB, about 9 GiB below idle |
| quality canaries (merge, logic, fr, primes) | **4/4**, twice (both builds) |
| decode, 700 tokens, short prompt | 38.8 / 36.7 / 26.5 tok/s (code, math, prose) |
| runs of token id 0 | none, in any of the above |

The depths above 120k were **not** measured, and cannot be on this lane as it is
configured: its KV pool is 189,056 tokens, so a 190k prompt does not fit. Sending
one anyway, direct to the engine past the proxy's guard, queued it forever
(`#queue-req: 1, #running-req: 0`) and wedged the scheduler for every request after
it; `/abort_request` answered `not found in rid_to_state`, which is sglang#36333,
whose fix (#36418) is not merged. That is a second reason agent clients must go
through the proxy, and it is why the author's 9/9 at 190k and 210k stays their
measurement rather than ours.

The 2026-08-28 Triton kernel stays in the tree as the fallback the route calls,
exactly as upstream's own adopter keeps it.
