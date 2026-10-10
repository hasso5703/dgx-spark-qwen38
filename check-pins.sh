#!/usr/bin/env bash
# Does every pin in install.sh still resolve upstream?
#
# The repo pins twice on purpose: a checkpoint revision at download time AND the
# same --revision passed to the server, plus image digests. That protects what is
# served from an upstream push, and it means a pin that upstream deletes turns a
# working install into a failing one on a machine that does not have the bytes
# yet. This script asks, once, over the network. It changes nothing.
#
#   ./check-pins.sh          # every pin
#   ./check-pins.sh flash    # only the pins whose name matches
#
# Exit 0 when every checked pin resolves, 1 otherwise: a pin that could not be
# asked about (no answer, no registry token) has not resolved. CI does not run this
# (a green build must not depend on Hugging Face being up); run it before a
# release and when an install fails on a fresh box. The "pin watch" scheduled
# workflow runs it daily and files a labeled issue when a pin dies: a watch,
# never a gate; the build stays offline-honest either way.
set -uo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILTER="${1:-}"
FAIL=0
FAILED=0
UNASKED=0
CHECKED=0

# A failure is one of two kinds. A pin asked about and answered "gone" (a 404, a digest that
# changed) is dead. A pin that could not be asked about (no answer, a 5xx, a rate limit, no
# registry token) is not resolved either, and says nothing about the pin: the scheduled run of
# 2026-10-01 filed issue #33 on three 504s of the GitHub API, and every pin resolved after.
failed() { FAIL=1; FAILED=$((FAILED + 1)); }
unasked() { failed; UNASKED=$((UNASKED + 1)); }
no_answer() {  # $1 http code ("000": none), [$2 x-ratelimit-remaining]: 0 when it is no answer
  case "$1" in
    000|""|429|5[0-9][0-9]) return 0 ;;
    403) [ "${2:-}" = 0 ] && return 0 ;;          # GitHub's rate limit, a 403 that says so
  esac
  return 1
}
said() { [ "${1:-000}" = 000 ] && echo "no answer" || echo "HTTP $1"; }

pins="$(grep -E '^(STOCK|UNC|FP8|UNCFP8|FLASH|FLASH_NVDA|FLASH_UNC|DRAFT|DRAFT2)_(REPO|REV)=' "$REPO_DIR/install.sh")"
eval "$pins"
# The pins that do not live in the pin block: opencode's release and its digest, and the
# image lane's checkpoint, source commit and wheel. None of them was checked, so a release
# or a commit removed upstream went unseen (found in review, 2026-09-24).
more="$(grep -E '^(OPENCODE_VERSION|OPENCODE_SHA256)=' "$REPO_DIR/install.sh")"
eval "$more"
lane="$(grep -E '^(IMAGE_MODEL_PIN|IMAGE_MODEL_PIN_REV|IMAGE_TURBO_PIN|IMAGE_TURBO_PIN_REV|PIN|WHEEL)=' "$REPO_DIR/install-image.sh")"
eval "$lane"
IMAGE_PIN="$PIN"; IMAGE_WHEEL="$WHEEL"
vline="$(grep -E '^(VIDEO_MODEL_PIN|VIDEO_MODEL_PIN_REV|PIN|WHEEL)=' "$REPO_DIR/install-video.sh")"
eval "$vline"
VIDEO_PIN="$PIN"; VIDEO_WHEEL="$WHEEL"

# Hugging Face answers an anonymous request with 401 both for a gated repo and for one that
# does not exist (or is private): only its x-error-code tells a gated repo apart, so the
# answer's code and that header are read together. HF_TOKEN, when set, is sent the way the
# installer's download sends it, and goes in on stdin rather than on the command line.
hf_answer() {  # $1 url: prints "<http code> <x-error-code>"
  local cfg=""
  [ -n "${HF_TOKEN:-}" ] && cfg="header = \"Authorization: Bearer $HF_TOKEN\""
  printf '%s\n' "$cfg" | curl -s -o /dev/null -m 25 -K - -w '%{http_code} %header{x-error-code}' "$1"
}

# GitHub answers an anonymous caller 60 requests an hour per address, and a runner's address is
# shared: the scheduled run of 2026-10-02 got 403 with the limit spent, on both of its tries ten
# minutes apart, and filed issue #34. GITHUB_TOKEN (the workflow's own token), when set, is sent
# the way HF_TOKEN is, on stdin rather than on the command line.
gh_api() {  # $1 url, $2 curl -w format, [$3 "nobody" to drop the body]: body, then the -w output
  local cfg="" out=()
  [ -n "${GITHUB_TOKEN:-}" ] && cfg="header = \"Authorization: Bearer $GITHUB_TOKEN\""
  [ "${3:-}" = nobody ] && out=(-o /dev/null)
  printf '%s\n' "$cfg" | curl -s ${out[@]+"${out[@]}"} -m 25 -K - -w "$2" "$1"
}

check_model() {  # $1 label, $2 repo, $3 revision, [$4 a file at its root, default config.json]
  case "$1" in *"$FILTER"*) ;; *) return 0 ;; esac
  CHECKED=$((CHECKED + 1))
  local code err
  read -r code err <<<"$(hf_answer "https://huggingface.co/$2/raw/$3/${4:-config.json}")"
  case "$code:$err" in
    200:*) printf '  \033[0;32mok\033[0m    %-14s %s @ %s\n' "$1" "$2" "${3:0:12}" ;;
    40[13]:GatedRepo)
      # the repo exists, and a fresh install without a token that accepted its terms cannot
      # download it: nothing in this repo expects a gated checkpoint
      if [ -n "${HF_TOKEN:-}" ]; then
        printf '  \033[0;31mFAIL\033[0m  %-14s %s @ %s (gated: accept its terms on huggingface.co with the account of HF_TOKEN)\n' "$1" "$2" "${3:0:12}"
      else
        printf '  \033[0;31mFAIL\033[0m  %-14s %s @ %s (gated: a fresh install needs an HF_TOKEN that accepted its terms)\n' "$1" "$2" "${3:0:12}"
      fi
      failed ;;
    40[13]:*) printf '  \033[0;31mFAIL\033[0m  %-14s %s @ %s (HTTP %s: the repo is gone or private)\n' "$1" "$2" "${3:0:12}" "$code"; failed ;;
    *) if no_answer "$code"; then
         printf '  \033[0;31mFAIL\033[0m  %-14s %s @ %s (%s from Hugging Face, so not checked)\n' "$1" "$2" "${3:0:12}" "$(said "$code")"; unasked
       else
         printf '  \033[0;31mFAIL\033[0m  %-14s %s @ %s (HTTP %s)\n' "$1" "$2" "${3:0:12}" "$code"; failed
       fi ;;
  esac
}

check_image() {  # $1 label, $2 image reference (name@sha256:... or name:tag)
  case "$1" in *"$FILTER"*) ;; *) return 0 ;; esac
  CHECKED=$((CHECKED + 1))
  local repo ref token code
  repo="${2%%@*}"; repo="${repo%%:*}"
  case "$2" in *@*) ref="${2#*@}" ;; *) ref="${2##*:}" ;; esac
  token="$(curl -s -m 25 "https://auth.docker.io/token?service=registry.docker.io&scope=repository:${repo}:pull" \
    | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')"
  if [ -z "$token" ]; then
    # not asked is not resolved: an offline run must not end in "all resolve"
    printf '  \033[0;31mFAIL\033[0m  %-14s %s (no registry token, so not checked: offline?)\n' "$1" "$2"
    unasked
    return 0
  fi
  code="$(curl -s -o /dev/null -m 25 -w '%{http_code}' -H "Authorization: Bearer $token" \
    -H 'Accept: application/vnd.oci.image.index.v1+json' \
    -H 'Accept: application/vnd.docker.distribution.manifest.list.v2+json' \
    -H 'Accept: application/vnd.oci.image.manifest.v1+json' \
    -H 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
    "https://registry-1.docker.io/v2/${repo}/manifests/${ref}")"
  case "$code" in
    200) printf '  \033[0;32mok\033[0m    %-14s %s\n' "$1" "$2" ;;
    *) if no_answer "$code"; then
         printf '  \033[0;31mFAIL\033[0m  %-14s %s (%s from the registry, so not checked)\n' "$1" "$2" "$(said "$code")"; unasked
       else
         printf '  \033[0;31mFAIL\033[0m  %-14s %s (HTTP %s)\n' "$1" "$2" "$code"; failed
       fi ;;
  esac
}

check_commit() {  # $1 label, $2 owner/repo on GitHub, $3 commit: install-image.sh fetches it by id
  case "$1" in *"$FILTER"*) ;; *) return 0 ;; esac
  CHECKED=$((CHECKED + 1))
  local code left
  read -r code left <<<"$(gh_api "https://api.github.com/repos/$2/commits/$3" \
    '%{http_code} %header{x-ratelimit-remaining}' nobody)"
  case "$code" in
    200) printf '  \033[0;32mok\033[0m    %-14s %s @ %s\n' "$1" "$2" "${3:0:12}" ;;
    *) if no_answer "$code" "$left"; then
         printf '  \033[0;31mFAIL\033[0m  %-14s %s @ %s (%s from the GitHub API, so not checked)\n' "$1" "$2" "${3:0:12}" "$(said "$code")"; unasked
       else
         printf '  \033[0;31mFAIL\033[0m  %-14s %s @ %s (HTTP %s from the GitHub API: no such commit)\n' "$1" "$2" "${3:0:12}" "$code"; failed
       fi ;;
  esac
}

check_wheel() {  # $1 label, $2 package, $3 version: released on PyPI, with a file not yanked
  case "$1" in *"$FILTER"*) ;; *) return 0 ;; esac
  CHECKED=$((CHECKED + 1))
  local why
  why="$(curl -s -m 25 "https://pypi.org/pypi/$2/$3/json" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    print("no answer from PyPI, so not checked"); sys.exit()
if (d.get("info") or {}).get("version") != sys.argv[1]:
    print("not on PyPI")
elif not [u for u in d.get("urls") or [] if not u.get("yanked")]:
    print("every file yanked")
' "$3")"
  if [ -z "$why" ]; then
    printf '  \033[0;32mok\033[0m    %-14s %s==%s\n' "$1" "$2" "$3"
  else
    printf '  \033[0;31mFAIL\033[0m  %-14s %s==%s (%s)\n' "$1" "$2" "$3" "$why"
    case "$why" in *"so not checked"*) unasked ;; *) failed ;; esac
  fi
}

check_asset() {  # $1 label, $2 owner/repo, $3 release tag, $4 asset, $5 its sha256: GitHub's digest must match
  case "$1" in *"$FILTER"*) ;; *) return 0 ;; esac
  CHECKED=$((CHECKED + 1))
  local answer status code left digest
  # The body alone said "no such release", "no such asset" and "no answer" the same way, and
  # issue #33 read a 504 as a release gone: the code and the rate-limit header come after it.
  answer="$(gh_api "https://api.github.com/repos/$2/releases/tags/$3" '\n%{http_code} %header{x-ratelimit-remaining}')"
  status="${answer##*$'\n'}"
  read -r code left <<<"$status"
  digest="$(printf '%s' "${answer%$'\n'*}" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    print("not json"); sys.exit()
print(next((a.get("digest") or "none" for a in d.get("assets") or [] if a.get("name") == sys.argv[1]), "absent"))
' "$4")"
  if [ "$code" != 200 ] && no_answer "$code" "$left"; then
    printf '  \033[0;31mFAIL\033[0m  %-14s %s %s %s (%s from the GitHub API, so not checked)\n' "$1" "$2" "$3" "$4" "$(said "$code")"; unasked
    return 0
  fi
  case "$code:$digest" in
    "200:sha256:$5") printf '  \033[0;32mok\033[0m    %-14s %s %s %s\n' "$1" "$2" "$3" "$4" ;;
    200:absent) printf '  \033[0;31mFAIL\033[0m  %-14s %s %s %s (the release has no such asset)\n' "$1" "$2" "$3" "$4"; failed ;;
    200:none) printf '  \033[0;31mFAIL\033[0m  %-14s %s %s %s (GitHub publishes no digest for it: not checked)\n' "$1" "$2" "$3" "$4"; failed ;;
    "200:not json") printf '  \033[0;31mFAIL\033[0m  %-14s %s %s %s (the GitHub API answered something that is not JSON, so not checked)\n' "$1" "$2" "$3" "$4"; unasked ;;
    200:*) printf '  \033[0;31mFAIL\033[0m  %-14s %s %s %s (its digest is now %s: installs refuse it)\n' "$1" "$2" "$3" "$4" "$digest"; failed ;;
    *) printf '  \033[0;31mFAIL\033[0m  %-14s %s %s %s (HTTP %s from the GitHub API: no such release)\n' "$1" "$2" "$3" "$4" "$code"; failed ;;
  esac
}

echo "Checkpoints"
check_model stock          "$STOCK_REPO"      "$STOCK_REV"
check_model uncensored     "$UNC_REPO"        "$UNC_REV"
check_model fp8            "$FP8_REPO"        "$FP8_REV"
check_model uncensored-fp8 "$UNCFP8_REPO"     "$UNCFP8_REV"
check_model flash          "$FLASH_REPO"      "$FLASH_REV"
check_model flash-nvda     "$FLASH_NVDA_REPO" "$FLASH_NVDA_REV"
check_model flash-unc      "$FLASH_UNC_REPO"  "$FLASH_UNC_REV"
check_model dflash2-draft  "$DRAFT2_REPO"     "$DRAFT2_REV"
check_model dspark-draft   "$DRAFT_REPO"      "$DRAFT_REV"

echo "Image lane and opencode"
check_model  qwen-image     "$IMAGE_MODEL_PIN" "$IMAGE_MODEL_PIN_REV" model_index.json
check_model  qwen-image-turbo "$IMAGE_TURBO_PIN" "$IMAGE_TURBO_PIN_REV" model_index.json
check_commit sglang-source  sgl-project/sglang "$IMAGE_PIN"
check_wheel  sglang-wheel   sglang "$IMAGE_WHEEL"
echo "Video lane"
check_model  minimax-h3     "$VIDEO_MODEL_PIN" "$VIDEO_MODEL_PIN_REV" model_index.json
check_commit sglang-source-video sgl-project/sglang "$VIDEO_PIN"
check_wheel  sglang-wheel-video sglang "$VIDEO_WHEEL"
check_asset  opencode       anomalyco/opencode "v$OPENCODE_VERSION" opencode-linux-arm64.tar.gz "$OPENCODE_SHA256"

echo "Images"
img="$(grep -E '^(IMAGE|FLASH_IMAGE)=' "$REPO_DIR/install.sh")"
eval "$img"
check_image 27b-base       "$IMAGE"
check_image flash-base     "$FLASH_IMAGE"

echo
if [ "$FAIL" -eq 0 ]; then
  printf '\033[0;32m%s pins checked, all resolve.\033[0m\n' "$CHECKED"
elif [ "$UNASKED" -eq "$FAILED" ]; then
  printf '\033[0;31m%s of %s pins could not be asked about.\033[0m That says nothing about the pins\n' "$UNASKED" "$CHECKED"
  printf '(no answer, or an error on the other side): run it again before re-pinning anything.\n'
else
  printf '\033[0;31mSome pins do not resolve.\033[0m A removed upstream revision is not fixable\n'
  printf 'from here: open an issue, and in the meantime MODEL_REV=main ./install.sh serves\n'
  printf 'the current revision instead of the validated one.\n'
fi
exit "$FAIL"
