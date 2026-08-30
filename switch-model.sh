#!/usr/bin/env bash
# Surgical target-model switch on a live install, between the three targets:
#
#   ./switch-model.sh stock        # RadixArk/Qwen3.8-27B-NVFP4 (SGLang)
#   ./switch-model.sh uncensored   # edp1096/Huihui-...-abliterated-NVFP4 (SGLang)
#   ./switch-model.sh flash        # RadixArk/Qwen3.8-Flash-Next-NVFP4 (vLLM)
#
# Within the 27B pair (stock <-> uncensored) it does what it always did:
#   1. downloads the checkpoint into $HF_CACHE (resumable, pinned revision);
#   2. applies the 1M YaRN config patch ONLY if the installed unit uses it;
#   3. regenerates the patched chat template from the target's own snapshot;
#   4. rewrites ONLY --model-path/--revision in the qwen38-sglang unit.
#
# Across lanes (27B <-> flash) both stacks must already be installed once
# (each by its own ./install.sh run: the switch is surgical, it does not
# download images or build overlays). It then re-verifies the checkpoint,
# regenerates the target's template, flips which unit is enabled at boot,
# and points the opencode default model at the target.
#
# The DFlash2 drafter (27B) and the NEXTN/MTP head (flash) are lossless speculative
# paths: drafts are verified against the target, quality is the target's own.
# The switch takes effect on the NEXT restart; this script NEVER restarts or
# stops a service itself. It prints the exact commands instead.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
die() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

CHOICE="${1:-${MODEL_CHOICE:-stock}}"
case "$CHOICE" in stock|uncensored|flash) ;; *) die "usage: ./switch-model.sh [stock|uncensored|flash]" ;; esac

PINS="$(grep -E '^(IMAGE|STOCK_REPO|STOCK_REV|UNC_REPO|UNC_REV|FLASH_REPO|FLASH_REV|FLASH_IMAGE|FLASH_SERVE_IMAGE|MODEL_CHOICE|HF_CACHE|CONFIG_DIR)=' "$REPO_DIR/install.sh" || true)"
[ "$(printf '%s\n' "$PINS" | wc -l)" -eq 12 ] || die "could not read the 12 pinned variables from install.sh (repo layout changed?)"
eval "$PINS"

SGL_UNIT="/etc/systemd/system/qwen38-sglang.service"
FLASH_UNIT="/etc/systemd/system/qwen38-flash.service"

case "$CHOICE" in
  stock)      TARGET_REPO="$STOCK_REPO"; TARGET_REV="$STOCK_REV"; TARGET_LANE=27b ;;
  uncensored) TARGET_REPO="$UNC_REPO";   TARGET_REV="$UNC_REV";   TARGET_LANE=27b ;;
  flash)      TARGET_REPO="$FLASH_REPO"; TARGET_REV="$FLASH_REV"; TARGET_LANE=flash ;;
esac

if [ "$TARGET_LANE" = "27b" ]; then
  [ -f "$SGL_UNIT" ] || die "the 27B stack is not installed on this box (no $SGL_UNIT). Install it once first: MODEL_CHOICE=$CHOICE ./install.sh"
  TARGET_UNIT="$SGL_UNIT"; TARGET_UNIT_NAME="qwen38-sglang"
  OTHER_UNIT="$FLASH_UNIT"; OTHER_UNIT_NAME="qwen38-flash"
  DL_IMAGE="$IMAGE"
else
  [ -f "$FLASH_UNIT" ] || die "the Flash-Next stack is not installed on this box (no $FLASH_UNIT). Install it once first: MODEL_CHOICE=flash ./install.sh (~136 GB download + image build)"
  TARGET_UNIT="$FLASH_UNIT"; TARGET_UNIT_NAME="qwen38-flash"
  OTHER_UNIT="$SGL_UNIT"; OTHER_UNIT_NAME="qwen38-sglang"
  DL_IMAGE="$FLASH_IMAGE"
fi
docker image inspect "$DL_IMAGE" >/dev/null 2>&1 \
  || die "the pinned image for this target is not present ($DL_IMAGE): run MODEL_CHOICE=$CHOICE ./install.sh once, the switch stays surgical"

if [ "$TARGET_LANE" = "27b" ]; then
  CUR="$(grep -oE -- '--model-path [^ ]+' "$TARGET_UNIT" | head -1 | cut -d' ' -f2 || true)"
  if [ "$CUR" = "$TARGET_REPO" ] && ! systemctl is-enabled --quiet "$OTHER_UNIT_NAME" 2>/dev/null; then
    echo "unit already points at $TARGET_REPO"
  else
    echo "switching: ${CUR:-<none>} -> $TARGET_REPO @ $TARGET_REV"
  fi
else
  echo "switching the serving lane to Flash-Next: $TARGET_REPO @ $TARGET_REV"
fi

# 1) checkpoint in cache (docker + pinned image python, same as install.sh step 4)
printf '\n\033[1;36m── Verifying/downloading %s @ %s (resumable)\033[0m\n' "$TARGET_REPO" "$TARGET_REV"
DL_TOKEN_ARGS=()
[ -n "${HF_TOKEN:-}" ] && DL_TOKEN_ARGS=(-e HF_TOKEN="$HF_TOKEN")
docker run --rm -i --network host --user "$(id -u):$(id -g)" \
  --entrypoint python3 \
  -e HF_HOME=/hf -e HF_HUB_DOWNLOAD_TIMEOUT=30 -e HF_HUB_DISABLE_XET=1 \
  -e MODEL_REPO="$TARGET_REPO" -e MODEL_REV="$TARGET_REV" \
  "${DL_TOKEN_ARGS[@]}" \
  -v "$HF_CACHE":/hf \
  "$DL_IMAGE" - <<'PYEOF' || die "download failed (re-run to resume; HuggingFace throttles unauthenticated downloads, set HF_TOKEN=<your token> if it stalls)"
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

# 2) 1M YaRN patch on the target's cached config.json (27B pair only, and only
#    if its unit uses it; flash serves its native window)
if [ "$TARGET_LANE" = "27b" ] && grep -q -- '--context-length 1010000' "$TARGET_UNIT"; then
  python3 "$REPO_DIR/patch-yarn.py" "$HF_CACHE" "$TARGET_REPO" "$TARGET_REV" || die "YaRN patch failed"
elif [ "$TARGET_LANE" = "27b" ]; then
  echo "unit does not use the 1M context flag; skipping the YaRN patch"
fi

# 2b) regenerate the patched chat template FROM the target's own snapshot.
# One template file per engine; the served template always follows the served
# model. Read at service startup only, so writing it now is safe.
TEMPLATE_OUT="$CONFIG_DIR/chat-template-sglang.jinja"
[ "$TARGET_LANE" = "flash" ] && TEMPLATE_OUT="$CONFIG_DIR/chat-template-flashnext.jinja"
python3 "$REPO_DIR/patch-template.py" "$HF_CACHE" "$TEMPLATE_OUT" "$TARGET_REV" "$TARGET_REPO" \
  || die "template patch failed for $TARGET_REPO (the switch was NOT applied to the unit yet)"

# 3) point the target unit at the exact checkpoint. For the 27B pair, rewrite
#    ONLY --model-path/--revision (the draft's own revision flag has a
#    different name and is never touched). For flash, refresh --revision to
#    the repo pin. Written to a temp file + sudo install -m 644.
TMP_UNIT="$(mktemp)"
trap 'rm -f "$TMP_UNIT"' EXIT
if [ "$TARGET_LANE" = "27b" ]; then
  sed -E -e "s|(--model-path )[A-Za-z0-9./_-]+|\1$TARGET_REPO|" \
         -e "s|(--revision )[A-Za-z0-9]+|\1$TARGET_REV|" "$TARGET_UNIT" > "$TMP_UNIT"
  grep -q -- "--model-path $TARGET_REPO" "$TMP_UNIT" || die "unit rewrite failed"
  if grep -q -- '--revision ' "$TARGET_UNIT"; then
    grep -q -- "--revision $TARGET_REV" "$TMP_UNIT" || die "unit revision rewrite failed"
  fi
  diff "$TMP_UNIT" "$TARGET_UNIT" || true   # show exactly what changes
  sudo install -m 644 "$TMP_UNIT" "$TARGET_UNIT"
else
  # Flash keeps its serving flags in a plain launch script; the unit just
  # points at it. Refresh --revision there.
  FLASH_LAUNCH="$CONFIG_DIR/launch-flash.sh"
  [ -f "$FLASH_LAUNCH" ] || die "flash launch script missing ($FLASH_LAUNCH): re-run MODEL_CHOICE=flash ./install.sh"
  sed -E -e "s|(--revision )[A-Za-z0-9]+|\1$TARGET_REV|" \
         -e "s|(SGLANG_QWEN4_PLE_TAG=)[A-Za-z0-9]+|\1$TARGET_REV|" "$FLASH_LAUNCH" > "$TMP_UNIT"
  grep -q -- " $TARGET_REPO " "$TMP_UNIT" || die "the flash launch script does not serve $TARGET_REPO (hand-edited?); re-run MODEL_CHOICE=flash ./install.sh"
  grep -q -- "--revision $TARGET_REV" "$TMP_UNIT" || die "launch script revision rewrite failed"
  bash -n "$TMP_UNIT" || die "rewritten launch script does not parse"
  diff "$TMP_UNIT" "$FLASH_LAUNCH" || true   # show exactly what changes
  install -m 755 "$TMP_UNIT" "$FLASH_LAUNCH"
fi

# 4) exactly one serving unit enabled at boot; the opencode default model
#    follows the switch (providers themselves are kept as installed).
if [ -f "$OTHER_UNIT" ] && systemctl is-enabled --quiet "$OTHER_UNIT_NAME" 2>/dev/null; then
  echo "disabling $OTHER_UNIT_NAME at boot (unit file kept for switching back)"
  sudo systemctl disable "$OTHER_UNIT_NAME"
fi
sudo systemctl enable "$TARGET_UNIT_NAME" >/dev/null 2>&1 || sudo systemctl enable "$TARGET_UNIT_NAME"
sudo systemctl daemon-reload
# 4b) the keepalive proxy's one-prompt ceiling follows the lane (v1.5.6 contract, see
#     install.sh: flash 128000 tokens by default, the 27B lane none), applied now so the
#     proxy matches the lane that serves after the restart below.
KA_UNIT="/etc/systemd/system/qwen38-keepalive.service"
if [ -f "$KA_UNIT" ]; then
  CEIL=0
  [ "$TARGET_LANE" = "flash" ] && CEIL="${PROMPT_CEILING_TOKENS:-128000}"
  if grep -q '^Environment=PROMPT_CEILING_TOKENS=' "$KA_UNIT"; then
    sudo sed -i "s/^Environment=PROMPT_CEILING_TOKENS=.*/Environment=PROMPT_CEILING_TOKENS=$CEIL/" "$KA_UNIT"
  else
    sudo sed -i "/^Environment=UPSTREAM=/a Environment=PROMPT_CEILING_TOKENS=$CEIL" "$KA_UNIT"
  fi
  sudo systemctl daemon-reload
  sudo systemctl restart qwen38-keepalive.service
  echo "keepalive proxy: one-prompt ceiling ${CEIL} tokens for the $TARGET_LANE lane"
fi
# Both files when present: the generated artifact (CONFIG_DIR) and the config
# opencode actually reads (~/.config/opencode). Updating only the artifact left
# the real default on the previous lane (seen on the reference box 2026-08-30).
# A box installed with --no-opencode (marker file) is left entirely alone.
if [ -f "$CONFIG_DIR/opencode.off" ]; then
  echo "opencode integration is off on this box (./install.sh --no-opencode): default model left alone"
else
for OC_JSON in "$CONFIG_DIR/opencode.json" "$HOME/.config/opencode/opencode.json"; do
  [ -f "$OC_JSON" ] || continue
  TARGET_LANE="$TARGET_LANE" OC_JSON="$OC_JSON" python3 - <<'PYEOF' || echo "NOTE: could not update $OC_JSON (hand-edited?); set its \"model\" field yourself"
import json
import os
p = os.environ["OC_JSON"]
want = "flashnext/qwen3.8-flash-next" if os.environ["TARGET_LANE"] == "flash" else "qwen38/qwen3.8-27b"
cfg = json.load(open(p))
prov = want.split("/")[0]
if prov not in cfg.get("provider", {}):
    print(f"NOTE: provider '{prov}' is not in {p} (config predates this target);")
    print( "      re-run ./install.sh once to regenerate it, keeping your choices.")
else:
    cfg["model"] = want
    cfg["small_model"] = want
    with open(p, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"opencode default model -> {want} ({p})")
PYEOF
done
fi

RUNNING=""
systemctl is-active --quiet "$OTHER_UNIT_NAME" 2>/dev/null && RUNNING="$OTHER_UNIT_NAME"
printf '\n\033[1;32mSwitch queued: %s (%s)\033[0m\n' "$TARGET_REPO" "$TARGET_UNIT_NAME"
if [ -n "$RUNNING" ]; then
  echo "Effective after:  sudo systemctl stop $RUNNING && sudo systemctl start $TARGET_UNIT_NAME   (or next reboot)"
else
  echo "Effective after:  sudo systemctl restart $TARGET_UNIT_NAME   (or next reboot)"
fi
case "$CHOICE" in
  stock)      echo "Switch back:      ./switch-model.sh uncensored   (or flash)" ;;
  uncensored) echo "Switch back:      ./switch-model.sh stock   (or flash)" ;;
  flash)      echo "Switch back:      ./switch-model.sh stock   (or uncensored)" ;;
esac
