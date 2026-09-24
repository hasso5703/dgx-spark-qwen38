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
CONFIG_DIR="$HOME/.config/qwen38"
SYSTEMD_DIR=/etc/systemd/system
SUDOERS_FILE=/etc/sudoers.d/qwen38-cockpit
PYSPY_WRAPPER=/usr/local/bin/qwen38-pyspy-scheduler
# Every cache and PLE folder the installed lanes use, not just the environment's: a box
# installed with HF_CACHE=/data/hf had 22 to 126 GB of weights and the 48 GB PLE file
# neither listed nor given a reclaim command (found in review, 2026-09-24).
unit_mount(){  # $1 = the container path; prints the host side of every -v mounting it
  grep -hoE -- "-v [^ :]+:$1( |\$|:)" "$SYSTEMD_DIR/qwen38-sglang.service" "$CONFIG_DIR/launch-flash.sh" 2>/dev/null \
    | sed -e 's/^-v //' -e "s|:$1.*\$||" || true
}
HF_CACHES="$( { printf '%s\n' "${HF_CACHE:-}"; unit_mount /root/.cache/huggingface
               { grep -m1 -E '^Environment=HF_HOME=' "$SYSTEMD_DIR/qwen38-image.service" 2>/dev/null || true; } | cut -d= -f3-
               printf '%s\n' "$HOME/.cache/huggingface"; } | awk 'NF && !seen[$0]++')"
PLE_DIRS="$( { printf '%s\n' "${PLE_DIR:-}"; unit_mount /ple; printf '%s\n' "$HOME/flashnext-ple"; } | awk 'NF && !seen[$0]++')"
# opencode reads all three global names, and creates opencode.jsonc itself on its first
# start: a provider block pasted into any of them reads the key as much as ours does.
OC_USER_CFGS=("$HOME/.config/opencode/config.json" "$HOME/.config/opencode/opencode.json" "$HOME/.config/opencode/opencode.jsonc")
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
LOCAL_IMAGE_REPOS="qwen38-dflash2 qwen38-flash qwen38-pinned"  # qwen38-dflash2 is the pre-v1.14 27B overlay, retired but still deletable; qwen38-pinned (v1.18.6) tags the digest pulls
BASE_IMAGES="lmsysorg/sglang:v0.5.19 lmsysorg/sglang@sha256:d6e7288627be8b02be88e4bba38e73f6d50e2826869f753c13a4c4385ab3eda9 lmsysorg/sglang:qwen38-27b lmsysorg/sglang@sha256:febfb971c7352570fc445c466ebd6ffc9d896024958e544a60f2137fd85856b1 lmsysorg/sglang:qwen38flashnext lmsysorg/sglang@sha256:12d3392bdc8be8d35e9a95f191df6aef99c5114bdbefd41bfdc7e760e6d25ec1 lmsysorg/sglang@sha256:9d2a843c706c74bc259c0d9abf360551eb2734e1e7d255ab012a6965f10480b6 lmsysorg/sglang@sha256:616a3e97f45191af975896cfa644279096cb31bd408a071c2e99ca7209c3cafe vllm/vllm-openai:qwen38-flash-next vllm/vllm-openai@sha256:fc120ece0a388cc0aa1caad4a9f1cd92113484ab7ec2fd0efadd62585be05bf8"
HF_REPOS="RadixArk/Qwen3.8-27B-NVFP4 edp1096/Huihui-RadixArk-Qwen3.8-27B-abliterated-NVFP4 Qwen/Qwen3.8-27B-FP8 edp1096/Huihui-Qwen3.8-27B-abliterated-FP8 RadixArk/Qwen3.8-27B-DSpark z-lab/Qwen3.8-27B-DFlash2 maurienne-ai/Qwen3.8-27B-DFlash2-NVFP4-RTNcal RadixArk/Qwen3.8-Flash-Next-NVFP4 nvidia/Qwen3.8-Flash-Next-NVFP4 dealignai/Qwen3.8-Flash-Next-ABLITERATED-NVFP4 Qwen/Qwen-Image-2.1"

FOUND_IMAGES=()   # "ref|size|every ref", deduplicated by image ID (a tag and its digest are one image)
# Every reference Docker keeps for one image. A digest-pulled image carries its digest
# and, since v1.18.6, a qwen38-pinned tag: removing one of the two only untags it.
image_refs() {
  docker image inspect "$1" --format '{{range .RepoTags}}{{.}} {{end}}{{range .RepoDigests}}{{.}} {{end}}' 2>/dev/null || true
}
inventory_images() {
  command -v docker >/dev/null 2>&1 || return 0
  docker info >/dev/null 2>&1 || return 0
  local seen_ids=" " ref id size
  for repo in $LOCAL_IMAGE_REPOS; do
    while IFS='|' read -r ref id size; do
      [ -n "$ref" ] || continue
      case "$seen_ids" in *" $id "*) continue ;; esac
      seen_ids="$seen_ids$id "
      FOUND_IMAGES+=("$ref|$size|$(image_refs "$ref")")
    done < <(docker images --format '{{.Repository}}:{{.Tag}}|{{.ID}}|{{.Size}}' "$repo" 2>/dev/null || true)
  done
  for ref in $BASE_IMAGES; do
    if docker image inspect "$ref" >/dev/null 2>&1; then
      id="$(docker image inspect "$ref" --format '{{.Id}}' | cut -c8-19)"
      case "$seen_ids" in *" $id "*) continue ;; esac
      seen_ids="$seen_ids$id "
      size="$(docker image inspect "$ref" --format '{{.Size}}' | awk '{printf "%.1fGB", $1/1e9}')"
      FOUND_IMAGES+=("$ref|$size|$(image_refs "$ref")")
    fi
  done
}

dir_size() { du -sh "$1" 2>/dev/null | cut -f1; }
# The reclaim command for a directory: the user's rm, or sudo when a directory in it is not
# theirs to empty. Engine containers of past versions ran as root on the mounted HF cache
# and left root-owned .no_exist and refs entries behind (reference box: three checkpoints,
# 2026-08-28 to 2026-09-12), and a plain rm -rf printed for them stopped on "Permission denied".
rm_cmd() {
  if [ -n "$(find "$1" -type d ! -writable -print -quit 2>/dev/null)" ]; then echo "sudo rm -rf"; else echo "rm -rf"; fi
}

echo "── Inventory (everything any version of this repo may have left here) ──"
for u in qwen38-sglang.service qwen38-flash.service qwen38-keepalive.service qwen38-dashboard.service qwen38-image.service opencode-web.service; do
  if [ -f "$SYSTEMD_DIR/$u" ]; then
    STATE="$(systemctl is-enabled "$u" 2>/dev/null || true)/$(systemctl is-active "$u" 2>/dev/null || true)"
    echo "  unit      $SYSTEMD_DIR/$u ($STATE)"
  fi
done
[ -d "$SYSTEMD_DIR/qwen38-sglang.service.d" ] && echo "  drop-ins  $SYSTEMD_DIR/qwen38-sglang.service.d (pre-v1.3 warmup lived here)"
[ -d "$SYSTEMD_DIR/qwen38-keepalive.service.d" ] && echo "  drop-ins  $SYSTEMD_DIR/qwen38-keepalive.service.d (switch-model.sh ceiling override)"
[ -d "$SYSTEMD_DIR/qwen38-dashboard.service.d" ] && echo "  drop-ins  $SYSTEMD_DIR/qwen38-dashboard.service.d (cockpit overrides)"
[ -f "$SUDOERS_FILE" ] && echo "  sudoers   $SUDOERS_FILE (cockpit argv allowlist, NOPASSWD)"
[ -f "$PYSPY_WRAPPER" ] && echo "  wrapper   $PYSPY_WRAPPER (cockpit forensics helper)"
# Read from the unit, not assumed: install-image.sh takes IMAGE_LANE_DIR, so a lane
# installed elsewhere would be reported clean and left on disk.
IMAGE_LANE_DIR="${IMAGE_LANE_DIR:-$({ grep -m1 -E '^WorkingDirectory=' "$SYSTEMD_DIR/qwen38-image.service" 2>/dev/null || true; } | cut -d= -f2-)}"
IMAGE_LANE_DIR="${IMAGE_LANE_DIR:-$HOME/.local/share/qwen38-image}"
[ -d "$IMAGE_LANE_DIR" ] && echo "  runtime   $IMAGE_LANE_DIR ($(dir_size "$IMAGE_LANE_DIR"), image lane venv + pinned SGLang checkout)"
for f in "$CONFIG_DIR"/*.bak-preupdate; do
  [ -f "$f" ] && echo "  backup    $f (pre-update unit backup)"
done
if [ -d "$CONFIG_DIR" ]; then
  echo "  config    $CONFIG_DIR ($(dir_size "$CONFIG_DIR")): api-key and the engine's copy of it, patched templates, opencode.json, launch scripts, compile cache"
  [ -f "$CONFIG_DIR/opencode-web.env" ] && echo "  config    $CONFIG_DIR/opencode-web.env (Agent tab: credentials of the opencode web server)"
  [ -f "$CONFIG_DIR/claude-code.env" ] && echo "  legacy    $CONFIG_DIR/claude-code.env (pre-v1.3 client config, unmaintained)"
  [ -f "$CONFIG_DIR/opencode.off" ] && echo "  marker    $CONFIG_DIR/opencode.off (opencode integration disabled with --no-opencode)"
  [ -f "$CONFIG_DIR/cockpit.off" ] && echo "  marker    $CONFIG_DIR/cockpit.off (cockpit disabled with --no-cockpit)"
fi
if grep -q 'dgx-spark-qwen38' "$HOME/.local/bin/oc" 2>/dev/null; then
  echo "  launcher  $HOME/.local/bin/oc (this repo's opencode launcher)"
fi
# install.sh installs opencode itself only when a box had none; it marks that in the
# PATH line it adds to ~/.bashrc. Listed, never removed: from then on it is the user's
# opencode as much as this repo's, with its own sessions under ~/.local/share/opencode.
if [ -x "$HOME/.opencode/bin/opencode" ] && grep -qs 'opencode, installed by dgx-spark-qwen38' "$HOME/.bashrc"; then
  echo "  opencode  $HOME/.opencode (installed by this repo; kept. Yours to remove: rm -rf ~/.opencode, and its PATH line in ~/.bashrc)"
fi
for f in "${OC_USER_CFGS[@]}"; do
  if grep -qsF '.config/qwen38/api-key' "$f"; then
    echo "  opencode  $f (opencode's own config: lists providers that read this box's API key)"
  fi
done
inventory_images
for entry in ${FOUND_IMAGES[@]+"${FOUND_IMAGES[@]}"}; do
  IFS='|' read -r ref size _ <<< "$entry"
  echo "  image     $ref ($size)"
done
while IFS= read -r cache; do
  for repo in $HF_REPOS; do
    d="$cache/hub/models--${repo//\//--}"
    if [ -d "$d" ]; then echo "  weights   $d ($(dir_size "$d"))"; fi
  done
done <<< "$HF_CACHES"
while IFS= read -r p; do
  if [ -n "$p" ] && [ -d "$p" ]; then echo "  ple-file  $p ($(dir_size "$p"), flash mmap backing store)"; fi
done <<< "$PLE_DIRS"
echo "──"

if [ "$LIST_ONLY" -eq 1 ]; then
  echo "Inventory only: nothing was changed. Remove services+config with ./uninstall.sh;"
  echo "data (images, weights, PLE file) always stays until you run the printed commands."
  exit 0
fi

PRIV=0
for f in "$SYSTEMD_DIR/qwen38-sglang.service" "$SYSTEMD_DIR/qwen38-flash.service" \
         "$SYSTEMD_DIR/qwen38-keepalive.service" "$SYSTEMD_DIR/qwen38-dashboard.service" \
         "$SYSTEMD_DIR/qwen38-image.service" "$SYSTEMD_DIR/opencode-web.service" \
         "$SYSTEMD_DIR/qwen38-sglang.service.d" "$SYSTEMD_DIR/qwen38-dashboard.service.d" \
         "$SYSTEMD_DIR/qwen38-keepalive.service.d" "$SUDOERS_FILE" "$PYSPY_WRAPPER"; do
  [ -e "$f" ] && PRIV=1
done
# What only root can remove, asked for only when some of it is there. A --no-service box has
# none of it and is the one made for boxes without sudo: sudo was called anyway, and the
# first refusal ended this script under set -e before it removed any of the user's own files
# (found in review, 2026-09-24). Refused, the services are left running and untouched, since
# a container removed under a unit that keeps running is started again by systemd.
PRIV_DONE=0
if [ "$PRIV" -eq 1 ]; then
  if sudo -v; then
    for u in qwen38-sglang qwen38-flash qwen38-keepalive qwen38-dashboard qwen38-image opencode-web; do
      sudo systemctl disable --now "$u.service" 2>/dev/null || true
    done
    docker rm -f qwen38-sglang qwen38-sglang-run qwen38-flash 2>/dev/null || true
    sudo rm -f "$SYSTEMD_DIR/qwen38-sglang.service" "$SYSTEMD_DIR/qwen38-flash.service" "$SYSTEMD_DIR/qwen38-keepalive.service" "$SYSTEMD_DIR/qwen38-dashboard.service" "$SYSTEMD_DIR/qwen38-image.service" "$SYSTEMD_DIR/opencode-web.service"
    sudo rm -rf "$SYSTEMD_DIR/qwen38-sglang.service.d" "$SYSTEMD_DIR/qwen38-dashboard.service.d" "$SYSTEMD_DIR/qwen38-keepalive.service.d"
    # The cockpit's privileged surface goes with it: a NOPASSWD allowlist left behind
    # after an uninstall is the one leftover that is not merely clutter.
    sudo rm -f "$SUDOERS_FILE" "$PYSPY_WRAPPER"
    sudo systemctl daemon-reload
    PRIV_DONE=1
  else
    echo "NOTE: sudo was refused, so the services, their units and the cockpit's sudoers entry stay,"
    echo "      running as they were. Re-run ./uninstall.sh where sudo works to remove them."
  fi
else
  docker rm -f qwen38-sglang-run 2>/dev/null || true      # ./run.sh's foreground engine, if one was left
fi
# The image lane's runtime is the user's, not root's: no sudo, and the 31 GB checkpoint
# stays in the HF cache with every other checkpoint, which this script reports separately.
# Only what install-image.sh put there (venv/ and sglang/): IMAGE_LANE_DIR can be a
# directory shared with other work, and removing it whole took the rest with it (found in
# review, 2026-09-24). The directory itself goes only once it is empty.
# Left alone when sudo was refused: the image lane may still be running from it.
if [ -d "$IMAGE_LANE_DIR" ] && { [ "$PRIV" -eq 0 ] || [ "$PRIV_DONE" -eq 1 ]; }; then
  rm -rf "$IMAGE_LANE_DIR/venv" "$IMAGE_LANE_DIR/sglang"
  rmdir "$IMAGE_LANE_DIR" 2>/dev/null || echo "kept $IMAGE_LANE_DIR: it holds files the image lane did not put there"
fi
# The oc launcher, only if it is ours (never a foreign oc binary)
if grep -q 'dgx-spark-qwen38' "$HOME/.local/bin/oc" 2>/dev/null; then
  rm -f "$HOME/.local/bin/oc"
fi
if [ "$PRIV_DONE" -eq 1 ]; then
  echo "services removed."
elif [ "$PRIV" -eq 0 ]; then
  echo "no service of this repo was installed: nothing to stop or remove there."
fi

# Nor the config, whose API key the services still running read.
if [ "$PRIV" -eq 1 ] && [ "$PRIV_DONE" -eq 0 ]; then
  PURGE_CONFIG=0; KEPT_SAID=1
  echo "config kept at ~/.config/qwen38: the services still running read the API key in it."
elif [ "$PURGE_CONFIG" -eq 0 ] && [ -t 0 ]; then
  read -r -p "Also delete ~/.config/qwen38 (API key, patched templates, compile cache)? opencode's config then loses this box's providers. [y/N] " ans
  { [ "${ans:-n}" = "y" ] || [ "${ans:-n}" = "Y" ]; } && PURGE_CONFIG=1 || true
fi
if [ "$PURGE_CONFIG" -eq 1 ]; then
  # opencode refuses to start at all, every provider included, while a {file:} reference
  # points at a file that is gone, and the providers install.sh writes read the API key
  # this deletes (reference box, 2026-09-23: "bad file reference"). They go first.
  for f in "${OC_USER_CFGS[@]}"; do
    if ! grep -qsF '.config/qwen38/api-key' "$f"; then continue; fi
    python3 "$REPO_DIR/oc-merge-limits.py" "$f" --remove-providers "$CONFIG_DIR/api-key" \
      || echo "NOTE: $f still reads $CONFIG_DIR/api-key: remove those providers by hand, or opencode will not start."
  done
  # The engine container runs as root and writes its compile cache here (the
  # .../sglang-cache:/cache mount), in directories that are root's: a plain rm died on
  # the first of them under set -e, a thousand "Permission denied" lines in, before
  # the reclaim commands (reference box, 2026-09-23). What it cannot remove, sudo does.
  case "$CONFIG_DIR" in */.config/qwen38) ;; *) echo "refusing to delete $CONFIG_DIR" >&2; exit 1 ;; esac
  if rm -rf "$CONFIG_DIR" 2>/dev/null || sudo rm -rf --one-file-system "$CONFIG_DIR"; then
    echo "config removed."
  else
    echo "NOTE: part of $CONFIG_DIR belongs to root (an engine's compile cache) and sudo was refused:"
    echo "      remove it where sudo works: sudo rm -rf --one-file-system $CONFIG_DIR"
  fi
else
  [ "${KEPT_SAID:-0}" -eq 1 ] || echo "config kept at ~/.config/qwen38 (delete manually or re-run with --yes)."
  for f in "${OC_USER_CFGS[@]}"; do
    if grep -qsF '.config/qwen38/api-key' "$f"; then
      echo "opencode's config ($f) still lists this box's providers: they answer again after ./install.sh."
    fi
  done
fi

echo
echo "To also reclaim disk space, run the commands for what the inventory found:"
for entry in ${FOUND_IMAGES[@]+"${FOUND_IMAGES[@]}"}; do
  IFS='|' read -r ref size refs <<< "$entry"
  cmd="docker rmi"
  for r in ${refs:-$ref}; do cmd="$cmd '$r'"; done
  echo "  $cmd    # $size"
done
# An image with no tag at all is dangling to Docker, and prune deletes it: the digest pulls
# of installs before v1.18.6, which tags them. Said only when one is actually there.
UNTAGGED=0
for entry in ${FOUND_IMAGES[@]+"${FOUND_IMAGES[@]}"}; do
  IFS='|' read -r ref _ _ <<< "$entry"
  if [ "$(docker image inspect "$ref" --format '{{len .RepoTags}}' 2>/dev/null || echo 1)" = "0" ]; then UNTAGGED=1; fi
done
if [ "$UNTAGGED" -eq 1 ]; then
  echo "  # never 'docker image prune' on this box: an image above with no tag looks dangling"
  echo "  # and prune deletes it (then a 30 GB re-pull before the lane reboots); ./install.sh tags them"
fi
while IFS= read -r cache; do
  for repo in $HF_REPOS; do
    d="$cache/hub/models--${repo//\//--}"
    if [ -d "$d" ]; then echo "  $(rm_cmd "$d") '$d'    # $(dir_size "$d")"; fi
  done
done <<< "$HF_CACHES"
while IFS= read -r p; do
  # an if, not an && list: as the last command of the script, a false test made the whole
  # uninstall exit 1 on a box without a PLE folder
  if [ -n "$p" ] && [ -d "$p" ]; then echo "  $(rm_cmd "$p") '$p'    # $(dir_size "$p"), flash PLE backing file"; fi
done <<< "$PLE_DIRS"
