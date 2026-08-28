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
with house punctuation, AST-verified).

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
