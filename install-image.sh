#!/usr/bin/env bash
# Install the Qwen-Image 2.1 lane: text-to-image, image editing and native RGBA,
# served by SGLang Diffusion on this box. Idempotent, and safe to run alone.
#
#   ./install-image.sh                 first install, or update in place
#   ./install-image.sh --no-smoke      skip the generation at the end
#   ./install-image.sh --uninstall     remove the unit and the venv (weights kept)
#
# install.sh runs this when it is given --with-image, which the one-liner passes
# through:  curl -fsSL .../get.sh | bash -s -- --with-image
#
# WHY IT IS OPT-IN. The checkpoint is 31 GB and the runtime another 7, on top of
# whatever the LLM lane already holds. A box installed for text should not silently
# grow 38 GB. Once installed, a plain ./install.sh keeps and updates it.
#
# WHY IT IS A VENV AND NOT DOCKER. The cookbook is explicit for this model: "This
# integration currently uses the Python/source command; no published Docker image is
# verified." Qwen-Image 2.1 is in no SGLang release either, so the runtime is a pinned
# source checkout. The release wheel goes in first all the same, because it carries the
# prebuilt aarch64 native kernels that a source tree does not build.
#
# ONE ENGINE AT A TIME. 31 GB of weights do not fit beside a serving LLM. The unit says
# so with Conflicts=, so starting this stops the LLM lane and starting an LLM lane stops
# this. Nothing here has to be remembered by the operator.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=qwen38-image.service
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
  -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
  *) die "unknown option: $a" ;;
esac; done

# What the installed unit says, so a re-run without variables changes nothing.
installed(){ { grep -m1 -E "^$1=" "$INSTALLED" 2>/dev/null || true; } | cut -d= -f2-; }
unit_flag(){ { grep -m1 -oE -- "$1 [^ \\\\]+" "$INSTALLED" 2>/dev/null || true; } | awk '{print $2}'; }

CONFIG_DIR="$HOME/.config/qwen38"
LANE_DIR="${IMAGE_LANE_DIR:-$(installed WorkingDirectory)}"; LANE_DIR="${LANE_DIR:-$HOME/.local/share/qwen38-image}"
VENV="$LANE_DIR/venv"
SRC="$LANE_DIR/sglang"
PORT="${IMAGE_PORT:-$(unit_flag --port)}"; PORT="${PORT:-30020}"
# The checkpoint and its revision, pinned like the rest: the revision this lane was
# measured with on 2026-09-22 (its main then, and on 2026-09-24). It was fetched at main,
# so a push upstream would have changed what a fresh install serves (found in review,
# 2026-09-24). IMAGE_MODEL_REV overrides it; another IMAGE_MODEL is fetched at
# IMAGE_MODEL_REV, or main.
IMAGE_MODEL_PIN="Qwen/Qwen-Image-2.1"
IMAGE_MODEL_PIN_REV="790c92633540aa0cb11d9abf19eb46d861714758"
MODEL="${IMAGE_MODEL:-$(unit_flag --model-path)}"; MODEL="${MODEL:-$IMAGE_MODEL_PIN}"
if [ "$MODEL" = "$IMAGE_MODEL_PIN" ]; then MODEL_REV="${IMAGE_MODEL_REV:-$IMAGE_MODEL_PIN_REV}"; else MODEL_REV="${IMAGE_MODEL_REV:-main}"; fi
# The installed unit's cache, then the one the text lane mounts, then the default: an
# update that ignored the unit downloaded 31 GB again into ~/.cache and rewrote HF_HOME on
# a box whose lane lived on another disk, and a first install beside a text lane on a
# custom cache put the checkpoint on the system disk (found in review, 2026-09-24).
installed_env(){ { grep -m1 -E "^Environment=$1=" "$INSTALLED" 2>/dev/null || true; } | cut -d= -f3-; }
TEXT_UNITS="${TEXT_UNITS:-/etc/systemd/system/qwen38-sglang.service $CONFIG_DIR/launch-flash.sh}"
text_cache(){
  # shellcheck disable=SC2086  # a list of paths
  { grep -hoE -- '-v [^ :]+:/root/\.cache/huggingface' $TEXT_UNITS 2>/dev/null || true; } \
    | head -1 | sed -e 's/^-v //' -e 's|:/root/\.cache/huggingface$||'
}
HF_CACHE="${HF_CACHE:-$(installed_env HF_HOME)}"; HF_CACHE="${HF_CACHE:-$(text_cache)}"
HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
# Qwen-Image 2.1 is in no SGLang release. This is the commit the lane on the reference
# box was measured against, end to end, on 2026-09-22. SGLANG_DIFFUSION_PIN overrides it
# only for someone deliberately testing another one.
PIN="${SGLANG_DIFFUSION_PIN:-ddebc52f237a1dbb56533469ab2ec2a7b856c4ab}"
# The released wheel that carries the prebuilt aarch64 kernels the source tree reuses.
WHEEL="${SGLANG_DIFFUSION_WHEEL:-0.5.20}"
# Loopback, and not from ENGINE_BIND: that variable belongs to the LLM lane, which has a
# key. This one has none (the diffusion parser has no --api-key at all), so a non-loopback
# bind puts an unauthenticated image generator on that interface. IMAGE_BIND overrides it
# for someone who means to, and is told what it costs.
IMAGE_BIND="${IMAGE_BIND:-$(unit_flag --host)}"; IMAGE_BIND="${IMAGE_BIND:-127.0.0.1}"
case "$IMAGE_BIND" in
  127.0.0.1|localhost) ;;
  *) echo "WARNING: binding $IMAGE_BIND. This lane has no API key (the diffusion runtime has"
     echo "         no --api-key), so anyone who can reach that address can generate on your GPU."
     echo "         Loopback plus the cockpit is the intended shape." ;;
esac
case "$IMAGE_BIND" in
  *[!0-9.]*|""|*..*) [ "$IMAGE_BIND" = localhost ] || die "IMAGE_BIND takes an IPv4 address, got: $IMAGE_BIND" ;;
esac
WEIGHTS_GB=31; RUNTIME_GB=11   # the checkpoint; the venv and checkout (7) and the pip build tree

if [ "$ACTION" = uninstall ]; then
  step "Removing the image lane"
  if [ -f "$INSTALLED" ]; then
    WAS_BOOT=0; systemctl is-enabled --quiet "$UNIT" 2>/dev/null && WAS_BOOT=1
    sudo systemctl disable --now "$UNIT" 2>/dev/null || true
    sudo rm -f "$INSTALLED"; sudo systemctl daemon-reload
    echo "unit removed"
    # When the image lane was the box's lane at boot, the text lane it replaced was
    # disabled by that switch: removing it left no engine at all, the next boot included,
    # and said nothing (found in review, 2026-09-24). The lane before images comes back.
    TEXT_ENABLED=0
    for u in qwen38-sglang.service qwen38-flash.service; do
      systemctl is-enabled --quiet "$u" 2>/dev/null && TEXT_ENABLED=1
    done
    if [ "$WAS_BOOT" -eq 1 ] && [ "$TEXT_ENABLED" -eq 0 ]; then
      BACK="$(cat "$CONFIG_DIR/lane-before-image" 2>/dev/null || true)"
      if [ -n "$BACK" ] && [ -f "/etc/systemd/system/$BACK" ]; then
        sudo systemctl enable "$BACK"
        echo "the text lane $BACK is enabled at boot again, as it was before the image lane;"
        echo "start it now with: sudo systemctl start $BACK   (or the cockpit)"
      else
        echo "NOTE: no engine is enabled at boot any more. Start a text lane from the cockpit, or:"
        echo "      sudo systemctl enable --now qwen38-sglang.service"
      fi
    fi
  fi
  # Only what this script put there: LANE_DIR can be a directory shared with other work,
  # and removing it whole took the rest with it (found in review, 2026-09-24).
  if [ -d "$LANE_DIR" ]; then
    rm -rf "$VENV" "$SRC"
    echo "runtime removed: $VENV and $SRC"
    rmdir "$LANE_DIR" 2>/dev/null || echo "kept $LANE_DIR: it holds files the image lane did not put there"
  fi
  echo "the 31 GB checkpoint is left in $HF_CACHE; delete it yourself if you want the space:"
  echo "  rm -rf $HF_CACHE/hub/models--${MODEL//\//--}"
  exit 0
fi

step "1/6 Preflight"
case "$(uname -m)" in aarch64|arm64) : ;; *) die "this lane is verified on the DGX Spark's ARM64 GB10 only (this box is $(uname -m))." ;; esac
command -v nvidia-smi >/dev/null || die "nvidia-smi not found: this needs the NVIDIA driver."
command -v git >/dev/null || die "git is required (stock on DGX OS)."
python3 -c 'import venv' 2>/dev/null || die "python3-venv is missing. Fix: sudo apt-get install -y python3-venv"
[ -s "$CONFIG_DIR/api-key" ] || die "no API key at $CONFIG_DIR/api-key. Run ./install.sh first: the cockpit is the authenticated door in front of this lane, and it reads that file."
# Measured where each part lands, not under $HOME (install.sh had fixed the same bug):
# the checkpoint goes to HF_CACHE, the runtime and its build to the lane's folder. What the
# cache already holds comes off the checkpoint's share, by bytes: the folder alone said
# nothing, since huggingface_hub creates it before the first byte (found in review,
# 2026-09-24).
existing(){ local p="$1"; while [ ! -e "$p" ]; do p="$(dirname "$p")"; done; printf '%s\n' "$p"; }
free_gb(){ { df -BG --output=avail "$(existing "$1")" 2>/dev/null || true; } | tail -1 | tr -dc '0-9'; }
HAVE_B="$({ du -s --apparent-size -B1 "$HF_CACHE/hub/models--${MODEL//\//--}/blobs" 2>/dev/null || true; } | cut -f1)"
# what is left, in whole GB (a complete 30.86 GiB checkpoint is 0 left, not 1)
WEIGHTS_NEED=$(( (WEIGHTS_GB * 1073741824 - ${HAVE_B:-0}) / 1073741824 )); [ "$WEIGHTS_NEED" -ge 0 ] || WEIGHTS_NEED=0
FREE_W="$(free_gb "$HF_CACHE")"; FREE_R="$(free_gb "$LANE_DIR")"
if [ "$(stat -c %d "$(existing "$HF_CACHE")")" = "$(stat -c %d "$(existing "$LANE_DIR")")" ]; then
  WANT=$((WEIGHTS_NEED + RUNTIME_GB))
  [ "${FREE_W:-0}" -ge "$WANT" ] \
    || die "${FREE_W:-?} GB free on the disk of $HF_CACHE and $LANE_DIR, this needs about $WANT GB ($WEIGHTS_NEED for the checkpoint, $RUNTIME_GB for the runtime and its build). Free some space, or point HF_CACHE or IMAGE_LANE_DIR at a bigger disk."
else
  [ "${FREE_W:-0}" -ge "$WEIGHTS_NEED" ] \
    || die "${FREE_W:-?} GB free under HF_CACHE=$HF_CACHE, the checkpoint needs about $WEIGHTS_NEED GB more. Free some space, or point HF_CACHE at a bigger disk."
  [ "${FREE_R:-0}" -ge "$RUNTIME_GB" ] \
    || die "${FREE_R:-?} GB free under $LANE_DIR, the runtime and its build need about $RUNTIME_GB GB. Free some space, or point IMAGE_LANE_DIR at a bigger disk."
fi
echo "OK: $(uname -m), key present, checkpoint $([ "$WEIGHTS_NEED" -eq 0 ] && echo 'already cached' || echo "$WEIGHTS_NEED GB to download") into $HF_CACHE"

step "2/6 Runtime ($LANE_DIR)"
mkdir -p "$LANE_DIR"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV" || die "could not create the venv at $VENV"
  echo "venv created on $("$VENV/bin/python" -V)"
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
# Two local changes to the pinned source, neither about images, both measured on the
# reference box (docs/image-lane.md, "The local changes to the pinned source"):
#   scheduler-idle-poll: the diffusion scheduler's loop never waits. recv_reqs() polls its
#     socket without blocking and nothing else in the loop sleeps, so a lane with nothing
#     to do held one CPU core at 100% (1.047 cores, against 0.029 for the 27B lane, which
#     parks itself with --sleep-on-idle; this runtime has no such flag). The patch waits on
#     the request socket for up to a second, as the LLM scheduler's own IdleSleeper does.
#     The same seed gives the same pixels, byte for byte, with and without it.
#   http-graceful-timeout: on shutdown its HTTP server waited for every open connection,
#     and a generation holds one for as long as it runs. A Stop during a 2048x2048 request
#     sat 60 s in "stopping" until systemd killed the lane and marked the unit failed
#     (2026-09-23). Now 5 s, then the requests in flight are cancelled and it stops clean.
PATCHES=(scheduler-idle-poll http-graceful-timeout)
declare -A PATCH_FIXES=(
  [scheduler-idle-poll]="an idle lane no longer holds a CPU core"
  [http-graceful-timeout]="a stop during a generation takes 5 s instead of timing out"
)
CURRENT="$(git -C "$SRC" rev-parse HEAD 2>/dev/null || true)"
if [ "$CURRENT" != "$PIN" ]; then
  # they are the only local edits in this tree: take them off, or the checkout trips on them
  for P in "${PATCHES[@]}"; do
    git -C "$SRC" apply --reverse "$HERE/image-sglang/$P.patch" >/dev/null 2>&1 || true
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
# again pulls a transformers that breaks the encoder this model needs.
# SGLANG_BUILD_RUST_EXTS=none: the pinned source declares five Rust extensions (gRPC, the
# Rust server, the radix tree, two multimodal processors), all of the LLM runtime; none
# is imported by the diffusion server. Building them needs cargo, which DGX OS does not
# ship: a box without a Rust toolchain failed here ("cargo is required ...", reference
# box in a clean login, 2026-09-23), and one with it spent the build on unused code.
if ! "$VENV/bin/python" -c 'import sglang, pathlib, sys; sys.exit(0 if str(pathlib.Path(sglang.__file__).parent).startswith("'"$SRC"'") else 1)' 2>/dev/null; then
  echo "overlaying the pinned source (editable, no dependency resolution, no Rust extensions)"
  SGLANG_BUILD_RUST_EXTS=none "$VENV/bin/pip" install --quiet --no-deps -e "$SRC/python" \
    || die "the editable overlay failed. The venv still holds the released wheel; re-run to retry."
fi
"$VENV/bin/python" - <<'PY' || die "the runtime does not know Qwen-Image 2.1. The pin may be wrong for this checkout."
import inspect, pathlib, sglang
from sglang.multimodal_gen import registry
src = pathlib.Path(sglang.__file__).parent
assert "Qwen/Qwen-Image-2.1" in inspect.getsource(registry), "Qwen-Image-2.1 is not in the model registry"
print(f"   runtime: {src}")
PY

step "4/6 Checkpoint ($MODEL at ${MODEL_REV:0:12}, ~31 GB, one-time, resumable)"
# The same two lessons the LLM lane learned the hard way: the Hub's Xet backend stalls
# silently on this box (0-8 MB/s against 89 on the classic CDN), and an unauthenticated
# pull gets throttled, so HF_TOKEN is passed through when it is set.
HF_HOME="$HF_CACHE" HF_HUB_DOWNLOAD_TIMEOUT=30 HF_HUB_DISABLE_XET=1 MODEL_REPO="$MODEL" MODEL_REV="$MODEL_REV" \
  "$VENV/bin/python" - <<'PY' || die "checkpoint download failed. Causes: no internet, HuggingFace throttling an unauthenticated download (set HF_TOKEN=<your token>), a pinned revision removed upstream (IMAGE_MODEL_REV=main serves the current one), or a permission error in the cache. Re-running resumes."
import os, time
from huggingface_hub import snapshot_download
repo, rev = os.environ["MODEL_REPO"], os.environ["MODEL_REV"]
for attempt in range(1, 5):
    try:
        path = snapshot_download(repo, revision=rev); print(path, flush=True); break
    except Exception as e:
        if attempt == 4:
            raise
        print(f"   attempt {attempt} stopped ({type(e).__name__}); resuming in 10 s", flush=True)
        time.sleep(10)
# The unit serves the repo by name with HF_HUB_OFFLINE=1, which resolves refs/main, and a
# download by commit writes no ref: main is pointed at the pinned commit, which is what
# is served.
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
RENDER="$(mktemp)"; trap 'rm -f "$RENDER"' EXIT
sed -e "s|__USER__|$(id -un)|g" -e "s|__GROUP__|$(id -gn)|g" \
    -e "s|__IMAGE_LANE_DIR__|$LANE_DIR|g" -e "s|__IMAGE_VENV__|$VENV|g" \
    -e "s|__IMAGE_MODEL__|$MODEL|g" -e "s|__IMAGE_PORT__|$PORT|g" \
    -e "s|__IMAGE_BIND__|$IMAGE_BIND|g" -e "s|__HF_CACHE__|$HF_CACHE|g" \
    -e "s|__HOME__|$HOME|g" \
    "$HERE/$UNIT.template" > "$RENDER"
grep -q '__[A-Z_]*__' "$RENDER" && die "the unit template still holds an unsubstituted placeholder: $(grep -o '__[A-Z_]*__' "$RENDER" | sort -u | tr '\n' ' ')"
# 0644 like every other unit, and set explicitly: mktemp creates 0600 and cp keeps the
# mode, which left this unit readable by root alone. systemd did not mind; everything
# else that reads the unit did, and failed quietly. switch-model.sh could not find the
# runtime, and the cockpit's reads of the port, the bind and the model all fell back to
# their defaults, which happened to be right on the reference box and would not have been
# on one installed with IMAGE_PORT= or IMAGE_BIND=.
if [ -f "$INSTALLED" ] && sudo cmp -s "$RENDER" "$INSTALLED"; then
  echo "unit unchanged"
  sudo chmod 644 "$INSTALLED"      # a unit installed before this fix is still 0600
else
  sudo install -m 644 "$RENDER" "$INSTALLED"; sudo systemctl daemon-reload
  echo "unit installed at $INSTALLED"
fi
# Installed, not switched to: the lane this box serves stays the lane it serves. The image
# lane becomes the boot lane the same way the other two do, by a switch, which is also
# what makes it come back after a reboot.
echo "installed as a lane of its own. Switch to it like any lane:"
echo "  the cockpit: pick Qwen-Image 2.1 in the switcher, Switch, stop the serving lane, Start"
echo "  a terminal : ./switch-model.sh image, then the two commands it prints"

if [ "$SMOKE" -eq 0 ]; then step "Done (smoke test skipped)"; exit 0; fi

step "6/6 Proving it serves (one image, then the box goes back to the lane it was serving)"
WAS_LLM=""
for u in qwen38-sglang.service qwen38-flash.service; do
  systemctl is-active --quiet "$u" 2>/dev/null && WAS_LLM="$u"
done
# If this lane was already serving, the test leaves it serving: stopping it on the way out
# would turn "re-run the installer" into "take the image lane down".
WAS_IMAGE=0
systemctl is-active --quiet "$UNIT" 2>/dev/null && WAS_IMAGE=1
[ -n "$WAS_LLM" ] && echo "note: $WAS_LLM is serving and will be stopped for this test, then started again."
# From here on the text lane is DOWN, so putting it back cannot live on the happy path:
# every die() below would leave the box serving nothing, with the image unit holding
# 31 GB, until somebody noticed. The trap runs on success and on every failure alike.
restore_text_lane() {
  [ "$WAS_IMAGE" -eq 1 ] || sudo systemctl stop "$UNIT" 2>/dev/null || true
  if [ -n "$WAS_LLM" ]; then
    echo "starting $WAS_LLM again"
    sudo systemctl start "$WAS_LLM" || echo "WARNING: could not restart $WAS_LLM. Do it by hand: sudo systemctl start $WAS_LLM"
  fi
  rm -f "$RENDER" "${OUT:-}"
}
trap restore_text_lane EXIT
sudo systemctl start "$UNIT"
READY=0
for i in $(seq 1 90); do
  curl -fsS -m 5 "http://$IMAGE_BIND:$PORT/health" >/dev/null 2>&1 && { READY=1; break; }
  sleep 10
done
[ "$READY" -eq 1 ] || { sudo journalctl -u "$UNIT" -n 40 --no-pager; die "the lane did not answer /health within 15 min. The journal above says why."; }
echo "up after ~$(( (i - 1) * 10 )) s; generating a 512x512 image"   # the first probe comes before any wait
OUT="$(mktemp)"
CODE=$(curl -sS -o "$OUT" -w '%{http_code}' -m 300 "http://$IMAGE_BIND:$PORT/v1/images/generations" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"A capybara reading a book by candlelight","size":"512x512","num_inference_steps":20,"output_format":"png","response_format":"b64_json","generator_device":"cpu","seed":42}')
[ "$CODE" = 200 ] || { echo "HTTP $CODE: $(head -c 300 "$OUT")"; die "the lane started but refused the smoke generation."; }
"$VENV/bin/python" - "$OUT" <<'PY' || die "the lane answered 200 with something that is not a 512x512 image."
import base64, json, struct, sys
d = json.load(open(sys.argv[1]))["data"][0]
raw = base64.b64decode(d["b64_json"])
assert raw[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
w, h = struct.unpack(">II", raw[16:24])
assert (w, h) == (512, 512), f"got {w}x{h}"
print(f"   {w}x{h} {'RGBA' if raw[25] == 6 else raw[25]}, {len(raw)/1e6:.1f} MB")
PY
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
    && echo "NOTE: the lane holds a CPU core while idle; the idle-loop fix is not in effect (see step 3)."
fi
# the trap stops the lane and brings the text lane back, whichever way this ends

step "Done: the image lane is installed and proved it serves on $IMAGE_BIND:$PORT"
echo "  switch to it : the cockpit's switcher (Qwen-Image 2.1), or ./switch-model.sh image"
echo "  back to text : the switcher again (any text target), or ./switch-model.sh stock"
echo "  generate     : the cockpit's Image tab, or the API on :$PORT (loopback, no key)"
