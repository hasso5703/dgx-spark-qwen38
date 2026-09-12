#!/usr/bin/env bash
# Surgical target-model switch on a live install, between the five targets:
#
#   ./switch-model.sh stock           # RadixArk/Qwen3.8-27B-NVFP4 (SGLang)
#   ./switch-model.sh uncensored      # edp1096/Huihui-...-abliterated-NVFP4 (SGLang)
#   ./switch-model.sh fp8             # Qwen/Qwen3.8-27B-FP8 (SGLang)
#   ./switch-model.sh uncensored-fp8  # edp1096/Huihui-...-abliterated-FP8 (SGLang)
#   ./switch-model.sh flash           # RadixArk/Qwen3.8-Flash-Next-NVFP4 (SGLang)
#   ./switch-model.sh flash-nvda      # nvidia/Qwen3.8-Flash-Next-NVFP4, ModelOpt mixed precision
#   ./switch-model.sh flash-uncensored # the abliterated build of the same tree
#
# Within the 27B lane (stock, uncensored, fp8, uncensored-fp8) it does what it
# always did:
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
case "$CHOICE" in stock|uncensored|fp8|uncensored-fp8|flash|flash-nvda|flash-uncensored) ;; *) die "usage: ./switch-model.sh [stock|uncensored|fp8|uncensored-fp8|flash|flash-nvda|flash-uncensored]" ;; esac

PINS="$(grep -E '^(IMAGE|STOCK_REPO|STOCK_REV|UNC_REPO|UNC_REV|FP8_REPO|FP8_REV|UNCFP8_REPO|UNCFP8_REV|FLASH_REPO|FLASH_REV|FLASH_NVDA_REPO|FLASH_NVDA_REV|FLASH_UNC_REPO|FLASH_UNC_REV|FLASH_IMAGE|FLASH_SERVE_IMAGE|OVERLAY_FLASH_SERVE_IMAGE|OVERLAY_SERVE_IMAGE|SERVE_IMAGE|MODEL_CHOICE|HF_CACHE|CONFIG_DIR)=' "$REPO_DIR/install.sh" || true)"
# A count of matched lines was the old check, and adding a pin broke both scripts
# at once (it did, on 2026-09-08). What matters is not how many lines matched but
# whether every name this script goes on to use is defined, so that is what is
# asserted, and a failure says which one.
eval "$PINS"
for _v in IMAGE SERVE_IMAGE STOCK_REPO STOCK_REV UNC_REPO UNC_REV FP8_REPO FP8_REV UNCFP8_REPO UNCFP8_REV FLASH_REPO FLASH_REV FLASH_NVDA_REPO FLASH_NVDA_REV FLASH_UNC_REPO FLASH_UNC_REV FLASH_IMAGE FLASH_SERVE_IMAGE OVERLAY_FLASH_SERVE_IMAGE OVERLAY_SERVE_IMAGE MODEL_CHOICE HF_CACHE CONFIG_DIR; do
  eval "[ -n \"\${$_v:-}\" ]" || die "install.sh no longer defines $_v (repo layout changed?)"
done
unset _v

SGL_UNIT="/etc/systemd/system/qwen38-sglang.service"
FLASH_UNIT="/etc/systemd/system/qwen38-flash.service"

case "$CHOICE" in
  stock)      TARGET_REPO="$STOCK_REPO"; TARGET_REV="$STOCK_REV"; TARGET_LANE=27b ;;
  uncensored) TARGET_REPO="$UNC_REPO";   TARGET_REV="$UNC_REV";   TARGET_LANE=27b ;;
  fp8)        TARGET_REPO="$FP8_REPO";   TARGET_REV="$FP8_REV";   TARGET_LANE=27b ;;
  uncensored-fp8) TARGET_REPO="$UNCFP8_REPO"; TARGET_REV="$UNCFP8_REV"; TARGET_LANE=27b ;;
  flash)      TARGET_REPO="$FLASH_REPO"; TARGET_REV="$FLASH_REV"; TARGET_LANE=flash ;;
  flash-nvda) TARGET_REPO="$FLASH_NVDA_REPO"; TARGET_REV="$FLASH_NVDA_REV"; TARGET_LANE=flash ;;
  flash-uncensored) TARGET_REPO="$FLASH_UNC_REPO"; TARGET_REV="$FLASH_UNC_REV"; TARGET_LANE=flash ;;
esac

# The pins come from install.sh through eval, so a pin renamed or emptied there
# would otherwise reach the download step as an empty repo or revision. CI
# guards the names; this guards the values on a box that already diverged.
[ -n "${TARGET_REPO:-}" ] && [ -n "${TARGET_REV:-}" ] \
  || die "target '$CHOICE' resolved to an empty checkpoint or revision; install.sh is missing its pin"

if [ "$TARGET_LANE" = "27b" ]; then
  [ -f "$SGL_UNIT" ] || die "the 27B stack is not installed on this box (no $SGL_UNIT). Install it once first: MODEL_CHOICE=$CHOICE ./install.sh"
  TARGET_UNIT="$SGL_UNIT"; TARGET_UNIT_NAME="qwen38-sglang.service"
  OTHER_UNIT="$FLASH_UNIT"; OTHER_UNIT_NAME="qwen38-flash.service"
  INVOCATION="$SGL_UNIT"
  PIN_IMAGE="$SERVE_IMAGE"
else
  [ -f "$FLASH_UNIT" ] || die "the Flash-Next stack is not installed on this box (no $FLASH_UNIT). Install it once first: MODEL_CHOICE=$CHOICE ./install.sh (~126 GB download, no image build)"
  TARGET_UNIT="$FLASH_UNIT"; TARGET_UNIT_NAME="qwen38-flash.service"
  OTHER_UNIT="$SGL_UNIT"; OTHER_UNIT_NAME="qwen38-sglang.service"
  INVOCATION="$CONFIG_DIR/launch-flash.sh"
  PIN_IMAGE="$FLASH_SERVE_IMAGE"
fi
# The image whose presence decides a switch is the one the installed invocation
# will actually run, not the base it may have been built from. Since v1.8 a box
# can serve the pinned official image and keep no base image at all, and both
# base images are re-pullable, so gating on a base turned a working switch into
# a false "run install.sh once". Read it the way the cockpit does: the line just
# above the one that calls the server.
unit_image() {
  [ -f "$1" ] || return 0
  awk '/sglang\.launch_server/ { print prev; exit } { prev = $0 }' "$1" \
    | sed -e 's/[[:space:]]*\\$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}
# Point the flash launcher at another checkpoint of the same lane: the model
# path, the revision, and the one flag line that belongs to the checkpoint.
#
# That line is REPLACED WHOLE, not patched in place. Patching fragments left one
# checkpoint's draft quantization standing behind the other's scheme (seen live
# on 2026-09-08 switching nvda -> flash), because the two strings hold a
# different number of flags. The rendered line always ends with the FP4 GEMM
# backend, so rebuilding it from the indent is exact.
#
# Tested by CI against both input shapes, sourced out of this file so a copy of
# the logic cannot drift from it.
# Point the 27B unit at another checkpoint of the same lane: the model path, the
# revision, and the KV cache dtype, which belongs to the checkpoint too.
#
# That last one is why this is a function and not two sed expressions. Until
# 2026-09-08 the switch rewrote only the path and the revision, so a switch from
# an NVFP4 target to Qwen's FP8 release left the unit with NO --kv-cache-dtype:
# that checkpoint carries no KV scales, "auto" falls back to a bf16 KV cache, and
# the pool halves (this repo measured 771,139 tokens against 382,706 on the same
# 1M unit). In the other direction the flag stayed on an NVFP4 target, which is a
# no-op there (both NVFP4 exports declare kv_cache_quant_algo: FP8, checked) but
# is what left this box reporting a drift for days.
#
# The fraction on that line is the box's own choice (0.50 native, 0.70 for 1M,
# and a hand-tuned value must survive a switch), so it is read from the unit and
# written back unchanged. Tested by CI against both directions and both modes,
# sourced out of this file so no copy of the logic can drift from it.
rewrite_27b_unit() {  # $1 unit, $2 repo, $3 revision, $4 kv args ("" or "--kv-cache-dtype fp8_e4m3 ")
  awk -v repo="$2" -v rev="$3" -v kv="$4" '
    {
      line = $0
      sub(/--model-path [A-Za-z0-9_.\/-]+/, "--model-path " repo, line)
      sub(/--revision [A-Za-z0-9]+/, "--revision " rev, line)
      if (match(line, /--mem-fraction-static [0-9.]+/)) {
        frac = substr(line, RSTART, RLENGTH)
        match(line, /^ */)
        line = substr(line, 1, RLENGTH) frac " " kv "\\"
      }
      print line
    }
  ' "$1"
}

rewrite_flash_launcher() {  # $1 launcher, $2 repo, $3 revision, $4 quant args
  awk -v repo="$2" -v rev="$3" -v quant="$4" '
    {
      line = $0
      sub(/--model-path [A-Za-z0-9_.\/-]+/, "--model-path " repo, line)
      sub(/--revision [A-Za-z0-9]+/, "--revision " rev, line)
      if (line ~ /^ *(--quantization|--moe-runner-backend)/) {
        match(line, /^ */)
        line = substr(line, 1, RLENGTH) quant "--fp4-gemm-backend flashinfer_cutlass \\"
      }
      print line
    }
  ' "$1"
}

DL_IMAGE="$(unit_image "$INVOCATION")"
case "$DL_IMAGE" in
  *:*) ;;                                  # name:tag or name@sha256:... both match
  *) DL_IMAGE="$PIN_IMAGE" ;;              # unreadable invocation: fall back to the repo's pin
esac
docker image inspect "$DL_IMAGE" >/dev/null 2>&1 \
  || die "the image this box would serve is not present ($DL_IMAGE): run MODEL_CHOICE=$CHOICE ./install.sh once, the switch stays surgical"

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
#    the repo pin. The 27B unit is installed with sudo, so it is staged at a
#    FIXED path the cockpit's exact-argv sudoers allowlist pins (a mktemp name
#    cannot be pinned); the user-owned flash launcher keeps a temp file.
TMP_UNIT="$(mktemp)"
STAGE_UNIT="$CONFIG_DIR/qwen38-sglang.service.switch-stage"
STAGE_CEIL="$CONFIG_DIR/keepalive-ceiling.conf.switch-stage"
trap 'rm -f "$TMP_UNIT" "$STAGE_UNIT" "$STAGE_CEIL"' EXIT
if [ "$TARGET_LANE" = "27b" ]; then
  # The KV cache dtype follows the checkpoint, exactly as install.sh decides it:
  # Qwen's FP8 release carries no KV scales and must ask for fp8 explicitly, the
  # NVFP4 exports declare it in their own quant config and must not be forced.
  # CI asserts these two strings still match install.sh's KV_CACHE_ARGS.
  case "$CHOICE" in
    fp8|uncensored-fp8) NEW_KV="--kv-cache-dtype fp8_e4m3 " ;;
    *)                  NEW_KV="" ;;
  esac
  rewrite_27b_unit "$TARGET_UNIT" "$TARGET_REPO" "$TARGET_REV" "$NEW_KV" > "$STAGE_UNIT"
  grep -q -- "--model-path $TARGET_REPO" "$STAGE_UNIT" || die "unit rewrite failed"
  KV_SEEN="$(grep -c -- '--kv-cache-dtype fp8_e4m3' "$STAGE_UNIT" || true)"
  case "$CHOICE" in
    fp8|uncensored-fp8)
      [ "$KV_SEEN" -eq 1 ] || die "the rewritten unit lost the fp8 KV cache this target needs (it costs half the pool); re-run MODEL_CHOICE=$CHOICE ./install.sh" ;;
    *)
      [ "$KV_SEEN" -eq 0 ] || die "the rewritten unit still forces an fp8 KV cache; re-run MODEL_CHOICE=$CHOICE ./install.sh" ;;
  esac
  grep -qE -- '--mem-fraction-static [0-9.]+' "$STAGE_UNIT" || die "the rewritten unit lost its memory fraction"
  if grep -q -- '--revision ' "$TARGET_UNIT"; then
    grep -q -- "--revision $TARGET_REV" "$STAGE_UNIT" || die "unit revision rewrite failed"
  fi
  diff "$STAGE_UNIT" "$TARGET_UNIT" || true   # show exactly what changes
  sudo install -m 644 "$STAGE_UNIT" "$TARGET_UNIT"
else
  # Flash keeps its serving flags in a plain launch script; the unit just
  # points at it. Refresh --revision there.
  FLASH_LAUNCH="$CONFIG_DIR/launch-flash.sh"
  [ -f "$FLASH_LAUNCH" ] || die "flash launch script missing ($FLASH_LAUNCH): re-run MODEL_CHOICE=flash ./install.sh"
  # The two flash checkpoints differ in more than their name: the RadixArk
  # export is plain NVFP4 and declares it, NVIDIA's is a mixed-precision export
  # that resolves its own scheme and needs the MoE runner pinned. So the switch
  # rewrites the model path, the revision and that one flag pair together.
  # These two strings are install.sh's FLASH_QUANT_ARGS, and CI asserts they
  # still match it character for character. They carry BOTH the target's scheme
  # and the draft's, because both belong to the checkpoint: the NVFP4 tree's
  # in-checkpoint MTP tensors are BF16, NVIDIA's are fp8 block-scaled.
  case "$CHOICE" in
    flash-nvda) NEW_QUANT="--moe-runner-backend flashinfer_cutlass " ;;
    *)          NEW_QUANT="--quantization modelopt_fp4 --speculative-draft-model-quantization unquant " ;;
  esac
  rewrite_flash_launcher "$FLASH_LAUNCH" "$TARGET_REPO" "$TARGET_REV" "$NEW_QUANT" > "$TMP_UNIT"
  grep -qF -- "$NEW_QUANT--fp4-gemm-backend" "$TMP_UNIT" \
    || die "the launcher's quantization line did not take this checkpoint's flags; re-run MODEL_CHOICE=$CHOICE ./install.sh"
  grep -q -- "--model-path $TARGET_REPO " "$TMP_UNIT" || die "the flash launch script does not serve $TARGET_REPO (hand-edited?); re-run MODEL_CHOICE=$CHOICE ./install.sh"

  grep -q -- "--revision $TARGET_REV" "$TMP_UNIT" || die "launch script revision rewrite failed"
  bash -n "$TMP_UNIT" || die "rewritten launch script does not parse"
  diff "$TMP_UNIT" "$FLASH_LAUNCH" || true   # show exactly what changes
  install -m 755 "$TMP_UNIT" "$FLASH_LAUNCH"
  # A switch changes the checkpoint, never the serving image: only install.sh builds
  # that. So a box that pulled a repo whose image pin moved would keep serving the old
  # one, silently, and the image is where the kernel fixes live (v1.6: the merged
  # sm_121 kernel). Say so instead of letting it pass.
  # Since v1.8 the launcher names the pinned official image (a digest), and
  # an OVERLAY_FLASH=1 install still names a local qwen38-flash:tag. Read either.
  LAUNCH_IMAGE="$(grep -oE '^[[:space:]]*(qwen38-flash:[A-Za-z0-9._-]+|lmsysorg/sglang@sha256:[0-9a-f]{64})' "$FLASH_LAUNCH" | tr -d '[:space:]' | head -1)"
  case "$LAUNCH_IMAGE" in
    qwen38-flash:*) WANT_IMAGE="${OVERLAY_FLASH_SERVE_IMAGE:-}" ;;   # an OVERLAY_FLASH=1 box
    *)              WANT_IMAGE="${FLASH_IMAGE:-}" ;;                  # the default: the pinned official image
  esac
  if [ -n "$LAUNCH_IMAGE" ] && [ -n "$WANT_IMAGE" ] && [ "$LAUNCH_IMAGE" != "$WANT_IMAGE" ]; then
    printf '\n\033[1;33mWARNING:\033[0m this box serves flash from %s, but this repo pins %s.\n' \
      "$LAUNCH_IMAGE" "$WANT_IMAGE"
    printf '         The serving image is where the kernel fixes live, and only the installer adopts it.\n'
    printf '         Run ./install.sh to adopt the pinned image before relying on this lane.\n'
  fi
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
#     install.sh: flash 200000 tokens by default, the 27B lane none), applied now so the
#     proxy matches the lane that serves after the restart below. Written as a systemd
#     drop-in, never by editing the unit in place: an in-place sed needs a sudoers
#     wildcard to stay valid, and a wildcard on sed is root (its w command writes any
#     file). The drop-in overrides the unit's Environment line; install.sh bakes the
#     ceiling into the unit and removes this drop-in, so either path converges.
KA_UNIT="/etc/systemd/system/qwen38-keepalive.service"
if [ -f "$KA_UNIT" ]; then
  CEIL=0
  [ "$TARGET_LANE" = "flash" ] && CEIL="${PROMPT_CEILING_TOKENS:-200000}"
  [[ "$CEIL" =~ ^[0-9]+$ ]] || die "PROMPT_CEILING_TOKENS must be a number (got '$CEIL')"
  printf '[Service]\nEnvironment=PROMPT_CEILING_TOKENS=%s\n' "$CEIL" > "$STAGE_CEIL"
  sudo install -m 644 -D "$STAGE_CEIL" "/etc/systemd/system/qwen38-keepalive.service.d/ceiling.conf"
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
# The limits follow the target, not just the model name. opencode compacts when a
# conversation reaches limit.context, and a limit larger than the lane can serve
# means the proxy refuses the request with a 400 before opencode ever compacts:
# a hard failure mid-session. Switching a 27B box in 1M mode (700,000) to the
# flash lane (200,000 ceiling) is exactly that, and until 2026-09-08 the switch
# left the old numbers in place. The table is oc-limits.sh, shared with
# install.sh so the two cannot disagree; the tier or context mode is read from
# the invocation this switch just wrote, which is what the box will serve.
# The label is read but not used here: the picker name is set by the python
# block below, from the same table's LABEL dict, so it is read into a throwaway.
if read -r SW_CTX SW_OUT _SW_LABEL \
     <<<"$("$REPO_DIR/oc-limits.sh" "$CHOICE" --from "$INVOCATION")" \
   && [ -n "${SW_CTX:-}" ]; then
  if [ "$TARGET_LANE" = "flash" ]; then SW_PROV=flashnext; SW_MODEL=qwen3.8-flash-next
  else SW_PROV=qwen38; SW_MODEL=qwen3.8-27b; fi
  for OC_JSON in "$CONFIG_DIR/opencode.json" "$HOME/.config/opencode/opencode.json"; do
    [ -f "$OC_JSON" ] || continue
    python3 "$REPO_DIR/oc-merge-limits.py" "$OC_JSON" "$SW_PROV" "$SW_MODEL" "$SW_CTX" "$SW_OUT" \
      || echo "NOTE: could not update the limits in $OC_JSON; check them by hand ($SW_CTX/$SW_OUT)"
  done
else
  echo "NOTE: oc-limits.sh gave no limits for $CHOICE; the opencode limits were left as they were"
fi
for OC_JSON in "$CONFIG_DIR/opencode.json" "$HOME/.config/opencode/opencode.json"; do
  [ -f "$OC_JSON" ] || continue
  # The window is read from the INVOCATION, not from the unit: on the flash lane
  # the unit is a thin pointer at the launcher and carries no --context-length,
  # so reading the unit labelled every flash switch "(local)" instead of
  # "(local, 262K)". Default model and picker name share one table in
  # oc-point-default.py with install.sh, so the three spellings cannot drift.
  OC_WINDOW="$(grep -oE -- '--context-length [0-9]+' "$INVOCATION" 2>/dev/null | awk '{print $2}' | head -1)"
  python3 "$REPO_DIR/oc-point-default.py" "$OC_JSON" "$TARGET_LANE" "$CHOICE" "$OC_WINDOW" \
    || echo "NOTE: could not update $OC_JSON (hand-edited?); set its \"model\" field yourself"
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
  stock)      echo "Switch back:      ./switch-model.sh uncensored   (or fp8, flash)" ;;
  uncensored) echo "Switch back:      ./switch-model.sh stock   (or fp8, flash)" ;;
  fp8)        echo "Switch back:      ./switch-model.sh stock   (or uncensored, uncensored-fp8, flash)" ;;
  uncensored-fp8) echo "Switch back:      ./switch-model.sh fp8   (or stock, uncensored, flash)" ;;
  flash)      echo "Switch back:      ./switch-model.sh stock   (or uncensored, fp8, flash-nvda, flash-uncensored)" ;;
  flash-nvda) echo "Switch back:      ./switch-model.sh flash   (or flash-uncensored, stock, uncensored, fp8)" ;;
  flash-uncensored) echo "Switch back:      ./switch-model.sh flash   (or flash-nvda, stock, uncensored, fp8)" ;;
esac
