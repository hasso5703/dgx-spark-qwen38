#!/usr/bin/env bash
# Surgical target-model switch on a live install: swap between the stock and
# the uncensored (huihui-ai abliterated) checkpoint WITHOUT reinstalling.
#
#   ./switch-model.sh uncensored   # edp1096/Huihui-RadixArk-Qwen3.8-27B-abliterated-NVFP4
#   ./switch-model.sh stock        # RadixArk/Qwen3.8-27B-NVFP4
#
# What it does:
#   1. downloads the checkpoint into $HF_CACHE (resumable, pinned revision);
#   2. applies the 1M YaRN config patch to the target's cached config.json
#      ONLY if the installed unit uses --context-length 1010000;
#   3. rewrites ONLY the --model-path value in /etc/systemd/system/qwen38-sglang.service;
#   4. daemon-reloads.
#
# The DFlash2 drafter is unchanged: spec decoding stays lossless (drafts are
# verified against the target), only the acceptance rate may vary slightly.
# The switch takes effect on the NEXT restart:  sudo systemctl restart qwen38-sglang
# (or at reboot). This script NEVER restarts or stops the service itself.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
die() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

CHOICE="${1:-${MODEL_CHOICE:-stock}}"
case "$CHOICE" in stock|uncensored) ;; *) die "usage: ./switch-model.sh [stock|uncensored]" ;; esac

PINS="$(grep -E '^(IMAGE|STOCK_REPO|STOCK_REV|UNC_REPO|UNC_REV|MODEL_CHOICE|HF_CACHE|CONFIG_DIR)=' "$REPO_DIR/install.sh" || true)"
[ "$(printf '%s\n' "$PINS" | wc -l)" -eq 8 ] || die "could not read the 8 pinned variables from install.sh (repo layout changed?)"
eval "$PINS"
if [ "$CHOICE" = "uncensored" ]; then TARGET_REPO="$UNC_REPO"; TARGET_REV="$UNC_REV"; else TARGET_REPO="$STOCK_REPO"; TARGET_REV="$STOCK_REV"; fi

UNIT="/etc/systemd/system/qwen38-sglang.service"
[ -f "$UNIT" ] || die "installed unit $UNIT not found (run ./install.sh first)"

CUR="$(grep -oE -- '--model-path [^ ]+' "$UNIT" | head -1 | cut -d' ' -f2 || true)"
if [ "$CUR" = "$TARGET_REPO" ]; then
  echo "unit already points at $TARGET_REPO"
else
  echo "switching: ${CUR:-<none>} -> $TARGET_REPO @ $TARGET_REV"
fi

# 1) checkpoint in cache (docker + pinned image python, same as install.sh step 4)
printf '\n\033[1;36m── Downloading %s @ %s (resumable)\033[0m\n' "$TARGET_REPO" "$TARGET_REV"
DL_TOKEN_ARGS=()
[ -n "${HF_TOKEN:-}" ] && DL_TOKEN_ARGS=(-e HF_TOKEN="$HF_TOKEN")
docker run --rm -i --network host --user "$(id -u):$(id -g)" \
  -e HF_HOME=/hf -e HF_HUB_DOWNLOAD_TIMEOUT=30 -e HF_HUB_DISABLE_XET=1 \
  -e MODEL_REPO="$TARGET_REPO" -e MODEL_REV="$TARGET_REV" \
  "${DL_TOKEN_ARGS[@]}" \
  -v "$HF_CACHE":/hf \
  "$IMAGE" python3 - <<'PYEOF' || die "download failed (re-run to resume; HuggingFace throttles unauthenticated downloads, set HF_TOKEN=<your token> if it stalls)"
import os
import time
from huggingface_hub import snapshot_download
print("──", os.environ["MODEL_REPO"], "@", os.environ["MODEL_REV"], flush=True)
for attempt in range(1, 6):  # a resumed attempt reuses every finished byte
    try:
        path = snapshot_download(os.environ["MODEL_REPO"], revision=os.environ["MODEL_REV"])
        break
    except Exception as e:
        if attempt == 5:
            raise
        print(f"download interrupted ({type(e).__name__}), resuming ({attempt}/5)...", flush=True)
        time.sleep(10)
# Same guarantee as install.sh: offline serving resolves "main" via refs/main,
# which a pinned-sha download never writes. Write it once, never overwrite.
sha = os.path.basename(path.rstrip("/"))
ref = os.path.join(os.path.dirname(os.path.dirname(path.rstrip("/"))), "refs", "main")
if len(sha) == 40 and not os.path.exists(ref):
    os.makedirs(os.path.dirname(ref), exist_ok=True)
    with open(ref, "w") as f:
        f.write(sha)
print("checkpoint ready", flush=True)
PYEOF

# 2) 1M YaRN patch on the target's cached config.json (only if the unit uses it)
if grep -q -- '--context-length 1010000' "$UNIT"; then
  python3 "$REPO_DIR/patch-yarn.py" "$HF_CACHE" "$TARGET_REPO" "$TARGET_REV" || die "YaRN patch failed"
else
  echo "unit does not use the 1M context flag; skipping the YaRN patch"
fi

# 2b) regenerate the patched chat template FROM the target's own snapshot.
# Both known targets ship byte-identical templates today (verified), so this
# normally changes nothing; it guarantees the served template follows the
# served model if one of them ever diverges. Read at service startup only,
# so writing it now is safe and takes effect with the switch itself.
python3 "$REPO_DIR/patch-template.py" "$HF_CACHE" "$CONFIG_DIR/chat-template-sglang.jinja" "$TARGET_REV" "$TARGET_REPO" \
  || die "template patch failed for $TARGET_REPO (the switch was NOT applied to the unit yet)"

# 3) rewrite ONLY the --model-path value (and the target's --revision when the
#    unit carries the serve-time lock; the draft's own revision flag has a
#    different name and is never touched). Every other flag is kept
#    (1M, mem-fraction, ...). Written to a temp file + sudo install -m 644.
TMP_UNIT="$(mktemp)"
trap 'rm -f "$TMP_UNIT"' EXIT
sed -E -e "s|(--model-path )[A-Za-z0-9./_-]+|\1$TARGET_REPO|" \
       -e "s|(--revision )[A-Za-z0-9]+|\1$TARGET_REV|" "$UNIT" > "$TMP_UNIT"
grep -q -- "--model-path $TARGET_REPO" "$TMP_UNIT" || die "unit rewrite failed"
if grep -q -- '--revision ' "$UNIT"; then
  grep -q -- "--revision $TARGET_REV" "$TMP_UNIT" || die "unit revision rewrite failed"
fi
diff "$TMP_UNIT" "$UNIT" || true   # show exactly what changes
sudo install -m 644 "$TMP_UNIT" "$UNIT"
sudo systemctl daemon-reload

printf '\n\033[1;32mSwitch queued: %s\033[0m\n' "$TARGET_REPO"
echo "Effective after:  sudo systemctl restart qwen38-sglang   (or next reboot)"
echo "Switch back:      ./switch-model.sh $([ "$CHOICE" = "stock" ] && echo uncensored || echo stock)"
