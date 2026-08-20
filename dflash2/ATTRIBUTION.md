# DFlash2 overlay: provenance and licenses

No official SGLang release image contains DFLASH2 yet (merged upstream 2026-08-19). Until one
does, `install.sh` builds the serving image locally: the pinned base image plus the five
sha256-verified files in `sglang/`, copied to `/sgl-workspace/sglang/python/sglang/`. Nothing
is downloaded at build time; `MANIFEST.sha256` is checked before every build.

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

This directory is deleted from the install path the day an official image ships DFLASH2; the
repo then pins that image digest instead.
