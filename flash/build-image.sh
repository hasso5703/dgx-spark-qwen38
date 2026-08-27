#!/usr/bin/env bash
# Build the Flash-Next serving image: pinned official vLLM base + the two
# sha256-verified overlay files (see ATTRIBUTION.md). Deterministic and offline
# (the base image must already be pulled by install.sh). After the build, the
# vendored bit-exactness test of the mmap gather runs INSIDE the image (CPU-only,
# ~30 s); the tag is refused if it fails.
# Usage: BASE_IMAGE=<pinned digest ref> TAG=<local tag> ./build-image.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_IMAGE="${BASE_IMAGE:?BASE_IMAGE required (pinned digest ref)}"
TAG="${TAG:?TAG required (local image tag)}"

docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 || { echo "base image not present: $BASE_IMAGE" >&2; exit 1; }
(cd "$DIR" && sha256sum -c MANIFEST.sha256 >/dev/null) || { echo "overlay checksum mismatch, refusing to build" >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp "$DIR/vllm_ple_mmap.py" "$STAGE/"
# Package layout inside the official image (vLLM 0.1.dev20073, torch 2.13 cu130).
# The hook append is a no-op unless VLLM_PLE_MMAP=1 at runtime, so the image
# behaves exactly like upstream when the flag is off. The ast.parse guard makes
# the build fail loudly if the upstream file layout ever changes.
cat > "$STAGE/Dockerfile" <<'DEOF'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG SP=/usr/local/lib/python3.12/dist-packages
ARG PLE=${SP}/vllm/models/qwen3_8_flash_next/nvidia/ple_layer.py
COPY vllm_ple_mmap.py ${SP}/vllm_ple_mmap.py
RUN cp ${PLE} ${PLE}.orig \
 && printf '\n\n# --- dgx-spark-qwen38 flash overlay: serve the PLE n-gram table from disk (VLLM_PLE_MMAP=1) ---\nfrom vllm_ple_mmap import apply as _ple_mmap_apply\n_ple_mmap_apply(Qwen3_8FlashNextNGramEmbedding)\n' >> ${PLE} \
 && python3 -c "import ast; ast.parse(open('${PLE}').read()); print('ple_layer.py patched OK')"
DEOF
DOCKER_BUILDKIT=1 docker build -q --build-arg BASE_IMAGE="$BASE_IMAGE" -t "$TAG" "$STAGE" >/dev/null

# Bit-exactness gate: the vendored correctness test runs inside the image we
# just built. A wrong gather would degrade the model into noise; refuse the tag.
if ! docker run --rm -v "$DIR/test_ple_mmap_cpu.py:/t/test_ple_mmap_cpu.py:ro" -w /t \
     --entrypoint python3 "$TAG" test_ple_mmap_cpu.py | grep -q 'ALL OK'; then
  docker rmi "$TAG" >/dev/null 2>&1 || true
  echo "PLE mmap gather test FAILED inside the built image, tag removed" >&2
  exit 1
fi
echo "built $TAG (base $BASE_IMAGE + $(wc -l < "$DIR/MANIFEST.sha256") verified files, gather test passed)"
