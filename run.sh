#!/usr/bin/env bash
# Run the exact same pinned config as the systemd service, but in the
# foreground: no systemd, no sudo, Ctrl+C stops it, --rm leaves nothing behind.
# For people who want to try / A/B this without committing to a service.
#
# Prerequisite (one time): ./install.sh --no-service   (image, checkpoints, key, template)
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
die() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ── Same pins as install.sh (read from it: single source of truth) ──
PINS="$(grep -E '^(IMAGE|STOCK_REPO|STOCK_REV|UNC_REPO|UNC_REV|FP8_REPO|FP8_REV|UNCFP8_REPO|UNCFP8_REV|MODEL_CHOICE|CONTEXT_MODE|DRAFT2_REPO|DRAFT2_REV|SERVE_IMAGE|PORT|HF_CACHE|CONFIG_DIR)=' "$REPO_DIR/install.sh" || true)"
[ "$(printf '%s\n' "$PINS" | wc -l)" -eq 15 ] || die "could not read the 15 pinned variables from install.sh (repo layout changed?)"
eval "$PINS"
case "${MODEL_CHOICE}" in
  stock)      MODEL_REPO="$STOCK_REPO"; MODEL_REV="${MODEL_REV:-$STOCK_REV}" ;;
  uncensored) MODEL_REPO="$UNC_REPO";   MODEL_REV="${MODEL_REV:-$UNC_REV}" ;;
  fp8)        MODEL_REPO="$FP8_REPO";   MODEL_REV="${MODEL_REV:-$FP8_REV}" ;;
  uncensored-fp8) MODEL_REPO="$UNCFP8_REPO"; MODEL_REV="${MODEL_REV:-$UNCFP8_REV}" ;;
  flash)      die "MODEL_CHOICE=flash is service-only in this release: MODEL_CHOICE=flash ./install.sh (./run.sh covers the 27B targets)" ;;
  *) die "MODEL_CHOICE must be stock, uncensored, fp8, uncensored-fp8 or flash (got: ${MODEL_CHOICE})" ;;
esac
# See install.sh: Qwen's FP8 checkpoint carries no KV scales, so without this the
# KV cache falls back to bf16 and costs about half the pool.
case "${MODEL_CHOICE}" in
  fp8|uncensored-fp8) KV_CACHE_ARGS="--kv-cache-dtype fp8_e4m3" ;;
  *)                  KV_CACHE_ARGS="" ;;
esac
if [ "${CONTEXT_MODE}" = "1m" ]; then
  die "CONTEXT_MODE=1m needs the systemd path (keepalive proxy + YaRN service units): run CONTEXT_MODE=1m ./install.sh. ./run.sh serves the native 262144 config only."
fi
# The fix-it command echoed by every check below, carrying the active choices
# so following it prepares THIS configuration (not the defaults).
PREP="./install.sh --no-service"
[ "$MODEL_CHOICE" != "stock" ] && PREP="MODEL_CHOICE=$MODEL_CHOICE $PREP"

# ── Everything prepared? ──
[ -s "$CONFIG_DIR/api-key" ] || die "no API key at $CONFIG_DIR/api-key. Run: $PREP"
[ -s "$CONFIG_DIR/chat-template-sglang.jinja" ] || die "no patched template in $CONFIG_DIR. Run: $PREP"
# Checkpoints: validate the snapshot of the PINNED revision, never
# "the first snapshot dir": a cache can hold several revisions (an old
# MODEL_REV=main attempt, an upstream bump), and `ls | head -1` picks
# alphabetically, failing a healthy install on a stale partial dir (issue #5).
for PAIR in "$MODEL_REPO=$MODEL_REV" "$DRAFT2_REPO=$DRAFT2_REV"; do
  REPO="${PAIR%%=*}"; REV="${PAIR#*=}"
  DIR="$HF_CACHE/hub/models--${REPO//\//--}"
  [ -d "$DIR/snapshots" ] || die "checkpoint $REPO not found in $HF_CACHE. Run: $PREP"
  # A ref name (e.g. 'main') resolves through refs/, a sha is used as-is.
  REV_SHA="$REV"
  [ -f "$DIR/refs/$REV" ] && REV_SHA="$(cat "$DIR/refs/$REV")"
  SNAP="$DIR/snapshots/$REV_SHA/"
  [ -d "$SNAP" ] || die "checkpoint $REPO is cached, but not at the pinned revision ${REV_SHA:0:12} (cached: $(ls "$DIR/snapshots" 2>/dev/null | cut -c1-12 | paste -sd' ')). Re-run: $PREP"
  # An interrupted download leaves snapshots/ in place with only the small
  # files, so also require finished blobs and actual weights (credit: helge).
  if compgen -G "$DIR/blobs/*.incomplete" >/dev/null 2>&1; then
    die "checkpoint $REPO download is incomplete (blobs/*.incomplete). Re-run: $PREP (it resumes)"
  fi
  compgen -G "${SNAP}*.safetensors" >/dev/null 2>&1 \
    || die "checkpoint $REPO has no weight files in its pinned snapshot ${REV_SHA:0:12}. Re-run: $PREP"
done
docker image inspect "$SERVE_IMAGE" >/dev/null 2>&1 || die "serving image $SERVE_IMAGE not built. Run: $PREP"

# ── Nothing else may be using the GPU or the port (GB10: one engine at a time) ──
if systemctl is-active --quiet qwen38-sglang 2>/dev/null; then
  die "the qwen38-sglang systemd service is running. Stop it first: sudo systemctl stop qwen38-sglang"
fi
for NAME in qwen38-sglang qwen38-sglang-run; do
  [ -z "$(docker ps -q -f "name=^${NAME}$")" ] || die "container $NAME is already running (docker stop $NAME)"
done
if ss -tlnH 2>/dev/null | awk '{print $4}' | grep -q ":$PORT\$"; then
  die "port $PORT is already in use (ss -tlnp | grep :$PORT). Free it or run: PORT=<other> ./run.sh"
fi
GPU_APPS="$(nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader 2>/dev/null || true)"
[ -z "$GPU_APPS" ] || die "the GPU is busy. This config needs the machine to itself (unified memory):
$GPU_APPS"

KEY="$(cat "$CONFIG_DIR/api-key")"
mkdir -p "$CONFIG_DIR/sglang-cache"
echo "Starting in the foreground (first boot ≈ 9 min: torch.compile + CUDA graph capture)."
echo "  Ready when the log says:  The server is fired up and ready to roll!"
echo "  Test from another shell:  curl http://127.0.0.1:$PORT/health"
echo "  opencode:                 provider config at $CONFIG_DIR/opencode.json (README, \"opencode integration\")"
echo "  Stop:                     Ctrl+C (container removed; compile cache kept for faster next boots)"
echo

# Verbatim the systemd unit's docker run (same caps, mounts, flags), foreground.
exec docker run --rm --name qwen38-sglang-run --gpus all \
  --memory 100g --memory-swap 100g --shm-size 16g --network host --ipc=host \
  -e TORCHINDUCTOR_CACHE_DIR=/cache/inductor \
  -v "$CONFIG_DIR/sglang-cache":/cache \
  -v "$HF_CACHE":/root/.cache/huggingface \
  -v "$CONFIG_DIR":/out \
  "$SERVE_IMAGE" \
  python3 -m sglang.launch_server \
    --trust-remote-code --model-path "$MODEL_REPO" --revision "$MODEL_REV" --tp-size 1 \
    --served-model-name qwen3.8-27b \
    --mem-fraction-static 0.50 ${KV_CACHE_ARGS} \
    --attention-backend flashinfer --chunked-prefill-size 8192 \
    --disable-prefill-cuda-graph --cuda-graph-max-bs 8 \
    --disable-flashinfer-autotune \
    --speculative-algorithm DFLASH --speculative-draft-model-path z-lab/Qwen3.8-27B-DFlash2 \
    --speculative-draft-model-revision "$DRAFT2_REV" \
    --speculative-num-draft-tokens 8 --speculative-draft-model-quantization unquant \
    --mamba-radix-cache-strategy extra_buffer --mamba-ssm-dtype bfloat16 \
    --max-mamba-cache-size 96 --max-running-requests 8 \
    --enable-torch-compile --torch-compile-max-bs 4 \
    --num-continuous-decode-steps 2 \
    --reasoning-parser qwen3 --tool-call-parser qwen3_coder \
    --chat-template /out/chat-template-sglang.jinja \
    --sleep-on-idle \
    --api-key "$KEY" \
    --host 0.0.0.0 --port "$PORT"
