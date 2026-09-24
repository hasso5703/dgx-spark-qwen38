#!/usr/bin/env bash
# Build the v1.5 to v1.7 Flash-Next overlay image: a base image plus exactly the files
# MANIFEST.sha256 verifies (see ATTRIBUTION.md). Nothing runs this since v1.18.7 retired
# OVERLAY_FLASH=1; it stays as the record, and runs by hand on a base you pulled
# yourself. Offline and deterministic. After the copy, the build verifies the modules
# still parse and that the QSA resolver gates are the patched ones; the tag is refused
# otherwise.
# Usage: BASE_IMAGE=<pinned digest ref> TAG=<local tag> ./build-image.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_IMAGE="${BASE_IMAGE:?BASE_IMAGE required (pinned digest ref)}"
TAG="${TAG:?TAG required (local image tag)}"

docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 || { echo "base image not present: $BASE_IMAGE" >&2; exit 1; }
(cd "$DIR" && sha256sum -c MANIFEST.sha256 >/dev/null) || { echo "overlay checksum mismatch, refusing to build" >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
# Exactly the verified files: a whole-directory copy took along anything else in
# kda_kernels, and a hash-checked .pyc in a __pycache__ there is imported instead of the
# verified kernel.py (found in review, 2026-09-24).
while read -r _sum f; do
  mkdir -p "$STAGE/$(dirname "$f")"
  cp "$DIR/$f" "$STAGE/$f"
done < "$DIR/MANIFEST.sha256"
cat > "$STAGE/Dockerfile" <<'DEOF'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG SGL=/sgl-workspace/sglang/python/sglang
COPY qwen4_exp.py ${SGL}/srt/models/qwen4_exp.py
COPY qwen_sparse_attn_backend.py ${SGL}/srt/layers/attention/qwen_sparse_attn_backend.py
COPY sm121_varlen.py ${SGL}/srt/layers/attention/qsa/sm121_varlen.py
COPY kda_kernels ${SGL}/kernels/kda_kernels
RUN touch ${SGL}/srt/layers/attention/qsa/__init__.py
RUN python3 - <<'PYEOF'
import ast
sgl = "/sgl-workspace/sglang/python/sglang"
for p in (f"{sgl}/srt/models/qwen4_exp.py",
          f"{sgl}/srt/layers/attention/qwen_sparse_attn_backend.py",
          f"{sgl}/srt/layers/attention/qsa/sm121_varlen.py"):
    ast.parse(open(p).read())
qsa = open(f"{sgl}/srt/layers/attention/qwen_sparse_attn_backend.py").read()
assert "is_sm100_supported() or is_sm120_supported()" not in qsa, \
    "widened trtllm gate present: that path routes GB10 decode to XQA, which corrupts long-context output (v1.5 bug)"
assert "qsa.sm121_varlen" in qsa, "QSA sm_121 Triton varlen fallback route missing"
assert "kda_kernels.qwen38_qsa_sm121" in qsa, "QSA sm_121 KDA route missing"
ast.parse(open(f"{sgl}/srt/layers/attention/qsa/sm121_varlen.py").read())
for p in (f"{sgl}/kernels/kda_kernels/__init__.py",
          f"{sgl}/kernels/kda_kernels/qwen38_qsa_sm121/__init__.py",
          f"{sgl}/kernels/kda_kernels/qwen38_qsa_sm121/kernel.py"):
    ast.parse(open(p).read())
# the route resolves for real, imports and all: a missing symbol here would only
# surface as a decode crash after a nine-minute boot
from sglang.kernels.kda_kernels.qwen38_qsa_sm121 import (  # noqa: F401
    can_use_qwen38_qsa_sm121, qwen38_qsa_sm121,
)
from sglang.srt.layers.attention.qsa.sm121_varlen import (  # noqa: F401
    qsa_sm121_varlen_attention,
)
from sglang.srt.utils import is_sm121  # noqa: F401 (the route's gate must exist)
q4 = open(f"{sgl}/srt/models/qwen4_exp.py").read()
assert "SGLANG_QWEN4_PLE_MMAP_DIR" in q4, "PLE mmap hook missing"
assert "_ple_table_complete" in q4 and "SGLANG_QWEN4_PLE_TAG" in q4, "PLE table reuse missing"
print("flash-sglang overlay verified in image")
PYEOF
DEOF
DOCKER_BUILDKIT=1 docker build -q --build-arg BASE_IMAGE="$BASE_IMAGE" -t "$TAG" "$STAGE" >/dev/null
echo "built $TAG (base $BASE_IMAGE + $(wc -l < "$DIR/MANIFEST.sha256") verified files, gates checked)"
