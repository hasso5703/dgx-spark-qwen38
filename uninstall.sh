#!/usr/bin/env bash
# Removes the qwen38-sglang service and its config.
# Keeps: the docker images (base + locally built qwen38-dflash2) and the downloaded checkpoints in ~/.cache/huggingface
# (delete those manually if you want the ~63 GB back: see bottom).
set -euo pipefail
UNIT_NAME="qwen38-sglang.service"
PURGE_CONFIG=0
for arg in "${@:-}"; do
  case "$arg" in
    --yes|-y) PURGE_CONFIG=1 ;;
    "") ;;
    -h|--help) echo "Usage: ./uninstall.sh [--yes]   (--yes also deletes ~/.config/qwen38 without asking)"; exit 0 ;;
  esac
done

sudo systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
docker rm -f qwen38-sglang qwen38-sglang-run 2>/dev/null || true
sudo rm -f "/etc/systemd/system/$UNIT_NAME"
sudo rm -rf "/etc/systemd/system/$UNIT_NAME.d"
sudo systemctl daemon-reload
echo "service removed."

if [ "$PURGE_CONFIG" -eq 0 ] && [ -t 0 ]; then
  read -r -p "Also delete ~/.config/qwen38 (API key, patched template, compile cache)? [y/N] " ans
  { [ "${ans:-n}" = "y" ] || [ "${ans:-n}" = "Y" ]; } && PURGE_CONFIG=1 || true
fi
if [ "$PURGE_CONFIG" -eq 1 ]; then
  rm -rf "$HOME/.config/qwen38"
  echo "config removed."
else
  echo "config kept at ~/.config/qwen38 (delete manually or re-run with --yes)."
fi

cat <<'EOF'

To also reclaim disk space:
  docker rmi $(docker images -q 'qwen38-dflash2')                            # locally built serving images
  docker rmi lmsysorg/sglang:qwen38-27b                                      # ~39 GB base image
  rm -rf ~/.cache/huggingface/hub/models--RadixArk--Qwen3.8-27B-NVFP4        # ~21 GB
  rm -rf ~/.cache/huggingface/hub/models--z-lab--Qwen3.8-27B-DFlash2         # ~4 GB
  rm -rf ~/.cache/huggingface/hub/models--RadixArk--Qwen3.8-27B-DSpark       # ~3 GB
EOF
