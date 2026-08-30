# 27B serving overlay: provenance and licenses

`install.sh` builds the 27B serving image locally: the pinned base image plus the eight
sha256-verified files in `sglang/`, copied to `/sgl-workspace/sglang/python/sglang/`. Nothing
is downloaded at build time; `MANIFEST.sha256` is checked before every build.

The overlay carries two upstream patches the pinned base predates.

## Patch 1: DFlash2 (five files)

No official SGLang release image contained DFLASH2 when the base was pinned (merged upstream
2026-08-19; an official `lmsysorg/sglang:dev-qwen38-27b-dflash2` image appeared 2026-08-22 and
supersedes this patch the day the repo pins it).

Provenance of the five files:

- Upstream: [sgl-project/sglang PR #35371](https://github.com/sgl-project/sglang/pull/35371)
  ("DFlash2: local convolution + candidate selector"), merged 2026-08-19 at `c14312a66420b75c`.
  License: Apache-2.0.
- Quantized-lm_head candidate path (runs the NVFP4 head in place via
  `lm_head.quant_method.apply`; the original dense-dequant approach allocated 2.5-5 GB during
  draft-graph capture and hard-rebooted GB10 boxes): by
  [MiaAI-Lab](https://github.com/MiaAI-Lab/Qwen3.8-27B-SGLang-DGX-Spark), vendored from their
  `patch/overlay-dflash2` at commit `c90d8c34cf795185ee8de736b7ded9bca3fe0de1`. License: MIT.
  The same in-place approach is used by
  [r0b0tlab](https://github.com/r0b0tlab/qwen38-27b-nvfp4-sm121-sglang), whose K sweep
  (block 8 optimal, block 9 collapses) fixed this config's draft token count.
- Draft model: [z-lab/Qwen3.8-27B-DFlash2](https://huggingface.co/z-lab/Qwen3.8-27B-DFlash2)
  (pinned by revision in `install.sh`).

## Patch 2: mrope height and width in the fused Qwen3.5 rope kernel (three files)

Upstream: [sgl-project/sglang PR #34446](https://github.com/sgl-project/sglang/pull/34446)
("[rotary] Fix the fused Qwen3.5 RoPE kernel discarding mrope height and width"), merged
2026-08-30 at `e6355774`. License: Apache-2.0.

`fused_qk_gemma_rmsnorm_rope_gate` loaded one position per token, so a multimodal
`[3, T]` mrope tensor only ever yielded row 0: every image token was rotated as if it sat at
its temporal position on all three axes. Text is unaffected (a text token holds the same
position on all three rows), which is why no text benchmark catches it. The path is not behind
a flag: `Qwen3_5AttentionDecoderLayer.self_attention` takes it whenever CUDA and
`attn_output_gate` are both true, which is this repo's 27B configuration
(`mrope_section [11, 11, 10]`, `mrope_interleaved`, gate on).

Provenance of the three files:

- `kernels/ops/attention/fused_qk_rmsnorm_rope_gate.py`: upstream's fixed file, with the five em
  dashes in its docstrings replaced by commas to satisfy this repo's house typography rule
  (comments only, no code change). The base image's copy was byte-identical to the upstream pre-fix version,
  so the fixed file drops in.
- `srt/layers/rotary_embedding/mrope.py` and `srt/models/qwen3_5.py`: the upstream hunks ported
  onto the base image's copies, which differ from upstream by four pre-existing local
  variations (an older `runtime_context` API and one CPU return placement). Verified by diffing
  the ported files against upstream's fixed versions: the only differences are those four.

Verification performed before vendoring (2026-08-30, reference box): upstream's own two test
files from the PR pass inside the built image; the built image boots, serves, and answers text
byte-identically to the unpatched image at the same speed (64.1 vs 64.6 tok/s, within noise).

## Retiring this overlay

Each patch is deleted from the install path the day an official image ships it, and the repo
pins that image digest instead. Patch 1 is already superseded upstream; patch 2 needs an image
built after 2026-08-30 07:37 UTC.
