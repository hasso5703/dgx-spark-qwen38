# Flash-Next overlay: provenance and licenses

The official `vllm/vllm-openai:qwen38-flash-next` image cannot serve the model on a
single GB10 box: the 176B checkpoint's 51B-parameter N-gram (PLE) table does not fit
in the 128 GB unified pool next to a real KV cache. `install.sh` (MODEL_CHOICE=flash)
builds the serving image locally: the pinned official base image plus the two
sha256-verified files in this directory. Nothing is downloaded at build time;
`MANIFEST.sha256` is checked before every build, and the bit-exactness test below
runs inside the freshly built image on every install.

Provenance of the two files:

- `vllm_ple_mmap.py`: the PLE-mmap patch by [blazux](https://github.com/blazux/qwen3.8-Flash-DGX)
  (`qwen3.8-Flash-DGX`, vendored 2026-08-27). License: Apache-2.0.
  It registers the PLE gather as a custom op (`vllm::ple_mmap_lookup`) served from
  the checkpoint's safetensors shards through mmap + page cache, instead of loading
  the 48 GiB table into the unified pool. That single change is what lets the
  NVFP4 checkpoint fit on one DGX Spark. Local modification: 4 em/en dashes in the
  module docstring replaced with house-style punctuation (code untouched, AST-verified).
- `test_ple_mmap_cpu.py`: blazux's correctness test, same repo and license, vendored
  unmodified. It builds synthetic FP8 shards with the real safetensors layout and
  checks the mmap gather bit-for-bit against a reference `table[ids]` (dedup,
  multi-shard spans, fp8 view path, out-of-range rejection). CPU-only, no network.
  `flash/build-image.sh` runs it inside the built image and refuses to tag on failure.

Why the three GB10-specific serving flags (carried in `qwen38-flash.service.template`):

- `-cc.cudagraph_mode=PIECEWISE` + the splitting-ops list: the PLE gather is CPU work
  plus a pageable host-to-device copy and cannot live inside a captured CUDA graph.
- `--no-enable-prefix-caching`: a GDN `in_proj` GEMM hits `CUBLAS_STATUS_INTERNAL_ERROR`
  on the cached-block path on sm_121 (stock-model bug, not introduced by the patch).
- torch.compile stays off for the lookup op: the stock embedding gather codegen trips
  an Inductor int64-indexing assert on sm_121.

Upstream model: [RadixArk/Qwen3.8-Flash-Next-NVFP4](https://huggingface.co/RadixArk/Qwen3.8-Flash-Next-NVFP4)
(NVFP4 quantization of Qwen/Qwen3.8-Flash-Next), served with the revision lock like
every other checkpoint in this repo.
