# shellcheck shell=bash
# lanes.sh: what switch-model.sh and the installers share about the lanes. Sourced, never run;
# it needs CONFIG_DIR, and reads LANES_UNIT_DIR (the units' directory, /etc/systemd/system).
#
# The text lane the box comes back to. A switch to a side lane (image, video, Kolibri) disables
# the text lane's unit, so enablement cannot say which text lane was in use: it is written down
# here, one file, the SGLang text unit (the 27B's or the flash's) the box had at boot last. Until
# v1.29 each side lane kept a link to the lane it took the boot from, written by every switch,
# read by install.sh's six lines and by three uninstalls, each its own way: a round trip between
# side lanes made two links name each other, a side lane removed left another's link naming a
# unit that was gone, and a new lane meant editing all of it (found in review, 2026-10-14). With
# one record there is no chain to follow, and no loop to close.
LANES_UNIT_DIR="${LANES_UNIT_DIR:-/etc/systemd/system}"

text_lane_note(){  # $1 unit: the box's text lane is this one; anything but an SGLang text unit is ignored
  case "$1" in
    qwen38-sglang.service|qwen38-flash.service) mkdir -p "$CONFIG_DIR"; printf '%s\n' "$1" > "$CONFIG_DIR/text-lane" ;;
  esac
}

text_lane_last(){  # [$1 the side lane at boot] [$2 a file]: the text lane noted, else the one the links of v1.29 lead to
  # With $2, the record is not read, and only the links newer than that file are followed: the
  # record knows better than a link written before it (text_lane_migrate's second reading).
  local noted cur side ref="${2:-}"
  if [ -z "$ref" ]; then
    noted="$(cat "$CONFIG_DIR/text-lane" 2>/dev/null || true)"
    case "$noted" in qwen38-sglang.service|qwen38-flash.service) echo "$noted"; return 0 ;; esac
  fi
  # A box switched before v1.30 has only the links: they are followed from the side lane at boot
  # first (the others may be older than its switch), at most once through each side lane.
  for side in ${1:+"$1"} image video kolibri; do
    cur="qwen38-$side.service"
    for _ in 1 2 3 4; do               # its own link, then each other side lane's once
      case "$cur" in
        qwen38-sglang.service|qwen38-flash.service) echo "$cur"; return 0 ;;
        qwen38-image.service|qwen38-video.service|qwen38-kolibri.service)
          cur="${cur#qwen38-}"; cur="$CONFIG_DIR/lane-before-${cur%.service}"
          if [ -n "$ref" ] && ! [ "$cur" -nt "$ref" ]; then break; fi
          cur="$(cat "$cur" 2>/dev/null || true)" ;;
        *) break ;;
      esac
    done
  done
  return 0
}

text_lane_migrate(){  # once, on a box switched before v1.30: what its links say becomes the record
  local u="" side t f was="" ref=""
  if [ -s "$CONFIG_DIR/text-lane" ]; then
    # A link newer than the record was written after it by a version before v1.30 (the box
    # went back to one): it says what that version saw, so the links newer than the record are
    # read again, and the record answers where they lead to an older one (found in review,
    # 2026-10-14). v1.30 writes no link.
    for f in "$CONFIG_DIR/lane-before-image" "$CONFIG_DIR/lane-before-video" "$CONFIG_DIR/lane-before-kolibri"; do
      if [ -e "$f" ] && [ "$f" -nt "$CONFIG_DIR/text-lane" ]; then was="$(cat "$CONFIG_DIR/text-lane")"; fi
    done
    [ -n "$was" ] || return 0
    ref="$CONFIG_DIR/text-lane"
  fi
  # a text lane at boot is the text lane; else the links, from the side lane at boot first
  for t in qwen38-sglang.service qwen38-flash.service; do
    systemctl is-enabled --quiet "$t" 2>/dev/null && { u="$t"; break; }
  done
  if [ -z "$u" ]; then
    for side in image video kolibri; do
      if systemctl is-enabled --quiet "qwen38-$side.service" 2>/dev/null; then u="$(text_lane_last "$side" "$ref")"; break; fi
    done
  fi
  [ -n "$u" ] || u="$(text_lane_last "" "$ref")"
  [ -n "$u" ] || u="$was"              # links that lead nowhere new: the record stays what it said
  [ -z "$u" ] || text_lane_note "$u"
  return 0
}

text_lane_back(){  # [$1 the side lane at boot]: the text unit to give the boot back to, or nothing
  local u
  u="$(text_lane_last "${1:-}")"
  if [ -n "$u" ] && [ -f "$LANES_UNIT_DIR/$u" ]; then echo "$u"; return 0; fi
  # the one noted is gone (or nothing was noted): whichever text lane is installed, the 27B first
  for u in qwen38-sglang.service qwen38-flash.service; do
    [ -f "$LANES_UNIT_DIR/$u" ] && { echo "$u"; return 0; }
  done
  return 0
}

# A lane's unit, put in place. The [Unit] lines that name the other lanes (Conflicts=, After= and
# the like) order and exclude them, and systemd applies them at its daemon-reload: the engine reads
# none of them. A lane added to the box changes them in every other unit, and its update read as a
# change of what each engine runs: install.sh restarted the text lane that served (5 to 15 min), and
# the other installers said theirs ran older files (found in review, 2026-10-15). Changed only
# there, the unit keeps its date, the one lane_serves_older and engine-inputs.py go by, which leaves
# those lines out too. Prints same, deps (only those lines changed) or new.
UNIT_DEP_KEYS='^(Description|Documentation|Wants|Requires|Requisite|BindsTo|PartOf|Conflicts|After|Before)='
unit_put(){  # $1 the rendered unit, $2 where it goes (root's: read and written with sudo)
  # Each step returns on its failure: a function run in $(...) does not stop at one under set -e,
  # and the caller, which assigns what it prints, stops on that return.
  local old
  if [ -f "$2" ] && sudo cmp -s "$1" "$2"; then echo same; return 0; fi
  if [ -f "$2" ] && cmp -s <(sudo cat "$2" | grep -vE "$UNIT_DEP_KEYS" || true) <(grep -vE "$UNIT_DEP_KEYS" "$1" || true); then
    old="$(stat -c %Y "$2")" || return 1
    sudo install -m 644 "$1" "$2" || return 1
    sudo touch -m -d "@$old" "$2" || return 1
    echo deps; return 0
  fi
  sudo install -m 644 "$1" "$2" || return 1
  echo new
}

lane_serves_older(){  # $1 unit, $2... its files: true when it serves and one of them is newer than its start
  local unit="$1" started f; shift
  [ "$(systemctl show -p ActiveState --value "$unit" 2>/dev/null)" = active ] || return 1
  started="$(systemctl show -p ExecMainStartTimestamp --value --timestamp=unix "$unit" 2>/dev/null | tr -dc '0-9')"
  [ -n "$started" ] || return 1
  for f in "$@"; do
    [ -e "$f" ] && [ "$(stat -L -c %Y "$f")" -gt "$started" ] && return 0
  done
  return 1
}

stale_since(){  # $1 unit, $2... its files: true when it does not serve, or serves older files than these
  local started
  [ "$(systemctl show -p ActiveState --value "$1" 2>/dev/null)" = active ] || return 0
  started="$(systemctl show -p ExecMainStartTimestamp --value --timestamp=unix "$1" 2>/dev/null | tr -dc '0-9')"
  [ -n "$started" ] || return 0
  lane_serves_older "$@"
}

# The builds of a vLLM lane's image that no installed unit names, once the unit names this run's.
# One image can serve two lanes (the Nemotron lanes', install-nemotron-super.sh says why): a build
# goes when no unit names it any more, and not when its own lane's unit names another, which took
# away the build Super VL's unit still named, and its next Load looped on preparing one already
# built (found in review, 2026-10-16). -w: a tag that is the start of another is not that one.
lane_prune_images(){  # $1 the image's repository (qwen38-nemotron), $2 the build kept ("" for none)
  local img
  for img in $(docker images --format '{{.Repository}}:{{.Tag}}' "$1" 2>/dev/null); do
    [ "$img" = "$2" ] && continue
    if grep -qsFw -- "$img" "$LANES_UNIT_DIR"/qwen38-*.service; then
      echo "image kept: $img, which an installed lane's unit names"
    else
      docker rmi "$img" >/dev/null 2>&1 && echo "image removed: $img"
    fi
  done
  return 0
}

# The flash lane's reduced draft vocabulary, built once into $CONFIG_DIR, by install.sh and by the
# lane's first Load when it is installed on demand (prepare-lane.sh flash). Built inside the
# serving image, so the tokenizer that ranks the ids is the one the served model ships, not
# whatever the host has (the host has no transformers at all). Corpus-free by default: the
# ranking falls back to the tokenizer's own construction order, which is a frequency ranking
# learned over the tokenizer's training corpus. Drop a corpus.jsonl in the config directory (one
# JSON object per line with a "text" field, ideally the model's own output) and it is used as
# the primary ranking instead, which is what the model's drafter actually has to predict.
# Needs REPO_DIR (build-token-map.py) and CONFIG_DIR.
flash_token_map(){  # $1 image, $2 HF cache, $3 repo, $4 revision, $5 size, $6 what to say when no snapshot is there yet
  local image="$1" hf="$2" repo="$3" rev="$4" size="$5" name="token-map-$5.pt" cache corpus=()
  if [ -s "$CONFIG_DIR/$name" ]; then
    echo "reduced draft vocabulary already built: $CONFIG_DIR/$name"
    return 0
  fi
  echo "building the reduced draft vocabulary ($size ids, ~30 s)"
  # the commit a ref names (MODEL_REV=main): snapshots/main never exists, and the map was
  # skipped on every run (found in review, 2026-09-24)
  cache="$hf/hub/models--${repo//\//--}"
  [ -f "$cache/refs/$rev" ] && rev="$(cat "$cache/refs/$rev")"
  if [ ! -d "$cache/snapshots/$rev" ]; then
    echo "NOTE: no snapshot at the pinned revision yet; the draft vocabulary is skipped this run."
    echo "      $6"
    return 0
  fi
  [ -s "$CONFIG_DIR/corpus.jsonl" ] && corpus=(--corpus /out/corpus.jsonl)
  # --init: a job the cockpit stops takes its container with it (switch-model.sh says why)
  docker run --rm --init --entrypoint python3 \
    -v "$hf":/root/.cache/huggingface \
    -v "$CONFIG_DIR":/out \
    -v "$REPO_DIR":/repo:ro \
    "$image" \
    /repo/build-token-map.py \
      --snapshot "/root/.cache/huggingface/hub/models--${repo//\//--}/snapshots/$rev" \
      --out "/out/$name" --size "$size" "${corpus[@]}" \
    || return 1
}

# A digest pull leaves the image with no tag, and to Docker an image with no tag is
# dangling: `docker image prune` or `docker system prune`, the usual way to win back room
# on a full disk, deletes it, and the lane pulls 30 GB again at its next start (both
# serving images of the reference box were listed dangling, 2026-09-23). A local tag takes
# it out of that set, for both lanes' pins when they are here (install.sh), and for the image
# a lane's first Load pulls (prepare-lane.sh). uninstall.sh knows the name.
pin_tag() {   # $1 lane, $2 image: a digest reference present on this box gets its local tag
  local tag id
  case "$2" in *@sha256:*) ;; *) return 0 ;; esac
  id="$(docker image inspect "$2" --format '{{.Id}}' 2>/dev/null)" || return 0   # not on this box
  tag="qwen38-pinned:$1-$(printf '%s' "${2##*@sha256:}" | cut -c1-12)"
  if [ "$(docker image inspect "$tag" --format '{{.Id}}' 2>/dev/null || true)" = "$id" ]; then
    return 0   # already tagged
  fi
  if docker tag "$2" "$tag"; then
    echo "tagged $tag, so a docker image prune leaves it alone"
  else
    echo "NOTE: could not tag $2; a docker image prune would delete it"
  fi
}

flash_cannot_start(){
  # Prints why this GPU cannot start the flash lane and returns 0, or returns 1. Only the check's
  # status 1 says so (HMM or None, issue #54): any other, a check that is not there or cannot
  # run included, is no answer, and the engine decides as before. Read as "cannot", a missing
  # check stopped the lane for good, with no reason given (found in review, 2026-10-19).
  local why rc=0
  why="$(bash "$REPO_DIR/flash-ats-check.sh" 2>/dev/null)" || rc=$?
  [ "$rc" -eq 1 ] && [ -n "$why" ] || return 1
  printf '%s\n' "$why"
}

# The image a text lane's launcher (flash) or unit (27B) runs: the word on the line before its
# server command, whatever its form (a digest pin, a tag an operator passed, a local build).
# Two server commands: SGLang's module, and vLLM's serve line (the flash lane's FP4-Local
# config runs the b12x vLLM container, whose image sits on the line above it).
engine_image(){  # $1 the launcher or the unit
  { grep -B1 -m1 -E -- 'python3 -m sglang\.launch_server|(^| )vllm serve ' "$1" 2>/dev/null || true; } | head -1 | awk '{print $1}'
}
# The checkpoint a launcher or unit names, whichever of the three spellings it uses:
# --model-path (every SGLang config), a bare --model, or vLLM's own serve argument
# (`vllm serve <repo> --revision <sha>`, the flash lane's FP4-Local config, and the
# retired v1.4 launcher). The comments of the fp4 launcher never put the word vllm
# before serve in lower case, so the serve line is the only match. Every reader of an
# installed checkpoint goes through this; each copy that read only one spelling was
# blind to the FP4-Local launcher (three found in review, 2026-10-19).
launcher_model(){  # $1 the launcher or the unit
  local m
  m="$({ grep -m1 -oE -- '--model-path [^ ]+' "$1" 2>/dev/null || true; } | head -1 | cut -d' ' -f2)"
  [ -n "$m" ] || m="$({ grep -m1 -oE -- '--model [^ ]+' "$1" 2>/dev/null || true; } | head -1 | cut -d' ' -f2)"
  [ -n "$m" ] || m="$({ grep -m1 -oE 'vllm serve [^ ]+' "$1" 2>/dev/null || true; } | head -1 | sed -E 's/^vllm serve //')"
  printf '%s' "$m"
}
# Whether a checkpoint is whole in a cache: every shard its index names is in the snapshot of the
# revision, since a file appears there only once its blob is whole (a link to a blob gone is not).
ckpt_cached(){  # $1 = the repo's cache folder, $2 = the revision (a commit or a ref name)
  python3 - "$1" "$2" <<'PY'
import json, pathlib, sys
repo, rev = pathlib.Path(sys.argv[1]), sys.argv[2]
ref = repo / "refs" / rev
snap = repo / "snapshots" / (ref.read_text().strip() if ref.is_file() else rev)
try:
    shards = set(json.loads((snap / "model.safetensors.index.json").read_text())["weight_map"].values())
except (OSError, ValueError, KeyError, TypeError, AttributeError):
    shards = {"model.safetensors"}
sys.exit(0 if shards and all((snap / s).exists() for s in shards) else 1)
PY
}
# The Hugging Face cache the first of these units or launchers mounts (-v <cache>:/root/.cache/
# huggingface), nothing when none does; and the nearest folder of a path that exists, where df and
# stat can look. One reading each for every script: they were copied into a dozen places, a fix to
# one missing from the next (found in review, 2026-10-16).
unit_hf_cache(){  # $1... units or launchers
  { grep -hoE -- '-v [^ :]+:/root/\.cache/huggingface' "$@" 2>/dev/null || true; } \
    | head -1 | sed -e 's/^-v //' -e 's|:/root/\.cache/huggingface$||'
}
existing(){  # $1 a path
  local p="$1"; while [ ! -e "$p" ]; do p="$(dirname "$p")"; done; printf '%s\n' "$p"
}

# Whether the DFlash2 draft a 27B unit names is in the cache it mounts, at its revision (a ref
# such as main read through refs/): its config and a weights file (a snapshot links a file only
# once it is whole).
draft_in_cache(){  # $1 the 27B unit
  local repo rev hf snap f
  repo="$({ grep -oE -- '--speculative-draft-model-path [^ ]+' "$1" 2>/dev/null || true; } | head -1 | cut -d' ' -f2)"
  [ -n "$repo" ] || return 0   # no draft named: its image is all the lane needs
  rev="$({ grep -oE -- '--speculative-draft-model-revision [^ ]+' "$1" || true; } | head -1 | cut -d' ' -f2)"
  hf="$(unit_hf_cache "$1")"
  snap="$hf/hub/models--${repo//\//--}"
  [ -n "$rev" ] || rev=main
  if [ -f "$snap/refs/$rev" ]; then rev="$(cat "$snap/refs/$rev")"; fi
  [ -f "$snap/snapshots/$rev/config.json" ] || return 1
  for f in "$snap/snapshots/$rev"/*.safetensors; do [ -e "$f" ] && return 0; done
  return 1
}
# Room on the disk that holds a path, in GB: the nearest folder of it that exists.
free_gb_at(){  # $1 a path
  { df -BG --output=avail "$(existing "$1")" 2>/dev/null || true; } | tail -1 | tr -dc '0-9'
}

# The video lane's runtime writes two caches of its own at its first start, reused after, on the
# disk of $HOME and outside the lane's folder: copies of 52 transformed DiT weights (fused q/k/v,
# reordered rows) as file mappings, 11.2 GiB, and the video decoder's weights converted to its
# decode dtype, 4.5 GiB (reference box, 2026-09-25). One line each, folder|GiB still needed|what
# (what is already there comes off): install-video.sh counts them with the runtime, and
# switch-model.sh with the checkpoint, on the disk each lands on.
video_first_start_caches(){
  local p d gb what have n
  for p in "$HOME/.cache/sglang/diffusion/host_spill|12|the runtime's transformed weights" \
           "$HOME/.cache/sgl_diffusion/decode_dtype_store|5|the decoder's converted weights"; do
    IFS='|' read -r d gb what <<< "$p"
    have="$({ du -s --apparent-size -B1 "$d" 2>/dev/null || true; } | cut -f1)"
    n=$(( (gb * 1073741824 - ${have:-0}) / 1073741824 )); [ "$n" -ge 0 ] || n=0
    printf '%s|%s|%s\n' "$d" "$n" "$what"
  done
}

# The room a text lane's checkpoint asks of its cache's disk, with its caches, whole GB still
# needed, as install.sh counts it: 10 when the target is whole in the cache, else 45 for a 27B
# target (its DFlash2 draft in it) and 180 for flash, what the cache holds of the repo off, 10 at
# least. The switch checks it (switch-model.sh), and so does a first Load before its first
# download (prepare-lane.sh). A flash checkpoint already whole was asked 54 here and 10 by
# install.sh, and a first Load after a Download from Library was refused for want of room it
# did not need (found in review, 2026-10-15).
ckpt_room_gb(){  # $1 flash|27b, $2 the HF cache, $3 the repo, $4 its revision
  local n=45 have dir="$2/hub/models--${3//\//--}"
  # 106 GiB for the FP4-Local checkpoint (95.2 GiB) and its caches, against 180 GiB for the
  # SGLang configs' 136 GB checkpoint and caches; the SGLang configs' 50 GiB PLE table comes
  # on top of the 180 (install.sh, prepare-lane.sh), so their full ask is 230 GiB (found in
  # review, 2026-10-20).
  [ "$1" = flash ] && case "$3" in *FP4-Local*) n=106 ;; *) n=180 ;; esac
  if ckpt_cached "$dir" "${4:-main}"; then echo 10; return 0; fi
  have="$({ du -s --apparent-size -B1 "$dir/blobs" 2>/dev/null || true; } | cut -f1)"
  n=$(( n - ${have:-0} / 1073741824 )); [ "$n" -ge 10 ] || n=10
  echo "$n"
}
# What a first Load downloads, summed on each disk before the first byte: "path|GB|what" each.
# Each part checked its own room as it came, so a disk with room for the image and the
# checkpoint and not for the PLE table beside them took 150 GB, then stopped (found in review,
# 2026-10-15); install.sh sums its parts on each disk too. The refusal names the disk by the
# folders of its parts: "on one disk" left a box with two to guess which (the same review).
room_per_disk(){
  local -A need=() free=() what=() at=()
  local part path gb w p dev
  for part in "$@"; do
    IFS='|' read -r path gb w <<< "$part"
    [ "${gb:-0}" -gt 0 ] || continue
    p="$(existing "$path")"
    dev="$(stat -L -c %d "$p")"
    need[$dev]=$(( ${need[$dev]:-0} + gb )); free[$dev]="$(free_gb_at "$path")"
    what[$dev]="${what[$dev]:+${what[$dev]}; }$gb for $w"; at[$dev]="${at[$dev]:+${at[$dev]} and }$path"
  done
  for dev in "${!need[@]}"; do
    [ "${free[$dev]:-0}" -ge "${need[$dev]}" ] \
      || die "need ~${need[$dev]} GB free on the disk of ${at[$dev]} for this first Load (${what[$dev]}); found ${free[$dev]:-unknown} GB. Free some space, then Load the lane again: what came already is kept."
  done
}
