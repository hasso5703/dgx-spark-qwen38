#!/usr/bin/env bash
# Surgical target-model switch on a live install, between these targets:
#
#   ./switch-model.sh stock           # RadixArk/Qwen3.8-27B-NVFP4 (SGLang)
#   ./switch-model.sh uncensored      # edp1096/Huihui-...-abliterated-NVFP4 (SGLang)
#   ./switch-model.sh fp8             # Qwen/Qwen3.8-27B-FP8 (SGLang)
#   ./switch-model.sh uncensored-fp8  # edp1096/Huihui-...-abliterated-FP8 (SGLang)
#   ./switch-model.sh flash           # RadixArk/Qwen3.8-Flash-Next-NVFP4 (SGLang)
#   ./switch-model.sh flash-nvda      # nvidia/Qwen3.8-Flash-Next-NVFP4, ModelOpt mixed precision
#   ./switch-model.sh flash-uncensored # the abliterated build of the same tree
#   ./switch-model.sh flash-fp4-local --fetch  # nvidia/Qwen3.8-Flash-Next-FP4-Local, the lane's
#                                     # b12x vLLM config. --fetch works everywhere; the full
#                                     # switch brings that config back where it is the installed
#                                     # one, and says MODEL_CHOICE=flash-fp4-local ./install.sh
#                                     # where the box still serves SGLang
#   ./switch-model.sh image           # Qwen/Qwen-Image-2.1 (SGLang Diffusion, own venv)
#   ./switch-model.sh image-turbo     # Qwen/Qwen-Image-2.1-Turbo, its eight-step distillation
#   ./switch-model.sh video           # MiniMaxAI/MiniMax-H3 (SGLang Diffusion, own venv)
#   ./switch-model.sh kolibri         # Aleph-Alpha/Kolibri-1, the official FP8 (vLLM + Aleph Alpha's plugin)
#   ./switch-model.sh kolibri-nvfp4   # Quantinine/Kolibri-1-NVFP4A16-FP8, its experts in NVFP4
#   ./switch-model.sh nemotron        # nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB and its drafter (vLLM nightly + b12x)
#   ./switch-model.sh nemotron-super  # nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4 and its drafter (the same image)
#   ./switch-model.sh <target> --fetch  # that target's checkpoint only, into the cache; nothing else changes
#
# Within the 27B lane (stock, uncensored, fp8, uncensored-fp8) it does what it
# always did:
#   1. downloads the checkpoint into $HF_CACHE (resumable, pinned revision);
#   2. applies the 1M YaRN config patch ONLY if the installed unit uses it;
#   3. regenerates the patched chat template from the target's own snapshot;
#   4. rewrites ONLY --model-path/--revision in the qwen38-sglang unit.
#
# Across lanes (27B <-> flash) both stacks must already be installed once
# (each by its own ./install.sh run, or a lane installed on demand prepared by
# its first Load, ./prepare-lane.sh: the switch is surgical, it does not
# download images or build overlays). It then re-verifies the checkpoint,
# regenerates the target's template, flips which unit is enabled at boot,
# and points the opencode default model at the target.
#
# The DFlash2 drafter (27B) and the NEXTN/MTP head (flash) are lossless speculative
# paths: drafts are verified against the target, quality is the target's own.
# The switch takes effect on the engine's NEXT restart: this script never starts, stops
# or restarts an engine, and prints the exact commands instead. The services around it: it
# restarts opencode-web when the limits it reads change (a switch across lanes), and, on a
# box whose proxy unit is from before v1.18.7, the proxy, the old way of moving its
# ceiling (since v1.18.7 the ceiling follows the lane that serves). For the 27B lane it
# rewrites --model-path, --revision and the KV cache line of the unit.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
die() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
# A download says its bytes every 10 s (hf-progress.py), and with no terminal on stdout (the
# cockpit's Load runs this as a job) snapshot_download's own bars go: they count files, a
# 53 GB safetensors the same as a 2 KB config, and in a job each redraw is one more line.
DL_NO_BARS=""; [ -t 1 ] || DL_NO_BARS=1

_ENV_HF_CACHE="${HF_CACHE:-}"      # before the pins below give it install.sh's default
# No target, no switch: run bare, this used to switch the box to stock, re-enabling the
# 27B lane and restarting what follows it, where the usage was all anyone wanted (found
# in review, 2026-09-24). MODEL_CHOICE= still names one, as install.sh takes it.
CHOICE="${1:-${MODEL_CHOICE:-}}"
case "$CHOICE" in stock|uncensored|fp8|uncensored-fp8|flash|flash-nvda|flash-uncensored|flash-fp4-local|image|image-turbo|video|kolibri|kolibri-nvfp4|nemotron|nemotron-super) ;; *) printf 'usage: ./switch-model.sh <stock|uncensored|fp8|uncensored-fp8|flash|flash-nvda|flash-uncensored|flash-fp4-local|image|image-turbo|video|kolibri|kolibri-nvfp4|nemotron|nemotron-super> [--fetch]\n' >&2; [ -z "$CHOICE" ] && exit 2; die "unknown target: $CHOICE" ;; esac
# --fetch: the target's checkpoint only, fetched as a switch fetches it (the cache first,
# resumable, its room checked), and nothing else: no unit, no boot, no engine, no record.
# Library's Download runs it, and a lane installed without its weights gets them so.
FETCH_ONLY=0
case "${2:-}" in
  "") ;;
  --fetch) FETCH_ONLY=1 ;;
  *) die "unknown option: $2 (the one option is --fetch)" ;;
esac
fetched(){  # $1 what: in --fetch mode the checkpoint is in the cache, and the run ends here
  [ "$FETCH_ONLY" -eq 1 ] || return 0
  printf '\n\033[1;32m── %s is in the cache; nothing else was changed (--fetch)\033[0m\n' "$1"
  exit 0
}

PINS="$(grep -E '^(IMAGE|STOCK_REPO|STOCK_REV|UNC_REPO|UNC_REV|FP8_REPO|FP8_REV|UNCFP8_REPO|UNCFP8_REV|FLASH_REPO|FLASH_REV|FLASH_NVDA_REPO|FLASH_NVDA_REV|FLASH_UNC_REPO|FLASH_UNC_REV|FLASH_LOCAL_REPO|FLASH_LOCAL_REV|FLASH_LOCAL_IMAGE|FLASH_LOCAL_SERVE_IMAGE|FLASH_IMAGE|FLASH_SERVE_IMAGE|SERVE_IMAGE|MODEL_CHOICE|HF_CACHE|CONFIG_DIR)=' "$REPO_DIR/install.sh" || true)"
# A count of matched lines was the old check, and adding a pin broke both scripts
# at once (it did, on 2026-09-08). What matters is not how many lines matched but
# whether every name this script goes on to use is defined, so that is what is
# asserted, and a failure says which one.
eval "$PINS"
for _v in IMAGE SERVE_IMAGE STOCK_REPO STOCK_REV UNC_REPO UNC_REV FP8_REPO FP8_REV UNCFP8_REPO UNCFP8_REV FLASH_REPO FLASH_REV FLASH_NVDA_REPO FLASH_NVDA_REV FLASH_UNC_REPO FLASH_UNC_REV FLASH_LOCAL_REPO FLASH_LOCAL_REV FLASH_LOCAL_IMAGE FLASH_LOCAL_SERVE_IMAGE FLASH_IMAGE FLASH_SERVE_IMAGE MODEL_CHOICE HF_CACHE CONFIG_DIR; do
  eval "[ -n \"\${$_v:-}\" ]" || die "install.sh no longer defines $_v (repo layout changed?)"
done
unset _v

IMAGE_UNIT="/etc/systemd/system/qwen38-image.service"
IMAGE_UNIT_NAME="qwen38-image.service"
# The image unit is rewritten between its two checkpoints (image, image-turbo) with sudo,
# so it is staged at a fixed path the cockpit's exact-argv sudoers allowlist names, as the
# 27B unit is below.
IMG_STAGE="$CONFIG_DIR/qwen38-image.service.switch-stage"
VIDEO_UNIT="/etc/systemd/system/qwen38-video.service"
VIDEO_UNIT_NAME="qwen38-video.service"

# The one fetch of every lane's checkpoint, run in the lane's image or venv. Checked here,
# before any branch mounts it: docker -v of a host file that is not there makes a
# root-owned directory in its place.
[ -f "$REPO_DIR/ckpt-fetch.py" ] || die "ckpt-fetch.py is missing next to this script: run it from the repository."

# Free GB under a path's filesystem, whole numbers, "" when df cannot answer. Every
# branch's room check reads through this, so it lives before the branches run.
dl_free_gb(){ { df -BG --output=avail "$(existing "${1:-$HF_CACHE}")" 2>/dev/null || true; } | tail -1 | tr -dc '0-9'; }

# The text lane the box comes back to, one record (lanes.sh says why): a switch to a side lane
# notes the SGLang text unit it takes the boot from, a switch to a text target notes its own.
# shellcheck source=lanes.sh
. "$REPO_DIR/lanes.sh"
[ "$FETCH_ONLY" -eq 1 ] || text_lane_migrate

# ── The Kolibri lane: a text lane on vLLM, switched to the way the image lane is ────
# For the proxy and its clients Kolibri is a text lane like the 27B and the flash: the same
# port, the same key. But nothing in the four hundred lines below is about it: they rewrite
# SGLang units, patch Qwen chat templates and move Qwen prompt ceilings. So it is handled
# here, whole, and exits, with the image lane's contract: verify the checkpoint (the first
# switch to a variant fetches it), name it in the unit, make it the one unit enabled at
# boot, never start or stop anything, print the exact commands.
KOLIBRI_UNIT="/etc/systemd/system/qwen38-kolibri.service"
KOLIBRI_UNIT_NAME="qwen38-kolibri.service"
# Rewritten with sudo between its two checkpoints, so staged at a fixed path the cockpit's
# exact-argv sudoers allowlist names, as the image unit is.
KOL_STAGE="$CONFIG_DIR/qwen38-kolibri.service.switch-stage"
if [ "$CHOICE" = "kolibri" ] || [ "$CHOICE" = "kolibri-nvfp4" ]; then
  [ -f "$KOLIBRI_UNIT" ] || die "the Kolibri lane is not installed on this box (no $KOLIBRI_UNIT). Install it once: ./install.sh --with-kolibri (about 102 GB)"
  # The lane's two checkpoints, their pinned revisions, memory shares and sizes, from
  # install-kolibri.sh, the one place they are declared (the shares and sizes were copied
  # here by hand, and a share tuned there came back at the next switch: found in review,
  # 2026-10-14).
  eval "$(grep -E '^KOLIBRI_(FP8|NVFP4)_(REPO|REV|GMU|GB)=' "$REPO_DIR/install-kolibri.sh" || true)"
  for _v in KOLIBRI_FP8_REPO KOLIBRI_FP8_REV KOLIBRI_FP8_GMU KOLIBRI_FP8_GB \
            KOLIBRI_NVFP4_REPO KOLIBRI_NVFP4_REV KOLIBRI_NVFP4_GMU KOLIBRI_NVFP4_GB; do
    eval "[ -n \"\${$_v:-}\" ]" || die "install-kolibri.sh no longer defines $_v (repo layout changed?)"
  done
  unset _v
  if [ "$CHOICE" = "kolibri-nvfp4" ]; then
    KOL_MODEL="$KOLIBRI_NVFP4_REPO"; KOL_REV="$KOLIBRI_NVFP4_REV"; KOL_GMU="$KOLIBRI_NVFP4_GMU"; KOL_NEED_GB="$KOLIBRI_NVFP4_GB"
  else
    KOL_MODEL="$KOLIBRI_FP8_REPO"; KOL_REV="$KOLIBRI_FP8_REV"; KOL_GMU="$KOLIBRI_FP8_GMU"; KOL_NEED_GB="$KOLIBRI_FP8_GB"
  fi
  KOL_IMAGE="$(grep -m1 -oE 'qwen38-kolibri:[^ ]+' "$KOLIBRI_UNIT" || true)"
  [ -n "$KOL_IMAGE" ] && docker image inspect "$KOL_IMAGE" >/dev/null 2>&1 \
    || die "the Kolibri lane's image (${KOL_IMAGE:-none named in its unit}) is missing: ./prepare-lane.sh kolibri builds it (no sudo; its first Load from the cockpit runs it), then switch again"
  KOL_HF="$(unit_hf_cache "$KOLIBRI_UNIT")"
  KOL_HF="${KOL_HF:-$HF_CACHE}"
  printf '\n\033[1;36m── Verifying %s @ %s in the cache (resumable)\033[0m\n' "$KOL_MODEL" "${KOL_REV:0:12}"
  KOL_TOKEN_ARGS=()
  # by name: docker takes the value from its own environment, so the token is in no argv
  if [ -n "${HF_TOKEN:-}" ]; then export HF_TOKEN; KOL_TOKEN_ARGS=(-e HF_TOKEN); fi
  KOL_PROGRESS_ARGS=()
  if [ -f "$REPO_DIR/hf-progress.py" ]; then
    KOL_PROGRESS_ARGS=(-v "$REPO_DIR/hf-progress.py:/opt/qwen38/hf-progress.py:ro")
  fi
  # In the lane's own image, as the text lanes fetch theirs in their serving image; --init
  # for the same reason as below: a cockpit job stopped past its timeout stops this too.
  # ckpt-fetch.py is the one fetch of every lane's checkpoint (it says why).
  docker run --rm --init --network host --user "$(id -u):$(id -g)" --entrypoint python3 \
    -e HF_HOME=/hf -e HF_HUB_DOWNLOAD_TIMEOUT=30 -e HF_HUB_DISABLE_XET=1 \
    -e HF_HUB_DISABLE_PROGRESS_BARS="$DL_NO_BARS" \
    "${KOL_TOKEN_ARGS[@]}" "${KOL_PROGRESS_ARGS[@]}" \
    -v "$REPO_DIR/ckpt-fetch.py:/opt/qwen38/ckpt-fetch.py:ro" \
    -v "$KOL_HF":/hf "$KOL_IMAGE" /opt/qwen38/ckpt-fetch.py "$KOL_MODEL" "$KOL_REV" --offline-first \
    --need-gb "$KOL_NEED_GB" --free-gb "$(dl_free_gb "$KOL_HF")" \
    || die "the Kolibri checkpoint could not be verified or fetched (re-run to resume; set HF_TOKEN if it stalls)"
  fetched "$KOL_MODEL @ ${KOL_REV:0:12}"
  # The unit names one checkpoint, its revision and its memory share, on one line each; a
  # switch rewrites those and nothing else.
  KOL_CUR="$(grep -m1 -oE -- "[^ ]+ --revision [0-9a-f]{40}" "$KOLIBRI_UNIT" || true)"
  KOL_CUR_GMU="$(grep -m1 -oE -- "--gpu-memory-utilization [0-9.]+" "$KOLIBRI_UNIT" || true)"
  [ -n "$KOL_CUR" ] && [ -n "$KOL_CUR_GMU" ] || die "the Kolibri unit names no checkpoint or memory share where this expects it (hand-edited unit?): re-run ./install-kolibri.sh --no-smoke"
  KOL_WANT="$KOL_MODEL --revision $KOL_REV"; KOL_WANT_GMU="--gpu-memory-utilization $KOL_GMU"
  KOL_REWRITTEN=""
  if [ "$KOL_CUR" != "$KOL_WANT" ] || [ "$KOL_CUR_GMU" != "$KOL_WANT_GMU" ]; then
    KOL_TEXT="$(cat "$KOLIBRI_UNIT")"
    KOL_TEXT="${KOL_TEXT//"$KOL_CUR"/"$KOL_WANT"}"
    printf '%s\n' "${KOL_TEXT//"$KOL_CUR_GMU"/"$KOL_WANT_GMU"}" > "$KOL_STAGE"
    if ! grep -q -- "$KOL_WANT " "$KOL_STAGE" || ! grep -q -- "$KOL_WANT_GMU " "$KOL_STAGE"; then
      rm -f "$KOL_STAGE"; die "the Kolibri unit's rewrite to $KOL_MODEL failed (hand-edited unit?): re-run ./install-kolibri.sh --no-smoke"
    fi
    diff "$KOL_STAGE" "$KOLIBRI_UNIT" || true   # show exactly what changes
    sudo install -m 644 "$KOL_STAGE" "$KOLIBRI_UNIT"
    rm -f "$KOL_STAGE"
    KOL_REWRITTEN=1
  fi
  # Which lane this box served at boot, written down before it is disabled: removing the
  # Kolibri lane later gives the boot back to it (install-kolibri.sh --uninstall).
  for TEXT_UNIT_NAME in qwen38-sglang.service qwen38-flash.service; do
    if systemctl is-enabled --quiet "$TEXT_UNIT_NAME" 2>/dev/null; then
      text_lane_note "$TEXT_UNIT_NAME"; break   # both enabled: the 27B, as install.sh follows it
    fi
  done
  # The loop variable ends in UNIT_NAME on purpose: CI checks every
  # `sudo systemctl <verb> "$..UNIT_NAME"` against the cockpit's sudoers allowlist.
  for TEXT_UNIT_NAME in qwen38-sglang.service qwen38-flash.service qwen38-image.service qwen38-video.service qwen38-nemotron.service qwen38-nemotron-super.service; do
    if systemctl is-enabled --quiet "$TEXT_UNIT_NAME" 2>/dev/null; then
      echo "disabling $TEXT_UNIT_NAME at boot (unit file kept for switching back)"
      sudo systemctl disable "$TEXT_UNIT_NAME"
    fi
  done
  sudo systemctl enable "$KOLIBRI_UNIT_NAME" >/dev/null 2>&1 || sudo systemctl enable "$KOLIBRI_UNIT_NAME"
  sudo systemctl daemon-reload
  RUNNING=""
  for u in qwen38-sglang.service qwen38-flash.service qwen38-image.service qwen38-video.service qwen38-nemotron.service qwen38-nemotron-super.service; do
    systemctl is-active --quiet "$u" 2>/dev/null && RUNNING="$u"
  done
  printf '\n\033[1;32mSwitch queued: %s (%s)\033[0m\n' "$KOL_MODEL" "$KOLIBRI_UNIT_NAME"
  # The checkpoint is the operator's choice from here, as a text lane's target (2026-10-17):
  # recorded in an installed.json that exists, erased when it is the recommended one.
  python3 "$REPO_DIR/conformity.py" choose --config-dir "$CONFIG_DIR" --repo-dir "$REPO_DIR" \
    KOLIBRI_VARIANT "$([ "$KOL_MODEL" = "$KOLIBRI_NVFP4_REPO" ] && echo nvfp4 || echo fp8)" || true
  if [ -n "$RUNNING" ]; then
    echo "Effective after:  sudo systemctl stop $RUNNING && sudo systemctl start $KOLIBRI_UNIT_NAME   (or next reboot)"
  elif systemctl is-active --quiet "$KOLIBRI_UNIT_NAME" 2>/dev/null; then
    if [ -n "$KOL_REWRITTEN" ]; then
      echo "Effective after:  sudo systemctl restart $KOLIBRI_UNIT_NAME   (it serves the previous checkpoint until then)"
    else
      echo "Its unit already named this checkpoint. Started before it did, the lane serves what it loaded: sudo systemctl restart $KOLIBRI_UNIT_NAME"
    fi
  else
    echo "Effective after:  sudo systemctl start $KOLIBRI_UNIT_NAME   (or next reboot; ready in about 10 min)"
  fi
  # opencode follows the lane, as on a text switch: the kolibri provider (added here to a
  # config written before the lane was installed) becomes the default of the generated
  # config and of the one opencode reads, and the server that reads them restarts when they
  # changed. A box installed with --no-opencode is left alone.
  if [ -f "$CONFIG_DIR/opencode.off" ]; then
    echo "opencode integration is off on this box (./install.sh --no-opencode): default model left alone"
  else
    KOL_OC_SUM="$({ cat "$CONFIG_DIR/opencode.json" "$HOME/.config/opencode/opencode.json" 2>/dev/null || true; } | sha256sum)"
    KOL_PAIR="$("$REPO_DIR/oc-limits.sh" "$CHOICE")" || KOL_PAIR=""
    KOL_NAME="$(python3 "$REPO_DIR/oc-point-default.py" --label "$CHOICE" 262144 || true)"
    KOL_PORT="$({ grep -m1 -oE 'PROXY_PORT=[0-9]+|keepalive-proxy.py [0-9]+' /etc/systemd/system/qwen38-keepalive.service 2>/dev/null || true; } | grep -oE '[0-9]+$' || true)"
    KOL_SRC="$(mktemp)"
    if [ -n "$KOL_PAIR" ] && python3 "$REPO_DIR/oc-kolibri.py" "http://127.0.0.1:${KOL_PORT:-30001}/v1" "$CONFIG_DIR/api-key" \
         "${KOL_PAIR%% *}" "$(echo "$KOL_PAIR" | cut -d' ' -f2)" "${KOL_NAME:-Kolibri-1 (local, 262K)}" > "$KOL_SRC"; then
      # The compaction block is global: sized from Kolibri's window as every text switch sizes
      # it from its own. Left as the Qwen lane wrote it (132,000 on a 27B in 1M mode), opencode
      # kept almost all of a session verbatim against Kolibri's threshold and compacted again
      # every step or two (found in review, 2026-10-14). pi's keepRecentTokens follows (pi-gen).
      KOL_KEEP="$("$REPO_DIR/oc-limits.sh" --preserve "${KOL_PAIR%% *}")" || KOL_KEEP=""
      for OC_JSON in "$CONFIG_DIR/opencode.json" "$HOME/.config/opencode/opencode.json"; do
        [ -f "$OC_JSON" ] || continue
        python3 "$REPO_DIR/oc-merge-limits.py" "$OC_JSON" --add-providers "$KOL_SRC" >/dev/null || true
        python3 "$REPO_DIR/oc-point-default.py" "$OC_JSON" kolibri "$CHOICE" 262144 \
          || echo "NOTE: could not update $OC_JSON (hand-edited?); set its \"model\" field yourself"
        [ -n "$KOL_KEEP" ] && { python3 "$REPO_DIR/oc-merge-limits.py" "$OC_JSON" --compaction "$KOL_KEEP" \
          || echo "NOTE: could not update the compaction block in $OC_JSON; check it by hand ($KOL_KEEP)"; }
      done
    fi
    rm -f "$KOL_SRC"
    if [ -d "$CONFIG_DIR/pi" ] || [ -d "$CONFIG_DIR/omp" ]; then
      python3 "$REPO_DIR/pi-gen.py" --config "$CONFIG_DIR/opencode.json" --out "$CONFIG_DIR" \
        || echo "NOTE: could not regenerate the pi/omp artifacts; run ./pi-gen.py by hand"
    fi
    if [ "$KOL_OC_SUM" != "$({ cat "$CONFIG_DIR/opencode.json" "$HOME/.config/opencode/opencode.json" 2>/dev/null || true; } | sha256sum)" ] \
       && systemctl list-unit-files opencode-web.service >/dev/null 2>&1 && systemctl is-active --quiet opencode-web.service; then
      sudo systemctl restart opencode-web.service \
        && echo "opencode-web.service restarted so it reads the new default" \
        || echo "NOTE: could not restart opencode-web.service; restart it by hand"
    fi
  fi
  echo "Clients reach it through the proxy on :30001 with the box's key, as model kolibri-1."
  echo "Switch back:      ./switch-model.sh stock   (or any other target)"
  exit 0
fi

# ── The Nemotron lanes: text lanes on vLLM, switched to the way the Kolibri lane is ──────
# Bolt (nemotron) and Super VL (nemotron-super), each its own unit and installer and one image for
# the two, under the same contract: verify the checkpoints (the model and its drafter; the first
# switch fetches them), name them in the unit with the memory share its installer pins, make it the
# one unit enabled at boot, never start or stop anything, print the exact commands.
NEMOTRON_UNIT="/etc/systemd/system/qwen38-nemotron.service"
NEMOTRON_UNIT_NAME="qwen38-nemotron.service"
NEMOTRON_SUPER_UNIT="/etc/systemd/system/qwen38-nemotron-super.service"
NEMOTRON_SUPER_UNIT_NAME="qwen38-nemotron-super.service"
# Rewritten with sudo when its pins moved, so staged at a fixed path the cockpit's exact-argv
# sudoers allowlist names, as the Kolibri unit is.
NEM_STAGE="$CONFIG_DIR/qwen38-nemotron.service.switch-stage"
NSV_STAGE="$CONFIG_DIR/qwen38-nemotron-super.service.switch-stage"
if [ "$CHOICE" = "nemotron" ] || [ "$CHOICE" = "nemotron-super" ]; then
  if [ "$CHOICE" = "nemotron" ]; then
    NEM_UNIT="$NEMOTRON_UNIT"; NEM_UNIT_NAME="$NEMOTRON_UNIT_NAME"; NEM_LANE_STAGE="$NEM_STAGE"
    NEM_INSTALLER=install-nemotron.sh; NEM_PIN=NEMOTRON_BOLT; NEM_TITLE="Nemotron 3.5 Bolt"
    NEM_SERVED=nemotron-3.5-bolt; NEM_WINDOW=262144; NEM_SIZE=51; NEM_BOOT="about 8 min"
    # Bolt loads its reasoning parser from its checkpoint's snapshot: its unit names that snapshot
    NEM_NEEDS_SNAP=1
  else
    NEM_UNIT="$NEMOTRON_SUPER_UNIT"; NEM_UNIT_NAME="$NEMOTRON_SUPER_UNIT_NAME"; NEM_LANE_STAGE="$NSV_STAGE"
    NEM_INSTALLER=install-nemotron-super.sh; NEM_PIN=NEMOTRON_SUPER; NEM_TITLE="Nemotron 3.5 Super VL"
    NEM_SERVED=nemotron-3.5-super-vl; NEM_WINDOW=1048576; NEM_SIZE=102; NEM_BOOT="about 11 min"
    NEM_NEEDS_SNAP=0
  fi
  [ -f "$NEM_UNIT" ] || die "the $NEM_TITLE lane is not installed on this box (no $NEM_UNIT). Install it once: ./install.sh --with-$CHOICE (about $NEM_SIZE GB)"
  # The checkpoint, its drafter, their pinned revisions, the memory share and the size, from the
  # lane's installer, the one place they are declared.
  eval "$(grep -E "^${NEM_PIN}_(REPO|REV|DRAFT_REPO|DRAFT_REV|GMU|GB)=" "$REPO_DIR/$NEM_INSTALLER" || true)"
  for _v in REPO REV DRAFT_REPO DRAFT_REV GMU GB; do
    eval "[ -n \"\${${NEM_PIN}_$_v:-}\" ]" || die "$NEM_INSTALLER no longer defines ${NEM_PIN}_$_v (repo layout changed?)"
  done
  unset _v
  eval "NEM_MODEL=\"\$${NEM_PIN}_REPO\"; NEM_REV=\"\$${NEM_PIN}_REV\"; NEM_GMU=\"\$${NEM_PIN}_GMU\"; NEM_NEED_GB=\"\$${NEM_PIN}_GB\""
  eval "NEM_DRAFT=\"\$${NEM_PIN}_DRAFT_REPO\"; NEM_DREV=\"\$${NEM_PIN}_DRAFT_REV\""
  # the two lanes run the one image the Bolt lane builds (install-nemotron-super.sh says why)
  NEM_IMAGE="$(grep -m1 -oE 'qwen38-nemotron:[^ ]+' "$NEM_UNIT" || true)"
  [ -n "$NEM_IMAGE" ] && docker image inspect "$NEM_IMAGE" >/dev/null 2>&1 \
    || die "the $NEM_TITLE lane's image (${NEM_IMAGE:-none named in its unit}) is missing: ./prepare-lane.sh $CHOICE builds it (no sudo; its first Load from the cockpit runs it), then switch again"
  NEM_HF="$(unit_hf_cache "$NEM_UNIT")"
  NEM_HF="${NEM_HF:-$HF_CACHE}"
  NEM_TOKEN_ARGS=()
  # by name: docker takes the value from its own environment, so the token is in no argv
  if [ -n "${HF_TOKEN:-}" ]; then export HF_TOKEN; NEM_TOKEN_ARGS=(-e HF_TOKEN); fi
  NEM_PROGRESS_ARGS=()
  if [ -f "$REPO_DIR/hf-progress.py" ]; then
    NEM_PROGRESS_ARGS=(-v "$REPO_DIR/hf-progress.py:/opt/qwen38/hf-progress.py:ro")
  fi
  # In the lane's own image, as the text lanes fetch theirs in their serving image; --init so
  # that a cockpit job stopped past its timeout stops this too. ckpt-fetch.py is the one fetch
  # of every lane's checkpoint (it says why). The room asked for is the pair's, given to the
  # first fetch, which the second then needs no more than its own share of.
  NEM_ASK="$NEM_NEED_GB"
  for NEM_CKPT in "$NEM_MODEL $NEM_REV" "$NEM_DRAFT $NEM_DREV"; do
    NEM_R="${NEM_CKPT%% *}"; NEM_V="${NEM_CKPT##* }"
    printf '\n\033[1;36m── Verifying %s @ %s in the cache (resumable)\033[0m\n' "$NEM_R" "${NEM_V:0:12}"
    docker run --rm --init --network host --user "$(id -u):$(id -g)" --entrypoint python3 \
      -e HF_HOME=/hf -e HF_HUB_DOWNLOAD_TIMEOUT=30 -e HF_HUB_DISABLE_XET=1 \
      -e HF_HUB_DISABLE_PROGRESS_BARS="$DL_NO_BARS" \
      "${NEM_TOKEN_ARGS[@]}" "${NEM_PROGRESS_ARGS[@]}" \
      -v "$REPO_DIR/ckpt-fetch.py:/opt/qwen38/ckpt-fetch.py:ro" \
      -v "$NEM_HF":/hf "$NEM_IMAGE" /opt/qwen38/ckpt-fetch.py "$NEM_R" "$NEM_V" --offline-first \
      --need-gb "$NEM_ASK" --free-gb "$(dl_free_gb "$NEM_HF")" \
      || die "the $NEM_TITLE checkpoint $NEM_R could not be verified or fetched (re-run to resume; set HF_TOKEN if it stalls)"
    NEM_ASK=1
  done
  # --fetch ends once both are in the cache: the lane loads the drafter with the model
  fetched "$NEM_MODEL @ ${NEM_REV:0:12}, with its drafter,"
  # The unit names the checkpoint's revision after the checkpoint (and Bolt's again, in the path of
  # the reasoning parser in that snapshot), the drafter and its revision, and the memory share: a
  # switch after the pins moved rewrites each of them. It rewrote the first and the share only, and
  # the engine would have started the new weights with the old snapshot's parser and a drafter this
  # switch had not checked (found in review, 2026-10-16).
  NEM_CUR="$(grep -m1 -oE -- "[^ ]+ --revision [0-9a-f]{40}" "$NEM_UNIT" || true)"
  NEM_CUR_GMU="$(grep -m1 -oE -- "--gpu-memory-utilization [0-9.]+" "$NEM_UNIT" || true)"
  # The requests at a time, for the lane whose installer pins them (Super VL's: its memory share was
  # measured with them), rewritten with the share: the two move together. The switch rewrote the
  # share alone, a pairing nobody had measured (found in review, 2026-10-16). Bolt's are its
  # template's own, as it renders them.
  NEM_SEQS="$({ grep -m1 -oE "^${NEM_PIN}_SEQS=\"?[0-9]+" "$REPO_DIR/$NEM_INSTALLER" || true; } | grep -oE '[0-9]+$' || true)"
  NEM_CUR_SEQS="$(grep -m1 -oE -- "--max-num-seqs [0-9]+" "$NEM_UNIT" || true)"
  NEM_WANT_SEQS="$NEM_CUR_SEQS"; [ -n "$NEM_SEQS" ] && NEM_WANT_SEQS="--max-num-seqs $NEM_SEQS"
  NEM_CUR_D="$(grep -m1 -oE -- "--speculative-config.model [^ ]+" "$NEM_UNIT" || true)"
  NEM_CUR_DREV="$(grep -m1 -oE -- "--speculative-config.revision [0-9a-f]{40}" "$NEM_UNIT" || true)"
  NEM_CUR_REPO="${NEM_CUR%% *}"; NEM_CUR_SNAP="models--${NEM_CUR_REPO//\//--}/snapshots/${NEM_CUR##* }/"
  # Bolt's unit must name its revision's snapshot (its parser's path), as before the block was the
  # two lanes': made optional for both, a hand-edited path was left behind the revision rewritten
  # around it, and the engine would have started offline on a parser not there (found in review,
  # 2026-10-16).
  [ -n "$NEM_CUR" ] && [ -n "$NEM_CUR_GMU" ] && [ -n "$NEM_CUR_D" ] && [ -n "$NEM_CUR_DREV" ] \
    && { [ "$NEM_NEEDS_SNAP" -eq 0 ] || grep -qF -- "$NEM_CUR_SNAP" "$NEM_UNIT"; } \
    && { [ -z "$NEM_SEQS" ] || [ -n "$NEM_CUR_SEQS" ]; } \
    || die "the $NEM_TITLE unit names no checkpoint, drafter or memory share where this expects it (hand-edited unit?): re-run ./$NEM_INSTALLER --no-smoke"
  NEM_WANT="$NEM_MODEL --revision $NEM_REV"; NEM_WANT_GMU="--gpu-memory-utilization $NEM_GMU"
  NEM_WANT_D="--speculative-config.model $NEM_DRAFT"; NEM_WANT_DREV="--speculative-config.revision $NEM_DREV"
  NEM_WANT_SNAP="models--${NEM_MODEL//\//--}/snapshots/$NEM_REV/"
  NEM_REWRITTEN=""
  if [ "$NEM_CUR|$NEM_CUR_GMU|$NEM_CUR_D|$NEM_CUR_DREV|$NEM_CUR_SEQS" != "$NEM_WANT|$NEM_WANT_GMU|$NEM_WANT_D|$NEM_WANT_DREV|$NEM_WANT_SEQS" ]; then
    NEM_TEXT="$(cat "$NEM_UNIT")"
    NEM_TEXT="${NEM_TEXT//"$NEM_CUR"/"$NEM_WANT"}"
    NEM_TEXT="${NEM_TEXT//"$NEM_CUR_SNAP"/"$NEM_WANT_SNAP"}"
    NEM_TEXT="${NEM_TEXT//"$NEM_CUR_D "/"$NEM_WANT_D "}"
    NEM_TEXT="${NEM_TEXT//"$NEM_CUR_DREV"/"$NEM_WANT_DREV"}"
    [ -n "$NEM_CUR_SEQS" ] && NEM_TEXT="${NEM_TEXT//"$NEM_CUR_SEQS "/"$NEM_WANT_SEQS "}"
    printf '%s\n' "${NEM_TEXT//"$NEM_CUR_GMU"/"$NEM_WANT_GMU"}" > "$NEM_LANE_STAGE"
    if ! grep -qF -- "$NEM_WANT " "$NEM_LANE_STAGE" || ! grep -qF -- "$NEM_WANT_GMU " "$NEM_LANE_STAGE" \
       || ! grep -qF -- "$NEM_WANT_D " "$NEM_LANE_STAGE" || ! grep -qF -- "$NEM_WANT_DREV " "$NEM_LANE_STAGE" \
       || { [ "$NEM_NEEDS_SNAP" -eq 1 ] && ! grep -qF -- "$NEM_WANT_SNAP" "$NEM_LANE_STAGE"; } \
       || { [ -n "$NEM_SEQS" ] && ! grep -qF -- "$NEM_WANT_SEQS " "$NEM_LANE_STAGE"; }; then
      rm -f "$NEM_LANE_STAGE"; die "the $NEM_TITLE unit's rewrite to $NEM_MODEL failed (hand-edited unit?): re-run ./$NEM_INSTALLER --no-smoke"
    fi
    diff "$NEM_LANE_STAGE" "$NEM_UNIT" || true   # show exactly what changes
    # one exact command per lane, as the cockpit's sudoers allowlist names it
    if [ "$CHOICE" = "nemotron" ]; then
      sudo install -m 644 "$NEM_STAGE" "$NEMOTRON_UNIT"
    else
      sudo install -m 644 "$NSV_STAGE" "$NEMOTRON_SUPER_UNIT"
    fi
    rm -f "$NEM_LANE_STAGE"
    NEM_REWRITTEN=1
  fi
  # Which lane this box served at boot, written down before it is disabled: removing the
  # Nemotron lane later gives the boot back to it (its installer's --uninstall).
  for TEXT_UNIT_NAME in qwen38-sglang.service qwen38-flash.service; do
    if systemctl is-enabled --quiet "$TEXT_UNIT_NAME" 2>/dev/null; then
      text_lane_note "$TEXT_UNIT_NAME"; break   # both enabled: the 27B, as install.sh follows it
    fi
  done
  # The loop variable ends in UNIT_NAME on purpose: CI checks every
  # `sudo systemctl <verb> "$..UNIT_NAME"` against the cockpit's sudoers allowlist.
  for TEXT_UNIT_NAME in qwen38-sglang.service qwen38-flash.service qwen38-image.service qwen38-video.service qwen38-kolibri.service qwen38-nemotron.service qwen38-nemotron-super.service; do
    [ "$TEXT_UNIT_NAME" = "$NEM_UNIT_NAME" ] && continue
    if systemctl is-enabled --quiet "$TEXT_UNIT_NAME" 2>/dev/null; then
      echo "disabling $TEXT_UNIT_NAME at boot (unit file kept for switching back)"
      sudo systemctl disable "$TEXT_UNIT_NAME"
    fi
  done
  if [ "$CHOICE" = "nemotron" ]; then
    sudo systemctl enable "$NEMOTRON_UNIT_NAME" >/dev/null 2>&1 || sudo systemctl enable "$NEMOTRON_UNIT_NAME"
  else
    sudo systemctl enable "$NEMOTRON_SUPER_UNIT_NAME" >/dev/null 2>&1 || sudo systemctl enable "$NEMOTRON_SUPER_UNIT_NAME"
  fi
  sudo systemctl daemon-reload
  RUNNING=""
  for u in qwen38-sglang.service qwen38-flash.service qwen38-image.service qwen38-video.service qwen38-kolibri.service qwen38-nemotron.service qwen38-nemotron-super.service; do
    [ "$u" = "$NEM_UNIT_NAME" ] && continue
    systemctl is-active --quiet "$u" 2>/dev/null && RUNNING="$u"
  done
  printf '\n\033[1;32mSwitch queued: %s (%s)\033[0m\n' "$NEM_MODEL" "$NEM_UNIT_NAME"
  if [ -n "$RUNNING" ]; then
    echo "Effective after:  sudo systemctl stop $RUNNING && sudo systemctl start $NEM_UNIT_NAME   (or next reboot)"
  elif systemctl is-active --quiet "$NEM_UNIT_NAME" 2>/dev/null; then
    if [ -n "$NEM_REWRITTEN" ]; then
      echo "Effective after:  sudo systemctl restart $NEM_UNIT_NAME   (it serves the previous unit until then)"
    else
      echo "Its unit already named this checkpoint. Started before it did, the lane serves what it loaded: sudo systemctl restart $NEM_UNIT_NAME"
    fi
  else
    echo "Effective after:  sudo systemctl start $NEM_UNIT_NAME   (or next reboot; ready in $NEM_BOOT)"
  fi
  # opencode follows the lane, as on a text switch: the lane's provider becomes the default of
  # the generated config and of the one opencode reads, and the server that reads them restarts
  # when they changed. A box installed with --no-opencode is left alone.
  if [ -f "$CONFIG_DIR/opencode.off" ]; then
    echo "opencode integration is off on this box (./install.sh --no-opencode): default model left alone"
  else
    NEM_OC_SUM="$({ cat "$CONFIG_DIR/opencode.json" "$HOME/.config/opencode/opencode.json" 2>/dev/null || true; } | sha256sum)"
    NEM_PAIR="$("$REPO_DIR/oc-limits.sh" "$CHOICE")" || NEM_PAIR=""
    NEM_NAME="$(python3 "$REPO_DIR/oc-point-default.py" --label "$CHOICE" "$NEM_WINDOW" || true)"
    NEM_PORT="$({ grep -m1 -oE 'PROXY_PORT=[0-9]+|keepalive-proxy.py [0-9]+' /etc/systemd/system/qwen38-keepalive.service 2>/dev/null || true; } | grep -oE '[0-9]+$' || true)"
    NEM_SRC="$(mktemp)"
    if [ -n "$NEM_PAIR" ] && python3 "$REPO_DIR/oc-nemotron.py" "http://127.0.0.1:${NEM_PORT:-30001}/v1" "$CONFIG_DIR/api-key" \
         "${NEM_PAIR%% *}" "$(echo "$NEM_PAIR" | cut -d' ' -f2)" "${NEM_NAME:-$NEM_TITLE (local)}" "$CHOICE" > "$NEM_SRC"; then
      # The compaction block is global: sized from this lane's window as every text switch sizes
      # it from its own. pi's keepRecentTokens follows (pi-gen).
      NEM_KEEP="$("$REPO_DIR/oc-limits.sh" --preserve "${NEM_PAIR%% *}")" || NEM_KEEP=""
      for OC_JSON in "$CONFIG_DIR/opencode.json" "$HOME/.config/opencode/opencode.json"; do
        [ -f "$OC_JSON" ] || continue
        python3 "$REPO_DIR/oc-merge-limits.py" "$OC_JSON" --add-providers "$NEM_SRC" >/dev/null || true
        python3 "$REPO_DIR/oc-point-default.py" "$OC_JSON" "$CHOICE" "$CHOICE" "$NEM_WINDOW" \
          || echo "NOTE: could not update $OC_JSON (hand-edited?); set its \"model\" field yourself"
        [ -n "$NEM_KEEP" ] && { python3 "$REPO_DIR/oc-merge-limits.py" "$OC_JSON" --compaction "$NEM_KEEP" \
          || echo "NOTE: could not update the compaction block in $OC_JSON; check it by hand ($NEM_KEEP)"; }
      done
    fi
    rm -f "$NEM_SRC"
    if [ -d "$CONFIG_DIR/pi" ] || [ -d "$CONFIG_DIR/omp" ]; then
      python3 "$REPO_DIR/pi-gen.py" --config "$CONFIG_DIR/opencode.json" --out "$CONFIG_DIR" \
        || echo "NOTE: could not regenerate the pi/omp artifacts; run ./pi-gen.py by hand"
    fi
    if [ "$NEM_OC_SUM" != "$({ cat "$CONFIG_DIR/opencode.json" "$HOME/.config/opencode/opencode.json" 2>/dev/null || true; } | sha256sum)" ] \
       && systemctl list-unit-files opencode-web.service >/dev/null 2>&1 && systemctl is-active --quiet opencode-web.service; then
      sudo systemctl restart opencode-web.service \
        && echo "opencode-web.service restarted so it reads the new default" \
        || echo "NOTE: could not restart opencode-web.service; restart it by hand"
    fi
  fi
  echo "Clients reach it through the proxy on :30001 with the box's key, as model $NEM_SERVED."
  echo "Switch back:      ./switch-model.sh stock   (or any other target)"
  exit 0
fi

# ── The image lane: a third lane, switched to the same way as the other two ─────
# Same contract as a cross-lane switch: verify the checkpoint, make it the one unit
# enabled at boot, never start or stop anything, print the exact commands. What it
# does NOT share with the text lanes is everything that is about text: there is no
# serving image to inspect (it runs from its own venv), nothing in the unit to rewrite but
# its --model-path (between the lane's checkpoints), no prompt ceiling on the proxy (the
# proxy is a text door and nothing reaches this lane through it), and no opencode default
# to move (opencode is a text client, and pointing it at an image lane would break every
# session it opens). So it is handled here, whole, and exits, instead of threading
# "unless image" through four hundred lines written for SGLang's text server.
if [ "$CHOICE" = "image" ] || [ "$CHOICE" = "image-turbo" ]; then
  [ -f "$IMAGE_UNIT" ] || die "the image lane is not installed on this box (no $IMAGE_UNIT). Install it once: ./install.sh --with-image (46 GB)"
  IMG_WD="$(grep -m1 -E '^WorkingDirectory=' "$IMAGE_UNIT" | cut -d= -f2- || true)"
  IMG_PY="${IMG_WD:-$HOME/.local/share/qwen38-image}/venv/bin/python"
  [ -x "$IMG_PY" ] || die "the image lane's runtime is missing ($IMG_PY): ./prepare-lane.sh image builds it (no sudo; its first Load from the cockpit runs it), then switch again"
  # The lane's two checkpoints and their pinned revisions, from install-image.sh, the one
  # place they are declared.
  eval "$(grep -E '^(IMAGE_MODEL_PIN|IMAGE_MODEL_PIN_REV|IMAGE_TURBO_PIN|IMAGE_TURBO_PIN_REV)=' "$REPO_DIR/install-image.sh" || true)"
  for _v in IMAGE_MODEL_PIN IMAGE_MODEL_PIN_REV IMAGE_TURBO_PIN IMAGE_TURBO_PIN_REV; do
    eval "[ -n \"\${$_v:-}\" ]" || die "install-image.sh no longer defines $_v (repo layout changed?)"
  done
  unset _v
  IMG_CUR="$(grep -oE -- '--model-path [^ ]+' "$IMAGE_UNIT" | head -1 | cut -d' ' -f2 || true)"
  IMG_CUR="${IMG_CUR:-$IMAGE_MODEL_PIN}"
  # image-turbo serves the Turbo; image serves the base, or the checkpoint the unit was
  # installed with (IMAGE_MODEL=) when it is neither, written down on the way to the Turbo
  # so that the way back finds it.
  IMG_BEFORE_TURBO="$CONFIG_DIR/image-model-before-turbo"
  if [ "$CHOICE" = "image-turbo" ]; then
    IMG_MODEL="$IMAGE_TURBO_PIN"
  elif [ "$IMG_CUR" = "$IMAGE_TURBO_PIN" ]; then
    IMG_MODEL="$(cat "$IMG_BEFORE_TURBO" 2>/dev/null || true)"; IMG_MODEL="${IMG_MODEL:-$IMAGE_MODEL_PIN}"
  else
    IMG_MODEL="$IMG_CUR"
  fi
  # The revision this box serves of that checkpoint, as the video lane reads it: the unit
  # names the repo with HF_HUB_OFFLINE=1, so its refs/main IS the served commit, and one an
  # install made with IMAGE_MODEL_REV= stays served. The installer's pin answers when the ref
  # is missing or holds no commit, and main for a checkpoint of the unit's own.
  IMG_HF="$(grep -m1 -E '^Environment=HF_HOME=' "$IMAGE_UNIT" | cut -d= -f3- || true)"
  IMG_MODEL_DIR="${IMG_HF:-$HF_CACHE}/hub/models--${IMG_MODEL//\//--}"
  IMG_REV=""
  if [ -f "$IMG_MODEL_DIR/refs/main" ]; then IMG_REV="$(cat "$IMG_MODEL_DIR/refs/main")"; fi
  [[ "$IMG_REV" =~ ^[0-9a-f]{40}$ ]] || IMG_REV=""
  if [ -z "$IMG_REV" ]; then
    case "$IMG_MODEL" in
      "$IMAGE_MODEL_PIN") IMG_REV="$IMAGE_MODEL_PIN_REV" ;;
      "$IMAGE_TURBO_PIN") IMG_REV="$IMAGE_TURBO_PIN_REV" ;;
      *) IMG_REV=main ;;
    esac
  fi
  if [ "$IMG_MODEL" = "$IMAGE_TURBO_PIN" ]; then
    # The Turbo samples on the eight-step grid its model_index.json carries, which only a
    # runtime with sgl-project/sglang#43391 follows; one without it loads the weights and
    # samples on a uniform schedule instead, answering 200 with worse images.
    IMG_WHY="$("$IMG_PY" "$REPO_DIR/image-sglang/turbo-grid-check.py" 2>&1)" \
      || die "the image lane's runtime cannot sample on the Turbo's eight-step grid (${IMG_WHY:-no reason given}). Update it first: ./install.sh (or ./install-image.sh), then switch again."
  fi
  printf '\n\033[1;36m── Verifying %s @ %s in the cache (resumable)\033[0m\n' "$IMG_MODEL" "$IMG_REV"
  # The same two lessons the text lanes learned: Xet stalls on this box, and an
  # unauthenticated pull gets throttled. ckpt-fetch.py, the one fetch of every lane: the
  # cache first, so a complete one answers without touching the network at all; both
  # checkpoints of this lane are about 33 GB, refused before the first byte when the room
  # is not there. A revision is fetched as such and refs/main pointed at it, because the
  # unit runs offline by name, which resolves refs/main: fetched at main, a push upstream
  # would have changed what the lane serves (the same lesson as install-image.sh, 2026-09-24).
  HF_HOME="${IMG_HF:-$HF_CACHE}" HF_HUB_DOWNLOAD_TIMEOUT=30 HF_HUB_DISABLE_XET=1 \
    HF_HUB_DISABLE_PROGRESS_BARS="$DL_NO_BARS" \
    "$IMG_PY" "$REPO_DIR/ckpt-fetch.py" "$IMG_MODEL" "$IMG_REV" --offline-first \
    --need-gb 34 --free-gb "$(dl_free_gb "${IMG_HF:-$HF_CACHE}")" --refs-main pin \
    || die "the image checkpoint could not be verified or fetched (re-run to resume; set HF_TOKEN if it stalls)"
  fetched "$IMG_MODEL @ ${IMG_REV:0:12}"
  # The unit names one checkpoint; a switch between the lane's two rewrites that name and
  # nothing else.
  IMG_REWRITTEN=""
  if [ "$IMG_CUR" != "$IMG_MODEL" ]; then
    IMG_UNIT_TEXT="$(cat "$IMAGE_UNIT")"
    printf '%s\n' "${IMG_UNIT_TEXT//"--model-path $IMG_CUR "/"--model-path $IMG_MODEL "}" > "$IMG_STAGE"
    if ! grep -q -- "--model-path $IMG_MODEL " "$IMG_STAGE"; then
      rm -f "$IMG_STAGE"; die "the image unit's rewrite to $IMG_MODEL failed (hand-edited unit?): re-run ./install-image.sh"
    fi
    diff "$IMG_STAGE" "$IMAGE_UNIT" || true   # show exactly what changes
    sudo install -m 644 "$IMG_STAGE" "$IMAGE_UNIT"
    rm -f "$IMG_STAGE"
    IMG_REWRITTEN=1
    if [ "$IMG_MODEL" = "$IMAGE_TURBO_PIN" ] && [ "$IMG_CUR" != "$IMAGE_MODEL_PIN" ]; then
      printf '%s\n' "$IMG_CUR" > "$IMG_BEFORE_TURBO"
    else
      rm -f "$IMG_BEFORE_TURBO"
    fi
  fi
  # Which text lane this box served, written down before it is disabled: install.sh
  # updates that lane on a later run, and enablement cannot tell it once both are off.
  for TEXT_UNIT_NAME in qwen38-sglang.service qwen38-flash.service; do
    if systemctl is-enabled --quiet "$TEXT_UNIT_NAME" 2>/dev/null; then
      text_lane_note "$TEXT_UNIT_NAME"; break   # both enabled: the 27B, as install.sh follows it
    fi
  done
  # The loop variable ends in UNIT_NAME on purpose: CI checks every
  # `sudo systemctl <verb> "$..UNIT_NAME"` against the cockpit's sudoers allowlist,
  # and a variable called $u would have walked straight past it.
  for TEXT_UNIT_NAME in qwen38-sglang.service qwen38-flash.service qwen38-video.service qwen38-kolibri.service qwen38-nemotron.service qwen38-nemotron-super.service; do
    if systemctl is-enabled --quiet "$TEXT_UNIT_NAME" 2>/dev/null; then
      echo "disabling $TEXT_UNIT_NAME at boot (unit file kept for switching back)"
      sudo systemctl disable "$TEXT_UNIT_NAME"
    fi
  done
  sudo systemctl enable "$IMAGE_UNIT_NAME" >/dev/null 2>&1 || sudo systemctl enable "$IMAGE_UNIT_NAME"
  sudo systemctl daemon-reload
  RUNNING=""
  for u in qwen38-sglang.service qwen38-flash.service qwen38-video.service qwen38-kolibri.service qwen38-nemotron.service qwen38-nemotron-super.service; do
    systemctl is-active --quiet "$u" 2>/dev/null && RUNNING="$u"
  done
  printf '\n\033[1;32mSwitch queued: %s (%s)\033[0m\n' "$IMG_MODEL" "$IMAGE_UNIT_NAME"
  # Its checkpoint is the operator's choice from here, as Kolibri's (2026-10-17).
  python3 "$REPO_DIR/conformity.py" choose --config-dir "$CONFIG_DIR" --repo-dir "$REPO_DIR" \
    IMAGE_MODEL "$IMG_MODEL" || true
  if [ -n "$RUNNING" ]; then
    echo "Effective after:  sudo systemctl stop $RUNNING && sudo systemctl start $IMAGE_UNIT_NAME   (or next reboot)"
  elif systemctl is-active --quiet "$IMAGE_UNIT_NAME" 2>/dev/null; then
    if [ -n "$IMG_REWRITTEN" ]; then
      echo "Effective after:  sudo systemctl restart $IMAGE_UNIT_NAME   (it serves the previous checkpoint until then)"
    else
      echo "Its unit already named this checkpoint. Started before it did, the lane serves what it loaded: sudo systemctl restart $IMAGE_UNIT_NAME"
    fi
  else
    echo "Effective after:  sudo systemctl start $IMAGE_UNIT_NAME   (or next reboot; ready in about 70 s)"
  fi
  echo "The proxy on :30001 and opencode are text clients and were left as they are."
  echo "Switch back:      ./switch-model.sh stock   (or any text target)"
  exit 0
fi

# ── The video lane: a fourth lane, switched to the same way as the image one ───
# Same contract as the image lane: verify the checkpoint, make it the one unit
# enabled at boot, never start or stop anything, print the exact commands. Like the
# image lane it shares nothing with the text lanes: no serving image to inspect (it
# runs from its own venv), no unit to rewrite (one checkpoint, one variant), no
# prompt ceiling on the proxy (the proxy is a text door and nothing reaches this
# lane through it), and no opencode default to move (opencode is a text client).
# Handled here, whole, and exits.
if [ "$CHOICE" = "video" ]; then
  [ -f "$VIDEO_UNIT" ] || die "the video lane is not installed on this box (no $VIDEO_UNIT). Install it once: ./install.sh --with-video (about 174 GB of headroom)"
  VID_WD="$(grep -m1 -E '^WorkingDirectory=' "$VIDEO_UNIT" | cut -d= -f2- || true)"
  VID_PY="${VID_WD:-$HOME/.local/share/qwen38-video}/venv/bin/python"
  [ -x "$VID_PY" ] || die "the video lane's runtime is missing ($VID_PY): ./prepare-lane.sh video builds it (no sudo; its first Load from the cockpit runs it), then switch again"
  VID_MODEL="$(grep -oE -- '--model-path [^ ]+' "$VIDEO_UNIT" | head -1 | cut -d' ' -f2 || true)"
  VID_MODEL="${VID_MODEL:-MiniMaxAI/MiniMax-H3}"
  VID_HF="$(grep -m1 -E '^Environment=HF_HOME=' "$VIDEO_UNIT" | cut -d= -f3- || true)"
  printf '\n\033[1;36m── Verifying %s in the cache (resumable)\033[0m\n' "$VID_MODEL"
  # Only the partition the installed unit serves is verified and fetched, by the same
  # allowlist as install-video.sh: a complete fl2va cache answers without touching the
  # network, and without it a switch re-downloaded the whole repo (Ref2VA included)
  # onto a full disk (reference box, 2026-09-26).
  VID_VARIANT="$(grep -oE -- '--model-variant [^ ]+' "$VIDEO_UNIT" | head -1 | cut -d' ' -f2 || true)"
  VID_VARIANT="${VID_VARIANT:-fl2va}"
  if [ "$VID_MODEL" = "MiniMaxAI/MiniMax-H3" ]; then
    if [ "$VID_VARIANT" = "fl2va" ]; then VID_ALLOW="FL2VA/* model_index.json modular_model_index.json scheduler/* audio_scheduler/* *.md LICENSE*"
    else VID_ALLOW="Ref2VA/* model_index.json modular_model_index.json scheduler/* audio_scheduler/* *.md LICENSE*"; fi
  else VID_ALLOW="";
  fi
  # Xet stalls on this box, and an unauthenticated pull gets throttled.
  # Fetch the revision this box actually serves, never the upstream main tip: the
  # unit names the repo with HF_HUB_OFFLINE=1, so refs/main IS the served commit.
  # A download without revision resolves main over the network, writes refs/main
  # at the tip BEFORE the first byte is downloaded (huggingface_hub writes the ref
  # before fetching: _snapshot_download.py), and resumes that half-fetched tip on
  # the next boot attempt: a switched-off pin, ~145 GB of another commit, a dead
  # offline unit. refs/main says what to fetch; the installer's pin answers when
  # the ref is missing (a download by sha wrote no ref).
  VID_MODEL_DIR="${VID_HF:-$HF_CACHE}/hub/models--${VID_MODEL//\//--}"
  VID_REV=""
  if [ -f "$VID_MODEL_DIR/refs/main" ]; then VID_REV="$(cat "$VID_MODEL_DIR/refs/main")"; fi
  [[ "$VID_REV" =~ ^[0-9a-f]{40}$ ]] || VID_REV=""
  if [ -z "$VID_REV" ]; then
    if [ "$VID_MODEL" = "MiniMaxAI/MiniMax-H3" ]; then
      VID_REV="$(grep -m1 -oE '^VIDEO_MODEL_PIN_REV="[0-9a-f]{40}"' "$REPO_DIR/install-video.sh" | cut -d'"' -f2 || true)"
    else VID_REV="main"; fi
  fi
  [ -n "$VID_REV" ] || die "no refs/main under $VID_MODEL_DIR and install-video.sh no longer defines its pin: re-run ./install-video.sh, it resumes"
  # Room first, the law the text branch learned on 2026-09-24 and this branch
  # skipped: the cockpit's Load journey runs this under a serving engine, and a
  # 145 GB weights fetch onto a fuller disk is a failure of everything on it
  # (found in the branch's reviews, 2026-09-28). What the cache already holds
  # comes off the need, so a complete cache (this box's normal state) needs only
  # the floor and downloads nothing; the number is install-video.sh's own.
  VID_WANT_GB="$(grep -m1 -oE '^WEIGHTS_GB=[0-9]+' "$REPO_DIR/install-video.sh" | cut -d= -f2 || true)"
  VID_WANT_GB="${VID_WANT_GB:-145}"
  VID_HAVE_B="$({ du -s --apparent-size -B1 "$VID_MODEL_DIR/blobs" 2>/dev/null || true; } | cut -f1)"
  VID_WANT_GB=$((VID_WANT_GB - ${VID_HAVE_B:-0} / 1073741824)); [ "$VID_WANT_GB" -ge 10 ] || VID_WANT_GB=10
  # The two caches the runtime writes at its first start (lanes.sh), with the checkpoint on the
  # disk they share and on their own otherwise: a first Load's preparation counts them with the
  # runtime, before the checkpoint, and this counted the checkpoint alone, so a disk with room
  # for each part passed both and filled up at the first start (found in review, 2026-10-15).
  dev_of(){ stat -L -c %d "$(existing "$1")"; }
  VID_DEV="$(dev_of "${VID_HF:-$HF_CACHE}")"; VID_WHAT="about $VID_WANT_GB GB for the checkpoint"
  declare -A C_NEED=() C_AT=() C_DIRS=() C_WHAT_ON=()
  while IFS='|' read -r C_DIR C_GB C_WHAT; do
    [ "$C_GB" -gt 0 ] || continue
    C_DEV="$(dev_of "$C_DIR")"
    if [ "$C_DEV" = "$VID_DEV" ]; then
      VID_WANT_GB=$((VID_WANT_GB + C_GB)); VID_WHAT="$VID_WHAT, $C_GB for $C_WHAT"
    else
      # summed on their own disk: each was checked alone there (found in review, 2026-10-15)
      C_NEED[$C_DEV]=$(( ${C_NEED[$C_DEV]:-0} + C_GB )); C_AT[$C_DEV]="${C_AT[$C_DEV]:-$C_DIR}"
      C_DIRS[$C_DEV]="${C_DIRS[$C_DEV]:+${C_DIRS[$C_DEV]} and }$C_DIR"     # each named, not the last alone
      C_WHAT_ON[$C_DEV]="${C_WHAT_ON[$C_DEV]:+${C_WHAT_ON[$C_DEV]}, }$C_GB for $C_WHAT"
    fi
  done < <(video_first_start_caches)
  for C_DEV in "${!C_NEED[@]}"; do
    C_FREE="$(dl_free_gb "${C_AT[$C_DEV]}")"
    [ "${C_FREE:-0}" -ge "${C_NEED[$C_DEV]}" ] \
      || die "not enough room for the video lane's first start under ${C_DIRS[$C_DEV]}: ${C_FREE:-?} GB free, about ${C_NEED[$C_DEV]} GB needed (${C_WHAT_ON[$C_DEV]}; nothing was changed). Free some space first."
  done
  VID_FREE_GB="$(dl_free_gb "${VID_HF:-$HF_CACHE}")"
  [ "${VID_FREE_GB:-0}" -ge "$VID_WANT_GB" ] \
    || die "not enough room for $VID_MODEL under HF_HOME=${VID_HF:-$HF_CACHE}: ${VID_FREE_GB:-?} GB free, about $VID_WANT_GB GB needed ($VID_WHAT; nothing was changed). Free some space first, or point HF_HOME at a bigger disk."
  # ckpt-fetch.py, the one fetch of every lane: the cache first (a full sha resolves straight
  # from snapshots/, refs or no refs), then the rest with resumed attempts. refs/main is what
  # the offline unit resolves, and a download by sha never writes it: --refs-main fill creates
  # it when missing and repairs it when it holds no commit hash (a corrupt ref strands the unit
  # offline whatever just succeeded here), and never moves a valid one the installer or the pin
  # watch will answer for. It runs on the local-cache success too: that is the common path.
  HF_HOME="${VID_HF:-$HF_CACHE}" HF_HUB_DOWNLOAD_TIMEOUT=30 HF_HUB_DISABLE_XET=1 \
    HF_HUB_DISABLE_PROGRESS_BARS="$DL_NO_BARS" \
    "$VID_PY" "$REPO_DIR/ckpt-fetch.py" "$VID_MODEL" "$VID_REV" --allow "$VID_ALLOW" --offline-first --refs-main fill \
    || die "the video checkpoint could not be verified or fetched (re-run to resume; set HF_TOKEN if it stalls; if the pinned revision was removed upstream, ./install-video.sh VIDEO_MODEL_REV=main re-pins it)"
  fetched "$VID_MODEL @ ${VID_REV:0:12}"
  # Which lane this box served, written down before it is disabled: install.sh
  # updates that lane on a later run, and enablement cannot tell it once all are off.
  for TEXT_UNIT_NAME in qwen38-sglang.service qwen38-flash.service; do
    if systemctl is-enabled --quiet "$TEXT_UNIT_NAME" 2>/dev/null; then
      text_lane_note "$TEXT_UNIT_NAME"; break   # both enabled: the 27B, as install.sh follows it
    fi
  done
  # The loop variable ends in UNIT_NAME on purpose: CI checks every
  # `sudo systemctl <verb> "$..UNIT_NAME"` against the cockpit's sudoers allowlist,
  # and a variable called $u would have walked straight past it.
  for OTHER_UNIT_NAME in qwen38-sglang.service qwen38-flash.service qwen38-image.service qwen38-kolibri.service qwen38-nemotron.service qwen38-nemotron-super.service; do
    if systemctl is-enabled --quiet "$OTHER_UNIT_NAME" 2>/dev/null; then
      echo "disabling $OTHER_UNIT_NAME at boot (unit file kept for switching back)"
      sudo systemctl disable "$OTHER_UNIT_NAME"
    fi
  done
  sudo systemctl enable "$VIDEO_UNIT_NAME" >/dev/null 2>&1 || sudo systemctl enable "$VIDEO_UNIT_NAME"
  sudo systemctl daemon-reload
  RUNNING=""
  for u in qwen38-sglang.service qwen38-flash.service qwen38-image.service qwen38-kolibri.service qwen38-nemotron.service qwen38-nemotron-super.service; do
    systemctl is-active --quiet "$u" 2>/dev/null && RUNNING="$u"
  done
  printf '\n\033[1;32mSwitch queued: %s (%s)\033[0m\n' "$VID_MODEL" "$VIDEO_UNIT_NAME"
  if [ -n "$RUNNING" ]; then
    echo "Effective after:  sudo systemctl stop $RUNNING && sudo systemctl start $VIDEO_UNIT_NAME   (or next reboot)"
  else
    echo "Effective after:  sudo systemctl start $VIDEO_UNIT_NAME   (or next reboot; ready in about 12 min)"
  fi
  echo "The proxy on :30001 and opencode are text clients and were left as they are."
  echo "Switch back:      ./switch-model.sh stock   (or any text target, or image)"
  exit 0
fi

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
  flash-fp4-local)  TARGET_REPO="$FLASH_LOCAL_REPO"; TARGET_REV="$FLASH_LOCAL_REV"; TARGET_LANE=flash ;;
esac
# The FP4-Local config of the flash lane runs a different engine from the rest of
# the lane (the community b12x vLLM container, not SGLang): --fetch treats it like
# any other target, in every config and even on a box without the lane. A full
# switch is the installer's job while the box serves SGLang: rendering the other
# launcher and the unit around it is install.sh's, so refuse and say the exact
# command. Where the unit ALREADY names the FP4-Local launcher, the full switch
# is the lane-level bring-back the cockpit's Load needs (fetch, template, enable,
# records, limits; the launcher's flags are never rewritten by a switch).
FP4_LANE_SWITCH=0
if [ "$CHOICE" = "flash-fp4-local" ] && [ "$FETCH_ONLY" -eq 0 ]; then
  if [ -f "$FLASH_UNIT" ] && grep -q launch-flash-fp4local "$FLASH_UNIT" 2>/dev/null; then
    FP4_LANE_SWITCH=1
  else
    die "flash-fp4-local is the flash lane's other engine, not a checkpoint switch: fetch it with ./switch-model.sh flash-fp4-local --fetch, then make it the serving config with MODEL_CHOICE=flash-fp4-local ./install.sh"
  fi
fi
if [ "$TARGET_LANE" = flash ] && [ "$FP4_LANE_SWITCH" -eq 0 ] && [ "$FETCH_ONLY" -eq 0 ] \
   && [ -f "$FLASH_UNIT" ] && grep -q launch-flash-fp4local "$FLASH_UNIT" 2>/dev/null; then
  die "this box serves the flash lane's FP4-Local config; going back to $CHOICE means the SGLang engine: MODEL_CHOICE=$CHOICE ./install.sh"
fi


# The pins come from install.sh through eval, so a pin renamed or emptied there
# would otherwise reach the download step as an empty repo or revision. CI
# guards the names; this guards the values on a box that already diverged.
[ -n "${TARGET_REPO:-}" ] && [ -n "${TARGET_REV:-}" ] \
  || die "target '$CHOICE' resolved to an empty checkpoint or revision; install.sh is missing its pin"

# A GPU outside its ATS addressing mode cannot start the flash lane (issue #54): no checkpoint is
# fetched for it and the boot is not pointed at it. A cockpit Load that points the boot at it runs
# this before it stops the serving lane (a failed step ends the journey), and so does the Library's
# Download (--fetch).
if [ "$TARGET_LANE" = flash ] && ATS_WHY="$(flash_cannot_start)"; then
  die "the flash lane cannot start on this box: $ATS_WHY"
fi

if [ "$TARGET_LANE" = "27b" ]; then
  [ -f "$SGL_UNIT" ] || die "the 27B stack is not installed on this box (no $SGL_UNIT). Install it once first: MODEL_CHOICE=$CHOICE ./install.sh"
  TARGET_UNIT="$SGL_UNIT"; TARGET_UNIT_NAME="qwen38-sglang.service"
  OTHER_UNIT="$FLASH_UNIT"; OTHER_UNIT_NAME="qwen38-flash.service"
  INVOCATION="$SGL_UNIT"
  PIN_IMAGE="$SERVE_IMAGE"
else
  if [ -f "$FLASH_UNIT" ]; then
    TARGET_UNIT="$FLASH_UNIT"; TARGET_UNIT_NAME="qwen38-flash.service"
    OTHER_UNIT="$SGL_UNIT"; OTHER_UNIT_NAME="qwen38-sglang.service"
    # The invocation is whichever launcher the unit runs: the FP4-Local config
    # carries its own, and the limits and cache below are read from it (a fixed
    # launch-flash.sh would name the other config's flags, found in review,
    # 2026-10-18). Read from the unit, not from the kind of switch: a --fetch on a
    # box serving FP4-Local read launch-flash.sh and the 27B unit for the cache and
    # fetched into another one than the engine mounts (found in review, 2026-10-20).
    if grep -q launch-flash-fp4local "$FLASH_UNIT" 2>/dev/null; then
      INVOCATION="$CONFIG_DIR/launch-flash-fp4local.sh"
    else
      INVOCATION="$CONFIG_DIR/launch-flash.sh"
    fi
    case "$CHOICE" in
      flash-fp4-local) PIN_IMAGE="$FLASH_LOCAL_SERVE_IMAGE" ;;
      *)               PIN_IMAGE="$FLASH_SERVE_IMAGE" ;;
    esac
  else
    # --fetch on a box that never installed the lane (the cockpit's Library
    # Download path): nothing to point at and nothing to serve; fetched() ends
    # the run before any unit step.
    [ "$FETCH_ONLY" -eq 1 ] \
      || die "the Flash-Next stack is not installed on this box (no $FLASH_UNIT). Install it once first: MODEL_CHOICE=$CHOICE ./install.sh (~152 GB download: the checkpoint 136, the image 16; no image build)"
    TARGET_UNIT_NAME="qwen38-flash.service"
    INVOCATION="" OTHER_UNIT="" OTHER_UNIT_NAME="qwen38-sglang.service"
    case "$CHOICE" in
      flash-fp4-local) PIN_IMAGE="$FLASH_LOCAL_SERVE_IMAGE" ;;
      *)               PIN_IMAGE="$FLASH_SERVE_IMAGE" ;;
    esac
  fi
fi
# The cache the installed engine mounts, read back the way install.sh does (CUR_HF),
# not the default: a box installed with HF_CACHE=/data/hf and switched from the cockpit,
# which passes no HF_CACHE, downloaded and patched into ~/.cache/huggingface while its
# unit kept mounting /data/hf, and the next start found no checkpoint (found in review,
# 2026-09-24). Both text lanes mount the same cache; an explicit HF_CACHE still wins.
if [ -z "$_ENV_HF_CACHE" ]; then
  for _inv in "$INVOCATION" "$SGL_UNIT" "$CONFIG_DIR/launch-flash.sh"; do
    _hf="$(unit_hf_cache "$_inv")"
    if [ -n "$_hf" ]; then
      [ "$_hf" != "$HF_CACHE" ] && echo "using the HF cache the installed engine mounts: $_hf"
      HF_CACHE="$_hf"
      break
    fi
  done
  unset _inv _hf
fi
# The image whose presence decides a switch is the one the installed invocation
# will actually run, not the base it may have been built from. Since v1.8 a box
# can serve the pinned official image and keep no base image at all, and both
# base images are re-pullable, so gating on a base turned a working switch into
# a false "run install.sh once". Read it the way the cockpit does: the line just
# above the one that calls the server.
unit_image() {
  [ -f "$1" ] || return 0
  awk '/sglang\.launch_server|(^| )vllm serve/ { print prev; exit } { prev = $0 }' "$1" \
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
# The fraction on that line is the box's own choice (0.50 native, 0.76 for 1M,
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
  *:*) ;;                                  # name:tag or name@digest both match
  *) DL_IMAGE="$PIN_IMAGE" ;;              # unreadable invocation: fall back to the repo's pin
esac
# The FP4-Local fetch runs in its own image's python: the SGLang image is what a
# box serving the other config has, and a units-only box has neither, so a fetch
# of this target must not depend on either (found in review, 2026-10-18). Where the
# invocation IS the FP4-Local launcher, the image it names is the one read above, as
# for every other invocation: the pin would refuse a box whose launcher runs another
# image, a pin bump not installed yet (found in review, 2026-10-20).
if [ "$CHOICE" = "flash-fp4-local" ] && [ "$INVOCATION" != "$CONFIG_DIR/launch-flash-fp4local.sh" ]; then
  DL_IMAGE="$FLASH_LOCAL_SERVE_IMAGE"
fi
# The FP4-Local config is prepared by its own target: a bare ./prepare-lane.sh flash
# prepares the SGLang config's image and checkpoint (found in review, 2026-10-20).
PREPARE_CMD="./prepare-lane.sh $TARGET_LANE"
[ "$CHOICE" = "flash-fp4-local" ] && PREPARE_CMD="./prepare-lane.sh flash flash-fp4-local"
docker image inspect "$DL_IMAGE" >/dev/null \
  || die "the image this box would fetch in is not present ($DL_IMAGE): a lane installed on demand is prepared by its first Load from the cockpit (or $PREPARE_CMD); otherwise run MODEL_CHOICE=$CHOICE ./install.sh once, the switch stays surgical"

if [ "$FETCH_ONLY" -eq 1 ]; then
  echo "fetching $TARGET_REPO @ $TARGET_REV; nothing else changes (--fetch)"
elif [ "$TARGET_LANE" = "27b" ]; then
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
# Room first, as install.sh does: a cockpit button can start this, and a download that
# fills the disk under a serving engine is a failure of everything on it, while the old
# message blamed an HF_TOKEN (found in review, 2026-09-24). What the cache already holds
# for the target comes off the lane's need (install.sh's numbers: 45 GB for a 27B
# checkpoint and its caches, 180 for flash).
DL_NEED_GB="$(ckpt_room_gb "$TARGET_LANE" "$HF_CACHE" "$TARGET_REPO" "$TARGET_REV")"   # lanes.sh, the first Load's too
DL_FREE_GB="$(dl_free_gb)"
[ "${DL_FREE_GB:-0}" -ge "$DL_NEED_GB" ] \
  || die "not enough room for $TARGET_REPO under HF_CACHE=$HF_CACHE: ${DL_FREE_GB:-?} GB free, about $DL_NEED_GB GB needed (nothing was changed). Free some space first."
DL_TOKEN_ARGS=()
# by name: docker takes the value from its own environment, so the token is not in the
# docker client's argv, which any local user can read in /proc (found in review, 2026-09-24)
if [ -n "${HF_TOKEN:-}" ]; then export HF_TOKEN; DL_TOKEN_ARGS=(-e HF_TOKEN); fi
# The byte count reads hf-progress.py in the container, beside ckpt-fetch.py. Mounted only
# when it is there: a bind of a missing file makes docker create a root-owned directory in
# its place.
DL_PROGRESS_ARGS=()
if [ -f "$REPO_DIR/hf-progress.py" ]; then
  DL_PROGRESS_ARGS=(-v "$REPO_DIR/hf-progress.py:/opt/qwen38/hf-progress.py:ro")
fi
# --init: the cockpit stops a switch that overruns its job timeout with a TERM to the
# whole process group, and docker run passes it on to the container, where python3 as
# PID 1 has no handler for it and ignores it. Measured on the reference box: without
# --init the container outlived the TERM and then a SIGKILL of its docker client, and
# went on downloading with nobody attached; with it, both were gone within 6 s.
# ckpt-fetch.py, the one fetch of every lane's checkpoint (resumed attempts, the Xet exception
# the flash checkpoint's 53.7 GB n-gram table needs, the byte count). The cache first, as the
# other lanes' switches ask it: a checkpoint every shard of which is there answers without
# the network, so a cockpit Load of a text lane works offline or with the Hub throttling
# (until v1.31 it asked the Hub every time, and died without it). Offline serving resolves
# "main" via refs/main, which a pinned-sha download never writes: --refs-main fill writes it
# where it is missing and never moves one the box serves.
docker run --rm --init --network host --user "$(id -u):$(id -g)" \
  --entrypoint python3 \
  -e HF_HOME=/hf -e HF_HUB_DOWNLOAD_TIMEOUT=30 -e HF_HUB_DISABLE_XET=1 \
  -e HF_HUB_DISABLE_PROGRESS_BARS="$DL_NO_BARS" \
  "${DL_TOKEN_ARGS[@]}" "${DL_PROGRESS_ARGS[@]}" \
  -v "$REPO_DIR/ckpt-fetch.py:/opt/qwen38/ckpt-fetch.py:ro" \
  -v "$HF_CACHE":/hf \
  "$DL_IMAGE" /opt/qwen38/ckpt-fetch.py "$TARGET_REPO" "$TARGET_REV" --offline-first --refs-main fill \
  || die "download failed ($(dl_free_gb) GB left under $HF_CACHE; if that is near 0 the disk filled up, otherwise re-run to resume: HuggingFace throttles unauthenticated downloads, set HF_TOKEN=<your token> if it stalls)"
fetched "$TARGET_REPO @ ${TARGET_REV:0:12}"

# 2) 1M YaRN patch on the target's cached config.json (27B pair only, and only
#    if its unit uses it; flash serves its native window)
if [ "$TARGET_LANE" = "27b" ] && grep -q -- '--context-length 1010000' "$TARGET_UNIT"; then
  python3 "$REPO_DIR/patch-yarn.py" "$HF_CACHE" "$TARGET_REPO" "$TARGET_REV" || die "YaRN patch failed"
elif [ "$TARGET_LANE" = "27b" ]; then
  # A native unit crashes at load on a YaRN-patched config, and a target this box served
  # while it was in 1m keeps its patch in the cache after the box went back to native
  # (install.sh restores the target it installs, not the others). So the target's config
  # is restored here, idempotently; the restore refuses, before the unit is touched, when
  # a patched config lost its backup (found in review, 2026-09-24).
  echo "unit does not use the 1M context flag: restoring the target's native config if it carries YaRN"
  python3 "$REPO_DIR/patch-yarn.py" --restore "$HF_CACHE" "$TARGET_REPO" "$TARGET_REV" \
    || die "the target's config carries the 1M YaRN patch and could not be restored (the switch was NOT applied to the unit yet)"
fi

# 2b) regenerate the patched chat template FROM the target's own snapshot.
# One template file per engine; the served template always follows the served
# model. Read at service startup only, so writing it now is safe.
TEMPLATE_OUT="$CONFIG_DIR/chat-template-sglang.jinja"
[ "$TARGET_LANE" = "flash" ] && TEMPLATE_OUT="$CONFIG_DIR/chat-template-flashnext.jinja"
[ "$FP4_LANE_SWITCH" -eq 1 ] && TEMPLATE_OUT="$CONFIG_DIR/chat-template-flashnext-local.jinja"
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
  # A unit that already says all this keeps its date: rewritten, it read as an update the
  # serving engine had not taken, and the cockpit offered a restart for nothing (found in
  # review, 2026-10-17). Kolibri, Nemotron and the image lane compare the same way.
  if cmp -s "$STAGE_UNIT" "$TARGET_UNIT"; then
    echo "unit unchanged: it already loads $CHOICE"
  else
    diff "$STAGE_UNIT" "$TARGET_UNIT" || true   # show exactly what changes
    sudo install -m 644 "$STAGE_UNIT" "$TARGET_UNIT"
  fi
elif [ "$FP4_LANE_SWITCH" -eq 1 ]; then
  # The FP4-Local launcher is rendered whole by the installer and its flags are
  # never a per-checkpoint choice, so this switch checks it and moves only the
  # --revision should install.sh's pin have shifted since the render.
  FLASH_FP4_LAUNCH="$CONFIG_DIR/launch-flash-fp4local.sh"
  [ -f "$FLASH_FP4_LAUNCH" ] \
    || die "the unit names the FP4-Local launcher but $FLASH_FP4_LAUNCH is missing: re-run MODEL_CHOICE=flash-fp4-local ./install.sh"
  [ "$(launcher_model "$FLASH_FP4_LAUNCH")" = "$TARGET_REPO" ] \
    || die "$FLASH_FP4_LAUNCH does not serve $TARGET_REPO (hand-edited?): re-run MODEL_CHOICE=flash-fp4-local ./install.sh"
  if grep -q -- '--revision ' "$FLASH_FP4_LAUNCH" && ! grep -q -- "--revision $TARGET_REV" "$FLASH_FP4_LAUNCH"; then
    awk -v rev="$TARGET_REV" '{ sub(/--revision [A-Za-z0-9]+/, "--revision " rev); print }' "$FLASH_FP4_LAUNCH" > "$TMP_UNIT"
    bash -n "$TMP_UNIT" || die "rewritten FP4-Local launch script does not parse"
    diff "$TMP_UNIT" "$FLASH_FP4_LAUNCH" || true   # show exactly what changes
    install -m 755 "$TMP_UNIT" "$FLASH_FP4_LAUNCH"
    echo "FP4-Local launcher moved to --revision $TARGET_REV"
  else
    echo "launch script unchanged: the FP4-Local config already loads $CHOICE"
  fi
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
  # the same for the launch script the flash unit runs (see the unit's case above)
  if cmp -s "$TMP_UNIT" "$FLASH_LAUNCH"; then
    echo "launch script unchanged: it already loads $CHOICE"
  else
    diff "$TMP_UNIT" "$FLASH_LAUNCH" || true   # show exactly what changes
    install -m 755 "$TMP_UNIT" "$FLASH_LAUNCH"
  fi
  # A switch changes the checkpoint, never the serving image: only install.sh builds
  # that. So a box that pulled a repo whose image pin moved would keep serving the old
  # one, silently, and the image is where the kernel fixes live (v1.6: the merged
  # sm_121 kernel). Say so instead of letting it pass.
  # Since v1.8 the launcher names the pinned official image (a digest); a box
  # installed with OVERLAY_FLASH=1 before v1.18.7 retired it names a local
  # qwen38-flash:tag, and is told the same thing: adopt the pin.
  # Same class as OC_WINDOW below: the empty case is handled three lines down
  # ([ -n "$LAUNCH_IMAGE" ]), and without || true set -e never lets it get there.
  LAUNCH_IMAGE="$(grep -oE '^[[:space:]]*(qwen38-flash:[A-Za-z0-9._-]+|lmsysorg/sglang@sha256:[0-9a-f]{64})' "$FLASH_LAUNCH" | tr -d '[:space:]' | head -1 || true)"
  WANT_IMAGE="${FLASH_IMAGE:-}"
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
# The image lane is the third serving unit, the video lane the fourth. Leaving either
# enabled while a text lane is made the boot lane would bring two engines up at the
# next reboot.
if [ -f "$IMAGE_UNIT" ] && systemctl is-enabled --quiet "$IMAGE_UNIT_NAME" 2>/dev/null; then
  echo "disabling $IMAGE_UNIT_NAME at boot (unit file kept for switching back)"
  sudo systemctl disable "$IMAGE_UNIT_NAME"
fi
if [ -f "$VIDEO_UNIT" ] && systemctl is-enabled --quiet "$VIDEO_UNIT_NAME" 2>/dev/null; then
  echo "disabling $VIDEO_UNIT_NAME at boot (unit file kept for switching back)"
  sudo systemctl disable "$VIDEO_UNIT_NAME"
fi
# The Kolibri lane is the fifth: the same reason.
if [ -f "$KOLIBRI_UNIT" ] && systemctl is-enabled --quiet "$KOLIBRI_UNIT_NAME" 2>/dev/null; then
  echo "disabling $KOLIBRI_UNIT_NAME at boot (unit file kept for switching back)"
  sudo systemctl disable "$KOLIBRI_UNIT_NAME"
fi
# The Nemotron lanes are the sixth and the seventh: the same reason.
if [ -f "$NEMOTRON_UNIT" ] && systemctl is-enabled --quiet "$NEMOTRON_UNIT_NAME" 2>/dev/null; then
  echo "disabling $NEMOTRON_UNIT_NAME at boot (unit file kept for switching back)"
  sudo systemctl disable "$NEMOTRON_UNIT_NAME"
fi
if [ -f "$NEMOTRON_SUPER_UNIT" ] && systemctl is-enabled --quiet "$NEMOTRON_SUPER_UNIT_NAME" 2>/dev/null; then
  echo "disabling $NEMOTRON_SUPER_UNIT_NAME at boot (unit file kept for switching back)"
  sudo systemctl disable "$NEMOTRON_SUPER_UNIT_NAME"
fi
sudo systemctl enable "$TARGET_UNIT_NAME" >/dev/null 2>&1 || sudo systemctl enable "$TARGET_UNIT_NAME"
text_lane_note "$TARGET_UNIT_NAME"
sudo systemctl daemon-reload
# 4b) the keepalive proxy's one-prompt ceiling follows the lane (v1.5.6 contract, see
#     install.sh: flash 250000 tokens by default, the 27B lane none), applied now so the
#     proxy matches the lane that serves after the restart below. Written as a systemd
#     drop-in, never by editing the unit in place: an in-place sed needs a sudoers
#     wildcard to stay valid, and a wildcard on sed is root (its w command writes any
#     file). The drop-in overrides the unit's Environment line; install.sh bakes the
#     ceiling into the unit and removes this drop-in, so either path converges.
KA_UNIT="/etc/systemd/system/qwen38-keepalive.service"
if [ -f "$KA_UNIT" ] && grep -q '^Environment=FLASH_PROMPT_CEILING_TOKENS=' "$KA_UNIT"; then
  # The proxy of this unit follows the lane that serves by itself (v6.25): nothing to move,
  # and nothing to restart. Moving it here applied the target's ceiling while the old lane
  # still served (a 27B serving 1M refused every prompt past 250,000 until the next boot),
  # and the restart cut every stream in flight through :30001 (found in review, 2026-09-24).
  echo "keepalive proxy: its one-prompt ceiling follows the lane that serves; left as it is"
elif [ -f "$KA_UNIT" ]; then
  # A unit from before v1.18.7, whose proxy only knows one ceiling: the old way, until a
  # ./install.sh writes the new unit.
  CEIL=0
  [ "$TARGET_LANE" = "flash" ] && CEIL="${PROMPT_CEILING_TOKENS:-250000}"
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
# what the opencode server reads, before this switch writes any of it (see 4d); a missing
# file is part of the answer, not a failure (install.sh's first installs died on the same
# line under pipefail)
oc_configs_sum(){ { cat "$CONFIG_DIR/opencode.json" "$HOME/.config/opencode/opencode.json" 2>/dev/null || true; } | sha256sum; }
SW_OC_SUM_BEFORE="$(oc_configs_sum)"
if read -r SW_CTX SW_OUT _SW_LABEL \
     <<<"$("$REPO_DIR/oc-limits.sh" "$CHOICE" --from "$INVOCATION")" \
   && [ -n "${SW_CTX:-}" ]; then
  if [ "$TARGET_LANE" = "flash" ]; then SW_PROV=flashnext; SW_MODEL=qwen3.8-flash-next
  else SW_PROV=qwen38; SW_MODEL=qwen3.8-27b; fi
  # The compaction block is global, so it is sized from the target's context and
  # rewritten with the limits: a switch that moved the threshold and left the old
  # preserve_recent_tokens behind would compact the new lane against the old
  # lane's window. Same table, same call as install.sh.
  SW_KEEP="$("$REPO_DIR/oc-limits.sh" --preserve "$SW_CTX")" || SW_KEEP=""
  for OC_JSON in "$CONFIG_DIR/opencode.json" "$HOME/.config/opencode/opencode.json"; do
    [ -f "$OC_JSON" ] || continue
    python3 "$REPO_DIR/oc-merge-limits.py" "$OC_JSON" "$SW_PROV" "$SW_MODEL" "$SW_CTX" "$SW_OUT" \
      || echo "NOTE: could not update the limits in $OC_JSON; check them by hand ($SW_CTX/$SW_OUT)"
    [ -n "$SW_KEEP" ] && { python3 "$REPO_DIR/oc-merge-limits.py" "$OC_JSON" --compaction "$SW_KEEP" \
      || echo "NOTE: could not update the compaction block in $OC_JSON; check it by hand ($SW_KEEP)"; }
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
  # || true, and it is the whole reason a switch to a native 27B target died
  # here on 2026-09-14: a native unit carries NO --context-length (262144 is the
  # engine default), grep exits 1 on zero matches, and under set -e with
  # pipefail that killed the script silently, three lines before the end. The
  # lane had already been swapped and enabled, so the box served the 27B while
  # opencode's default model still pointed at flashnext: a switch that reported
  # failure and had in fact done nine tenths of the job. install.sh carries the
  # same pipeline and was fixed the same way on 2026-09-12; this copy was not.
  OC_WINDOW="$(grep -ohE -- '--context-length [0-9]+|--max-model-len [0-9]+' "$INVOCATION" 2>/dev/null | awk '{print $2}' | head -1 || true)"
  # The FP4-Local launcher spells its window --max-model-len (vLLM's word for it);
  # a launcher naming neither leaves the label unwindowed, as a native 27B's does.
  python3 "$REPO_DIR/oc-point-default.py" "$OC_JSON" "$TARGET_LANE" "$CHOICE" "$OC_WINDOW" \
    || echo "NOTE: could not update $OC_JSON (hand-edited?); set its \"model\" field yourself"
done
# 4d) the opencode server, if this box runs one, is restarted so the numbers above
#     actually reach it. `opencode serve` reads opencode.json ONCE at startup and
#     never again: measured 2026-09-13, the file on disk said 225,000 while the
#     running server still answered 175,000 on /config. Every switch before this
#     rewrote the limits correctly and left the Agent tab compacting against the
#     previous lane's window, which is a silent version of the mid-session 400
#     this whole table exists to prevent.
#     Only when they changed: a switch between two targets of one lane (stock to
#     uncensored) writes the same numbers, and the restart ended the turn the Agent tab
#     was in all the same (found in review, 2026-09-24).
if systemctl list-unit-files opencode-web.service >/dev/null 2>&1 \
   && systemctl is-active --quiet opencode-web.service; then
  if [ "$(oc_configs_sum)" != "$SW_OC_SUM_BEFORE" ]; then
    sudo systemctl restart opencode-web.service \
      && echo "opencode-web.service restarted so it reads the new limits" \
      || echo "NOTE: could not restart opencode-web.service; restart it by hand or the Agent view keeps the old limits"
  else
    echo "opencode-web.service kept: the limits it reads did not change"
  fi
fi
fi

# 4f) pi and omp (docs/pi.md): the same dynamic the block above gives opencode.
#     Their configs carry no second table of names and limits: pi-gen.py
#     regenerates them from the opencode.json this switch just rewrote, as
#     artifacts under ~/.config/qwen38 (pi/ JSON, omp/ YAML converted from it).
#     No agent directory is ever written: the agents' own files are the user's
#     copies, so re-copy them after a switch to pick up the new lane. Only on a
#     box that asked for them once (./pi-gen.py made the directories): a switch
#     writes no file for an agent nobody uses.
if [ -d "$CONFIG_DIR/pi" ] || [ -d "$CONFIG_DIR/omp" ]; then
  python3 "$REPO_DIR/pi-gen.py" --config "$CONFIG_DIR/opencode.json" --out "$CONFIG_DIR" \
    || echo "NOTE: could not regenerate the pi/omp artifacts; run ./pi-gen.py by hand"
fi

RUNNING=""
systemctl is-active --quiet "$OTHER_UNIT_NAME" 2>/dev/null && RUNNING="$OTHER_UNIT_NAME"
systemctl is-active --quiet "$IMAGE_UNIT_NAME" 2>/dev/null && RUNNING="$IMAGE_UNIT_NAME"
systemctl is-active --quiet "$VIDEO_UNIT_NAME" 2>/dev/null && RUNNING="$VIDEO_UNIT_NAME"
systemctl is-active --quiet "$KOLIBRI_UNIT_NAME" 2>/dev/null && RUNNING="$KOLIBRI_UNIT_NAME"
systemctl is-active --quiet "$NEMOTRON_UNIT_NAME" 2>/dev/null && RUNNING="$NEMOTRON_UNIT_NAME"
systemctl is-active --quiet "$NEMOTRON_SUPER_UNIT_NAME" 2>/dev/null && RUNNING="$NEMOTRON_SUPER_UNIT_NAME"
printf '\n\033[1;32mSwitch queued: %s (%s)\033[0m\n' "$TARGET_REPO" "$TARGET_UNIT_NAME"
# The target is the operator's choice from here (2026-10-17): an installed.json that a run of
# v1.36.0 or later wrote records it, and a switch back to the recommended target erases the
# entry (conformity.py choose). Never fatal: the next ./install.sh classes what the unit serves.
python3 "$REPO_DIR/conformity.py" choose --config-dir "$CONFIG_DIR" --repo-dir "$REPO_DIR" \
  "target.$TARGET_LANE" "$CHOICE" || true
if [ -n "$RUNNING" ]; then
  echo "Effective after:  sudo systemctl stop $RUNNING && sudo systemctl start $TARGET_UNIT_NAME   (or next reboot)"
else
  echo "Effective after:  sudo systemctl restart $TARGET_UNIT_NAME   (or next reboot)"
fi
# The limits written above are the ones this mode DESERVES, not the ones this
# box can serve: the pool is only known once the engine has booted, and it moves
# with the checkpoint and the image. install.sh fits them at install time; a
# switch cannot, because the engine it just pointed at is not up yet. Say so,
# or the operator learns it from a proxy 400 late in a session (or, on a box
# with the cockpit, from its banner: this is the "Fit the limits to this
# engine" button). Measured three times on 2026-09-18 while testing switches.
echo "Then fit them:    python3 oc-fit-limits.py   (the limits above are this target's"
echo "                  nominal pair; the pool is only known once the engine is up)"
# The first target a hint names is a standard one: the project advises the standard
# checkpoints. The abliterated targets stay named, last in the parentheses, so that they are
# known without ever being the default. This line named `uncensored` first after every
# switch to stock.
case "$CHOICE" in
  stock)      echo "Switch back:      ./switch-model.sh flash   (or fp8, image, video, uncensored)" ;;
  uncensored) echo "Switch back:      ./switch-model.sh stock   (or fp8, flash)" ;;
  fp8)        echo "Switch back:      ./switch-model.sh stock   (or flash, uncensored, uncensored-fp8)" ;;
  uncensored-fp8) echo "Switch back:      ./switch-model.sh fp8   (or stock, flash, uncensored)" ;;
  flash)      echo "Switch back:      ./switch-model.sh stock   (or fp8, flash-nvda, uncensored, flash-uncensored)" ;;
  flash-nvda) echo "Switch back:      ./switch-model.sh flash   (or stock, fp8, flash-uncensored, uncensored)" ;;
  flash-uncensored) echo "Switch back:      ./switch-model.sh flash   (or flash-nvda, stock, fp8, uncensored)" ;;
  flash-fp4-local) echo "Switch back:      MODEL_CHOICE=flash-nvda ./install.sh (a SGLang config; from there ./switch-model.sh reaches the other targets)" ;;
esac
