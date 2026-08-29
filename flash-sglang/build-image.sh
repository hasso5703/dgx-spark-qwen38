#!/usr/bin/env bash
# Build the Flash-Next SGLang serving image: pinned official base + the two
# sha256-verified overlay files (see ATTRIBUTION.md). Deterministic and offline
# (the base image must already be pulled by install.sh). After the copy, the
# build verifies both modules still parse and that the two QSA resolver gates
# are the patched ones; the tag is refused otherwise.
# Usage: BASE_IMAGE=<pinned digest ref> TAG=<local tag> ./build-image.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_IMAGE="${BASE_IMAGE:?BASE_IMAGE required (pinned digest ref)}"
TAG="${TAG:?TAG required (local image tag)}"

docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 || { echo "base image not present: $BASE_IMAGE" >&2; exit 1; }
(cd "$DIR" && sha256sum -c MANIFEST.sha256 >/dev/null) || { echo "overlay checksum mismatch, refusing to build" >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp "$DIR/qwen4_exp.py" "$DIR/qwen_sparse_attn_backend.py" "$DIR/sm121_varlen.py" "$STAGE/"
cat > "$STAGE/Dockerfile" <<'DEOF'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG SGL=/sgl-workspace/sglang/python/sglang
COPY qwen4_exp.py ${SGL}/srt/models/qwen4_exp.py
COPY qwen_sparse_attn_backend.py ${SGL}/srt/layers/attention/qwen_sparse_attn_backend.py
COPY sm121_varlen.py ${SGL}/srt/layers/attention/qsa/sm121_varlen.py
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
assert "qsa.sm121_varlen" in qsa, "QSA sm_121 Triton varlen route missing"
ast.parse(open(f"{sgl}/srt/layers/attention/qsa/sm121_varlen.py").read())
from sglang.srt.utils import is_sm121  # noqa: F401 (the route's gate must exist)
assert "SGLANG_QWEN4_PLE_MMAP_DIR" in open(f"{sgl}/srt/models/qwen4_exp.py").read(), "PLE mmap hook missing"
print("flash-sglang overlay verified in image")
PYEOF
DEOF
DOCKER_BUILDKIT=1 docker build -q --build-arg BASE_IMAGE="$BASE_IMAGE" -t "$TAG" "$STAGE" >/dev/null
echo "built $TAG (base $BASE_IMAGE + $(wc -l < "$DIR/MANIFEST.sha256") verified files, gates checked)"
