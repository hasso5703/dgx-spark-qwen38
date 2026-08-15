#!/usr/bin/env bash
# Qwen3.8-27B NVFP4 + DSpark on DGX Spark (GB10) — SGLang, systemd, hardened.
# Idempotent: safe to re-run at any time (uses local caches when present).
# Everything is PINNED to the versions validated on 2026-08-15; override with
# env vars if you want to try newer builds (see --help).
set -euo pipefail
trap 'printf "\n\033[1;31mInstall failed at line %s (command: %s).\033[0m\nRe-running ./install.sh is safe — completed steps are skipped.\n" "$LINENO" "$BASH_COMMAND" >&2' ERR

# ── Pinned, validated versions (override via env if you know what you do) ──
IMAGE="${IMAGE:-lmsysorg/sglang@sha256:febfb971c7352570fc445c466ebd6ffc9d896024958e544a60f2137fd85856b1}"  # = lmsysorg/sglang:qwen38-27b, 2026-08-15
MODEL_REPO="RadixArk/Qwen3.8-27B-NVFP4"
DRAFT_REPO="RadixArk/Qwen3.8-27B-DSpark"
MODEL_REV="${MODEL_REV:-52d1adc5f38aa5ebf099c29ed7025ba34cfbb854}"
DRAFT_REV="${DRAFT_REV:-923ed3a8572615643f0137e424e4ce4edd7f1cda}"
PORT="${PORT:-30000}"
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
CONFIG_DIR="$HOME/.config/qwen38"
UNIT_NAME="qwen38-sglang.service"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WITH_WARMUP=0
NO_START=0
NO_SERVICE=0
for arg in "$@"; do
  case "$arg" in
    --with-claude-warmup) WITH_WARMUP=1 ;;
    --no-start) NO_START=1 ;;
    --no-service) NO_SERVICE=1 ;;
    -h|--help)
      cat <<'HLP'
Usage: ./install.sh [--with-claude-warmup] [--no-start] [--no-service]

  --with-claude-warmup  pre-warm your Claude Code system prompt after each boot
                        (requires the 'claude' CLI in PATH)
  --no-start            install everything but don't start the service now
  --no-service          no systemd, no sudo: just prepare everything (image,
                        checkpoints, key, template), then run in the foreground
                        anytime with ./run.sh (Ctrl+C stops it)

Env overrides (defaults are pinned to the validated 2026-08-15 versions):
  IMAGE=lmsysorg/sglang:qwen38-27b   use the moving tag instead of the digest
  MODEL_REV=main  DRAFT_REV=main     use latest checkpoint revisions
  HF_CACHE=/path                     HuggingFace cache location (needs ~24 GB)
  PORT=30000                         serving port
HLP
      exit 0 ;;
    *) printf 'Unknown flag: %s (see --help)\n' "$arg" >&2; exit 1 ;;
  esac
done
if [ "$NO_SERVICE" -eq 1 ] && [ "$WITH_WARMUP" -eq 1 ]; then
  printf -- '--with-claude-warmup is part of the systemd service; it cannot be combined with --no-service\n' >&2; exit 1
fi
if [ "$NO_SERVICE" -eq 1 ] && [ "$NO_START" -eq 1 ]; then
  printf -- '--no-start controls the systemd service; with --no-service there is no service (drop one flag)\n' >&2; exit 1
fi

step() { printf '\n\033[1;36m── %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

step "1/8 Preflight checks"
[ "$(uname -m)" = "aarch64" ] || die "This setup targets GB10 (aarch64). Detected: $(uname -m)."
command -v nvidia-smi >/dev/null || die "nvidia-smi not found — is the NVIDIA driver stack installed? (stock on DGX OS)"
GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
echo "GPU: $GPU_NAME"
case "$GPU_NAME" in *GB10*) ;; *) echo "WARNING: expected GB10, found '$GPU_NAME'. Continuing — but this config was only validated on GB10 (memory sizing may not fit other GPUs)." ;; esac
command -v docker >/dev/null || die "docker not found. Install Docker + NVIDIA Container Toolkit (stock on DGX OS)."
docker info >/dev/null 2>&1 || die "Cannot talk to the docker daemon. Fix: sudo usermod -aG docker \$USER && re-login (or run with a user in the docker group)."
TOTAL_GB=$(free -g | awk '/^Mem/{print $2}')
[ "$TOTAL_GB" -ge 110 ] || die "This config needs a ~121 GB unified-memory machine; found ${TOTAL_GB} GB."
FREE_DISK_GB=$(df -BG --output=avail "$HOME" | tail -1 | tr -dc '0-9')
[ "$FREE_DISK_GB" -ge 45 ] || die "Need ~45 GB free under \$HOME for the checkpoints and caches; found ${FREE_DISK_GB} GB. Free some space or set HF_CACHE to another disk."
DOCKER_ROOT=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)
DOCKER_FREE_GB=$(df -BG --output=avail "$DOCKER_ROOT" 2>/dev/null | tail -1 | tr -dc '0-9')
[ "${DOCKER_FREE_GB:-0}" -ge 45 ] || die "Need ~45 GB free on $DOCKER_ROOT for the 39 GB Docker image; found ${DOCKER_FREE_GB:-?} GB (docker images live there, not under \$HOME)."
if ss -tlnH 2>/dev/null | awk '{print $4}' | grep -q ":$PORT\$"; then
  if docker inspect qwen38-sglang --format '{{join .Args " "}}' 2>/dev/null | grep -qE -- "--port ${PORT}(\s|$)"; then
    echo "Note: $UNIT_NAME is already running on :$PORT — re-installing over it (converging config)."
  else
    die "Port $PORT is already in use by another program (see: ss -tlnp | grep :$PORT). Free it, or install with PORT=<other> ./install.sh"
  fi
fi
if [ "$WITH_WARMUP" -eq 1 ]; then command -v claude >/dev/null || die "--with-claude-warmup requires the 'claude' CLI in PATH (https://claude.com/claude-code)."; fi
echo "OK (aarch64, ${TOTAL_GB} GB RAM, ${FREE_DISK_GB} GB free)"

step "2/8 Pulling the SGLang image (~39 GB, one-time — resumable)"
docker pull "$IMAGE" || die "docker pull failed. Causes: no internet, Docker Hub rate limit (retry in a few minutes or 'docker login'), or the pinned digest was removed upstream — try IMAGE=lmsysorg/sglang:qwen38-27b ./install.sh"

step "3/8 Verifying the container can see the GPU"
docker run --rm --gpus all "$IMAGE" nvidia-smi -L >/dev/null 2>&1 \
  || die "'docker run --gpus all' cannot access the GPU. The NVIDIA Container Toolkit is missing or unconfigured. Fix: sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
echo "OK — container sees: $(docker run --rm --gpus all "$IMAGE" nvidia-smi -L 2>/dev/null | head -1)"

step "4/8 Downloading checkpoints (~24 GB, one-time — reuses/resumes any local copy)"
mkdir -p "$HF_CACHE" "$CONFIG_DIR/sglang-cache"
docker run --rm -i --network host --user "$(id -u):$(id -g)" \
  -e HF_HOME=/hf \
  -e MODEL_REPO="$MODEL_REPO" -e MODEL_REV="$MODEL_REV" \
  -e DRAFT_REPO="$DRAFT_REPO" -e DRAFT_REV="$DRAFT_REV" \
  -v "$HF_CACHE":/hf \
  "$IMAGE" python3 - <<'PYEOF' || die "Checkpoint download failed. Causes: no internet, HuggingFace outage/rate-limit (re-run: downloads resume), a pinned revision removed (try MODEL_REV=main DRAFT_REV=main ./install.sh), or a permission error — if your $HF_CACHE contains root-owned files from other tools, fix with: sudo chown -R \$(id -u):\$(id -g) $HF_CACHE"
import os
from huggingface_hub import snapshot_download
for repo, rev in ((os.environ["MODEL_REPO"], os.environ["MODEL_REV"]),
                  (os.environ["DRAFT_REPO"], os.environ["DRAFT_REV"])):
    print(f"── {repo} @ {rev}", flush=True)
    snapshot_download(repo, revision=rev)
print("checkpoints ready", flush=True)
PYEOF

step "5/8 API key + patched chat template"
if [ ! -s "$CONFIG_DIR/api-key" ]; then
  head -c 24 /dev/urandom | base64 | tr -d '/+=' > "$CONFIG_DIR/api-key"
  chmod 600 "$CONFIG_DIR/api-key"
  echo "API key generated at $CONFIG_DIR/api-key"
else
  echo "API key already present — keeping it"
fi
python3 "$REPO_DIR/patch-template.py" "$HF_CACHE" "$CONFIG_DIR/chat-template-sglang.jinja" \
  || die "Template patch failed (see message above). If the upstream template changed, please open an issue on this repo."

step "6/8 Claude Code env file"
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
chmod 600 "$CONFIG_DIR/claude-code.env"
echo "wrote $CONFIG_DIR/claude-code.env (mode 600 — it contains the API key)"

if [ "$NO_SERVICE" -eq 1 ]; then
  printf '\n\033[1;32m✅ Prepared (no systemd, nothing needed sudo).\033[0m\n'
  echo "  Run in the foreground: ./run.sh     (Ctrl+C stops it; first boot ≈ 9 min)"
  echo "  Everything it uses lives in $CONFIG_DIR and $HF_CACHE — delete those to remove."
  exit 0
fi

step "7/8 Installing the systemd service (sudo needed)"
TMP_UNIT="$(mktemp)"
sed -e "s|__HOME__|$HOME|g" \
    -e "s|__USER__|$(id -un)|g" \
    -e "s|__GROUP__|$(id -gn)|g" \
    -e "s|__PORT__|$PORT|g" \
    -e "s|__IMAGE__|$IMAGE|g" \
    -e "s|__HF_CACHE__|$HF_CACHE|g" \
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
  step "Done (service installed and enabled at boot — start it with: sudo systemctl start $UNIT_NAME)"
  exit 0
fi

step "8/8 Starting (first boot ≈ 9 min: torch.compile + CUDA graph capture; later boots are faster)"
sudo systemctl restart "$UNIT_NAME"
for i in $(seq 1 150); do
  if curl -s -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "health OK — running a real generation smoke test..."
    SMOKE="$(curl -s -m 300 "http://127.0.0.1:$PORT/v1/chat/completions" \
      -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
      -d '{"model":"qwen3.8-27b","messages":[{"role":"user","content":"Reply with exactly: READY"}],"max_tokens":600}' \
      | python3 -c 'import json,sys
try:
    m=json.load(sys.stdin)["choices"][0]["message"]
    print("OK" if (m.get("content") or m.get("reasoning_content") or "").strip() else "EMPTY")
except Exception as e:
    print(f"FAIL:{e}")')"
    [ "$SMOKE" = "OK" ] || die "Server is up but the smoke generation failed ($SMOKE). Check: journalctl -u $UNIT_NAME -n 50"
    printf '\n\033[1;32m✅ Installed, verified, and enabled at boot.\033[0m\n'
    echo "  OpenAI     : http://<host>:$PORT/v1/chat/completions"
    echo "  Anthropic  : http://<host>:$PORT/v1/messages   (Bearer auth only)"
    echo "  API key    : $CONFIG_DIR/api-key"
    echo "  Claude Code: source $CONFIG_DIR/claude-code.env && claude --model qwen3.8-27b"
    echo "  Benchmark  : ./bench.sh"
    exit 0
  fi
  ST="$(systemctl is-active "$UNIT_NAME" || true)"
  [ "$ST" = "failed" ] && { journalctl -u "$UNIT_NAME" --no-pager | tail -25; die "Service failed during startup — logs above. Common cause: another process eating GPU/unified memory (this config needs the machine to itself)."; }
  [ $((i % 15)) -eq 0 ] && echo "  still loading... ($((i*8))s — first boot compiles kernels, be patient)"
  sleep 8
done
die "Server did not come up within 20 min. Watch: journalctl -u $UNIT_NAME -f"
