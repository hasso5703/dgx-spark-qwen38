#!/usr/bin/env bash
# Removes the qwen38 serving services (27B SGLang and/or Flash-Next) and their config,
# and knows every artifact ANY past version of this repo may have left on the box
# (v1.0 through v1.5): units, drop-ins, backups, local and base docker images
# (tag or digest form), checkpoints, the PLE mmap file, the oc launcher.
#
#   ./uninstall.sh --list    inventory only: show what is present and how big it
#                            is, decide for yourself; changes NOTHING, needs no sudo
#   ./uninstall.sh           remove services + (after a prompt) the config;
#                            data (images, checkpoints, PLE file) is never deleted,
#                            the exact reclaim command for each present item is printed
#   ./uninstall.sh --yes     same, config removed without asking
set -euo pipefail
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
CONFIG_DIR="$HOME/.config/qwen38"

LIST_ONLY=0
PURGE_CONFIG=0
for arg in "${@:-}"; do
  case "$arg" in
    --list|-l) LIST_ONLY=1 ;;
    --yes|-y) PURGE_CONFIG=1 ;;
    "") ;;
    -h|--help) echo "Usage: ./uninstall.sh [--list] [--yes]   (--list = inventory only; --yes also deletes ~/.config/qwen38 without asking)"; exit 0 ;;
    *) echo "Unknown flag: $arg (see --help)" >&2; exit 1 ;;
  esac
done

# ── Inventory: every name this repo has ever created, shown only if present ──
# Local serving images built by install.sh across versions (any tag: v1.2,
# v1.2.2, v1.4, v1.5, ...), plus the pinned base images, matched by tag AND by
# digest: a digest pull leaves no tag behind.
LOCAL_IMAGE_REPOS="qwen38-dflash2 qwen38-flash"
BASE_IMAGES="lmsysorg/sglang:qwen38-27b lmsysorg/sglang@sha256:febfb971c7352570fc445c466ebd6ffc9d896024958e544a60f2137fd85856b1 lmsysorg/sglang:qwen38flashnext lmsysorg/sglang@sha256:12d3392bdc8be8d35e9a95f191df6aef99c5114bdbefd41bfdc7e760e6d25ec1 vllm/vllm-openai:qwen38-flash-next vllm/vllm-openai@sha256:fc120ece0a388cc0aa1caad4a9f1cd92113484ab7ec2fd0efadd62585be05bf8"
HF_REPOS="RadixArk/Qwen3.8-27B-NVFP4 edp1096/Huihui-RadixArk-Qwen3.8-27B-abliterated-NVFP4 RadixArk/Qwen3.8-27B-DSpark z-lab/Qwen3.8-27B-DFlash2 RadixArk/Qwen3.8-Flash-Next-NVFP4"

FOUND_IMAGES=()   # "ref|size", deduplicated by image ID (a tag and its digest are one image)
inventory_images() {
  command -v docker >/dev/null 2>&1 || return 0
  docker info >/dev/null 2>&1 || return 0
  local seen_ids=" " ref id size
  for repo in $LOCAL_IMAGE_REPOS; do
    while IFS='|' read -r ref id size; do
      [ -n "$ref" ] || continue
      case "$seen_ids" in *" $id "*) continue ;; esac
      seen_ids="$seen_ids$id "
      FOUND_IMAGES+=("$ref|$size")
    done < <(docker images --format '{{.Repository}}:{{.Tag}}|{{.ID}}|{{.Size}}' "$repo" 2>/dev/null || true)
  done
  for ref in $BASE_IMAGES; do
    if docker image inspect "$ref" >/dev/null 2>&1; then
      id="$(docker image inspect "$ref" --format '{{.Id}}' | cut -c8-19)"
      case "$seen_ids" in *" $id "*) continue ;; esac
      seen_ids="$seen_ids$id "
      size="$(docker image inspect "$ref" --format '{{.Size}}' | awk '{printf "%.1fGB", $1/1e9}')"
      FOUND_IMAGES+=("$ref|$size")
    fi
  done
}

dir_size() { du -sh "$1" 2>/dev/null | cut -f1; }

echo "── Inventory (everything any version of this repo may have left here) ──"
for u in qwen38-sglang.service qwen38-flash.service qwen38-keepalive.service; do
  if [ -f "/etc/systemd/system/$u" ]; then
    STATE="$(systemctl is-enabled "$u" 2>/dev/null || true)/$(systemctl is-active "$u" 2>/dev/null || true)"
    echo "  unit      /etc/systemd/system/$u ($STATE)"
  fi
done
[ -d /etc/systemd/system/qwen38-sglang.service.d ] && echo "  drop-ins  /etc/systemd/system/qwen38-sglang.service.d (pre-v1.3 warmup lived here)"
for f in "$CONFIG_DIR"/*.bak-preupdate; do
  [ -f "$f" ] && echo "  backup    $f (pre-update unit backup)"
done
if [ -d "$CONFIG_DIR" ]; then
  echo "  config    $CONFIG_DIR ($(dir_size "$CONFIG_DIR")): api-key, patched templates, opencode.json, launch script, compile cache"
  [ -f "$CONFIG_DIR/claude-code.env" ] && echo "  legacy    $CONFIG_DIR/claude-code.env (pre-v1.3 client config, unmaintained)"
fi
if grep -q 'dgx-spark-qwen38' "$HOME/.local/bin/oc" 2>/dev/null; then
  echo "  launcher  $HOME/.local/bin/oc (this repo's opencode launcher)"
fi
inventory_images
for entry in ${FOUND_IMAGES[@]+"${FOUND_IMAGES[@]}"}; do
  echo "  image     ${entry%%|*} (${entry##*|})"
done
for repo in $HF_REPOS; do
  d="$HF_CACHE/hub/models--${repo//\//--}"
  [ -d "$d" ] && echo "  weights   $d ($(dir_size "$d"))"
done
for p in "${PLE_DIR:-}" "$HOME/flashnext-ple"; do
  [ -n "$p" ] && [ -d "$p" ] && { echo "  ple-file  $p ($(dir_size "$p"), flash mmap backing store)"; break; }
done
echo "──"

if [ "$LIST_ONLY" -eq 1 ]; then
  echo "Inventory only: nothing was changed. Remove services+config with ./uninstall.sh;"
  echo "data (images, weights, PLE file) always stays until you run the printed commands."
  exit 0
fi

sudo systemctl disable --now qwen38-sglang.service 2>/dev/null || true
sudo systemctl disable --now qwen38-flash.service 2>/dev/null || true
sudo systemctl disable --now qwen38-keepalive.service 2>/dev/null || true
docker rm -f qwen38-sglang qwen38-sglang-run qwen38-flash 2>/dev/null || true
sudo rm -f /etc/systemd/system/qwen38-sglang.service /etc/systemd/system/qwen38-flash.service /etc/systemd/system/qwen38-keepalive.service
sudo rm -rf /etc/systemd/system/qwen38-sglang.service.d
sudo systemctl daemon-reload
# The oc launcher, only if it is ours (never a foreign oc binary)
if grep -q 'dgx-spark-qwen38' "$HOME/.local/bin/oc" 2>/dev/null; then
  rm -f "$HOME/.local/bin/oc"
fi
echo "services removed."

if [ "$PURGE_CONFIG" -eq 0 ] && [ -t 0 ]; then
  read -r -p "Also delete ~/.config/qwen38 (API key, patched templates, compile cache)? [y/N] " ans
  { [ "${ans:-n}" = "y" ] || [ "${ans:-n}" = "Y" ]; } && PURGE_CONFIG=1 || true
fi
if [ "$PURGE_CONFIG" -eq 1 ]; then
  rm -rf "$CONFIG_DIR"
  echo "config removed."
else
  echo "config kept at ~/.config/qwen38 (delete manually or re-run with --yes)."
fi

echo
echo "To also reclaim disk space, run the commands for what the inventory found:"
for entry in ${FOUND_IMAGES[@]+"${FOUND_IMAGES[@]}"}; do
  echo "  docker rmi '${entry%%|*}'    # ${entry##*|}"
done
for repo in $HF_REPOS; do
  d="$HF_CACHE/hub/models--${repo//\//--}"
  [ -d "$d" ] && echo "  rm -rf '$d'    # $(dir_size "$d")"
done
for p in "${PLE_DIR:-}" "$HOME/flashnext-ple"; do
  [ -n "$p" ] && [ -d "$p" ] && { echo "  rm -rf '$p'    # $(dir_size "$p"), flash PLE backing file"; break; }
done
