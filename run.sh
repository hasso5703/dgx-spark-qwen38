#!/usr/bin/env bash
# Run the exact same pinned config as the systemd service, but in the
# foreground: no systemd, no sudo, Ctrl+C stops it, --rm leaves nothing behind.
# For people who want to try / A/B this without committing to a service.
#
# Prerequisite (one time): ./install.sh --no-service   (image, checkpoints, key, template)
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
die() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ── Same pins as install.sh (read from it — single source of truth) ──
PINS="$(grep -E '^(IMAGE|MODEL_REPO|DRAFT_REPO|MODEL_REV|DRAFT_REV|PORT|HF_CACHE|CONFIG_DIR)=' "$REPO_DIR/install.sh" || true)"
[ "$(printf '%s\n' "$PINS" | wc -l)" -eq 8 ] || die "could not read the 8 pinned variables from install.sh (repo layout changed?)"
eval "$PINS"

# ── Everything prepared? ──
[ -s "$CONFIG_DIR/api-key" ] || die "no API key at $CONFIG_DIR/api-key — run: ./install.sh --no-service"
[ -s "$CONFIG_DIR/chat-template-sglang.jinja" ] || die "no patched template in $CONFIG_DIR — run: ./install.sh --no-service"
for REPO in "$MODEL_REPO" "$DRAFT_REPO"; do
  DIR="$HF_CACHE/hub/models--${REPO//\//--}"
  SNAP="$(ls -d "$DIR"/snapshots/*/ 2>/dev/null | head -1 || true)"
  [ -n "$SNAP" ] || die "checkpoint $REPO not found in $HF_CACHE — run: ./install.sh --no-service"
  # An interrupted download leaves snapshots/ in place with only the small
  # files — so also require finished blobs and actual weights (credit: helge).
  if compgen -G "$DIR/blobs/*.incomplete" >/dev/null 2>&1; then
    die "checkpoint $REPO download is incomplete (blobs/*.incomplete) — re-run: ./install.sh --no-service (it resumes)"
  fi
  compgen -G "${SNAP}*.safetensors" >/dev/null 2>&1 \
    || die "checkpoint $REPO has no weight files in its snapshot — re-run: ./install.sh --no-service"
done
docker image inspect "$IMAGE" >/dev/null 2>&1 || die "image $IMAGE not pulled — run: ./install.sh --no-service"

# ── Nothing else may be using the GPU or the port (GB10: one engine at a time) ──
if systemctl is-active --quiet qwen38-sglang 2>/dev/null; then
  die "the qwen38-sglang systemd service is running — stop it first: sudo systemctl stop qwen38-sglang"
fi
for NAME in qwen38-sglang qwen38-sglang-run; do
  [ -z "$(docker ps -q -f "name=^${NAME}$")" ] || die "container $NAME is already running (docker stop $NAME)"
done
if ss -tlnH 2>/dev/null | awk '{print $4}' | grep -q ":$PORT\$"; then
  die "port $PORT is already in use (ss -tlnp | grep :$PORT). Free it or run: PORT=<other> ./run.sh"
fi
GPU_APPS="$(nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader 2>/dev/null || true)"
[ -z "$GPU_APPS" ] || die "the GPU is busy — this config needs the machine to itself (unified memory):
$GPU_APPS"

KEY="$(cat "$CONFIG_DIR/api-key")"
mkdir -p "$CONFIG_DIR/sglang-cache"
echo "Starting in the foreground (first boot ≈ 9 min: torch.compile + CUDA graph capture)."
echo "  Ready when the log says:  The server is fired up and ready to roll!"
echo "  Test from another shell:  curl http://127.0.0.1:$PORT/health"
echo "  Claude Code:              source $CONFIG_DIR/claude-code.env && claude --model qwen3.8-27b"
echo "  Stop:                     Ctrl+C (container removed; compile cache kept for faster next boots)"
echo

# Verbatim the systemd unit's docker run (same caps, mounts, flags), foreground.
exec docker run --rm --name qwen38-sglang-run --gpus all \
  --memory 100g --memory-swap 100g --shm-size 16g --network host --ipc=host \
  -e TORCHINDUCTOR_CACHE_DIR=/cache/inductor \
  -v "$CONFIG_DIR/sglang-cache":/cache \
  -v "$HF_CACHE":/root/.cache/huggingface \
  -v "$CONFIG_DIR":/out \
  "$IMAGE" \
  python3 -m sglang.launch_server \
    --trust-remote-code --model-path RadixArk/Qwen3.8-27B-NVFP4 --tp-size 1 \
    --served-model-name qwen3.8-27b \
    --mem-fraction-static 0.50 \
    --attention-backend flashinfer --chunked-prefill-size 8192 \
    --disable-prefill-cuda-graph --cuda-graph-max-bs 4 \
    --speculative-algorithm DSPARK --speculative-draft-model-path RadixArk/Qwen3.8-27B-DSpark \
    --speculative-dspark-block-size 7 --speculative-draft-model-quantization unquant \
    --mamba-scheduler-strategy extra_buffer \
    --enable-torch-compile --torch-compile-max-bs 4 \
    --num-continuous-decode-steps 2 \
    --reasoning-parser qwen3 --tool-call-parser qwen3_coder \
    --chat-template /out/chat-template-sglang.jinja \
    --api-key "$KEY" \
    --host 0.0.0.0 --port "$PORT"
