# shellcheck shell=bash
# vllm-lane.sh: what the three vLLM lanes' installers share (install-kolibri.sh, install-nemotron.sh,
# install-nemotron-super.sh). Sourced after lanes.sh, never run. Each installer keeps what is its
# own (its pins, its image, its unit's placeholders, its words) and names itself first:
#
#   VLANE        the lane (kolibri, nemotron, nemotron-super): its opencode provider, the record of
#                the text lane it took the boot from (lanes.sh), its --no-<lane>, its cache folder
#   VLANE_SHORT  the lane in a sentence (Kolibri, Nemotron, Super VL)
#   UNIT, INSTALLED, CONTAINER, CONFIG_DIR, HERE; and, for each part below, what it says it reads.
#
# Until v1.35 each installer carried its own copy of all of it, one renamed from the other: a fix
# made in one was missing from the next (the shared image's builds were pruned in one of them only,
# found in review, 2026-10-16).

# ── The engine's cache, port and bind ─────────────────────────────────────────────────────────
# The text lanes' cache and port: a vLLM lane is one of them for the proxy, so it answers where the
# proxy sends (its UPSTREAM), on the bind the other lanes use, from the same cache.
vlane_text_cache(){
  # shellcheck disable=SC2086  # a list of paths
  unit_hf_cache $INSTALLED $TEXT_UNITS
}
vlane_text_flag(){  # $1 the flag (--port, --host)
  # shellcheck disable=SC2086  # a list of paths
  { grep -hoE -- "$1 [^ \\\\']+" $INSTALLED $TEXT_UNITS 2>/dev/null || true; } | head -1 | awk '{print $2}'
}
# The bind the text lanes use, theirs first and this lane's own unit only on a box with no text
# lane installed (2026-10-17). Read the other way round, a vLLM lane kept the 0.0.0.0 an older
# release's default had given the text lane it copied, after an update moved that text lane to
# 127.0.0.1 (v1.36.0): a lane left out that day, brought back later, read its own unit first.
vlane_bind(){
  # shellcheck disable=SC2086  # a list of paths
  { grep -hoE -- "--host [^ \\']+" $TEXT_UNITS $INSTALLED 2>/dev/null || true; } | head -1 | awk '{print $2}'
}
vlane_engine_env(){  # $1 the lane's own port (KOLIBRI_PORT and the like), empty when not set
  TEXT_UNITS="${TEXT_UNITS:-$LANES_UNIT_DIR/qwen38-sglang.service $CONFIG_DIR/launch-flash.sh}"
  HF_CACHE="${HF_CACHE:-$(vlane_text_cache)}"; HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface}"
  UPSTREAM_PORT="$({ grep -m1 -oE 'UPSTREAM=http://[^:]+:[0-9]+' "$LANES_UNIT_DIR/qwen38-keepalive.service" 2>/dev/null || true; } | awk -F: '{print $NF}')"
  PORT="${1:-${UPSTREAM_PORT:-$(vlane_text_flag --port)}}"; PORT="${PORT:-30000}"
  ENGINE_BIND="${ENGINE_BIND:-$(vlane_bind)}"; ENGINE_BIND="${ENGINE_BIND:-127.0.0.1}"
  case "$PORT" in ''|*[!0-9]*) die "the engine port must be a number, got: $PORT" ;; esac
}

# ── --uninstall ──────────────────────────────────────────────────────────────────────────────
# The unit goes, and the boot with it goes back to the text lane this one took it from: removing
# the lane at boot would leave no engine at the next boot.
vlane_uninstall_unit(){
  # what a box switched before v1.30 has in its links becomes the record first (lanes.sh)
  text_lane_migrate
  [ -f "$INSTALLED" ] || return 0
  local was_boot=0 back
  systemctl is-enabled --quiet "$UNIT" 2>/dev/null && was_boot=1
  sudo systemctl disable --now "$UNIT" 2>/dev/null || true
  sudo rm -f "$INSTALLED"; sudo systemctl daemon-reload
  echo "unit removed"
  [ "$was_boot" -eq 1 ] || return 0
  # The text lane noted (lanes.sh), or the one installed: a side lane's own link, followed past a
  # side lane removed since, was v1.29's way (found in review, 2026-10-14), and one record needs none.
  back="$(text_lane_back "$VLANE")"
  if [ -n "$back" ]; then
    sudo systemctl enable "$back"
    echo "$back is enabled at boot again, as it was before $VLANE_SHORT; start it now with: sudo systemctl start $back   (or the cockpit)"
  else
    echo "NOTE: no engine is enabled at boot any more. Start a lane from the cockpit, or:"
    echo "      sudo systemctl enable --now qwen38-sglang.service"
  fi
}

# opencode and pi: the lane's provider goes, and a default that named it follows the text lane this
# box comes back to (lanes.sh; oc-point-default.py with no target: no name changes). Left as they
# were, opencode offered and defaulted to a model nothing served, and its small-model calls sent
# Kolibri's level "none" to the Qwen template, which refused it (found in review, 2026-10-14).
vlane_uninstall_clients(){
  local oc_lane oc_json was_default oc_sum_before oc_def_ctx oc_def_keep
  case "$(text_lane_back "$VLANE")" in
    qwen38-flash.service) oc_lane=flash ;;
    *) oc_lane=27b ;;
  esac
  oc_sum_before="$({ cat "$CONFIG_DIR/opencode.json" "$HOME/.config/opencode/opencode.json" 2>/dev/null || true; } | sha256sum)"
  for oc_json in "$CONFIG_DIR/opencode.json" "$HOME/.config/opencode/opencode.json"; do
    [ -f "$oc_json" ] || continue
    was_default=0
    grep -qsE '"model"[[:space:]]*:[[:space:]]*"'"$VLANE"'/' "$oc_json" && was_default=1
    python3 "$HERE/oc-merge-limits.py" "$oc_json" --remove-provider "$VLANE" \
      || echo "NOTE: could not take the $VLANE provider out of $oc_json; remove it by hand"
    [ "$was_default" -eq 1 ] || continue
    python3 "$HERE/oc-point-default.py" "$oc_json" "$oc_lane" "" "" \
      || echo "NOTE: could not point $oc_json at the $oc_lane lane; set its \"model\" field yourself"
    # The compaction with it, from the window of the model put back: sized from Kolibri's, it kept
    # 46,000 tokens verbatim where a 27B at 1M keeps 132,000 (found in review, 2026-10-14).
    oc_def_ctx="$(python3 - "$HERE/oc-merge-limits.py" "$oc_json" <<'PYCTX' || true
import importlib.util, sys
spec = importlib.util.spec_from_file_location("ocm", sys.argv[1])
ocm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ocm)
j = ocm.load(open(sys.argv[2], encoding="utf-8").read())
prov, _, mid = str(j.get("model") or "").partition("/")
entry = ((j.get("provider") or {}).get(prov) or {}).get("models", {}).get(mid) or {}
ctx = (entry.get("limit") or {}).get("context") if isinstance(entry, dict) else None
print(ctx if isinstance(ctx, int) and ctx > 0 else "")
PYCTX
)"
    oc_def_keep=""
    [ -n "$oc_def_ctx" ] && oc_def_keep="$("$HERE/oc-limits.sh" --preserve "$oc_def_ctx" 2>/dev/null || true)"
    if [ -n "$oc_def_keep" ]; then
      python3 "$HERE/oc-merge-limits.py" "$oc_json" --compaction "$oc_def_keep" \
        || echo "NOTE: could not size the compaction of $oc_json for the $oc_lane lane ($oc_def_keep tokens); set it yourself"
    fi
  done
  if [ -d "$CONFIG_DIR/pi" ] || [ -d "$CONFIG_DIR/omp" ]; then
    python3 "$HERE/pi-gen.py" --config "$CONFIG_DIR/opencode.json" --out "$CONFIG_DIR" \
      || echo "NOTE: could not regenerate the pi/omp artifacts; run ./pi-gen.py by hand"
  fi
  # A restart that fails (no ticket, no terminal) is said: the uninstall goes on. As this function's
  # last line it was its return code, and set -e stopped the installer half done (found in review,
  # 2026-10-16).
  if [ "$oc_sum_before" != "$({ cat "$CONFIG_DIR/opencode.json" "$HOME/.config/opencode/opencode.json" 2>/dev/null || true; } | sha256sum)" ] \
     && systemctl is-active --quiet opencode-web.service 2>/dev/null; then
    if sudo systemctl restart opencode-web.service; then
      echo "opencode-web.service restarted so it reads the change"
    else
      echo "NOTE: opencode-web.service could not restart; it reads the change at its next start: sudo systemctl restart opencode-web.service"
    fi
  fi
  return 0
}

# Its container, the builds of its image no other installed unit names (lanes.sh), its own caches
# and the key's middleware unless another vLLM lane runs it; the checkpoints stay, said.
vlane_uninstall_runtime(){  # $1 the image's repository, $2 the base image, $3... the checkpoints' repos
  local image_repo="$1" base="$2" u r d keep=""; shift 2
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  lane_prune_images "$image_repo" ""
  echo "the base image $base stays (docker rmi it yourself if nothing else uses it)"
  # vLLM writes its caches as root inside the container: what the user cannot remove, sudo does
  rm -rf "$CONFIG_DIR/$VLANE-cache" 2>/dev/null || sudo rm -rf --one-file-system "$CONFIG_DIR/$VLANE-cache"
  # The key's middleware is every vLLM lane's (the unit mounts it, read-only, at every start): it
  # stays while another one is installed. Removed by either, it left the other's engine without it.
  for u in qwen38-kolibri.service qwen38-nemotron.service qwen38-nemotron-super.service; do
    [ "$u" != "$UNIT" ] && [ -f "$LANES_UNIT_DIR/$u" ] && keep=1
  done
  # The flash lane is one of these only in its FP4-Local config (the b12x vLLM one),
  # and that config mounts the middleware in its LAUNCHER, not in the unit; the unit
  # carries the word nowhere (grep of it was the miss, found in review, 2026-10-18:
  # uninstalling the last other vLLM lane deleted vllm-auth under the serving engine).
  { [ "$UNIT" != "qwen38-flash.service" ] \
    && { grep -q vllm-auth "$LANES_UNIT_DIR/qwen38-flash.service" 2>/dev/null \
         || grep -q vllm-auth "$CONFIG_DIR/launch-flash-fp4local.sh" 2>/dev/null; } \
    && keep=1; } || true
  if [ -n "$keep" ]; then
    echo "vllm-auth kept: another vLLM lane uses it too"
  else
    rm -rf "$CONFIG_DIR/vllm-auth"
  fi
  for r in "$@"; do
    d="$HF_CACHE/hub/models--${r//\//--}"
    [ -d "$d" ] && echo "checkpoint kept, delete it yourself for the space: rm -rf $d"
  done
  return 0
}

# ── The service: the one part that needs root ────────────────────────────────────────────────
# The start scripts the unit runs before every start, and the middleware that asks the key on every
# route of the engine (see vllm_engine_auth.py), in a folder of its own the unit mounts read-only on
# the module path; and the lane's caches, made here as this user so that docker does not make them
# as root when it mounts them.
vlane_service_files(){  # $1... the lane's cache folders
  mkdir -p "$CONFIG_DIR" "$@"
  cmp -s "$HERE/engine-preflight.sh" "$CONFIG_DIR/engine-preflight.sh" \
    || install -m 755 "$HERE/engine-preflight.sh" "$CONFIG_DIR/engine-preflight.sh"
  cmp -s "$HERE/engine-secrets.sh" "$CONFIG_DIR/engine-secrets.sh" \
    || install -m 755 "$HERE/engine-secrets.sh" "$CONFIG_DIR/engine-secrets.sh"
  mkdir -p "$CONFIG_DIR/vllm-auth"
  cmp -s "$HERE/vllm_engine_auth.py" "$CONFIG_DIR/vllm-auth/vllm_engine_auth.py" \
    || install -m 644 "$HERE/vllm_engine_auth.py" "$CONFIG_DIR/vllm-auth/vllm_engine_auth.py"
}

# The rendered unit put in place (lanes.sh's unit_put), and what that means for a lane that serves:
# it goes on serving the unit, the runtime and the start scripts it started with, said here when no
# proof follows to restart it, whether this run or an earlier update changed them (an update leaves
# a serving lane serving, and said nothing for the image and video lanes: found in review,
# 2026-10-14). LANE_FILES is what the proof's restart decision reads too.
vlane_put_unit(){  # $1 the rendered unit
  grep -q '__[A-Z][A-Z0-9_]*__' "$1" \
    && die "the unit template still holds an unsubstituted placeholder: $(grep -o '__[A-Z][A-Z0-9_]*__' "$1" | sort -u | tr '\n' ' ')"
  local put
  put="$(unit_put "$1" "$INSTALLED")"
  case "$put" in
    same) echo "unit unchanged" ;;
    deps) sudo systemctl daemon-reload
          echo "unit updated: only the lines naming the other lanes changed, which the lane does not read" ;;
    *)    sudo systemctl daemon-reload
          echo "unit installed at $INSTALLED" ;;
  esac
  LANE_FILES=("$INSTALLED" "$CONFIG_DIR/vllm-auth/vllm_engine_auth.py" "$CONFIG_DIR/engine-preflight.sh" "$CONFIG_DIR/engine-secrets.sh")
  if [ "$SMOKE" -eq 0 ] && lane_serves_older "$UNIT" "${LANE_FILES[@]}"; then
    echo "NOTE: $VLANE_SHORT runs older files than the ones installed now (its unit, its runtime or its start scripts); it runs these after: sudo systemctl restart $UNIT   (or Load it again from the cockpit)"
  fi
}

# ── The preflight and the room ───────────────────────────────────────────────────────────────
vlane_preflight(){  # $1... the files of the repository this needs beside it
  local f
  case "$(uname -m)" in aarch64|arm64) : ;; *) die "this lane is verified on the DGX Spark's ARM64 GB10 only (this box is $(uname -m))." ;; esac
  command -v nvidia-smi >/dev/null || die "nvidia-smi not found: this needs the NVIDIA driver."
  command -v docker >/dev/null || die "docker is required (stock on DGX OS)."
  docker info >/dev/null 2>&1 || die "docker does not answer for $(id -un). Is it running, and is $(id -un) in the docker group?"
  [ -s "$CONFIG_DIR/api-key" ] || die "no API key at $CONFIG_DIR/api-key. Run ./install.sh first: this lane serves behind the proxy with that key."
  for f in "$@"; do
    [ -f "$HERE/$f" ] || die "$f is missing next to this script: run it from the repository."
  done
}

# Measured where each part lands: the weights go to HF_CACHE, the image to Docker's own disk. What
# the cache already holds of the checkpoints comes off their share, by bytes; an image built, or
# its base pulled, needs no room. -L: a cache moved to another disk behind a symlink
# (~/.cache/huggingface on an SSD) is on that disk; the link's own device is the home directory's,
# which skipped Docker's check. Reads WEIGHTS_GB, IMAGE_GB, LOCAL_IMAGE and MODE; sets WEIGHTS_NEED,
# IMAGE_NEED and DOCKER_ROOT, and refuses when a disk is short, before anything is downloaded.
vlane_room(){  # $1 the base image, $2... the checkpoints' repos
  local base="$1" r b have=0 what free_w free_d hf_dev docker_dev dl_gb
  shift
  for r in "$@"; do
    b="$({ du -s --apparent-size -B1 -L "$HF_CACHE/hub/models--${r//\//--}/blobs" 2>/dev/null || true; } | cut -f1)"
    have=$(( have + ${b:-0} ))
  done
  # In GiB, rounded up, as df -BG counts the free space and IMAGE_GB the image: the download is
  # given in GB of 10^9 bytes, and its GB held against GiB asked a Kolibri-1 install for five
  # GiB too many (found in review, 2026-10-16).
  WEIGHTS_NEED=$(( (WEIGHTS_GB * 1000000000 - have + 1073741823) / 1073741824 )); [ "$WEIGHTS_NEED" -ge 0 ] || WEIGHTS_NEED=0
  # and what is left to download, in GB as the lane's docs give it, for the line that says so
  dl_gb=$(( (WEIGHTS_GB * 1000000000 - have + 999999999) / 1000000000 )); [ "$dl_gb" -ge 0 ] || dl_gb=0
  # --prepare: the checkpoints are the Load's switch's to fetch, and to measure
  [ "$MODE" = prepare ] && WEIGHTS_NEED=0
  DOCKER_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
  IMAGE_NEED="$IMAGE_GB"
  docker image inspect "$LOCAL_IMAGE" >/dev/null 2>&1 && IMAGE_NEED=0
  docker image inspect "$base" >/dev/null 2>&1 && IMAGE_NEED=0
  [ "$MODE" = units ] && return 0
  what="checkpoints"; [ "$#" -eq 1 ] && what="checkpoint"
  free_w="$(free_gb_at "$HF_CACHE")"; free_d="$(free_gb_at "$DOCKER_ROOT")"
  hf_dev="$(existing "$HF_CACHE")"; docker_dev="$(existing "$DOCKER_ROOT")"
  if [ "$(stat -L -c %d "$hf_dev")" = "$(stat -L -c %d "$docker_dev")" ]; then
    [ "${free_w:-0}" -ge "$((WEIGHTS_NEED + IMAGE_NEED))" ] \
      || die "${free_w:-?} GB free on the disk of $HF_CACHE and $DOCKER_ROOT, this needs about $((WEIGHTS_NEED + IMAGE_NEED)) GB ($WEIGHTS_NEED for the $what, $IMAGE_NEED for the image). Free some space, or ./install.sh --no-$VLANE to leave this lane out."
  else
    [ "${free_w:-0}" -ge "$WEIGHTS_NEED" ] \
      || die "${free_w:-?} GB free under HF_CACHE=$HF_CACHE, the $what need$([ "$#" -eq 1 ] && echo s) about $WEIGHTS_NEED GB more. Free some space, or ./install.sh --no-$VLANE."
    [ "${free_d:-0}" -ge "$IMAGE_NEED" ] \
      || die "${free_d:-?} GB free under $DOCKER_ROOT, the image needs about $IMAGE_NEED GB. Free some space, or ./install.sh --no-$VLANE."
  fi
  echo "OK: $(uname -m), key present, $what $([ "$MODE" = prepare ] && echo "left to the Load's switch" || { [ "$WEIGHTS_NEED" -eq 0 ] && echo 'already cached'; } || echo "$dl_gb GB to download") into $HF_CACHE, engine on $ENGINE_BIND:$PORT"
}

# ── The checkpoints ──────────────────────────────────────────────────────────────────────────
# In the lane's own image, with its huggingface_hub, as install.sh does for the other text lanes,
# with the same Xet ban (the classic CDN path measured 89 MB/s where Xet stalled at 0 to 8), through
# ckpt-fetch.py, the one fetch of every lane's checkpoint: --offline-first takes a snapshot the cache
# holds only when every shard its index names is there (huggingface_hub's own check sees a missing
# file only with its tree cache). The token by name: docker takes the value from its own
# environment, so it is in no argv. The byte count when the script is there: a mount of a missing
# file makes a root-owned directory.
vlane_fetch(){  # $1 the repo, $2 the revision
  local token_args=() progress_args=()
  mkdir -p "$HF_CACHE"
  [ -f "$HERE/hf-progress.py" ] && progress_args=(-v "$HERE/hf-progress.py:/opt/qwen38/hf-progress.py:ro")
  if [ -n "${HF_TOKEN:-}" ]; then export HF_TOKEN; token_args=(-e HF_TOKEN); fi
  docker run --rm --network host --user "$(id -u):$(id -g)" --entrypoint python3 \
    -e HF_HOME=/hf -e HF_HUB_DOWNLOAD_TIMEOUT=30 -e HF_HUB_DISABLE_XET=1 "${token_args[@]}" \
    -v "$HERE/ckpt-fetch.py:/opt/qwen38/ckpt-fetch.py:ro" "${progress_args[@]}" \
    -v "$HF_CACHE":/hf "$LOCAL_IMAGE" /opt/qwen38/ckpt-fetch.py "$1" "$2" --offline-first \
    || die "download of $1 failed. Causes: no internet, HuggingFace throttling an unauthenticated download (set HF_TOKEN=<your token>), a pinned revision removed upstream, or a permission error in $HF_CACHE. Re-running resumes."
}

# ── The proof ────────────────────────────────────────────────────────────────────────────────
# A reply and a tool call, then the box goes back to the lane it was serving; the key in none of the
# lane's journal lines. Reads VLANE_SERVED (the model name it serves), VLANE_KWARGS (the
# chat_template_kwargs that turn its reasoning off), VLANE_PARSERS (what to suspect when no tool call
# comes back: "the qwen3_coder parser is"), VLANE_BOOT (its boot on the reference box, said while it
# loads), VLANE_WAIT_MIN (how long it is given, in minutes).
vlane_prove(){
  local u r0 ready=0 i code journal_copy
  # The proof restarts a lane serving older files than the service step put in place (LANE_FILES):
  # run before that step, it would find none and prove the engine that ran (found in review,
  # 2026-10-16: "${LANE_FILES[@]}" of an array never set expands to nothing, even under set -u).
  { [ -n "${LANE_FILES+x}" ] && [ "${#LANE_FILES[@]}" -gt 0 ]; } \
    || die "the proof runs before the service step (repo bug: please open an issue)"
  VLANE_WAS=""
  for u in qwen38-sglang.service qwen38-flash.service qwen38-image.service qwen38-video.service qwen38-kolibri.service qwen38-nemotron.service qwen38-nemotron-super.service; do
    [ "$u" = "$UNIT" ] && continue
    systemctl is-active --quiet "$u" 2>/dev/null && VLANE_WAS="$u"
  done
  VLANE_WAS_SELF=0
  systemctl is-active --quiet "$UNIT" 2>/dev/null && VLANE_WAS_SELF=1
  [ -n "$VLANE_WAS" ] && echo "note: $VLANE_WAS is serving and will be stopped for this test, then started again."
  # From here on the serving lane is down: putting it back cannot live on the happy path.
  trap vlane_restore_lane EXIT
  KEY="$(head -1 "$CONFIG_DIR/api-key")"
  SINCE="$(date '+%Y-%m-%d %H:%M:%S')"
  # A lane that runs older files than the ones installed now restarts for the proof: the engine that
  # runs proved nothing about what this run installed (found in review, 2026-10-14).
  if [ "$VLANE_WAS_SELF" -eq 1 ] && lane_serves_older "$UNIT" "${LANE_FILES[@]}"; then
    echo "$VLANE_SHORT runs older files than the ones installed now (its unit, its runtime or its start scripts): it restarts to prove these"
    sudo systemctl restart "$UNIT"
  elif [ "$VLANE_WAS_SELF" -eq 0 ]; then
    sudo systemctl start "$UNIT"
  fi
  # read after the start: a start or a restart resets the count, and one read before it, from a
  # unit that had restarted on its own before, let as many crashes of the new one go unseen (found
  # in review, 2026-10-14)
  r0="$(systemctl show -p NRestarts --value "$UNIT" 2>/dev/null | tr -dc '0-9')"
  for i in $(seq 1 $((VLANE_WAIT_MIN * 60 / 8))); do
    curl -fsS -m 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && { ready=1; break; }
    if [ "$(systemctl is-active "$UNIT" || true)" = failed ] \
       || [ "$(systemctl show -p NRestarts --value "$UNIT" 2>/dev/null | tr -dc '0-9')" -gt "${r0:-0}" ]; then
      { sudo journalctl -u "$UNIT" --since "$SINCE" --no-pager || true; } | tail -40
      die "the lane died while starting; the journal above says why."
    fi
    [ $((i % 15)) -eq 0 ] && echo "  still loading... ($((i * 8)) s; $VLANE_BOOT on the reference box)"
    sleep 8
  done
  # a journal that cannot be read (sudo without a ticket) must not end this before the reason:
  # under pipefail it did, with only sudo's own line printed (found in review, 2026-10-14)
  [ "$ready" -eq 1 ] || { { sudo journalctl -u "$UNIT" --since "$SINCE" --no-pager || true; } | tail -40; die "the lane did not answer /health within $VLANE_WAIT_MIN min. The journal above says why."; }
  echo "up after ~$(( (i - 1) * 8 )) s"
  OUT="$(mktemp)"
  code="$(vlane_ask '{"model":"'"$VLANE_SERVED"'","messages":[{"role":"user","content":"Reply with exactly: READY"}],"max_tokens":64,"chat_template_kwargs":'"$VLANE_KWARGS"'}')"
  [ "$code" = 200 ] || { echo "HTTP ${code:-000}: $(head -c 300 "$OUT")"; die "the lane came up and its first generation failed (HTTP ${code:-000})."; }
  python3 - "$OUT" <<'PY' || die "the lane answered 200 without the expected reply."
import json, sys
m = json.load(open(sys.argv[1]))["choices"][0]["message"]
text = (m.get("content") or "").strip()
if "READY" not in text:
    raise SystemExit(f"reply: {text[:200]!r}")
print(f"   reply: {text[:40]!r}")
PY
  code="$(vlane_ask '{"model":"'"$VLANE_SERVED"'","messages":[{"role":"user","content":"What is the weather in Berlin? Use celsius."}],"max_tokens":2048,"chat_template_kwargs":'"$VLANE_KWARGS"',"tools":[{"type":"function","function":{"name":"get_weather","description":"Get the current weather for a city.","parameters":{"type":"object","properties":{"city":{"type":"string"},"unit":{"type":"string","enum":["celsius","fahrenheit"]}},"required":["city"]}}}]}')"
  [ "$code" = 200 ] || { echo "HTTP ${code:-000}: $(head -c 300 "$OUT")"; die "the tool-call request failed (HTTP ${code:-000})."; }
  python3 - "$OUT" <<'PY' || die "the lane did not return a parsed tool call ($VLANE_PARSERS not in effect?)."
import json, sys
m = json.load(open(sys.argv[1]))["choices"][0]["message"]
calls = m.get("tool_calls") or []
if not calls or calls[0]["function"]["name"] != "get_weather":
    raise SystemExit(f"no get_weather call: {json.dumps(m)[:300]}")
args = json.loads(calls[0]["function"]["arguments"])
print(f"   tool call: get_weather({args})")
PY
  # The key must not be in the lane's journal (vLLM masks api_key in its argument log line).
  # Read whole first, then searched: piped into grep -q under pipefail, a journal long enough
  # to fill the pipe died of SIGPIPE at the first match, and the check read "not found".
  journal_copy="$(mktemp)"
  # shellcheck disable=SC2024  # the copy is this user's own temporary file, written by this shell
  if ! sudo journalctl -u "$UNIT" --since "$SINCE" --no-pager > "$journal_copy" 2>/dev/null; then
    rm -f "$journal_copy"; die "the lane's journal could not be read to check the key is not in it."
  fi
  if grep -q -F -f <(head -1 "$CONFIG_DIR/api-key") "$journal_copy"; then
    rm -f "$journal_copy"; die "the API key appears in the lane's journal: rotate it, and report this."
  fi
  echo "   the key is in none of the lane's $(wc -l < "$journal_copy") journal lines"
  rm -f "$journal_copy"
  # the trap stops the lane and brings the serving lane back, whichever way this ends
}
vlane_restore_lane(){
  [ "$VLANE_WAS_SELF" -eq 1 ] || sudo systemctl stop "$UNIT" 2>/dev/null || true
  if [ -n "$VLANE_WAS" ]; then
    echo "starting $VLANE_WAS again"
    sudo systemctl start "$VLANE_WAS" || echo "WARNING: could not restart $VLANE_WAS. Do it by hand: sudo systemctl start $VLANE_WAS"
  fi
  rm -f "${RENDER:-}" "${OUT:-}"
}
vlane_ask(){  # $1: the request's body; the reply lands in $OUT, the HTTP code is printed
  curl -sS -o "$OUT" -w '%{http_code}' -m 300 "http://127.0.0.1:$PORT/v1/chat/completions" \
    -H @<(printf 'Authorization: Bearer %s\n' "$KEY") -H 'Content-Type: application/json' -d "$1" || true
}
