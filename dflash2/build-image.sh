#!/usr/bin/env bash
# Build the 27B serving image: pinned base + the sha256-verified overlay files.
# Deterministic and offline (the base image must already be pulled by install.sh).
# Usage: BASE_IMAGE=<pinned digest ref> TAG=<local tag> ./build-image.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_IMAGE="${BASE_IMAGE:?BASE_IMAGE required (pinned digest ref)}"
TAG="${TAG:?TAG required (local image tag)}"

docker image inspect "$BASE_IMAGE" >/dev/null 2>&1 || { echo "base image not present: $BASE_IMAGE" >&2; exit 1; }
(cd "$DIR" && sha256sum -c MANIFEST.sha256 >/dev/null) || { echo "overlay checksum mismatch, refusing to build" >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -r "$DIR/sglang" "$STAGE/sglang"
{
  echo "FROM $BASE_IMAGE"
  while IFS= read -r line; do
    rel="${line#*  sglang/}"
    echo "COPY sglang/$rel /sgl-workspace/sglang/python/sglang/$rel"
  done < "$DIR/MANIFEST.sha256"
} > "$STAGE/Dockerfile"
DOCKER_BUILDKIT=1 docker build -q -t "$TAG" -f "$STAGE/Dockerfile" "$STAGE" >/dev/null
echo "built $TAG (base $BASE_IMAGE + $(wc -l < "$DIR/MANIFEST.sha256") verified files)"
