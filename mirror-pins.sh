#!/usr/bin/env bash
set -u
# Do the pins exist where THEY will live when upstream dies?
#
# check-pins.sh asks upstream whether the pins still resolve; this asks the
# other half of the question: whether the mirror holds them. It is a plan-
# first tool: --dry-run prints what would be copied where (this mode needs
# no credentials and is the one the docs show), and the execute mode copies
# only what is actually missing, resumably, without ever rewriting a pin.
#
# The contract it protects (MIRROR.md is the runbook):
#   - a checkpoint's pinned revision, downloaded at that revision and
#     re-uploaded as a revision that must match file for file,
#   - an image pulled by digest (bytes addressed by hash), retagged and
#     pushed; the digest is the identity, the mirror cannot drift.
#
# What this script will NOT do: invent a license conclusion. Each source
# repo must pass the human license check in MIRROR.md before it earns a row
# in the table below; the table is the conclusion, not the check.
#
#   ./mirror-pins.sh --dry-run          # the plan, no credentials, no writes
#   ./mirror-pins.sh                    # HF part (needs HF_TOKEN, HF_MIRROR_ORG)
#   ./mirror-pins.sh --images           # docker part (needs MIRROR_REGISTRY)
set -o pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:---dry-run}"
HF_MIRROR_ORG="${HF_MIRROR_ORG:-}"
MIRROR_REGISTRY="${MIRROR_REGISTRY:-}"
RC=0

# Same extraction as check-pins.sh (keep the two in sync on purpose: one
# asks upstream, this one asks the mirror; both read the one pin block).
pins="$(grep -E '^(STOCK|UNC|FP8|UNCFP8|FLASH|FLASH_NVDA|FLASH_UNC|DRAFT|DRAFT2)_(REPO|REV)=' "$REPO_DIR/install.sh")"
eval "$pins"


mirror_model() {  # $1 label, $2 repo, $3 revision
  local dst="${HF_MIRROR_ORG:-<HF_MIRROR_ORG unset>}/${2##*/}"
  printf '%-12s %s\n' "  model" "$1"
  printf '           source: %s @ %s\n' "$2" "$3"
  printf '           mirror: %s\n' "$dst"
  if [ "$MODE" = "--dry-run" ]; then
    printf '           plan: download the pinned revision, upload as the same revision (skip when present)\n'
    return 0
  fi
  [ -n "$HF_MIRROR_ORG" ] || { echo "           refuse: HF_MIRROR_ORG not set"; RC=1; return; }
  [ -n "${HF_TOKEN:-}" ] || { echo "           refuse: HF_TOKEN not set"; RC=1; return; }
  # Existence of the exact revision on the mirror answers "already done".
  local code
  code="$(curl -s -o /dev/null -m 25 -H "Authorization: Bearer ${HF_TOKEN}" \
    -w '%{http_code}' "https://huggingface.co/$dst/raw/$3/config.json")"
  case "$code" in
    200) printf '           done: revision already on the mirror\n'; return 0 ;;
    401|403) printf '           gated mirror (private target?): verify by hand\n'; RC=1; return ;;
  esac
  python3 - "$2" "$3" "$dst" <<'PY'
import os, sys
from huggingface_hub import snapshot_download
from huggingface_hub import HfApi
src, rev, dst = sys.argv[1], sys.argv[2], sys.argv[3]
path = snapshot_download(src, revision=rev)
HfApi().create_repo(repo_id=dst, exist_ok=True)
HfApi().upload_folder(path, repo_id=dst, revision=rev, commit_message=f"mirror of {src}@{rev[:12]}")
print("           uploaded", dst, "at", rev[:12])
PY
  [ $? -eq 0 ] || { echo "           FAIL (see above)"; RC=1; }
}

mirror_image() {  # $1 label, $2 upstream reference (name@sha256:... preferred)
  printf '%-12s %s\n' "  image" "$1"
  printf '           source: %s\n' "$2"
  if [ "$MODE" = "--dry-run" ]; then
    printf '           plan: docker pull by digest, retag, push; digest identity survives registries\n'
    return 0
  fi
  [ -n "$MIRROR_REGISTRY" ] || { echo "           refuse: MIRROR_REGISTRY not set"; RC=1; return; }
  command -v docker >/dev/null || { echo "           refuse: docker not on PATH"; RC=1; return; }
  local dst="${MIRROR_REGISTRY}/${2%%@*}"
  dst="${dst#*/}"
  docker pull "$2" || { echo "           FAIL pull"; RC=1; return; }
  docker tag "$2" "$dst" || { echo "           FAIL tag"; RC=1; return; }
  docker push "$dst"    || { echo "           FAIL push"; RC=1; return; }
  printf '           pushed: %s (digest must match upstream, verify: docker inspect --format "{{index .RepoDigests 0}}")\n' "$dst"
}

IMAGE_LINES="$(grep -E '^(STOCK|FLASH|DRAFT)(_[A-Z0-9]+)?_IMAGE=' "$REPO_DIR/install.sh")"
eval "$IMAGE_LINES"

case "$MODE" in
  --dry-run) echo "mirror plan (no writes):" ;;
  ""|"--images"|* ) ;;
esac

[ "$MODE" = "--images" ] || {
  mirror_model "stock"    "${STOCK_REPO}" "${STOCK_REV}"
  mirror_model "unc"      "${UNC_REPO}"   "${UNC_REV}"
  mirror_model "fp8"      "${FP8_REPO}"   "${FP8_REV}"
  mirror_model "uncfp8"   "${UNCFP8_REPO}" "${UNCFP8_REV}"
  mirror_model "flash"    "${FLASH_REPO}" "${FLASH_REV}"
  mirror_model "draft2"   "${DRAFT2_REPO}" "${DRAFT2_REV}"
}
if [ "$MODE" = "--images" ] || [ "$MODE" = "--dry-run" ]; then
  [ -z "${STOCK_IMAGE:-}" ] || mirror_image "engine" "${STOCK_IMAGE}"
  [ -z "${FLASH_IMAGE:-}" ] || mirror_image "flash engine" "${FLASH_IMAGE}"
fi
exit $RC
