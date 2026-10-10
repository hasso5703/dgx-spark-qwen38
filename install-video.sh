#!/usr/bin/env bash
# Install the MiniMax-H3 video lane: text-to-video and video-and-audio generation,
# served by SGLang Diffusion on this box. Idempotent, and safe to run alone.
#
#   ./install-video.sh                 first install, or update in place
#   ./install-video.sh --no-smoke      skip the generation at the end
#   ./install-video.sh --uninstall     remove the unit and the venv (weights kept)
#
# install.sh runs this on every plain install since v1.20: a box gets every lane. The
# checkpoint (the fl2va partition plus the shared components, 135 GiB, WEIGHTS_GB below)
# and the runtime (another 11) need about 150 GB of headroom; when the disk does not
# have it, this refuses before downloading anything, install.sh says so and goes on, and
# ./install.sh --no-video leaves the lane out for good (a marker file remembers it).
#
# WHY IT IS A VENV AND NOT DOCKER. Like the image lane, no published Docker image is
# verified for this model: the cookbook serves it from Python/source. The release wheel
# goes in first all the same, because it carries the prebuilt aarch64 native kernels
# that a source tree does not build.
#
# WHY MINIMAX-H3. The SGLang cookbook verifies this model on the DGX Spark itself:
# about 12.1 s per denoise step steady-state, about 40 s of decode, about 12 min per
# warm request at 480P, with no flags at all (adding the discrete-GPU offload flags
# measured 2.1x slower on the same box). So this lane passes no placement flags: the
# verified recipe is the whole recipe. Video and audio are denoised jointly in one
# pass and muxed into one MP4.
#
# ONE ENGINE AT A TIME. The video checkpoint does not fit beside any serving lane.
# The unit says so with Conflicts=, so starting this stops the other lanes and
# starting another lane stops this. Nothing here has to be remembered by the operator.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=qwen38-video.service
INSTALLED="/etc/systemd/system/$UNIT"
die(){ printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
step(){ printf '\n\033[1;36m── %s\033[0m\n' "$*"; }

# Under sudo every path below moves to /root: the venv, the weights and the key the
# unit reads. install.sh refuses the same way, for the same reason.
if [ "$(id -u)" = "0" ] && [ "${ALLOW_ROOT:-0}" != "1" ]; then
  die "run this as the user who will use the box, not as root (everything it installs is addressed from \$HOME)."
fi

SMOKE=1; ACTION=install
for a in "$@"; do case "$a" in
  --no-smoke) SMOKE=0 ;;
  --uninstall) ACTION=uninstall ;;
  -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
  *) die "unknown option: $a" ;;
esac; done

# What the installed unit says, so a re-run without variables changes nothing.
installed(){ { grep -m1 -E "^$1=" "$INSTALLED" 2>/dev/null || true; } | cut -d= -f2-; }
unit_flag(){ { grep -m1 -oE -- "$1 [^ \\\\]+" "$INSTALLED" 2>/dev/null || true; } | awk '{print $2}'; }

CONFIG_DIR="$HOME/.config/qwen38"
LANE_DIR="${VIDEO_LANE_DIR:-$(installed WorkingDirectory)}"; LANE_DIR="${LANE_DIR:-$HOME/.local/share/qwen38-video}"
VENV="$LANE_DIR/venv"
SRC="$LANE_DIR/sglang"
PORT="${VIDEO_PORT:-$(unit_flag --port)}"; PORT="${PORT:-30022}"
# The checkpoint and its revision, pinned like the rest: refs/main on 2026-09-25.
# The first smoke on the reference box confirms it; until then the pin-watch alarms
# on any upstream move. VIDEO_MODEL_REV overrides it; another VIDEO_MODEL is fetched
# at VIDEO_MODEL_REV, or main.
VIDEO_MODEL_PIN="MiniMaxAI/MiniMax-H3"
VIDEO_MODEL_PIN_REV="42ed227ee7df40d41602854ae760620d6eb651fe"
MODEL="${VIDEO_MODEL:-$(unit_flag --model-path)}"; MODEL="${MODEL:-MiniMaxAI/MiniMax-H3}"   # = VIDEO_MODEL_PIN
if [ "$MODEL" = "$VIDEO_MODEL_PIN" ]; then MODEL_REV="${VIDEO_MODEL_REV:-$VIDEO_MODEL_PIN_REV}"; else MODEL_REV="${VIDEO_MODEL_REV:-main}"; fi
# fl2va serves text-only plus first/last-frame conditioning; ref2va serves reference
# image/audio/video conditioning instead. They are checkpoint partitions: the variant
# must match the weights fetched. VIDEO_VARIANT overrides it.
VARIANT="${VIDEO_VARIANT:-$(unit_flag --model-variant)}"; VARIANT="${VARIANT:-fl2va}"
case "$VARIANT" in fl2va|ref2va) ;; *) die "VIDEO_VARIANT takes fl2va or ref2va, got: $VARIANT" ;; esac
# The installed unit's cache, then the one the text lane mounts, then the default: an
# update that ignored the unit downloaded into ~/.cache and rewrote HF_HOME on a box
# whose lane lived on another disk (same bug as the image lane, 2026-09-24).
installed_env(){ { grep -m1 -E "^Environment=$1=" "$INSTALLED" 2>/dev/null || true; } | cut -d= -f3-; }
TEXT_UNITS="${TEXT_UNITS:-/etc/systemd/system/qwen38-sglang.service $CONFIG_DIR/launch-flash.sh}"
text_cache(){
  # shellcheck disable=SC2086  # a list of paths
  { grep -hoE -- '-v [^ :]+:/root/\.cache/huggingface' $TEXT_UNITS 2>/dev/null || true; } \
    | head -1 | sed -e 's/^-v //' -e 's|:/root/\.cache/huggingface$||'
}
HF_CACHE="${HF_CACHE:-$(installed_env HF_HOME)}"; HF_CACHE="${HF_CACHE:-$(text_cache)}"
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
# The SGLang source this lane was developed against, the image lane's pin until v1.24 (that
# lane moved to the v0.5.21 release), which registers MiniMaxH3Pipeline (checked 2026-09-25).
# SGLANG_DIFFUSION_PIN overrides it only for someone deliberately testing another one.
PIN="${SGLANG_DIFFUSION_PIN:-ddebc52f237a1dbb56533469ab2ec2a7b856c4ab}"
# The released wheel that carries the prebuilt aarch64 kernels the source tree reuses.
WHEEL="${SGLANG_DIFFUSION_WHEEL:-0.5.20}"
# Loopback, and not from ENGINE_BIND: that variable belongs to the LLM lane, which has
# a key. This one has none (the diffusion parser has no --api-key at all), so a
# non-loopback bind puts an unauthenticated video generator on that interface.
# VIDEO_BIND overrides it for someone who means to, and is told what it costs.
VIDEO_BIND="${VIDEO_BIND:-$(unit_flag --host)}"; VIDEO_BIND="${VIDEO_BIND:-127.0.0.1}"
case "$VIDEO_BIND" in
  127.0.0.1|localhost) ;;
  0.0.0.0) die "VIDEO_BIND=0.0.0.0 puts a keyless video generator on EVERY interface (LAN, tailnet, future NICs). This lane has no --api-key; bind loopback (the cockpit is the door) or name one specific address." ;;
  *) echo "WARNING: binding $VIDEO_BIND. This lane has no API key (the diffusion runtime has"
     echo "         no --api-key), so anyone who can reach that address can generate on your GPU."
     echo "         Loopback plus the cockpit is the intended shape." ;;
esac
case "$VIDEO_BIND" in
  *[!0-9.]*|""|*..*) [ "$VIDEO_BIND" = localhost ] || die "VIDEO_BIND takes an IPv4 address, got: $VIDEO_BIND" ;;
esac
WEIGHTS_GB=135; RUNTIME_GB=11   # the fl2va partition plus the shared components and root
# metadata, measured 2026-09-25: 144.1 GB cached in 88 files, which is 134.2 GiB, and the
# room check below counts GiB (it said 145, and a box with the whole checkpoint cached was
# told "10 GB to download", 2026-10-01); the venv and checkout (7) and the pip build tree

if [ "$ACTION" = uninstall ]; then
  step "Removing the video lane"
  if [ -f "$INSTALLED" ]; then
    WAS_BOOT=0; systemctl is-enabled --quiet "$UNIT" 2>/dev/null && WAS_BOOT=1
    sudo systemctl disable --now "$UNIT" 2>/dev/null || true
    sudo rm -f "$INSTALLED"; sudo systemctl daemon-reload
    echo "unit removed"
    # When the video lane was the box's lane at boot, whatever it replaced was disabled
    # by that switch: removing it left no engine at all (same trap as the image lane).
    # The lane before video comes back.
    TEXT_ENABLED=0
    for u in qwen38-sglang.service qwen38-flash.service qwen38-image.service; do
      systemctl is-enabled --quiet "$u" 2>/dev/null && TEXT_ENABLED=1
    done
    if [ "$WAS_BOOT" -eq 1 ] && [ "$TEXT_ENABLED" -eq 0 ]; then
      BACK="$(cat "$CONFIG_DIR/lane-before-video" 2>/dev/null || true)"
      if [ -n "$BACK" ] && [ -f "/etc/systemd/system/$BACK" ]; then
        sudo systemctl enable "$BACK"
        echo "the lane $BACK is enabled at boot again, as it was before the video lane;"
        echo "start it now with: sudo systemctl start $BACK   (or the cockpit)"
      else
        echo "NOTE: no engine is enabled at boot any more. Start a lane from the cockpit, or:"
        echo "      sudo systemctl enable --now qwen38-sglang.service"
      fi
    fi
  fi
  # Only what this script put there: LANE_DIR can be a directory shared with other work.
  if [ -d "$LANE_DIR" ]; then
    rm -rf "$VENV" "$SRC"
    echo "runtime removed: $VENV and $SRC"
    rmdir "$LANE_DIR" 2>/dev/null || echo "kept $LANE_DIR: it holds files the video lane did not put there"
  fi
  echo "the video checkpoint is left in $HF_CACHE; delete it yourself if you want the space:"
  echo "  rm -rf $HF_CACHE/hub/models--${MODEL//\//--}"
  exit 0
fi

step "1/6 Preflight"
case "$(uname -m)" in aarch64|arm64) : ;; *) die "this lane is verified on the DGX Spark's ARM64 GB10 only (this box is $(uname -m))." ;; esac
command -v nvidia-smi >/dev/null || die "nvidia-smi not found: this needs the NVIDIA driver."
command -v git >/dev/null || die "git is required (stock on DGX OS)."
# The MiniMax-H3 pipeline requires ffmpeg and ffprobe for media processing and
# validated output delivery, and refuses to start without them (its own words in
# minimax_h3_pipeline.py). Installed from the OS package, once, before anything else.
command -v ffmpeg >/dev/null || die "ffmpeg is missing, and the MiniMax-H3 pipeline refuses to start without it (with ffprobe). Fix: sudo apt-get install -y ffmpeg"
command -v ffprobe >/dev/null || die "ffprobe is missing, and the MiniMax-H3 pipeline refuses to start without it (with ffmpeg). Fix: sudo apt-get install -y ffmpeg"
# ensurepip, not just venv: a box without python3-venv makes a venv with no pip.
python3 -c 'import venv, ensurepip' 2>/dev/null || die "python3-venv is missing (a venv made without it has no pip). Fix: sudo apt-get install -y python3-venv"
# python3-dev too: xatlas, which sglang[diffusion] asks for, is built here against Python's
# headers (PyPI has no aarch64 wheel of it), and DGX OS installs python3-pip without them.
[ -f "$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["include"])')/Python.h" ] || die "python3-dev is missing (xatlas, which this lane needs, is built here against Python's headers). Fix: sudo apt-get install -y python3-dev"
[ -s "$CONFIG_DIR/api-key" ] || die "no API key at $CONFIG_DIR/api-key. Run ./install.sh first: the cockpit is the authenticated door in front of this lane, and it reads that file."
# Measured where each part lands, by bytes in the blobs: the folder alone says nothing,
# since huggingface_hub creates it before the first byte.
existing(){ local p="$1"; while [ ! -e "$p" ]; do p="$(dirname "$p")"; done; printf '%s\n' "$p"; }
free_gb(){ { df -BG --output=avail "$(existing "$1")" 2>/dev/null || true; } | tail -1 | tr -dc '0-9'; }
HAVE_B="$({ du -s --apparent-size -B1 "$HF_CACHE/hub/models--${MODEL//\//--}/blobs" 2>/dev/null || true; } | cut -f1)"
# what is left, in whole GB (a complete checkpoint is 0 left, not 1)
WEIGHTS_NEED=$(( (WEIGHTS_GB * 1073741824 - ${HAVE_B:-0}) / 1073741824 )); [ "$WEIGHTS_NEED" -ge 0 ] || WEIGHTS_NEED=0
FREE_W="$(free_gb "$HF_CACHE")"; FREE_R="$(free_gb "$LANE_DIR")"
if [ "$(stat -c %d "$(existing "$HF_CACHE")")" = "$(stat -c %d "$(existing "$LANE_DIR")")" ]; then
  WANT=$((WEIGHTS_NEED + RUNTIME_GB))
  [ "${FREE_W:-0}" -ge "$WANT" ] \
    || die "${FREE_W:-?} GB free on the disk of $HF_CACHE and $LANE_DIR, this needs about $WANT GB ($WEIGHTS_NEED for the checkpoint, $RUNTIME_GB for the runtime and its build). Free some space, or point HF_CACHE or VIDEO_LANE_DIR at a bigger disk."
else
  [ "${FREE_W:-0}" -ge "$WEIGHTS_NEED" ] \
    || die "${FREE_W:-?} GB free under HF_CACHE=$HF_CACHE, the checkpoint needs about $WEIGHTS_NEED GB more. Free some space, or point HF_CACHE at a bigger disk."
  [ "${FREE_R:-0}" -ge "$RUNTIME_GB" ] \
    || die "${FREE_R:-?} GB free under $LANE_DIR, the runtime and its build need about $RUNTIME_GB GB. Free some space, or point VIDEO_LANE_DIR at a bigger disk."
fi
echo "OK: $(uname -m), key present, checkpoint $([ "$WEIGHTS_NEED" -eq 0 ] && echo 'already cached' || echo "$WEIGHTS_NEED GB to download") into $HF_CACHE"

step "2/6 Runtime ($LANE_DIR)"
mkdir -p "$LANE_DIR"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV" || die "could not create the venv at $VENV"
  echo "venv created on $("$VENV/bin/python" -V)"
elif ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
  # A venv made while python3-venv was missing has its python and no pip, and every
  # later run took it for a finished one, then died on its first pip call.
  echo "the venv at $VENV has no pip: making it again"
  python3 -m venv --clear "$VENV" || die "could not make the venv at $VENV again"
else
  echo "venv already at $VENV ($("$VENV/bin/python" -V))"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null

step "3/6 SGLang Diffusion (released wheel first, then the pinned source over it)"
# Order matters and is not cosmetic. The wheel resolves the whole dependency tree AND
# ships sglang-kernel built for aarch64; installing the source tree first leaves the
# runtime without those kernels and the server dies on the first request.
if ! "$VENV/bin/python" -c 'import sglang' 2>/dev/null; then
  echo "installing sglang[diffusion]==$WHEEL (~10 min on this box: torch and the kernels are large)"
  "$VENV/bin/pip" install --quiet --pre "sglang[diffusion]==$WHEEL" \
    || die "the released wheel would not install. Re-run: pip resumes. If it keeps failing, SGLANG_DIFFUSION_WHEEL=<version> picks another."
fi
if [ ! -d "$SRC/.git" ]; then
  git clone --quiet https://github.com/sgl-project/sglang "$SRC" || die "could not clone SGLang."
fi
# One local change to the pinned source, the image lane's scheduler-idle-poll (one file
# in image-sglang/ for both lanes, which applies to the pin of each). The diffusion
# scheduler's loop never waits: recv_reqs() polls its socket without blocking and nothing
# else in the loop sleeps, so a lane with nothing to do held one CPU core at 100%, kept
# the box's hottest zone near 61 C at rest (43 C with no lane loaded) and its fans
# loud. The patch waits on the request socket for up to a second, as the LLM scheduler's
# own IdleSleeper does. Measured on the reference box (2026-10-06), 3 min after the lane
# was ready: 1.05 cores without it, 0.05 with it, the hottest zone 67 C against 54 C; a
# 4 s 480P request was picked up in the second it arrived and ran in 691 s (592 to 760
# measured before), and a stop at rest took 0.24 s. The rest is the cookbook's recipe
# as upstream wrote it at the pinned commit; the honest consequences live in the unit
# template (a stop cancels the generation in flight, measured under a second on all
# seven logged stops, with TimeoutStopSec as a ceiling, not a wait) and in
# docs/video-lane.md.
PATCHES=(scheduler-idle-poll)
declare -A PATCH_FIXES=(
  [scheduler-idle-poll]="an idle lane no longer holds a CPU core"
)
CURRENT="$(git -C "$SRC" rev-parse HEAD 2>/dev/null || true)"
if [ "$CURRENT" != "$PIN" ]; then
  # it is the only local edit in this tree: take it off, or the checkout trips on it
  # Only from a file that has local edits: a fresh clone stands on upstream's main, where a
  # patch merged upstream reverses cleanly too, and taking it off there wrote the very edit
  # that then blocked the checkout (a first install, 2026-10-10, once main carried #43391).
  for P in "${PATCHES[@]}"; do
    PF="$HERE/image-sglang/$P.patch"
    git -C "$SRC" diff --quiet -- "$(grep -m1 '^+++ b/' "$PF" | cut -c7-)" 2>/dev/null && continue
    git -C "$SRC" apply --reverse "$PF" >/dev/null 2>&1 || true
  done
  git -C "$SRC" fetch --quiet origin "$PIN" 2>/dev/null || git -C "$SRC" fetch --quiet origin
  git -C "$SRC" checkout --quiet "$PIN" \
    || die "could not check out $PIN: not in the SGLang repository, or local edits in $SRC block it (git -C $SRC status says which)."
  echo "source checked out at ${PIN:0:12}"
else
  echo "source already at ${PIN:0:12}"
fi
for P in "${PATCHES[@]}"; do
  PF="$HERE/image-sglang/$P.patch"
  if git -C "$SRC" apply --reverse --check "$PF" >/dev/null 2>&1; then
    echo "$P: already applied"
  elif git -C "$SRC" apply --check "$PF" >/dev/null 2>&1; then
    git -C "$SRC" apply "$PF" || die "$P passed its check and then failed to apply to $SRC."
    echo "$P: applied, ${PATCH_FIXES[$P]} (effective at the lane's next start)"
  else
    # A newer pin may have changed that code, or fixed it upstream. The lane works either
    # way; the smoke test below measures what an idle lane costs and says so.
    echo "NOTE: $P does not apply to ${PIN:0:12}; that part runs as upstream wrote it."
  fi
done
# --no-deps: the wheel above already resolved them, and letting the source tree resolve
# again pulls a transformers that breaks the encoders this model needs.
# SGLANG_BUILD_RUST_EXTS=none: the pinned source declares five Rust extensions, all of
# the LLM runtime; none is imported by the diffusion server. Building them needs cargo,
# which DGX OS does not ship.
if ! "$VENV/bin/python" -c 'import sglang, pathlib, sys; sys.exit(0 if str(pathlib.Path(sglang.__file__).parent).startswith("'"$SRC"'") else 1)' 2>/dev/null; then
  echo "overlaying the pinned source (editable, no dependency resolution, no Rust extensions)"
  SGLANG_BUILD_RUST_EXTS=none "$VENV/bin/pip" install --quiet --no-deps -e "$SRC/python" \
    || die "the editable overlay failed. The venv still holds the released wheel; re-run to retry."
fi
"$VENV/bin/python" - <<'PY' || die "the runtime does not know MiniMax-H3. The pin may be wrong for this checkout."
import inspect, pathlib, sglang
from sglang.multimodal_gen import registry
src = pathlib.Path(sglang.__file__).parent
assert "MiniMax-H3" in inspect.getsource(registry), "MiniMax-H3 is not in the model registry"
print(f"   runtime: {src}")
PY

step "4/6 Checkpoint ($MODEL at ${MODEL_REV:0:12}, one-time, resumable)"
# Only the partition this lane serves is fetched, by allowlist. The repo holds both
# weight partitions (FL2VA/ and Ref2VA/, 81 files each) plus a root-level native
# layout, and --model-variant fl2va roots the pipeline in the FL2VA subfolder alone
# (default_model_subfolder in minimax_h3_pipeline.py): fetching the rest is dead
# weight on a box whose disk is the constraint (seen on the reference box, 2026-09-25:
# the full pull filled the disk at 98% and died). A lane installed on the ref2va
# weights fetches those instead.
if [ "$MODEL" = "$VIDEO_MODEL_PIN" ]; then
  if [ "$VARIANT" = "fl2va" ]; then ALLOW="FL2VA/* model_index.json modular_model_index.json scheduler/* audio_scheduler/* *.md LICENSE*"
  else ALLOW="Ref2VA/* model_index.json modular_model_index.json scheduler/* audio_scheduler/* *.md LICENSE*"; fi
else ALLOW="";
fi
# Said out loud, because a silent empty here refetches the other partition.
echo "fetching with allow: ${ALLOW:-(whole repo)} (variant $VARIANT, model $MODEL)"
# The same two lessons the LLM lane learned the hard way: the Hub's Xet backend stalls
# silently on this box (0-8 MB/s against 89 on the classic CDN), and an unauthenticated
# pull gets throttled, so HF_TOKEN is passed through when it is set.
HF_HOME="$HF_CACHE" HF_HUB_DOWNLOAD_TIMEOUT=30 HF_HUB_DISABLE_XET=1 MODEL_REPO="$MODEL" MODEL_REV="$MODEL_REV" MODEL_ALLOW="$ALLOW" \
  "$VENV/bin/python" - <<'PY' || die "checkpoint download failed. Causes: no internet, HuggingFace throttling an unauthenticated download (set HF_TOKEN=<your token>), a pinned revision removed upstream (VIDEO_MODEL_REV=main serves the current one), a disk too small for the partition served, or a permission error in the cache. Re-running resumes."
import os, time
from huggingface_hub import snapshot_download
repo, rev = os.environ["MODEL_REPO"], os.environ["MODEL_REV"]
allow = [p for p in os.environ["MODEL_ALLOW"].split() if p] or None
for attempt in range(1, 5):
    try:
        path = snapshot_download(repo, revision=rev, allow_patterns=allow); print(path, flush=True); break
    except Exception as e:
        if attempt == 4:
            raise
        print(f"   attempt {attempt} stopped ({type(e).__name__}); resuming in 10 s", flush=True)
        time.sleep(10)
# The unit serves the repo by name with HF_HUB_OFFLINE=1, which resolves refs/main, and
# a download by commit writes no ref: main is pointed at the pinned commit, which is
# what is served.
sha = os.path.basename(path.rstrip("/"))
ref = os.path.join(os.path.dirname(os.path.dirname(path.rstrip("/"))), "refs", "main")
if len(sha) == 40 and sha == rev:
    os.makedirs(os.path.dirname(ref), exist_ok=True)
    old = open(ref).read().strip() if os.path.exists(ref) else ""
    if old != sha:
        with open(ref, "w") as f:
            f.write(sha)
        print(f"   refs/main -> {sha[:12]}" + (f" (was {old[:12]})" if old else ""), flush=True)
PY

step "5/6 Service"
# The lane's own output directory: the download needs the file to still be there,
# so the server writes every MP4 here instead of a temp dir it deletes. Old videos
# stay until deleted by hand; uninstall.sh leaves them alone.
mkdir -p "$LANE_DIR/outputs"
# The unit runs engine-preflight.sh before every start: run on its own, this installer
# must put it in place, or the lane would never start.
mkdir -p "$CONFIG_DIR"
cmp -s "$HERE/engine-preflight.sh" "$CONFIG_DIR/engine-preflight.sh" \
  || install -m 755 "$HERE/engine-preflight.sh" "$CONFIG_DIR/engine-preflight.sh"
RENDER="$(mktemp)"; trap 'rm -f "$RENDER"' EXIT
sed -e "s|__USER__|$(id -un)|g" -e "s|__GROUP__|$(id -gn)|g" \
    -e "s|__VIDEO_LANE_DIR__|$LANE_DIR|g" -e "s|__VIDEO_VENV__|$VENV|g" \
    -e "s|__VIDEO_MODEL__|$MODEL|g" -e "s|__VIDEO_VARIANT__|$VARIANT|g" \
    -e "s|__VIDEO_PORT__|$PORT|g" \
    -e "s|__VIDEO_BIND__|$VIDEO_BIND|g" -e "s|__HF_CACHE__|$HF_CACHE|g" \
    -e "s|__HOME__|$HOME|g" \
    "$HERE/$UNIT.template" > "$RENDER"
grep -q '__[A-Z][A-Z0-9_]*__' "$RENDER" && die "the unit template still holds an unsubstituted placeholder: $(grep -o '__[A-Z][A-Z0-9_]*__' "$RENDER" | sort -u | tr '\n' ' ')"
# 0644 like every other unit, and set explicitly: mktemp creates 0600 and cp keeps the
# mode, which left the image unit readable by root alone until this fix. Everything
# that reads the unit (switch-model.sh, the cockpit) failed quietly on a 0600 unit.
if [ -f "$INSTALLED" ] && sudo cmp -s "$RENDER" "$INSTALLED"; then
  echo "unit unchanged"
  sudo chmod 644 "$INSTALLED"      # a unit installed before this fix is still 0600
else
  sudo install -m 644 "$RENDER" "$INSTALLED"; sudo systemctl daemon-reload
  echo "unit installed at $INSTALLED"
fi
# Installed, not switched to: the lane this box serves stays the lane it serves. The
# video lane becomes the boot lane the same way the other lanes do, by a switch, which
# is also what makes it come back after a reboot.
echo "installed as a lane of its own. Switch to it like any lane:"
echo "  the cockpit: Load on MiniMax-H3 in the Lanes view (it switches, stops the serving lane, starts this one)"
echo "  a terminal : ./switch-model.sh video, then the two commands it prints"

# The smoke body is t2va on MiniMax-H3's fl2va weights: the ref2va weights take no
# unconditioned call, and another model's id the smoke does not name. Asking for
# either and booting fifteen minutes for a body declared wrong here is a 400 on a
# 15-minute bill; skip the smoke and say why (the same exit as --no-smoke).
if [ "$SMOKE" -eq 1 ] && { [ "$VARIANT" != "fl2va" ] || [ "$MODEL" != "MiniMaxAI/MiniMax-H3" ]; }; then
  echo "smoke skipped: its t2va body fits MiniMax-H3 on the fl2va weights only (this install: model $MODEL, variant $VARIANT)"
  SMOKE=0
fi
if [ "$SMOKE" -eq 0 ]; then step "Done (smoke test skipped)"; exit 0; fi

step "6/6 Proving it serves (one short video, then the box goes back to the lane it was serving)"
WAS_OTHER=""
for u in qwen38-sglang.service qwen38-flash.service qwen38-image.service qwen38-llamacpp.service; do
  systemctl is-active --quiet "$u" 2>/dev/null && WAS_OTHER="$u"
done
# If this lane was already serving, the test leaves it serving: stopping it on the way
# out would turn "re-run the installer" into "take the video lane down".
WAS_VIDEO=0
systemctl is-active --quiet "$UNIT" 2>/dev/null && WAS_VIDEO=1
[ -n "$WAS_OTHER" ] && echo "note: $WAS_OTHER is serving and will be stopped for this test, then started again."
# From here on the other lane is DOWN, so putting it back cannot live on the happy
# path: every die() below would leave the box serving nothing. The trap runs on
# success and on every failure alike.
restore_other_lane() {
  [ "$WAS_VIDEO" -eq 1 ] || sudo systemctl stop "$UNIT" 2>/dev/null || true
  if [ -n "$WAS_OTHER" ]; then
    echo "starting $WAS_OTHER again"
    sudo systemctl start "$WAS_OTHER" || echo "WARNING: could not restart $WAS_OTHER. Do it by hand: sudo systemctl start $WAS_OTHER"
  fi
  rm -f "$RENDER" "${OUT:-}"
}
trap restore_other_lane EXIT
sudo systemctl start "$UNIT"
READY=0
for i in $(seq 1 90); do
  curl -fsS -m 5 "http://$VIDEO_BIND:$PORT/health" >/dev/null 2>&1 && { READY=1; break; }
  sleep 10
done
[ "$READY" -eq 1 ] || { sudo journalctl -u "$UNIT" -n 40 --no-pager || true; die "the lane did not answer /health within 15 min. The journal above says why."; }
echo "up after ~$(( (i - 1) * 10 )) s; generating a 4 s video at 480P (about 12 min on this box)"
OUT="$(mktemp)"
# Video creation is asynchronous: POST returns an id with a queued status, then the
# content is downloaded once the poll says completed. `|| true`: a transport failure
# made curl exit non-zero inside the assignment, and set -e ended the script before
# the message below (same trap as the image lane).
# The body carries task and target because the lane 400s without each (measured
# 2026-09-25, docs/video-lane.md): a 400 here throws away a 15-minute boot. t2va
# is the smoke of the default fl2va variant; a ref2va install would need a
# conditioned body, which no default install path uses.
CODE=$(curl -sS -o "$OUT" -w '%{http_code}' -m 60 "http://$VIDEO_BIND:$PORT/v1/videos" \
  -H 'Content-Type: application/json' \
  -d '{"model":"MiniMax-H3","prompt":"A cat walks across a sunlit room, dust in the air","seconds":4,"task":"t2va","target":{"short_edge":480,"aspect_ratio":"16:9","duration_seconds":4}}' || true)
if [ "$CODE" != 200 ]; then
  echo "HTTP ${CODE:-000}: $(head -c 300 "$OUT" 2>/dev/null)"
  sudo journalctl -u "$UNIT" -n 40 --no-pager || true
  die "the lane started but did not take the smoke video (HTTP ${CODE:-000}); the journal above says why."
fi
VID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("id",""))' "$OUT")"
[ -n "$VID" ] || die "the lane answered 200 with no video id: $(head -c 300 "$OUT")"
echo "   video $VID queued; polling to completed (up to 40 min)"
DONE=0
for i in $(seq 1 240); do
  ST="$(curl -sS -m 20 "http://$VIDEO_BIND:$PORT/v1/videos" -H 'Content-Type: application/json' 2>/dev/null | python3 -c '
import json,sys
try:
    items = json.load(sys.stdin)["data"]
except Exception:
    print("unknown"); raise SystemExit
print(next((v.get("status","unknown") for v in items if v.get("id") == sys.argv[1]), "missing"))' "$VID" 2>/dev/null || echo unknown)"
  case "$ST" in
    completed) DONE=1; break ;;
    failed|error) sudo journalctl -u "$UNIT" -n 40 --no-pager || true
                  die "the smoke video $VID ended in $ST; the journal above says why." ;;
  esac
  sleep 10
done
[ "$DONE" -eq 1 ] || { sudo journalctl -u "$UNIT" -n 40 --no-pager || true
  die "the smoke video $VID did not complete in 40 min (last status: $ST); the journal above says why."; }
curl -fsS -m 300 "http://$VIDEO_BIND:$PORT/v1/videos/$VID/content" -o "$OUT.mp4" \
  || die "the video completed but its content did not download."
SZ="$(stat -c %s "$OUT.mp4" 2>/dev/null || echo 0)"
[ "$SZ" -gt 100000 ] || die "the downloaded video is only $SZ bytes, not a real video."
echo "   $(numfmt --to=iec "$SZ") of MP4; keeping it at $OUT.mp4 is left to you (trap removes only \$OUT)"
# What the lane costs with nothing to do, from its own cgroup: the CPU time systemd
# accounts to it over 10 s, after 5 s for the request's tail to finish. About 0 with the
# idle-loop fix above, about 1 core without it. A note, not a failure: it serves either way.
CG="/sys/fs/cgroup$(systemctl show -p ControlGroup --value "$UNIT" 2>/dev/null)"
if [ -r "$CG/cpu.stat" ]; then
  sleep 5
  U0=$(awk '/^usage_usec/{print $2}' "$CG/cpu.stat"); sleep 10
  U1=$(awk '/^usage_usec/{print $2}' "$CG/cpu.stat")
  IDLE_CORES=$(awk -v a="$U0" -v b="$U1" 'BEGIN{printf "%.2f", (b-a)/1e7}')
  echo "   at rest: $IDLE_CORES CPU cores"
  awk -v c="$IDLE_CORES" 'BEGIN{exit !(c > 0.5)}' \
    && echo "NOTE: the lane holds a CPU core while idle; the idle-loop fix is not in effect (see step 3; a lane started before it went in keeps the old loop until its next start)."
fi
# the trap stops the lane and brings the other lane back, whichever way this ends

step "Done: the video lane is installed and proved it serves on $VIDEO_BIND:$PORT"
echo "  switch to it : Load on MiniMax-H3 in the cockpit's Lanes view, or ./switch-model.sh video"
echo "  back         : Load on any other lane there, or ./switch-model.sh stock"
echo "  generate     : the cockpit's Video view, or the API on :$PORT (loopback, no key)"
