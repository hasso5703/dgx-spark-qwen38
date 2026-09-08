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


## The 27B migration, and what blocks it (2026-09-08, v1.8)

An official image ships DFLASH2: `lmsysorg/sglang:dev-qwen38-27b-dflash2`, built
from `1cf2b8c` and multi-arch, so GB10 pulls it natively. It is the image the
[cookbook](https://docs.sglang.io/cookbook/autoregressive/Qwen/Qwen3.8-27B) now
points RTX PRO 6000, RTX 5090 and DGX Spark at, after
[#35825](https://github.com/sgl-project/sglang/pull/35825) re-ran all 48 cells on
it (and moved the DGX Spark base mem-fraction from 0.85 to 0.80: at 0.85, 15 of
48 cells were killed by DGX OS earlyoom, which sits right where 0.85 of 128 GB
leaves the host). It is also built after two DFlash2 changes this base predates,
and the pinned `z-lab/Qwen3.8-27B-DFlash2` checkpoint has been asking for both
all along, since its `dflash_config` declares `conv_kernel_size: 2`,
`conv_group_size: 16`, `selector_rank: 256` and `selector_top_k: 16`:

- **Grouped dynamic depthwise convolution and a candidate selector**
  ([#35371](https://github.com/sgl-project/sglang/pull/35371), merged
  2026-08-19). Both are switched on by the checkpoint, so a checkpoint that
  declares them served by an engine that does not know them simply takes the old
  path, which is what this box has been doing. Published evaluation on the
  draft's model card: acceptance length 5.46 on GSM8K against MTP's 5.02 and
  DSpark's 4.36, and 3.43x no-speculation throughput at batch 1.
- **A quantized target lm_head in the selector**
  ([#35496](https://github.com/sgl-project/sglang/pull/35496), merged
  2026-08-20). The selector projects draft hidden states through the target's own
  `lm_head`, which a packed NVFP4 head cannot serve by row slicing; the PR runs
  `quant_method.apply` over the padded local vocab and masks the tail. This also
  closes the BF16-lm_head question this repo parked on 2026-09-03: the packed
  head is supported, so there is nothing this box needs to switch to.

**It is not adopted, for one reason.** That image was built on 2026-08-22 and
patch 2 below merged upstream on 2026-08-30, so it does not carry the mrope fix.
Serving it as it stands would rotate every image token as if it sat at its
temporal position on all three axes: text unaffected, image inputs silently
wrong. Checked in the image rather than assumed, its
`fused_qk_rmsnorm_rope_gate.py` still reads `pos = tl.load(positions_ptr +
token)`, one position per token.

**And the port is not mechanical.** Diffed against the pinned base rather than
assumed: `kernels/ops/attention/fused_qk_rmsnorm_rope_gate.py` is byte-identical
between the two bases, so that file drops in, but `mrope.py` changed API
(`get_exec()` became `attention_backends()`, and the deterministic screen became
`self._force_native`) and `qwen3_5.py` changed by 398 lines. Re-porting patch 2
onto the newer copies of those two files, on a path whose failure mode is silent
image corruption, is a named task with its own validation (upstream's two test
files from #34446, a vision probe, the text canaries and a bench), not a
drive-by bump.

`install.sh` pins that image as `DFLASH2_OFFICIAL_IMAGE` so the day the fix
lands in a build for sm_121 this is a one-line change. `lmsysorg/sglang:v0.5.19`
has both fixes and is not the answer: its `torch.cuda.get_arch_list()` stops at
`sm_120` and its `sgl_kernel` ships only sm90 and sm100 variants, while GB10 is
sm_121, which is why the cookbook points DGX Spark at `dev-*` images at all.

## Retiring this overlay

Each patch is deleted from the install path the day an official image ships it,
and the repo pins that image digest instead. Patch 1 is superseded by the image
above; patch 2 needs an image for sm_121 built after 2026-08-30 07:37 UTC. The
flash lane crossed that line first: see flash-sglang/ATTRIBUTION.md.
