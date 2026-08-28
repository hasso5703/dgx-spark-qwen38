#!/usr/bin/env bash
# Qwen3.8 serving stack on DGX Spark (GB10): 27B (SGLang+DFlash2) or Flash-Next
# 176B (SGLang+NEXTN, PLE table mmap-served from NVMe). Systemd, hardened.
# Idempotent: safe to re-run at any time (uses local caches when present).
# Everything is PINNED to the versions validated on 2026-08-15; override with
# env vars if you want to try newer builds (see --help).
set -euo pipefail
trap 'printf "\n\033[1;31mInstall failed at line %s (command: %s).\033[0m\nRe-running ./install.sh is safe: completed steps are skipped.\n" "$LINENO" "$BASH_COMMAND" >&2' ERR

# Remember which knobs the operator set explicitly on THIS invocation, before
# the defaults below fill them in: an explicit env var beats the installed
# unit, which beats the defaults (see the convergence block further down).
_ENV_MODEL_CHOICE="${MODEL_CHOICE:-}"; _ENV_MODEL_REV="${MODEL_REV:-}"
_ENV_PORT="${PORT:-}"; _ENV_HF_CACHE="${HF_CACHE:-}"
_ENV_CONTEXT_MODE="${CONTEXT_MODE:-}"; _ENV_PROXY_PORT="${PROXY_PORT:-}"
_ENV_PLE_DIR="${PLE_DIR:-}"

# ── Pinned, validated versions (override via env if you know what you do) ──
IMAGE="${IMAGE:-lmsysorg/sglang@sha256:febfb971c7352570fc445c466ebd6ffc9d896024958e544a60f2137fd85856b1}"  # = lmsysorg/sglang:qwen38-27b, 2026-08-15
# Target model choice: "stock" (validated censored base, default) or "uncensored"
# (huihui-ai abliteration re-quantized with the identical RadixArk modelopt
# NVFP4 recipe: same architecture, chat template, MTP + vision, ~22 GB).
STOCK_REPO="RadixArk/Qwen3.8-27B-NVFP4"
STOCK_REV="52d1adc5f38aa5ebf099c29ed7025ba34cfbb854"
UNC_REPO="edp1096/Huihui-RadixArk-Qwen3.8-27B-abliterated-NVFP4"
UNC_REV="21565d389fe573a32c1c425e0c7ade204ddb2263"
# Third target: Qwen3.8-Flash-Next (176B hybrid MoE, 6B active) in NVFP4 on
# SGLang (same engine as the 27B pair), single box. The official image cannot
# fit it on a GB10 as shipped; the locally built two-file overlay mmaps the 51B
# N-gram (PLE) table from NVMe and unblocks QSA decode on sm_121 (see
# flash-sglang/ATTRIBUTION.md). Same service surface: port, API key, keepalive
# proxy, opencode wiring, and (v1.5) working prefix caching, vision and the
# Anthropic endpoint. Serving flags validated on the reference box 2026-08-28.
FLASH_REPO="RadixArk/Qwen3.8-Flash-Next-NVFP4"
FLASH_REV="7b719225242aacd3dbd3f9407468c2ee9a9d2594"
FLASH_IMAGE="${FLASH_IMAGE:-lmsysorg/sglang@sha256:12d3392bdc8be8d35e9a95f191df6aef99c5114bdbefd41bfdc7e760e6d25ec1}"  # = lmsysorg/sglang:qwen38flashnext, 2026-08-26
FLASH_SERVE_IMAGE="${FLASH_SERVE_IMAGE:-qwen38-flash:v1.5}"
# Backing store for the flash target's mmap-served 51B PLE table (~48 GiB,
# written once at first boot, reused afterwards; delete it to reclaim).
PLE_DIR="${PLE_DIR:-$HOME/flashnext-ple}"
MODEL_CHOICE="${MODEL_CHOICE:-stock}"
case "$MODEL_CHOICE" in
  stock)      MODEL_REPO="$STOCK_REPO"; MODEL_REV="${MODEL_REV:-$STOCK_REV}" ;;
  uncensored) MODEL_REPO="$UNC_REPO";   MODEL_REV="${MODEL_REV:-$UNC_REV}" ;;
  flash)      MODEL_REPO="$FLASH_REPO"; MODEL_REV="${MODEL_REV:-$FLASH_REV}" ;;
  *) printf 'ERROR: MODEL_CHOICE must be "stock", "uncensored" or "flash" (got: %s)\n' "$MODEL_CHOICE" >&2; exit 1 ;;
esac
DRAFT_REPO="RadixArk/Qwen3.8-27B-DSpark"
DRAFT_REV="${DRAFT_REV:-85ef153be924f17ce4bf62726954eeaa4a73e854}"
DRAFT2_REPO="z-lab/Qwen3.8-27B-DFlash2"
DRAFT2_REV="${DRAFT2_REV:-50307d4c4cde6860d4eee73e2547cd786fe8e8a4}"
# Context mode: "native" (262144, the validated default) or "1m" (1,010,000
# via YaRN static scaling, mem-fraction 0.70, plus a keepalive proxy for agent
# clients; the field-tested preset from the reference box, see the README).
CONTEXT_MODE="${CONTEXT_MODE:-native}"
case "$CONTEXT_MODE" in
  native|1m) ;;
  *) printf 'ERROR: CONTEXT_MODE must be "native" or "1m" (got: %s)\n' "$CONTEXT_MODE" >&2; exit 1 ;;
esac
if [ "$MODEL_CHOICE" = "flash" ] && [ "$CONTEXT_MODE" = "1m" ]; then
  printf 'ERROR: CONTEXT_MODE=1m is a 27B mode. Flash-Next serves its full native 262144 window\n' >&2
  printf '       by default; a validated long-context mode for it may come in a later release.\n' >&2
  exit 1
fi
# Lane implied by the target model: the 27B pair and Flash-Next each have
# their own unit and serving image (both SGLang since v1.5), same port,
# never enabled together.
LANE=27b; UNIT_NAME="qwen38-sglang.service"
[ "$MODEL_CHOICE" = "flash" ] && { LANE=flash; UNIT_NAME="qwen38-flash.service"; }
# Served image = pinned base + the 5 sha256-verified DFlash2 files (dflash2/, built locally,
# offline). Replaced by an official image digest the day one ships DFLASH2.
SERVE_IMAGE="${SERVE_IMAGE:-qwen38-dflash2:v1.2.2}"
PORT="${PORT:-30000}"
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
CONFIG_DIR="$HOME/.config/qwen38"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NO_START=0
NO_SERVICE=0
for arg in "$@"; do
  case "$arg" in
    --no-start) NO_START=1 ;;
    --no-service) NO_SERVICE=1 ;;
    --with-claude-warmup)
      echo "NOTE: --with-claude-warmup was removed in v1.3 (the repo's client story moved to opencode)."
      echo "      The flag is ignored; an installed warmup drop-in from an earlier version is cleaned up." ;;
    -h|--help)
      cat <<'HLP'
Usage: ./install.sh [--no-start] [--no-service]

  --no-start            install everything but don't start the service now
  --no-service          no systemd, no sudo: just prepare everything (image,
                        checkpoints, key, template), then run in the foreground
                        anytime with ./run.sh (Ctrl+C stops it)

Re-running over an existing install keeps the operator's choices: the target
model (stock/uncensored), the context mode (native/1m), the port and the HF
cache location are read from the installed unit unless the env var is passed
explicitly.

Env overrides (defaults are pinned to the validated 2026-08-15 versions):
  IMAGE=lmsysorg/sglang:qwen38-27b   use the moving tag instead of the digest
  MODEL_REV=main  DRAFT_REV=main     use latest checkpoint revisions
  DRAFT2_REV=main                    latest DFlash2 draft revision
  MODEL_CHOICE=uncensored            serve the huihui-abliterated model
                                     (edp1096 NVFP4) instead of the stock base
  MODEL_CHOICE=flash                 serve Qwen3.8-Flash-Next (176B hybrid MoE,
                                     NVFP4, SGLang engine, ~136 GB download; the
                                     51B N-gram table is mmap-served from NVMe,
                                     with working prefix caching and vision)
  PLE_DIR=~/flashnext-ple            flash only: where the ~48 GB mmap backing
                                     file of the N-gram table lives
  CONTEXT_MODE=1m                    1,010,000-token context via YaRN static
                                     scaling, 27B targets only (README, "The 1M context mode")
  PROXY_PORT=30001                   keepalive proxy port (default: PORT+1)
  SERVE_IMAGE=name:tag               local tag for the built 27B serving image
  FLASH_SERVE_IMAGE=name:tag         local tag for the built Flash-Next serving image
  HF_CACHE=/path                     HuggingFace cache location (~28 GB for a 27B
                                     target, ~136 GB for flash)
  PORT=30000                         serving port
HLP
      exit 0 ;;
    *) printf 'Unknown flag: %s (see --help)\n' "$arg" >&2; exit 1 ;;
  esac
done

# ── Converge on the operator's installed choices ──
# Re-running the installer (or the get.sh one-liner) must never silently reset
# a choice that is already serving: the target model (stock/uncensored/flash),
# the port, and the HF cache location are read from the installed units and
# kept, unless the corresponding env var was passed explicitly on this
# invocation. Everything else (image digest, checkpoint revisions, launch
# flags) always follows the repo: that is what an upgrade is.
KEEP_MODEL_VERBATIM=0
SGL_UNIT_PATH="/etc/systemd/system/qwen38-sglang.service"
FLASH_UNIT_PATH="/etc/systemd/system/qwen38-flash.service"
for _p in "$SGL_UNIT_PATH" "$FLASH_UNIT_PATH"; do
  if [ -f "$_p" ] && [ ! -r "$_p" ]; then
    echo "NOTE: an installed unit ($_p) exists but is not readable (pre-v1.2.3 installs used mode 600),"
    echo "      so its choices cannot be preserved automatically. If you had a custom PORT= or"
    echo "      MODEL_CHOICE=, pass them explicitly on this command."
  fi
done
# Which choice is installed? The flash unit wins only when it is the enabled
# one; a box can hold both unit files but only one engine serves the port.
# On the (hand-made) both-enabled state, the 27B unit is followed and a note
# is printed: predictable beats clever here.
INSTALLED_CHOICE=""
SGL_READABLE=0; FLASH_READABLE=0
[ -f "$SGL_UNIT_PATH" ] && [ -r "$SGL_UNIT_PATH" ] && SGL_READABLE=1
[ -f "$FLASH_UNIT_PATH" ] && [ -r "$FLASH_UNIT_PATH" ] && FLASH_READABLE=1
FLASH_ENABLED=0; SGL_ENABLED=0
systemctl is-enabled --quiet qwen38-flash.service 2>/dev/null && FLASH_ENABLED=1
systemctl is-enabled --quiet qwen38-sglang.service 2>/dev/null && SGL_ENABLED=1
if [ "$FLASH_READABLE" -eq 1 ] && [ "$FLASH_ENABLED" -eq 1 ] && [ "$SGL_ENABLED" -eq 1 ]; then
  echo "NOTE: both qwen38-sglang and qwen38-flash are enabled (only one can serve the port)."
  echo "      Following the 27B unit; run ./switch-model.sh to resolve this cleanly."
fi
if [ "$FLASH_READABLE" -eq 1 ] && [ "$FLASH_ENABLED" -eq 1 ] && [ "$SGL_ENABLED" -eq 0 ]; then
  INSTALLED_CHOICE=flash
elif [ "$SGL_READABLE" -eq 1 ]; then
  INSTALLED_CHOICE=27b
elif [ "$FLASH_READABLE" -eq 1 ]; then
  # flash unit present but not enabled and no sglang unit: still the only choice
  INSTALLED_CHOICE=flash
fi
if [ "$INSTALLED_CHOICE" = "flash" ]; then
  # Converge on the installed flash launch script (the unit only points at it;
  # the vLLM flags and mounts live in $CONFIG_DIR/launch-flash.sh).
  FLASH_LAUNCH="$CONFIG_DIR/launch-flash.sh"
  CUR_REV=""; CUR_PORT=""; CUR_HF=""
  if [ -r "$FLASH_LAUNCH" ]; then
    CUR_REV="$(grep -oE -- '--revision [^ ]+' "$FLASH_LAUNCH" | head -1 | cut -d' ' -f2 || true)"
    if grep -q 'sglang.launch_server' "$FLASH_LAUNCH"; then
      # v1.5+ shape (SGLang, host networking): --port IS the host port.
      CUR_PORT="$(grep -oE -- '--port [0-9]+' "$FLASH_LAUNCH" | head -1 | tr -dc '0-9' || true)"
      CUR_HF="$(grep -oE -- '-v [^ :]+:/root/\.cache/huggingface' "$FLASH_LAUNCH" | head -1 | sed -e 's/^-v //' -e 's|:/root/\.cache/huggingface$||' || true)"
    else
      # v1.4 shape (vLLM, bridge networking): the host port is the -p mapping;
      # its in-container --port 8000 must NOT be read as a host port. The
      # upgrade regenerates the launcher on the new engine, keeping only
      # port/HF/model choices.
      CUR_PORT="$(grep -oE -- '-p [0-9]+:8000' "$FLASH_LAUNCH" | head -1 | grep -oE '^-p [0-9]+' | tr -dc '0-9' || true)"
      CUR_HF="$(grep -oE -- '-v [^ :]+:/hf' "$FLASH_LAUNCH" | head -1 | sed -e 's/^-v //' -e 's|:/hf$||' || true)"
      echo "NOTE: the installed flash lane is the v1.4 vLLM engine; this upgrade moves it to SGLang"
      echo "      (working prefix caching; see CHANGELOG v1.5). Port/cache/model choices are kept."
    fi
    CUR_PLE="$(grep -oE -- '-v [^ :]+:/ple' "$FLASH_LAUNCH" | head -1 | sed -e 's/^-v //' -e 's|:/ple$||' || true)"
    if [ -z "${_ENV_PLE_DIR:-}" ] && [ -n "$CUR_PLE" ] && [ "$CUR_PLE" != "$PLE_DIR" ]; then
      PLE_DIR="$CUR_PLE"
      echo "Keeping the installed PLE table location: $PLE_DIR. Pass PLE_DIR= to change."
    fi
  fi
  if [ -z "$_ENV_MODEL_CHOICE" ] && [ "$MODEL_CHOICE" != "flash" ]; then
    MODEL_CHOICE=flash; MODEL_REPO="$FLASH_REPO"; MODEL_REV="${_ENV_MODEL_REV:-$FLASH_REV}"
    LANE=flash; UNIT_NAME="qwen38-flash.service"
    echo "Keeping the installed target model: flash ($FLASH_REPO). Pass MODEL_CHOICE= to change."
  fi
elif [ "$INSTALLED_CHOICE" = "27b" ]; then
  UNIT_PATH="$SGL_UNIT_PATH"
  CUR_MODEL="$(grep -oE -- '--model-path [^ ]+' "$UNIT_PATH" | head -1 | cut -d' ' -f2 || true)"
  # The target's --revision (never matches --speculative-draft-model-revision:
  # that flag has a single dash before "revision")
  CUR_REV="$(grep -oE -- '--revision [^ ]+' "$UNIT_PATH" | head -1 | cut -d' ' -f2 || true)"
  CUR_PORT="$(grep -oE -- '--port [0-9]+' "$UNIT_PATH" | head -1 | tr -dc '0-9' || true)"
  CUR_HF="$(grep -oE -- '-v [^ :]+:/root/\.cache/huggingface' "$UNIT_PATH" | head -1 | sed -e 's/^-v //' -e 's|:/root/\.cache/huggingface$||' || true)"
  if [ -z "$_ENV_MODEL_CHOICE" ] && [ -n "$CUR_MODEL" ] && [ "$CUR_MODEL" != "$MODEL_REPO" ]; then
    case "$CUR_MODEL" in
      "$STOCK_REPO") MODEL_CHOICE=stock;      MODEL_REPO="$STOCK_REPO"; MODEL_REV="${_ENV_MODEL_REV:-$STOCK_REV}" ;;
      "$UNC_REPO")   MODEL_CHOICE=uncensored; MODEL_REPO="$UNC_REPO";   MODEL_REV="${_ENV_MODEL_REV:-$UNC_REV}" ;;
      *) KEEP_MODEL_VERBATIM=1; MODEL_CHOICE=custom; MODEL_REPO="$CUR_MODEL" ;;
    esac
    LANE=27b; UNIT_NAME="qwen38-sglang.service"
    if [ "$KEEP_MODEL_VERBATIM" -eq 1 ]; then
      echo "NOTE: keeping the custom --model-path already installed ($CUR_MODEL)."
      echo "      Its download and template steps are skipped (it is already serving from cache)."
      echo "      Pass MODEL_CHOICE=stock or MODEL_CHOICE=uncensored to override."
    else
      echo "Keeping the installed target model: $MODEL_CHOICE ($MODEL_REPO). Pass MODEL_CHOICE= to change."
    fi
  fi
fi
if [ -n "$INSTALLED_CHOICE" ]; then
  if [ -z "$_ENV_PORT" ] && [ -n "${CUR_PORT:-}" ] && [ "$CUR_PORT" != "$PORT" ]; then
    PORT="$CUR_PORT"
    echo "Keeping the installed port: :$PORT. Pass PORT= to change."
  fi
  if [ -z "$_ENV_HF_CACHE" ] && [ -n "${CUR_HF:-}" ] && [ "$CUR_HF" != "$HF_CACHE" ]; then
    HF_CACHE="$CUR_HF"
    echo "Keeping the installed HF cache location: $HF_CACHE. Pass HF_CACHE= to change."
  fi
  if [ "$INSTALLED_CHOICE" = "27b" ] && [ "$MODEL_CHOICE" != "flash" ] \
     && [ -z "$_ENV_CONTEXT_MODE" ] && grep -q -- '--context-length 1010000' "$SGL_UNIT_PATH"; then
    CONTEXT_MODE=1m
    echo "Keeping the installed context mode: 1m. Pass CONTEXT_MODE=native to change."
  fi
fi
if [ -z "$_ENV_PROXY_PORT" ] && [ -r "/etc/systemd/system/qwen38-keepalive.service" ]; then
  CUR_PROXY="$(grep -oE 'keepalive-proxy\.py [0-9]+' /etc/systemd/system/qwen38-keepalive.service | head -1 | tr -dc '0-9' || true)"
  [ -n "${CUR_PROXY:-}" ] && PROXY_PORT="$CUR_PROXY"
fi
PROXY_PORT="${PROXY_PORT:-$((PORT+1))}"

if [ "$NO_SERVICE" -eq 1 ] && [ "$NO_START" -eq 1 ]; then
  printf -- '--no-start controls the systemd service; with --no-service there is no service (drop one flag)\n' >&2; exit 1
fi
if [ "$NO_SERVICE" -eq 1 ] && [ "$CONTEXT_MODE" = "1m" ]; then
  printf -- 'CONTEXT_MODE=1m needs the systemd path (keepalive proxy service); ./run.sh serves the native config only.\nEither drop --no-service, or pass CONTEXT_MODE=native explicitly.\n' >&2; exit 1
fi
if [ "$NO_SERVICE" -eq 1 ] && [ "$MODEL_CHOICE" = "flash" ]; then
  printf -- 'MODEL_CHOICE=flash is service-only in this release (the vLLM path was validated as a systemd unit).\nDrop --no-service, or install one of the 27B targets for the foreground ./run.sh path.\n' >&2; exit 1
fi

step() { printf '\n\033[1;36m── %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

step "1/9 Preflight checks"
[ "$(uname -m)" = "aarch64" ] || die "This setup targets GB10 (aarch64). Detected: $(uname -m)."
command -v nvidia-smi >/dev/null || die "nvidia-smi not found. Is the NVIDIA driver stack installed? (stock on DGX OS)"
GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
echo "GPU: $GPU_NAME"
case "$GPU_NAME" in *GB10*) ;; *) echo "WARNING: expected GB10, found '$GPU_NAME'. Continuing, but this config was only validated on GB10 (memory sizing may not fit other GPUs)." ;; esac
command -v docker >/dev/null || die "docker not found. Install Docker + NVIDIA Container Toolkit (stock on DGX OS)."
command -v python3 >/dev/null || die "python3 not found on the host (needed for the template patcher; stock on DGX OS)."
docker info >/dev/null 2>&1 || die "Cannot talk to the docker daemon. Fix: sudo usermod -aG docker \$USER && re-login (or run with a user in the docker group)."
# /proc/meminfo, not `free`: free(1) localizes its row labels (issue #3)
TOTAL_GB=$(awk '/^MemTotal/{print int($2/1048576)}' /proc/meminfo)
[ "$TOTAL_GB" -ge 110 ] || die "This config needs a ~121 GB unified-memory machine; found ${TOTAL_GB} GB."
FREE_DISK_GB=$(df -BG --output=avail "$HOME" | tail -1 | tr -dc '0-9')
# Fresh installs need ~45 GB for the 27B stack (checkpoints + caches) and
# ~145 GB for Flash-Next (the NVFP4 checkpoint alone is ~136 GB and doubles as
# the mmap-served PLE table). Upgrades with the big checkpoint already cached
# only need working room.
NEED_GB=45; DOCKER_NEED_GB=45; IMG_LABEL="39 GB Docker image"
if [ "$MODEL_CHOICE" = "flash" ]; then
  NEED_GB=195; DOCKER_NEED_GB=35; IMG_LABEL="30 GB Docker image"
fi
ls -d "$HF_CACHE/hub/models--${MODEL_REPO//\//--}/snapshots/"*/ >/dev/null 2>&1 && NEED_GB=10
if [ "$MODEL_CHOICE" = "flash" ] && ! ls "$PLE_DIR"/ple_table_*.bin >/dev/null 2>&1; then
  # The ~48 GiB mmap backing file is written at first boot.
  NEED_GB=$((NEED_GB + 50))
fi
[ "$FREE_DISK_GB" -ge "$NEED_GB" ] || die "Need ~${NEED_GB} GB free under \$HOME for the checkpoints and caches; found ${FREE_DISK_GB} GB. Free some space or set HF_CACHE to another disk."
DOCKER_ROOT=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)
DOCKER_FREE_GB=$(df -BG --output=avail "$DOCKER_ROOT" 2>/dev/null | tail -1 | tr -dc '0-9')
[ "${DOCKER_FREE_GB:-0}" -ge "$DOCKER_NEED_GB" ] || die "Need ~${DOCKER_NEED_GB} GB free on $DOCKER_ROOT for the $IMG_LABEL; found ${DOCKER_FREE_GB:-?} GB (docker images live there, not under \$HOME)."
if ss -tlnH 2>/dev/null | awk '{print $4}' | grep -q ":$PORT\$"; then
  # The port may be held by either of OUR engines: same-engine reinstall
  # (converge) or a cross-engine switch (the old engine is stopped at step 9).
  if docker inspect qwen38-sglang --format '{{join .Args " "}}' 2>/dev/null | grep -qE -- "--port ${PORT}(\s|$)"; then
    echo "Note: qwen38-sglang is already running on :$PORT, installing over it ($([ "$LANE" = flash ] && echo 'lane switch at the final step' || echo 'converging config'))."
  elif docker inspect qwen38-flash --format '{{join .Args " "}}' 2>/dev/null | grep -qE -- "--port ${PORT}(\s|$)" \
       || docker inspect qwen38-flash --format '{{json .HostConfig.PortBindings}}' 2>/dev/null | grep -q "\"${PORT}\""; then
    echo "Note: qwen38-flash is already running on :$PORT, installing over it ($([ "$LANE" = 27b ] && echo 'lane switch at the final step' || echo 'converging config'))."
  else
    die "Port $PORT is already in use by another program (see: ss -tlnp | grep :$PORT). Free it, or install with PORT=<other> ./install.sh"
  fi
fi
if [ "$NO_SERVICE" -eq 0 ] && ss -tlnH 2>/dev/null | awk '{print $4}' | grep -q ":$PROXY_PORT\$"; then
  if systemctl is-active --quiet qwen38-keepalive 2>/dev/null; then
    echo "Note: the keepalive proxy is already running on :$PROXY_PORT, re-installing over it."
  else
    die "Port $PROXY_PORT (keepalive proxy) is already in use by another program. Free it, or install with PROXY_PORT=<other>"
  fi
fi
echo "OK (aarch64, ${TOTAL_GB} GB RAM, ${FREE_DISK_GB} GB free)"

if [ "$LANE" = "flash" ]; then
  step "2/9 Pulling the official SGLang Flash-Next image (~30 GB, one-time, resumable)"
  docker pull "$FLASH_IMAGE" || die "docker pull failed. Causes: no internet, Docker Hub rate limit (retry in a few minutes or 'docker login'), or the pinned digest was removed upstream: try FLASH_IMAGE=lmsysorg/sglang:qwen38flashnext ./install.sh"
  PULLED_IMAGE="$FLASH_IMAGE"
else
  step "2/9 Pulling the SGLang image (~39 GB, one-time, resumable)"
  docker pull "$IMAGE" || die "docker pull failed. Causes: no internet, Docker Hub rate limit (retry in a few minutes or 'docker login'), or the pinned digest was removed upstream: try IMAGE=lmsysorg/sglang:qwen38-27b ./install.sh"
  PULLED_IMAGE="$IMAGE"
fi

step "3/9 Verifying the container can see the GPU"
# --entrypoint: the vLLM image's entrypoint is `vllm serve`, so a bare command
# would be parsed as serve arguments instead of running nvidia-smi.
GPU_SEEN="$(docker run --rm --gpus all --entrypoint nvidia-smi "$PULLED_IMAGE" -L 2>/dev/null | grep -m1 '^GPU' || true)"
[ -n "$GPU_SEEN" ] \
  || die "'docker run --gpus all' cannot see the GPU (no GPU line from nvidia-smi -L in the container). The NVIDIA Container Toolkit is missing or unconfigured. Fix: sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
echo "OK, container sees: $GPU_SEEN"

if [ "$LANE" = "flash" ]; then
  step "4/9 Downloading the Flash-Next checkpoint (~136 GB, one-time, reuses/resumes any local copy)"
  echo "This is the big one: at 100 MB/s it takes ~25 min. Interrupting is safe, re-running resumes."
else
  step "4/9 Downloading checkpoints (~28 GB, one-time, reuses/resumes any local copy)"
fi
mkdir -p "$HF_CACHE" "$CONFIG_DIR/sglang-cache"
# A kept custom model is already serving from this cache: skip its download.
DL_MODEL_REPO="$MODEL_REPO"
[ "$KEEP_MODEL_VERBATIM" -eq 1 ] && DL_MODEL_REPO=""
# Flash has no separate draft checkpoints: its MTP head ships inside the
# target checkpoint itself. Empty repo vars are skipped by the downloader.
DL_DRAFT_REPO="$DRAFT_REPO"; DL_DRAFT2_REPO="$DRAFT2_REPO"
if [ "$LANE" = "flash" ]; then DL_DRAFT_REPO=""; DL_DRAFT2_REPO=""; fi
# Unauthenticated downloads get throttled by the Hub (measured: a fresh-cache
# pull stalled at 3.3 GB). A token in $HF_CACHE/token is picked up through the
# mount; HF_TOKEN in the environment is passed through as well.
DL_TOKEN_ARGS=()
[ -n "${HF_TOKEN:-}" ] && DL_TOKEN_ARGS=(-e HF_TOKEN="$HF_TOKEN")
# HF_HUB_DISABLE_XET: the hub library's Xet transfer backend stalled silently
# during the release campaign (ESTAB socket, zero bytes, forever; 0-8 MB/s
# when moving at all) while the classic CDN path measured 89 MB/s on the same
# box, same second. HF_HUB_DOWNLOAD_TIMEOUT turns any remaining silent stall
# into a ReadTimeout that the retry loop below resumes from.
docker run --rm -i --network host --user "$(id -u):$(id -g)" \
  --entrypoint python3 \
  -e HF_HOME=/hf -e HF_HUB_DOWNLOAD_TIMEOUT=30 -e HF_HUB_DISABLE_XET=1 \
  -e MODEL_REPO="$DL_MODEL_REPO" -e MODEL_REV="$MODEL_REV" \
  -e DRAFT_REPO="$DL_DRAFT_REPO" -e DRAFT_REV="$DRAFT_REV" \
  -e DRAFT2_REPO="$DL_DRAFT2_REPO" -e DRAFT2_REV="$DRAFT2_REV" \
  "${DL_TOKEN_ARGS[@]}" \
  -v "$HF_CACHE":/hf \
  "$PULLED_IMAGE" - <<'PYEOF' || die "Checkpoint download failed. Causes: no internet, HuggingFace throttling of unauthenticated downloads (set HF_TOKEN=<your token>, or re-run: downloads resume), a pinned revision removed (try MODEL_REV=main DRAFT_REV=main ./install.sh), or a permission error: if your $HF_CACHE contains root-owned files from other tools, fix with: sudo chown -R \$(id -u):\$(id -g) $HF_CACHE"
import os
import time
from huggingface_hub import snapshot_download
for repo, rev in ((os.environ["MODEL_REPO"], os.environ["MODEL_REV"]),
                  (os.environ["DRAFT_REPO"], os.environ["DRAFT_REV"]),
                  (os.environ["DRAFT2_REPO"], os.environ["DRAFT2_REV"])):
    if not repo:  # kept custom model: already in cache, nothing to download
        continue
    print(f"── {repo} @ {rev}", flush=True)
    for attempt in range(1, 6):  # a resumed attempt reuses every finished byte
        try:
            path = snapshot_download(repo, revision=rev)
            break
        except Exception as e:
            if attempt == 5:
                raise
            print(f"download interrupted ({type(e).__name__}), resuming ({attempt}/5)...", flush=True)
            time.sleep(10)
    # A pinned-sha download writes no refs/main; serving later with
    # HF_HUB_OFFLINE=1 (the 1m unit) resolves "main" through that file and
    # would fail on a fresh machine. Write it once, never overwrite.
    sha = os.path.basename(path.rstrip("/"))
    ref = os.path.join(os.path.dirname(os.path.dirname(path.rstrip("/"))), "refs", "main")
    if len(sha) == 40 and not os.path.exists(ref):
        os.makedirs(os.path.dirname(ref), exist_ok=True)
        with open(ref, "w") as f:
            f.write(sha)
print("checkpoints ready", flush=True)
PYEOF

if [ "$LANE" = "flash" ]; then
  step "5/9 Building the Flash-Next serving image (pinned base + 2 verified files + gate checks, offline, ~2 min)"
  BASE_IMAGE="$FLASH_IMAGE" TAG="$FLASH_SERVE_IMAGE" "$REPO_DIR/flash-sglang/build-image.sh" \
    || die "Flash overlay image build failed: see flash-sglang/ATTRIBUTION.md; the checksums and in-image checks run before tagging, so a failure means a corrupted checkout (git status) or an upstream image layout change."
else
  step "5/9 Building the DFlash2 serving image (pinned base + 5 verified files, offline, ~1 min)"
  BASE_IMAGE="$IMAGE" TAG="$SERVE_IMAGE" "$REPO_DIR/dflash2/build-image.sh" \
    || die "DFlash2 image build failed: see dflash2/ATTRIBUTION.md; the checksums are verified before building, so a mismatch means a corrupted checkout (git status)."
fi

step "6/9 API key + patched chat template"
if [ ! -s "$CONFIG_DIR/api-key" ]; then
  head -c 24 /dev/urandom | base64 | tr -d '/+=' > "$CONFIG_DIR/api-key"
  chmod 600 "$CONFIG_DIR/api-key"
  echo "API key generated at $CONFIG_DIR/api-key"
else
  echo "API key already present, keeping it"
fi
KEY="$(cat "$CONFIG_DIR/api-key")"   # used by the step-9 smoke test
# One patched template per engine file name: the served template always follows
# the served model (both fixes: reasoning_effort normalization + mid-conversation
# system messages as <system-reminder> blocks; see patch-template.py).
TEMPLATE_OUT="$CONFIG_DIR/chat-template-sglang.jinja"
[ "$LANE" = "flash" ] && TEMPLATE_OUT="$CONFIG_DIR/chat-template-flashnext.jinja"
if [ "$KEEP_MODEL_VERBATIM" -eq 1 ]; then
  [ -s "$TEMPLATE_OUT" ] \
    || die "custom model kept, but no patched template at $TEMPLATE_OUT. Pass MODEL_CHOICE=stock or MODEL_CHOICE=uncensored to regenerate it."
  echo "custom target model kept: existing patched template left untouched"
else
  python3 "$REPO_DIR/patch-template.py" "$HF_CACHE" "$TEMPLATE_OUT" "$MODEL_REV" "$MODEL_REPO" \
    || die "Template patch failed (see message above). If the upstream template changed, please open an issue on this repo."
fi
if [ "$CONTEXT_MODE" = "1m" ]; then
  # Both configs must carry the YaRN patch (target AND draft, or the draft
  # crashes at load). Idempotent; originals backed up as config.json.pre-yarn.
  # A kept custom model has no known pin: its newest cached snapshot is patched.
  if [ "$KEEP_MODEL_VERBATIM" -eq 1 ]; then
    python3 "$REPO_DIR/patch-yarn.py" "$HF_CACHE" "$MODEL_REPO" || die "YaRN patch failed on the target model"
  else
    python3 "$REPO_DIR/patch-yarn.py" "$HF_CACHE" "$MODEL_REPO" "$MODEL_REV" || die "YaRN patch failed on the target model"
  fi
  python3 "$REPO_DIR/patch-yarn.py" "$HF_CACHE" "$DRAFT2_REPO" "$DRAFT2_REV" || die "YaRN patch failed on the DFlash2 draft"
fi

step "7/9 opencode provider config + oc launcher"
# A complete, ready-to-use opencode config (https://opencode.ai). The limits
# satisfy the serving window with margin in BOTH modes, including when
# opencode's hidden 32000 output cap is lifted by the oc launcher below:
#   native: 194048 + 64000 = 258048 <= 262144 - 4096
#   1m:     700000 (compaction at 680000) + 200000 = 880000 <= worst measured
#           KV pool at mem-fraction 0.70 (boot lottery floor: 917877 measured)
# Service installs point agent clients at the keepalive proxy (step 8): SGLang
# buffers tool-call arguments at any context length and agent CLIs abort
# silent streams. --no-service has no proxy: direct server port for ./run.sh.
# The key is referenced via {file:...}: no secret in the file.
# opencode limits per context mode
if [ "${LANE:-27b}" = "flash" ]; then
  OC_CTX=226000; OC_OUT=32000;  OC_LABEL="local"   # 226000+32000 = 258000 <= 262144-4096
elif [ "$CONTEXT_MODE" = "1m" ]; then
  OC_CTX=700000; OC_OUT=200000; OC_LABEL="local, 1M"
else
  OC_CTX=194048; OC_OUT=64000;  OC_LABEL="local"
fi
OC_PORT="$PROXY_PORT"
[ "${NO_SERVICE:-0}" -eq 1 ] && OC_PORT="$PORT"
# end oc mode
# A provider appears in the picker only when its engine is installed on this
# box (nobody should be offered a model that nothing serves); the model being
# installed right now always appears. The default model is the one being
# installed. json.dump writes the file: no hand-escaping.
OC_27B=0; OC_FLASH=0
{ [ "$LANE" = "27b" ] || [ -f "$SGL_UNIT_PATH" ]; } && OC_27B=1
{ [ "$LANE" = "flash" ] || [ -f "$FLASH_UNIT_PATH" ]; } && OC_FLASH=1
# The other engine's limits, for when both providers are present: the 27B block
# keeps its context-mode limits, flash always serves its native window.
OC_LANE="$LANE" OC_27B="$OC_27B" OC_FLASH="$OC_FLASH" OC_PORT="$OC_PORT" \
OC_CTX="$OC_CTX" OC_OUT="$OC_OUT" OC_LABEL="$OC_LABEL" OC_CONTEXT_MODE="$CONTEXT_MODE" \
OC_CONFIG_DIR="$CONFIG_DIR" python3 - <<'PYEOF' || die "could not write the opencode provider config"
import json
import os

cfg_dir = os.environ["OC_CONFIG_DIR"]
lane = os.environ["OC_LANE"]
variants = {lvl: {"chat_template_kwargs": {"reasoning_effort": lvl}}
            for lvl in ("low", "medium", "xhigh")}
key_ref = f"{{file:{cfg_dir}/api-key}}"
base_url = f"http://127.0.0.1:{os.environ['OC_PORT']}/v1"

def prov(name, model_id, model_name, ctx, out):
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": name,
        "options": {"baseURL": base_url, "apiKey": key_ref},
        "models": {model_id: {
            "name": model_name,
            "limit": {"context": ctx, "input": ctx, "output": out},
            "variants": variants,
            "attachment": True,
            "modalities": {"input": ["text", "image"], "output": ["text"]},
        }},
    }

providers = {}
if os.environ["OC_27B"] == "1":
    if lane == "27b":
        ctx, out, label = int(os.environ["OC_CTX"]), int(os.environ["OC_OUT"]), os.environ["OC_LABEL"]
    else:  # flash install on a box that also has the 27B unit: keep its own limits
        one_m = os.environ["OC_CONTEXT_MODE"] == "1m"
        ctx, out, label = (700000, 200000, "local, 1M") if one_m else (194048, 64000, "local")
    providers["qwen38"] = prov("Qwen3.8-27B (DGX Spark)", "qwen3.8-27b",
                               f"Qwen3.8-27B NVFP4+DFlash2 ({label})", ctx, out)
if os.environ["OC_FLASH"] == "1":
    providers["flashnext"] = prov("Qwen3.8-Flash-Next (DGX Spark)", "qwen3.8-flash-next",
                                  "Qwen3.8-Flash-Next NVFP4+MTP (local, 262K)", 226000, 32000)

default = "flashnext/qwen3.8-flash-next" if lane == "flash" else "qwen38/qwen3.8-27b"
doc = {"$schema": "https://opencode.ai/config.json", "provider": providers,
       "model": default, "small_model": default}
with open(f"{cfg_dir}/opencode.json", "w") as f:
    json.dump(doc, f, indent=2)
    f.write("\n")
print(f"wrote {cfg_dir}/opencode.json (default {default}, "
      f"providers: {', '.join(providers) or 'none'})")
PYEOF
echo "opencode limits: context $OC_CTX, output $OC_OUT, port $OC_PORT"
echo "  no opencode config yet:  mkdir -p ~/.config/opencode && cp $CONFIG_DIR/opencode.json ~/.config/opencode/opencode.json"
echo "  existing config:         merge the \"qwen38\" provider block into it (README, \"opencode integration\")"
# oc: launcher that lifts opencode's hidden 32000 max_tokens cap to the
# declared output limit (without it, long thinking is cut at 32000 and the
# turn ends silently). Never clobbers a foreign oc binary (e.g. OpenShift).
OC_BIN="$HOME/.local/bin/oc"
OC_EXISTING="$(command -v oc || true)"
if [ -n "$OC_EXISTING" ] && [ "$OC_EXISTING" != "$OC_BIN" ] && ! grep -q 'dgx-spark-qwen38' "$OC_EXISTING" 2>/dev/null; then
  echo "NOTE: an unrelated 'oc' command exists at $OC_EXISTING; not installing the launcher."
  echo "      Launch opencode with:  OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=$OC_OUT opencode --yolo"
else
  mkdir -p "$HOME/.local/bin"
  cat > "$OC_BIN" <<OCWRAP
#!/bin/bash
# oc launcher installed by dgx-spark-qwen38: opencode wired to the local server.
# Lifts opencode's hidden 32000 max_tokens cap to the declared output limit;
# without this, long thinking phases are cut at 32000 and the turn ends silently.
# --yolo auto-approves permissions (the reference box runs this way; remove it
# below if you prefer per-action prompts).
export OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=$OC_OUT
OPENCODE_BIN="\$(command -v opencode || true)"
[ -n "\$OPENCODE_BIN" ] || OPENCODE_BIN="\$HOME/.opencode/bin/opencode"
# --yolo goes LAST: opencode's parser rejects global flags before a
# subcommand (opencode --yolo run ... prints the help instead of running)
exec "\$OPENCODE_BIN" "\$@" --yolo
OCWRAP
  chmod +x "$OC_BIN"
  echo "installed the oc launcher at $OC_BIN (output cap lifted to $OC_OUT)"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "NOTE: $HOME/.local/bin is not in your PATH; add it or call $OC_BIN directly." ;;
  esac
fi

if [ "$NO_SERVICE" -eq 1 ]; then
  printf '\n\033[1;32m✅ Prepared (no systemd, nothing needed sudo).\033[0m\n'
  echo "  Run in the foreground: ./run.sh     (Ctrl+C stops it; first boot ≈ 9 min)"
  echo "  Everything it uses lives in $CONFIG_DIR and $HF_CACHE: delete those to remove."
  exit 0
fi

step "8/9 Installing the systemd service (sudo needed)"
UNIT_PATH="/etc/systemd/system/$UNIT_NAME"
if [ -f "$UNIT_PATH" ]; then
  # Safety net for hand-tuned units: the previous unit stays recoverable.
  sudo cp "$UNIT_PATH" "$CONFIG_DIR/$UNIT_NAME.bak-preupdate"
  sudo chown "$(id -u):$(id -g)" "$CONFIG_DIR/$UNIT_NAME.bak-preupdate"
  echo "previous unit backed up at $CONFIG_DIR/$UNIT_NAME.bak-preupdate"
fi
if [ "$LANE" = "flash" ]; then
  UNIT_TPL="$REPO_DIR/qwen38-flash.service.template"
  SERVE_IMAGE_FINAL="$FLASH_SERVE_IMAGE"
else
  UNIT_TPL="$REPO_DIR/qwen38-sglang.service.template"
  [ "$CONTEXT_MODE" = "1m" ] && UNIT_TPL="$REPO_DIR/qwen38-sglang-1m.service.template"
  SERVE_IMAGE_FINAL="$SERVE_IMAGE"
fi
# Serve-time revision lock: the pinned sha is passed to the server itself, so
# an upstream push to the checkpoint repo can never change what is served
# (download-time pinning alone leaves "main" resolvable at boot). A kept
# custom model reuses its unit's existing --revision, or none.
if [ "$KEEP_MODEL_VERBATIM" -eq 1 ]; then
  MODEL_REV_ARGS=""
  [ -n "${CUR_REV:-}" ] && MODEL_REV_ARGS="--revision $CUR_REV"
else
  MODEL_REV_ARGS="--revision $MODEL_REV"
fi
render_tpl() {  # $1 template file; substituted result on stdout
  sed -e "s|__HOME__|$HOME|g" \
      -e "s|__USER__|$(id -un)|g" \
      -e "s|__GROUP__|$(id -gn)|g" \
      -e "s|__PORT__|$PORT|g" \
      -e "s|__IMAGE__|$SERVE_IMAGE_FINAL|g" \
      -e "s|__HF_CACHE__|$HF_CACHE|g" \
      -e "s|__DRAFT2_REV__|$DRAFT2_REV|g" \
      -e "s|__MODEL_REV_ARGS__|$MODEL_REV_ARGS|g" \
      -e "s|__MODEL__|$MODEL_REPO|g" \
      -e "s|__PLE_DIR__|$PLE_DIR|g" \
      "$1"
}
if [ "$LANE" = "flash" ]; then
  mkdir -p "$PLE_DIR"
  # The docker-run command lives in a plain launch script, not in ExecStart:
  # systemd applies its own C-style unescaping before bash would, and the JSON
  # arguments (--speculative-config, splitting_ops) do not survive two rounds.
  TMP_LAUNCH="$(mktemp)"
  render_tpl "$REPO_DIR/qwen38-flash-launch.sh.template" > "$TMP_LAUNCH"
  bash -n "$TMP_LAUNCH" || die "rendered flash launch script does not parse (report this repo bug)"
  install -m 755 "$TMP_LAUNCH" "$CONFIG_DIR/launch-flash.sh"; rm -f "$TMP_LAUNCH"
  echo "wrote $CONFIG_DIR/launch-flash.sh"
fi
TMP_UNIT="$(mktemp)"
render_tpl "$UNIT_TPL" > "$TMP_UNIT"
sudo install -m 644 "$TMP_UNIT" "/etc/systemd/system/$UNIT_NAME"; rm -f "$TMP_UNIT"
# Cross-engine switch: exactly one serving unit may start at boot. The other
# engine's unit (if present) is disabled now and stopped at step 9, right
# before this one starts; its unit file is kept for a fast switch back.
OTHER_UNIT=""
if [ "$LANE" = "flash" ]; then
  [ -f "$SGL_UNIT_PATH" ] && OTHER_UNIT="qwen38-sglang.service"
else
  [ -f "$FLASH_UNIT_PATH" ] && OTHER_UNIT="qwen38-flash.service"
fi
if [ -n "$OTHER_UNIT" ] && systemctl is-enabled --quiet "$OTHER_UNIT" 2>/dev/null; then
  echo "disabling the other engine's unit at boot: $OTHER_UNIT (file kept, switch back anytime with ./switch-model.sh)"
  sudo systemctl disable "$OTHER_UNIT"
fi
KEEPALIVE_UNIT="qwen38-keepalive.service"
# Every service install gets the keepalive proxy: SGLang buffers tool-call
# arguments while they stream (127 s of measured silence on a 400-line write,
# at native context) and agent CLIs abort a silent stream (~140-180 s for
# opencode). It also aborts zombie generations when the client disconnects.
install -m 755 "$REPO_DIR/keepalive-proxy.py" "$CONFIG_DIR/keepalive-proxy.py"
TMP_KA="$(mktemp)"
sed -e "s|__HOME__|$HOME|g" \
    -e "s|__USER__|$(id -un)|g" \
    -e "s|__GROUP__|$(id -gn)|g" \
    -e "s|__PORT__|$PORT|g" \
    -e "s|__PROXY_PORT__|$PROXY_PORT|g" \
    "$REPO_DIR/qwen38-keepalive.service.template" > "$TMP_KA"
sudo install -m 644 "$TMP_KA" "/etc/systemd/system/$KEEPALIVE_UNIT"; rm -f "$TMP_KA"
sudo systemctl enable "$KEEPALIVE_UNIT"
# The Claude Code warmup was removed in v1.3: clean up what earlier versions
# installed (only the warmup drop-in; any other drop-in in the .d dir is kept).
if [ -f "/etc/systemd/system/$UNIT_NAME.d/warmup.conf" ]; then
  echo "removing the deprecated Claude Code warmup drop-in"
  sudo rm -f "/etc/systemd/system/$UNIT_NAME.d/warmup.conf"
  sudo rmdir "/etc/systemd/system/$UNIT_NAME.d" 2>/dev/null || true
fi
rm -f "$CONFIG_DIR/warmup-claude-code.sh"
sudo systemctl daemon-reload
sudo systemctl enable "$UNIT_NAME"

if [ "$NO_START" -eq 1 ]; then
  step "Done (service installed and enabled at boot; start it with: sudo systemctl start $UNIT_NAME)"
  if [ -n "$OTHER_UNIT" ] && systemctl is-active --quiet "$OTHER_UNIT" 2>/dev/null; then
    echo "NOTE: $OTHER_UNIT is still serving; stop it first (one engine at a time):"
    echo "      sudo systemctl stop $OTHER_UNIT && sudo systemctl start $UNIT_NAME"
  fi
  echo "also start the keepalive proxy with: sudo systemctl start $KEEPALIVE_UNIT"
  exit 0
fi

if [ "$LANE" = "flash" ]; then
  step "9/9 Starting (first boot ≈ 15 min: weight load writes the 48 GiB PLE file, then CUDA graph capture; later boots ≈ 10 min)"
else
  step "9/9 Starting (first boot ≈ 9 min: torch.compile + CUDA graph capture; later boots are faster)"
fi
if [ -n "$OTHER_UNIT" ] && systemctl is-active --quiet "$OTHER_UNIT" 2>/dev/null; then
  echo "stopping the other engine first ($OTHER_UNIT): one engine at a time on a GB10"
  sudo systemctl stop "$OTHER_UNIT"
fi
SMOKE_MODEL="qwen3.8-27b"
[ "$LANE" = "flash" ] && SMOKE_MODEL="qwen3.8-flash-next"
sudo systemctl restart "$UNIT_NAME"
for i in $(seq 1 150); do
  if curl -s -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "health OK, running a real generation smoke test..."
    SMOKE="$(curl -s -m 300 "http://127.0.0.1:$PORT/v1/chat/completions" \
      -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
      -d '{"model":"'"$SMOKE_MODEL"'","messages":[{"role":"user","content":"Reply with exactly: READY"}],"max_tokens":600}' \
      | python3 -c 'import json,sys
try:
    m=json.load(sys.stdin)["choices"][0]["message"]
    print("OK" if (m.get("content") or m.get("reasoning_content") or "").strip() else "EMPTY")
except Exception as e:
    print(f"FAIL:{e}")')"
    [ "$SMOKE" = "OK" ] || die "Server is up but the smoke generation failed ($SMOKE). Check: journalctl -u $UNIT_NAME -n 50"
    sudo systemctl restart "$KEEPALIVE_UNIT"
    PROXY_OK=0
    for _ in 1 2 3 4 5; do
      curl -s -m 5 "http://127.0.0.1:$PROXY_PORT/health" >/dev/null 2>&1 && { PROXY_OK=1; break; }
      sleep 2
    done
    [ "$PROXY_OK" = 1 ] || die "the keepalive proxy did not come up on :$PROXY_PORT. Check: journalctl -u $KEEPALIVE_UNIT -n 30"
    echo "keepalive proxy OK on :$PROXY_PORT (agent clients use this port)"
    # Superseded artifacts from earlier versions of THIS repo, if any: detect
    # and say how to reclaim them; never delete data on the operator's behalf.
    LEFTOVER_NOTES=""
    if [ "$LANE" = "flash" ]; then
      for ref in "vllm/vllm-openai:qwen38-flash-next" "vllm/vllm-openai@sha256:fc120ece0a388cc0aa1caad4a9f1cd92113484ab7ec2fd0efadd62585be05bf8" "qwen38-flash:v1.4"; do
        docker image inspect "$ref" >/dev/null 2>&1 && LEFTOVER_NOTES="${LEFTOVER_NOTES}      docker rmi '$ref'\n"
      done
    fi
    while IFS= read -r tagref; do
      [ -n "$tagref" ] && [ "$tagref" != "$SERVE_IMAGE" ] && [ "$tagref" != "$FLASH_SERVE_IMAGE" ] \
        && case "$LEFTOVER_NOTES" in *"'$tagref'"*) ;; *) LEFTOVER_NOTES="${LEFTOVER_NOTES}      docker rmi '$tagref'\n" ;; esac
    done < <({ docker images --format '{{.Repository}}:{{.Tag}}' qwen38-dflash2 2>/dev/null; docker images --format '{{.Repository}}:{{.Tag}}' qwen38-flash 2>/dev/null; } || true)
    if [ -n "$LEFTOVER_NOTES" ]; then
      echo "  Note: earlier versions of this repo left superseded images; reclaim when you like:"
      printf '%b' "$LEFTOVER_NOTES"
      echo "      (full inventory anytime: ./uninstall.sh --list)"
    fi
    printf '\n\033[1;32m✅ Installed, verified, and enabled at boot.\033[0m\n'
    echo "  OpenAI     : http://<host>:$PORT/v1/chat/completions"
    echo "  Anthropic  : http://<host>:$PORT/v1/messages   (Bearer auth only)"
    echo "  Agent CLIs : http://<host>:$PROXY_PORT (keepalive proxy, use THIS for opencode)"
    echo "  API key    : $CONFIG_DIR/api-key"
    echo "  opencode   : provider config ready at $CONFIG_DIR/opencode.json (README, \"opencode integration\")"
    [ "$LANE" = "27b" ] && echo "  Benchmark  : ./bench.sh"
    exit 0
  fi
  ST="$(systemctl is-active "$UNIT_NAME" || true)"
  [ "$ST" = "failed" ] && { journalctl -u "$UNIT_NAME" --no-pager | tail -25; die "Service failed during startup, logs above. Common cause: another process eating GPU/unified memory (this config needs the machine to itself)."; }
  [ $((i % 15)) -eq 0 ] && echo "  still loading... ($((i*8))s; first boot compiles kernels, be patient)"
  sleep 8
done
die "Server did not come up within 20 min. Watch: journalctl -u $UNIT_NAME -f"
