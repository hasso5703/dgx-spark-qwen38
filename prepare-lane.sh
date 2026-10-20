#!/usr/bin/env bash
# The heavy half of a lane installed on demand, as the user, with no sudo: what the cockpit's
# first Load of the lane runs before its switch. The image and video lanes' runtime (a venv and
# SGLang Diffusion, the video lane's Turbo adapter) and the Kolibri and Nemotron lanes' images
# (one image for the two Nemotron lanes), each by its own installer's --prepare; their
# checkpoint comes with the Load's switch, as for any Load. The
# other text lane, beside the lane that serves: the image its launcher (flash) or unit (27B)
# runs, the checkpoint of the target it serves (the switch's own fetch, ./switch-model.sh
# <target> --fetch), and what that lane builds or keeps beside it: the flash lane's reduced
# draft vocabulary (lanes.sh), which its launcher takes at its next start, and the 27B's DFlash2
# draft, its config set for the window the unit serves. Each download is refused before its
# first byte when its room is not there.
#
#   ./prepare-lane.sh image|video|kolibri|nemotron|nemotron-super
#   ./prepare-lane.sh flash|27b [<target>]
#
# The target is the one the Load loads, for the two lanes whose preparation fetches a
# checkpoint: flash, flash-nvda or flash-uncensored; stock, uncensored, fp8 or uncensored-fp8.
# Without one, the target the launcher or unit serves. The preparation fetched that one only,
# the default on a new box, and the Load's switch then fetched the target loaded (found in
# review, 2026-10-15). The lane and the target are arguments of their own: the cockpit's
# actions never build one argument out of a parameter (dashboard/tests/test_cockpit_http.py
# holds them to that).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
usage(){ echo "usage: ./prepare-lane.sh <image|video|kolibri|nemotron|nemotron-super> | <flash|27b> [<target>]" >&2; exit 2; }
die(){ printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
section(){ printf '\n\033[1;36m── %s\033[0m\n' "$*"; }
case "${1:-}" in
  image|video|kolibri|nemotron|nemotron-super) [ "$#" -eq 1 ] || usage ;;
  flash|27b) [ "$#" -le 2 ] || usage ;;
  *) usage ;;
esac
TARGET="${2:-}"

# What a launcher or unit says. head -1 each time: -m1 stops at the first line, and -o prints
# every match on it (the flash lane's draft vocabulary line names its file twice).
arg_of(){  # $1 file, $2 flag: the value the file gives that flag
  { grep -m1 -oE -- "$2 [^ ]+" "$1" || true; } | head -1 | cut -d' ' -f2
}
mount_of(){  # $1 file, $2 the container path: the host folder mounted there
  { grep -m1 -oE -- "-v [^ :]+:$2( |$)" "$1" || true; } | head -1 | sed -e 's/^-v //' -e "s|:$2 *\$||"
}
# The switch the Load runs next reads an image named name:tag or name@sha256:... and serves its
# pin for any other word: an image named with no tag was pulled here, then the switch died on a
# pin the box did not have (found in review, 2026-10-15).
image_as_the_switch_reads(){  # $1 the launcher or the unit, $2 the image it names (none: said after)
  case "$2" in
    ""|*:*) ;;
    *) die "$1 names its image as $2, with no tag or digest: the switch reads name:tag or name@sha256:... only (hand-edited? ./install.sh writes it again)" ;;
  esac
}
room_or_die(){  # $1 GB, $2 a path on the disk it goes to, $3 what for
  local free; free="$(free_gb_at "$2")"
  [ "${free:-0}" -ge "$1" ] || die "need ~$1 GB free under $2 for $3; found ${free:-unknown} GB. Free some space, then Load the lane again: what came already is kept."
}
pull_image(){  # $1 image, $2 the lane (its local tag), $3 the room its pull needs in GB
  local name="$2"; [ "$2" = 27b ] && name=27B
  section "The $name lane's image (${1:0:40})"
  if docker image inspect "$1" >/dev/null 2>&1; then
    echo "already here"
  else
    room_or_die "$3" "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)" "its image"
    docker pull "$1" || die "docker pull of $1 failed (no internet, or a Docker Hub rate limit: retry in a few minutes)"
  fi
  pin_tag "$2" "$1"
}
pins_or_die(){  # the pins install.sh declares, the one place they are, each checked defined
  local v
  eval "$(grep -E '^(FLASH(_NVDA|_UNC|_LOCAL)?|STOCK|UNC|FP8|UNCFP8)_(REPO|REV)=|^FLASH_LOCAL_IMAGE=|^FLASH_LOCAL_SERVE_IMAGE=' "$HERE/install.sh" || true)"
  for v in FLASH_REPO FLASH_REV FLASH_NVDA_REPO FLASH_NVDA_REV FLASH_UNC_REPO FLASH_UNC_REV \
           FLASH_LOCAL_REPO FLASH_LOCAL_REV FLASH_LOCAL_SERVE_IMAGE \
           STOCK_REPO STOCK_REV UNC_REPO UNC_REV FP8_REPO FP8_REV UNCFP8_REPO UNCFP8_REV; do
    eval "[ -n \"\${$v:-}\" ]" || die "install.sh no longer defines $v (repo layout changed?)"
  done
}

prepare_flash(){
  local launch="$CONFIG_DIR/launch-flash.sh" img model hf ple map target rev why
  # FP4-Local is the lane's other engine (the b12x vLLM container). Its prep needs no
  # unit and no launcher on this box, only install.sh's pins: the cockpit's Library
  # Download runs this before the config was ever installed, and its first install
  # then finds image and checkpoint in place. No PLE table and no draft-vocabulary
  # step: the b12x runtime reads the n-gram table from the checkpoint itself.
  if [ "${TARGET:-}" = flash-fp4-local ] || [ "${TARGET:-}" = "$FLASH_LOCAL_REPO" ]; then
    # nothing downloads on a GPU outside its ATS mode: the guard holds for the whole
    # lane (issue #54), and the FP4-Local launcher asks it at its start too
    if why="$(flash_cannot_start)"; then die "the flash lane cannot start on this box: $why"; fi
    # The cache by switch-model.sh's own rule, and handed to it, so the room is checked
    # where the checkpoint lands: an explicit HF_CACHE, else the launcher the flash unit
    # runs, the 27B unit, launch-flash.sh, the default. A stale fp4 launcher won over the
    # serving one here while the switch read another cache (found in review, 2026-10-20).
    local serving=""
    if [ -f /etc/systemd/system/qwen38-flash.service ]; then
      serving="$launch"
      grep -q launch-flash-fp4local /etc/systemd/system/qwen38-flash.service 2>/dev/null \
        && serving="$CONFIG_DIR/launch-flash-fp4local.sh"
    fi
    hf="${HF_CACHE:-}"
    [ -n "$hf" ] || hf="$(unit_hf_cache "$serving" /etc/systemd/system/qwen38-sglang.service "$launch")"
    [ -n "$hf" ] || hf="$HOME/.cache/huggingface"
    local img_need=28
    docker image inspect "$FLASH_LOCAL_SERVE_IMAGE" >/dev/null 2>&1 && img_need=0
    room_per_disk "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)|$img_need|its image" \
                  "$hf|$(ckpt_room_gb flash "$hf" "$FLASH_LOCAL_REPO" "$FLASH_LOCAL_REV")|its checkpoint and its caches"
    # the fp4 image's own tag key, as install.sh pins it: the shared flash key would
    # let a 27B-lane install untag it again (found in review, 2026-10-18)
    pull_image "$FLASH_LOCAL_SERVE_IMAGE" flash-local 28   # its peak, as install.sh counts it
    HF_CACHE="$hf" bash "$HERE/switch-model.sh" flash-fp4-local --fetch
    printf '\n\033[1;32m── Done: the FP4-Local config is prepared; MODEL_CHOICE=flash-fp4-local ./install.sh makes it the serving one\033[0m\n'
    return 0
  fi
  [ -f "$launch" ] || die "the flash lane is not installed on this box (no $launch): ./install.sh installs it"
  # its image and its checkpoint (223 GB in all) for a lane that cannot start on a GB10 outside
  # its ATS addressing mode (issue #54): said before the first byte
  if why="$(flash_cannot_start)"; then die "the flash lane cannot start on this box: $why"; fi
  img="$(engine_image "$launch")"; model="$(arg_of "$launch" --model-path)"; hf="$(mount_of "$launch" /root/.cache/huggingface)"
  ple="$(mount_of "$launch" /ple)"
  # a flag where the image should be: a launcher edited by hand left another word there, and its
  # pull failed as if the internet were down
  case "$img" in -*) img="" ;; esac
  image_as_the_switch_reads "$launch" "$img"
  if [ -z "$img" ] || [ -z "$model" ] || [ -z "$hf" ]; then
    die "$launch names no image, model or cache where this expects it (hand-edited?): ./install.sh writes it again"
  fi
  case "${TARGET:-$model}" in
    flash|"$FLASH_REPO") target=flash; model="$FLASH_REPO"; rev="$FLASH_REV" ;;
    flash-nvda|"$FLASH_NVDA_REPO") target=flash-nvda; model="$FLASH_NVDA_REPO"; rev="$FLASH_NVDA_REV" ;;
    flash-uncensored|"$FLASH_UNC_REPO") target=flash-uncensored; model="$FLASH_UNC_REPO"; rev="$FLASH_UNC_REV" ;;
    *) if [ -n "$TARGET" ]; then die "$TARGET is not a flash lane target (flash, flash-nvda, flash-uncensored or flash-fp4-local)"; fi
       die "the flash launcher serves $model, which no flash target names: ./switch-model.sh flash points it at one" ;;
  esac
  # what it downloads, summed on each disk before the first byte (lanes.sh room_per_disk); each
  # part checks its own room again as it comes
  local img_need=49 table_need=0
  if docker image inspect "$img" >/dev/null 2>&1; then img_need=0; fi
  if [ -n "$ple" ] && ! ls "$ple"/ple_table_*.bin >/dev/null 2>&1; then table_need=50; fi
  room_per_disk "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)|$img_need|its image" \
                "$hf|$(ckpt_room_gb flash "$hf" "$model" "$rev")|its checkpoint and its caches" \
                "${ple:-$hf}|$table_need|its PLE table, 47.7 GiB written at every boot"
  pull_image "$img" flash 49     # its peak, as install.sh counts it (DOCKER_NEED_GB)
  bash "$HERE/switch-model.sh" "$target" --fetch
  # The PLE table its launcher writes at every boot, 47.7 GiB in PLE_DIR, needs its room there,
  # counted after the checkpoint when the two share a disk (install.sh asks for both at once).
  if [ -n "$ple" ] && ! ls "$ple"/ple_table_*.bin >/dev/null 2>&1; then
    room_or_die 50 "$ple" "the flash lane's PLE table (47.7 GiB, written at every boot)"
  fi
  map="$({ grep -m1 -oE 'token-map-[0-9]+\.pt' "$launch" || true; } | head -1)"
  if [ -n "$map" ]; then
    section "The flash lane's reduced draft vocabulary"
    map="${map#token-map-}"
    flash_token_map "$img" "$hf" "$model" "$rev" "${map%.pt}" "its checkpoint did not come: re-run ./prepare-lane.sh flash" \
      || die "building the reduced draft vocabulary failed (the lines above say why): this Load stops here, and a Load again tries again (./prepare-lane.sh flash $target in a terminal)"
  fi
  printf '\n\033[1;32m── Done: the flash lane is prepared; its Load switches to it and starts it\033[0m\n'
}

prepare_27b(){
  local unit=/etc/systemd/system/qwen38-sglang.service img model draft drev hf target trepo trev tok=()
  [ -f "$unit" ] || die "the 27B lane is not installed on this box (no $unit): ./install.sh installs it"
  img="$(engine_image "$unit")"; model="$(arg_of "$unit" --model-path)"; hf="$(mount_of "$unit" /root/.cache/huggingface)"
  draft="$(arg_of "$unit" --speculative-draft-model-path)"; drev="$(arg_of "$unit" --speculative-draft-model-revision)"
  case "$img" in -*) img="" ;; esac                  # a flag where the image should be, as for the flash lane
  image_as_the_switch_reads "$unit" "$img"
  if [ -z "$img" ] || [ -z "$model" ] || [ -z "$hf" ]; then
    die "$unit names no image, model or cache where this expects it (hand-edited?): ./install.sh writes it again"
  fi
  case "${TARGET:-$model}" in
    stock|"$STOCK_REPO") target=stock; trepo="$STOCK_REPO"; trev="$STOCK_REV" ;;
    uncensored|"$UNC_REPO") target=uncensored; trepo="$UNC_REPO"; trev="$UNC_REV" ;;
    fp8|"$FP8_REPO") target=fp8; trepo="$FP8_REPO"; trev="$FP8_REV" ;;
    uncensored-fp8|"$UNCFP8_REPO") target=uncensored-fp8; trepo="$UNCFP8_REPO"; trev="$UNCFP8_REV" ;;
    *) if [ -n "$TARGET" ]; then die "$TARGET is not a 27B lane target (stock, uncensored, fp8 or uncensored-fp8)"; fi
       die "the 27B unit serves $model, which no 27B target names: ./switch-model.sh stock points it at one" ;;
  esac
  # what it downloads, summed on each disk before the first byte, as for the flash lane; the
  # checkpoint's 45 hold its DFlash2 draft, as install.sh counts them (5 more for it here asked
  # 98 where install.sh asks 93, found in review, 2026-10-15)
  local img_need=48
  if docker image inspect "$img" >/dev/null 2>&1; then img_need=0; fi
  room_per_disk "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)|$img_need|its image" \
                "$hf|$(ckpt_room_gb 27b "$hf" "$trepo" "$trev")|its checkpoint, its DFlash2 draft and their caches"
  pull_image "$img" 27b 48       # its peak, as install.sh counts it (DOCKER_NEED_GB)
  if [ -n "$draft" ]; then
    section "The 27B lane's DFlash2 draft ($draft @ ${drev:-main})"
    draft_in_cache "$unit" || room_or_die 5 "$hf" "its DFlash2 draft"
    # in the lane's own image, as install.sh fetches it; a token goes by name, never on a command line
    if [ -n "${HF_TOKEN:-}" ]; then export HF_TOKEN; tok=(-e HF_TOKEN); fi
    # --init: a job the cockpit stops takes its container with it (switch-model.sh says why)
    docker run --rm --init --network host --user "$(id -u):$(id -g)" --entrypoint python3 \
      -e HF_HOME=/hf -e HF_HUB_DOWNLOAD_TIMEOUT=30 -e HF_HUB_DISABLE_XET=1 ${tok[@]+"${tok[@]}"} \
      -v "$HERE/ckpt-fetch.py:/opt/qwen38/ckpt-fetch.py:ro" -v "$hf":/hf \
      "$img" /opt/qwen38/ckpt-fetch.py "$draft" "${drev:-main}" --offline-first --refs-main fill \
      || die "the DFlash2 draft did not come (no internet, or HuggingFace throttling: set HF_TOKEN, or Load again: downloads resume)"
    # Its config carries the YaRN patch when the unit serves the 1M window, or the draft crashes
    # at load; the switch patches the target's (install.sh does both, at once).
    if grep -q -- '--context-length 1010000' "$unit"; then
      python3 "$HERE/patch-yarn.py" "$hf" "$draft" "${drev:-main}" || die "YaRN patch failed on the DFlash2 draft"
    else
      python3 "$HERE/patch-yarn.py" --restore "$hf" "$draft" "${drev:-main}" || die "YaRN restore failed on the DFlash2 draft"
    fi
  fi
  bash "$HERE/switch-model.sh" "$target" --fetch
  printf '\n\033[1;32m── Done: the 27B lane is prepared; its Load switches to it and starts it\033[0m\n'
}

case "$1" in
  image|video|kolibri|nemotron|nemotron-super)
    # its checkpoint comes with its switch, as for any Load: no target here
    [ -f "/etc/systemd/system/qwen38-$1.service" ] || die "the $1 lane is not installed on this box (no unit): ./install.sh installs its units"
    exec bash "$HERE/install-$1.sh" --prepare ;;
  flash|27b)
    CONFIG_DIR="$HOME/.config/qwen38"; REPO_DIR="$HERE"
    # shellcheck source=lanes.sh
    . "$HERE/lanes.sh"
    pins_or_die
    if [ "$1" = flash ]; then prepare_flash; else prepare_27b; fi ;;
  *) usage ;;
esac
