#!/usr/bin/env bash
# Removes the qwen38-sglang service and its config.
# Keeps: the SGLang docker image and the downloaded checkpoints in ~/.cache/huggingface
# (delete those manually if you want the ~63 GB back — see bottom).
set -euo pipefail
UNIT_NAME="qwen38-sglang.service"

sudo systemctl disable --now "$UNIT_NAME" 2>/dev/null || true
docker rm -f qwen38-sglang 2>/dev/null || true
sudo rm -f "/etc/systemd/system/$UNIT_NAME"
sudo rm -rf "/etc/systemd/system/$UNIT_NAME.d"
sudo systemctl daemon-reload
echo "service removed."

read -r -p "Also delete ~/.config/qwen38 (API key, patched template, compile cache)? [y/N] " ans
if [ "${ans:-n}" = "y" ] || [ "${ans:-n}" = "Y" ]; then
  rm -rf "$HOME/.config/qwen38"
  echo "config removed."
fi

cat <<'EOF'

To also reclaim disk space:
  docker rmi lmsysorg/sglang:qwen38-27b                                      # ~39 GB
  rm -rf ~/.cache/huggingface/hub/models--RadixArk--Qwen3.8-27B-NVFP4        # ~21 GB
  rm -rf ~/.cache/huggingface/hub/models--RadixArk--Qwen3.8-27B-DSpark       # ~3 GB
EOF
