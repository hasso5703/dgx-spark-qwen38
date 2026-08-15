#!/usr/bin/env bash
# Qwen3.8-27B NVFP4 + DSpark on DGX Spark (GB10) — SGLang, systemd, hardened.
# Idempotent: safe to re-run. See README.md for what and why.
set -euo pipefail

IMAGE="lmsysorg/sglang:qwen38-27b"
MODEL_REPO="RadixArk/Qwen3.8-27B-NVFP4"
DRAFT_REPO="RadixArk/Qwen3.8-27B-DSpark"
PORT="${PORT:-30000}"
CONFIG_DIR="$HOME/.config/qwen38"
HF_CACHE="$HOME/.cache/huggingface"
UNIT_NAME="qwen38-sglang.service"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WITH_WARMUP=0
NO_START=0
for arg in "$@"; do
  case "$arg" in
    --with-claude-warmup) WITH_WARMUP=1 ;;
    --no-start) NO_START=1 ;;
    -h|--help)
      echo "Usage: ./install.sh [--with-claude-warmup] [--no-start]"
      echo "  --with-claude-warmup  pre-warm the Claude Code system prompt after each boot (needs 'claude' CLI)"
      echo "  --no-start            install everything but don't start the service now"
      exit 0 ;;
    *) echo "Unknown flag: $arg (see --help)"; exit 1 ;;
  esac
done

step() { printf '\n\033[1;36m── %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

step "1/7 Preflight checks"
[ "$(uname -m)" = "aarch64" ] || die "This is for GB10 (aarch64). Detected: $(uname -m)"
command -v nvidia-smi >/dev/null || die "nvidia-smi not found — is this a DGX Spark with the NVIDIA stack?"
GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
echo "GPU: $GPU_NAME"
case "$GPU_NAME" in *GB10*) ;; *) echo "WARNING: expected GB10, found '$GPU_NAME' — continuing, but the config was validated on GB10 only." ;; esac
command -v docker >/dev/null || die "docker not found (stock on DGX OS — install docker + NVIDIA container toolkit)"
docker info >/dev/null 2>&1 || die "docker daemon unreachable (is your user in the docker group?)"
docker info 2>/dev/null | grep -qi 'nvidia' || echo "WARNING: NVIDIA container runtime not visible in 'docker info' — '--gpus all' must work for this to run."
TOTAL_GB=$(free -g | awk '/^Mem/{print $2}')
[ "$TOTAL_GB" -ge 110 ] || die "Needs ~121 GB unified memory, found ${TOTAL_GB}G"
FREE_DISK_GB=$(df -BG --output=avail "$HOME" | tail -1 | tr -dc '0-9')
[ "$FREE_DISK_GB" -ge 85 ] || die "Needs ~85 GB free disk (image 39 GB + checkpoints 24 GB + caches), found ${FREE_DISK_GB}G"
if [ "$WITH_WARMUP" -eq 1 ]; then command -v claude >/dev/null || die "--with-claude-warmup needs the 'claude' CLI in PATH"; fi
echo "OK (aarch64, ${TOTAL_GB}G RAM, ${FREE_DISK_GB}G free disk)"

step "2/7 Pulling SGLang image (~39 GB, one-time)"
docker pull "$IMAGE"

step "3/7 Downloading checkpoints into your HF cache (~24 GB, one-time)"
mkdir -p "$HF_CACHE" "$CONFIG_DIR/sglang-cache"
docker run --rm --network host \
  -v "$HF_CACHE":/root/.cache/huggingface \
  "$IMAGE" python3 - <<PYEOF
from huggingface_hub import snapshot_download
for repo in ("$MODEL_REPO", "$DRAFT_REPO"):
    print(f"downloading {repo} ...", flush=True)
    snapshot_download(repo)
print("checkpoints ready")
PYEOF

step "4/7 API key + patched chat template"
if [ ! -s "$CONFIG_DIR/api-key" ]; then
  head -c 24 /dev/urandom | base64 | tr -d '/+=' > "$CONFIG_DIR/api-key"
  chmod 600 "$CONFIG_DIR/api-key"
  echo "API key generated at $CONFIG_DIR/api-key"
else
  echo "API key already present — keeping it"
fi
python3 "$REPO_DIR/patch-template.py" "$HF_CACHE" "$CONFIG_DIR/chat-template-sglang.jinja"

step "5/7 Claude Code env file"
KEY="$(cat "$CONFIG_DIR/api-key")"
cat > "$CONFIG_DIR/claude-code.env" <<ENVEOF
# source this file, then run:  claude --model qwen3.8-27b
export ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT"
export ANTHROPIC_AUTH_TOKEN="$KEY"
export ANTHROPIC_API_KEY=""
export ANTHROPIC_MODEL="qwen3.8-27b"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="qwen3.8-27b"
export ANTHROPIC_DEFAULT_SONNET_MODEL="qwen3.8-27b"
export ANTHROPIC_DEFAULT_OPUS_MODEL="qwen3.8-27b"
export ANTHROPIC_DEFAULT_FABLE_MODEL="qwen3.8-27b"
export CLAUDE_CODE_SUBAGENT_MODEL="qwen3.8-27b"
export API_TIMEOUT_MS=3600000
export CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS=1800000
export CLAUDE_STREAM_IDLE_TIMEOUT_MS=1800000
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=262144
export CLAUDE_CODE_ATTRIBUTION_HEADER=0
export CLAUDE_CODE_ENABLE_TELEMETRY=0
ENVEOF
echo "wrote $CONFIG_DIR/claude-code.env"

step "6/7 Installing systemd service (sudo needed)"
TMP_UNIT="$(mktemp)"
sed -e "s|__HOME__|$HOME|g" \
    -e "s|__USER__|$(id -un)|g" \
    -e "s|__GROUP__|$(id -gn)|g" \
    -e "s|__PORT__|$PORT|g" \
    "$REPO_DIR/qwen38-sglang.service.template" > "$TMP_UNIT"
sudo cp "$TMP_UNIT" "/etc/systemd/system/$UNIT_NAME"; rm -f "$TMP_UNIT"
if [ "$WITH_WARMUP" -eq 1 ]; then
  sed -e "s|__HOME__|$HOME|g" -e "s|__PORT__|$PORT|g" \
      "$REPO_DIR/warmup-claude-code.sh" > "$CONFIG_DIR/warmup-claude-code.sh"
  chmod +x "$CONFIG_DIR/warmup-claude-code.sh"
  sudo mkdir -p "/etc/systemd/system/$UNIT_NAME.d"
  printf '[Service]\nExecStartPost=/bin/sh -c '\''%s/warmup-claude-code.sh &'\''\n' "$CONFIG_DIR" \
    | sudo tee "/etc/systemd/system/$UNIT_NAME.d/warmup.conf" >/dev/null
fi
sudo systemctl daemon-reload
sudo systemctl enable "$UNIT_NAME"

if [ "$NO_START" -eq 1 ]; then
  step "Done (service installed, not started — sudo systemctl start $UNIT_NAME)"
  exit 0
fi

step "7/7 Starting (first boot ≈ 9 min: torch.compile + CUDA graph capture)"
sudo systemctl start "$UNIT_NAME"
for i in $(seq 1 150); do
  if curl -s -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    printf '\n\033[1;32m✅ Server is up.\033[0m\n'
    echo "  OpenAI     : http://<host>:$PORT/v1/chat/completions"
    echo "  Anthropic  : http://<host>:$PORT/v1/messages   (Bearer auth only)"
    echo "  API key    : $CONFIG_DIR/api-key"
    echo "  Claude Code: source $CONFIG_DIR/claude-code.env && claude --model qwen3.8-27b"
    echo "  Benchmark  : ./bench.sh"
    exit 0
  fi
  ST="$(systemctl is-active "$UNIT_NAME" || true)"
  [ "$ST" = "failed" ] && { journalctl -u "$UNIT_NAME" --no-pager | tail -20; die "service failed — logs above"; }
  sleep 8
done
die "server did not come up within 20 min — check: journalctl -u $UNIT_NAME -f"
