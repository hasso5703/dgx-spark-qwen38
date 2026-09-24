#!/usr/bin/env bash
set -u
# Do the pins exist where THEY will live when upstream dies?
#
# check-pins.sh asks upstream whether the pins still resolve; this asks the
# other half of the question: whether the mirror holds them. It is a plan-
# first tool: the default, --dry-run, prints what would be copied where (this
# mode needs no credentials and no network), and each execute mode copies only
# what is missing, resumably, and never rewrites a pin.
#
# The contract it protects (MIRROR.md is the runbook):
#   - a checkpoint's pinned revision, downloaded at that revision, uploaded to
#     a repo of its own on the mirror and tagged upstream-<revision>, then
#     compared with upstream file for file (name, size, hash). A commit id
#     cannot be carried over: the tag is what names the pin on the mirror.
#   - an image pinned by digest, copied whole (every platform of its index, by
#     docker buildx imagetools) so the mirror answers the same digest, which is
#     checked after the copy. A pull and a push carry one platform, under
#     another digest.
#
# What this script will NOT do: invent a license conclusion. A checkpoint is
# mirrored only once its row in MIRROR.md's table carries a dated conclusion;
# the table is the conclusion, a human writes it, and this script reads it.
#
#   ./mirror-pins.sh                    # the plan (same as --dry-run): no credentials, no writes
#   ./mirror-pins.sh --models           # checkpoints (needs HF_TOKEN, HF_MIRROR_ORG, huggingface_hub)
#   ./mirror-pins.sh --images           # images (needs MIRROR_REGISTRY, docker buildx, a registry login)
#
# An execute mode exits 0 when every pin it covers is on the mirror, 1 otherwise
# (a pin whose license is not concluded counts: it is not mirrored); the plan
# exits 0, and an unknown mode 2.
set -o pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:---dry-run}"
case "$MODE" in
  --dry-run|--models|--images) ;;
  *) echo "usage: $0 [--dry-run | --models | --images]   (got: $MODE)" >&2; exit 2 ;;
esac
HF_MIRROR_ORG="${HF_MIRROR_ORG:-}"
MIRROR_REGISTRY="${MIRROR_REGISTRY:-}"
MIRROR_MD="${MIRROR_MD:-$REPO_DIR/MIRROR.md}"
RC=0

# Same extraction as check-pins.sh (keep the two in sync on purpose: one
# asks upstream, this one asks the mirror; both read the one pin block).
pins="$(grep -E '^(STOCK|UNC|FP8|UNCFP8|FLASH|FLASH_NVDA|FLASH_UNC|DRAFT|DRAFT2)_(REPO|REV)=' "$REPO_DIR/install.sh")"
eval "$pins"
img="$(grep -E '^(IMAGE|FLASH_IMAGE)=' "$REPO_DIR/install.sh")"
eval "$img"

# The conclusion MIRROR.md records for a pin, or nothing when its table has no row.
license_of() {  # $1 label
  awk -F'|' -v pin="$1" '{ l = $2; gsub(/^ +| +$/, "", l) } l == pin { c = $4; gsub(/^ +| +$/, "", c); print c; exit }' "$MIRROR_MD"
}
# Concluded: a row that says something other than "not yet checked", with the date it was.
license_concluded() {  # $1 conclusion
  case "$1" in ""|"not yet checked"*) return 1 ;; esac
  printf '%s' "$1" | grep -qE '(19|20)[0-9]{2}-[0-9]{2}-[0-9]{2}'
}

# The interpreter that has huggingface_hub: the runbook's venv when it exists.
MIRROR_PYTHON="${MIRROR_PYTHON:-}"
if [ -z "$MIRROR_PYTHON" ]; then
  MIRROR_PYTHON=python3
  [ -x "$REPO_DIR/.venv-mirror/bin/python" ] && MIRROR_PYTHON="$REPO_DIR/.venv-mirror/bin/python"
fi

mirror_model() {  # $1 label, $2 repo, $3 revision
  # owner and name both: two owners publish checkpoints under one name
  # (RadixArk/ and nvidia/Qwen3.8-Flash-Next-NVFP4), and a mirror named after the
  # name alone would put both in one repo. "--" is refused in repo ids, "__" is not.
  local dst="${HF_MIRROR_ORG:-<HF_MIRROR_ORG>}/${2%%/*}__${2##*/}" tag="upstream-$3" lic
  lic="$(license_of "$1")"
  printf '%-12s %s\n' "  model" "$1"
  printf '           source: %s @ %s\n' "$2" "$3"
  printf '           mirror: %s @ %s\n' "$dst" "$tag"
  printf '           license: %s\n' "${lic:-no row in MIRROR.md}"
  if ! license_concluded "$lic"; then
    echo "           skip: no dated license conclusion in MIRROR.md, so this pin is not mirrored"
    [ "$MODE" = "--dry-run" ] || RC=1
    return 0
  fi
  if [ "$MODE" = "--dry-run" ]; then
    echo "           plan: download the pinned revision, upload, tag $tag, compare file for file"
    return 0
  fi
  [ -n "$HF_MIRROR_ORG" ] || { echo "           refuse: HF_MIRROR_ORG not set"; RC=1; return 0; }
  [ -n "${HF_TOKEN:-}" ] || { echo "           refuse: HF_TOKEN not set"; RC=1; return 0; }
  "$MIRROR_PYTHON" -c 'import huggingface_hub' 2>/dev/null || {
    echo "           refuse: $MIRROR_PYTHON has no huggingface_hub (MIRROR.md: python3 -m venv .venv-mirror && .venv-mirror/bin/pip install huggingface_hub)"
    RC=1; return 0; }
  "$MIRROR_PYTHON" - "$2" "$3" "$dst" "$tag" <<'PY' || { echo "           FAIL (see above)"; RC=1; }
import sys
from huggingface_hub import HfApi, snapshot_download
src, rev, dst, tag = sys.argv[1:5]
api = HfApi()


def files(repo, revision):
    """What must match: every file's name, size and content hash."""
    info = api.model_info(repo, revision=revision, files_metadata=True)
    return {(s.rfilename, s.size, s.lfs.sha256 if s.lfs else s.blob_id) for s in info.siblings}


want = files(src, rev)
refs = {t.name for t in api.list_repo_refs(dst).tags} if api.repo_exists(dst) else set()
if tag not in refs:
    path = snapshot_download(src, revision=rev)
    api.create_repo(repo_id=dst, exist_ok=True)
    commit = api.upload_folder(repo_id=dst, folder_path=path, commit_message=f"mirror of {src}@{rev}")
    api.create_tag(dst, tag=tag, revision=commit.oid, tag_message=f"{src}@{rev}")
    print(f"           uploaded {dst} at {commit.oid[:12]}, tagged {tag}")
else:
    print(f"           already there: {dst} @ {tag}")
got = files(dst, tag)
if got != want:
    diff = sorted(f[0] for f in want ^ got)
    sys.exit(f"           FAIL: {dst} @ {tag} differs from {src} @ {rev[:12]} on {len(diff)} files: {diff[:5]}")
print(f"           done: {len(want)} files, name, size and hash equal to upstream")
PY
}

mirror_image() {  # $1 label, $2 upstream reference, pinned by digest
  local name="${2%%@*}" digest="${2#*@}" first
  printf '%-12s %s\n' "  image" "$1"
  printf '           source: %s\n' "$2"
  case "$2" in *@sha256:*) ;; *) echo "           refuse: not pinned by digest, so there is nothing fixed to mirror"; RC=1; return 0 ;; esac
  # the mirror keeps the source's repository path under its own host: a registry
  # host in the source (docker.io/..., ghcr.io/...) is not part of that path
  first="${name%%/*}"
  case "$first" in *.*|*:*|localhost) name="${name#*/}" ;; esac
  local mirror="${MIRROR_REGISTRY:-<MIRROR_REGISTRY>}"
  mirror="${mirror%/}/$name"
  printf '           mirror: %s@%s (tag %s)\n' "$mirror" "$digest" "${digest/:/-}"
  if [ "$MODE" = "--dry-run" ]; then
    echo "           plan: copy the whole index by digest (docker buildx imagetools create), then ask the mirror for that digest"
    return 0
  fi
  [ -n "$MIRROR_REGISTRY" ] || { echo "           refuse: MIRROR_REGISTRY not set"; RC=1; return 0; }
  docker buildx version >/dev/null 2>&1 || { echo "           refuse: docker buildx is needed (a pull and a push copy one platform, under another digest)"; RC=1; return 0; }
  if docker buildx imagetools inspect "$mirror@$digest" >/dev/null 2>&1; then
    echo "           done: the mirror already answers $digest"; return 0
  fi
  docker buildx imagetools create --tag "$mirror:${digest/:/-}" "$2" || { echo "           FAIL copy"; RC=1; return 0; }
  if docker buildx imagetools inspect "$mirror@$digest" >/dev/null 2>&1; then
    echo "           done: copied, and the mirror answers $digest"
  else
    echo "           FAIL: after the copy the mirror does not answer $digest; an install pinned to it would not find it there"
    RC=1
  fi
}

[ "$MODE" = "--dry-run" ] && echo "mirror plan (no writes):"
if [ "$MODE" != "--images" ]; then
  echo "Checkpoints"
  mirror_model "stock"      "$STOCK_REPO"      "$STOCK_REV"
  mirror_model "unc"        "$UNC_REPO"        "$UNC_REV"
  mirror_model "fp8"        "$FP8_REPO"        "$FP8_REV"
  mirror_model "uncfp8"     "$UNCFP8_REPO"     "$UNCFP8_REV"
  mirror_model "flash"      "$FLASH_REPO"      "$FLASH_REV"
  mirror_model "flash-nvda" "$FLASH_NVDA_REPO" "$FLASH_NVDA_REV"
  mirror_model "flash-unc"  "$FLASH_UNC_REPO"  "$FLASH_UNC_REV"
  mirror_model "draft"      "$DRAFT_REPO"      "$DRAFT_REV"
  mirror_model "draft2"     "$DRAFT2_REPO"     "$DRAFT2_REV"
fi
if [ "$MODE" != "--models" ]; then
  echo "Images"
  mirror_image "27b-base"     "$IMAGE"
  mirror_image "flash-base"   "$FLASH_IMAGE"
fi
exit $RC
