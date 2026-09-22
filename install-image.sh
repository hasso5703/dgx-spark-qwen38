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
MODEL="${IMAGE_MODEL:-$(unit_flag --model-path)}"; MODEL="${MODEL:-Qwen/Qwen-Image-2.1}"
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
NEED_GB=42   # 31 weights + 7 runtime + margin for the pip build tree

if [ "$ACTION" = uninstall ]; then
  step "Removing the image lane"
  if [ -f "$INSTALLED" ]; then
    sudo systemctl disable --now "$UNIT" 2>/dev/null || true
    sudo rm -f "$INSTALLED"; sudo systemctl daemon-reload
    echo "unit removed"
  fi
  [ -d "$LANE_DIR" ] && { rm -rf "$LANE_DIR"; echo "runtime removed: $LANE_DIR"; }
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
FREE_GB=$(df -BG --output=avail "$HOME" | tail -1 | tr -dc '0-9')
HAVE_WEIGHTS=0
[ -d "$HF_CACHE/hub/models--${MODEL//\//--}" ] && HAVE_WEIGHTS=1
WANT=$NEED_GB; [ "$HAVE_WEIGHTS" -eq 1 ] && WANT=11
[ "${FREE_GB:-0}" -ge "$WANT" ] \
  || die "$FREE_GB GB free on \$HOME, this needs about $WANT GB (31 for the checkpoint, 7 for the runtime, the rest for the build). Free some space, or point HF_CACHE at a bigger disk."
echo "OK: $(uname -m), $FREE_GB GB free, key present, checkpoint $([ $HAVE_WEIGHTS -eq 1 ] && echo 'already cached' || echo 'to download')"

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
CURRENT="$(git -C "$SRC" rev-parse HEAD 2>/dev/null || true)"
if [ "$CURRENT" != "$PIN" ]; then
  git -C "$SRC" fetch --quiet origin "$PIN" 2>/dev/null || git -C "$SRC" fetch --quiet origin
  git -C "$SRC" checkout --quiet "$PIN" || die "commit $PIN not found in the SGLang repository."
  echo "source checked out at ${PIN:0:12}"
else
  echo "source already at ${PIN:0:12}"
fi
# --no-deps: the wheel above already resolved them, and letting the source tree resolve
# again pulls a transformers that breaks the encoder this model needs.
if ! "$VENV/bin/python" -c 'import sglang, pathlib, sys; sys.exit(0 if str(pathlib.Path(sglang.__file__).parent).startswith("'"$SRC"'") else 1)' 2>/dev/null; then
  echo "overlaying the pinned source (editable, no dependency resolution)"
  "$VENV/bin/pip" install --quiet --no-deps -e "$SRC/python" \
    || die "the editable overlay failed. The venv still holds the released wheel; re-run to retry."
fi
"$VENV/bin/python" - <<'PY' || die "the runtime does not know Qwen-Image 2.1. The pin may be wrong for this checkout."
import inspect, pathlib, sglang
from sglang.multimodal_gen import registry
src = pathlib.Path(sglang.__file__).parent
assert "Qwen/Qwen-Image-2.1" in inspect.getsource(registry), "Qwen-Image-2.1 is not in the model registry"
print(f"   runtime: {src}")
PY

step "4/6 Checkpoint ($MODEL, ~31 GB, one-time, resumable)"
# The same two lessons the LLM lane learned the hard way: the Hub's Xet backend stalls
# silently on this box (0-8 MB/s against 89 on the classic CDN), and an unauthenticated
# pull gets throttled, so HF_TOKEN is passed through when it is set.
HF_HOME="$HF_CACHE" HF_HUB_DOWNLOAD_TIMEOUT=30 HF_HUB_DISABLE_XET=1 MODEL_REPO="$MODEL" \
  "$VENV/bin/python" - <<'PY' || die "checkpoint download failed. Causes: no internet, HuggingFace throttling an unauthenticated download (set HF_TOKEN=<your token>), or a permission error in the cache. Re-running resumes."
import os, time
from huggingface_hub import snapshot_download
repo = os.environ["MODEL_REPO"]
for attempt in range(1, 5):
    try:
        print(snapshot_download(repo), flush=True); break
    except Exception as e:
        if attempt == 4:
            raise
        print(f"   attempt {attempt} stopped ({type(e).__name__}); resuming in 10 s", flush=True)
        time.sleep(10)
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
if [ -f "$INSTALLED" ] && sudo cmp -s "$RENDER" "$INSTALLED"; then
  echo "unit unchanged"
else
  sudo cp "$RENDER" "$INSTALLED"; sudo systemctl daemon-reload
  echo "unit installed at $INSTALLED"
fi
# Not enabled at boot on purpose: starting it stops the LLM lane, and a box that reboots
# should come back the way its owner left it, not holding 31 GB of image weights.
echo "not enabled at boot (starting it stops the LLM lane). Start it when you want images:"
echo "  sudo systemctl start $UNIT"

if [ "$SMOKE" -eq 0 ]; then step "Done (smoke test skipped)"; exit 0; fi

step "6/6 Proving it serves (starts the lane, generates one image, stops it)"
WAS_LLM=""
for u in qwen38-sglang.service qwen38-flash.service; do
  systemctl is-active --quiet "$u" 2>/dev/null && WAS_LLM="$u"
done
[ -n "$WAS_LLM" ] && echo "note: $WAS_LLM is serving and will be stopped for this test, then started again."
sudo systemctl start "$UNIT"
READY=0
for i in $(seq 1 90); do
  curl -fsS -m 5 "http://$IMAGE_BIND:$PORT/health" >/dev/null 2>&1 && { READY=1; break; }
  sleep 10
done
[ "$READY" -eq 1 ] || { sudo journalctl -u "$UNIT" -n 40 --no-pager; die "the lane did not answer /health within 15 min. The journal above says why."; }
echo "up after ~$((i*10)) s; generating a 512x512 image"
OUT="$(mktemp)"; trap 'rm -f "$RENDER" "$OUT"' EXIT
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
sudo systemctl stop "$UNIT"
if [ -n "$WAS_LLM" ]; then sudo systemctl start "$WAS_LLM"; echo "$WAS_LLM started again"; fi

step "Done: the image lane serves on $IMAGE_BIND:$PORT"
echo "  start   : sudo systemctl start $UNIT      (this stops the LLM lane)"
echo "  stop    : sudo systemctl stop $UNIT"
echo "  back to text: sudo systemctl start qwen38-sglang.service"
echo "  drive it from the cockpit's Image tab, or straight from the API on :$PORT"
