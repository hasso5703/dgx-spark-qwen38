#!/usr/bin/env bash
# One-liner bootstrap: first install AND update, same command, any directory:
#   curl -fsSL https://raw.githubusercontent.com/hasso5703/dgx-spark-qwen38/main/get.sh | bash
# Pass install.sh options after `bash -s --`, e.g.:
#   curl -fsSL .../get.sh | bash -s -- --no-service
# Env overrides pass through (PORT=, HF_CACHE=, DIR= for a custom clone location).
# Everything is wrapped in main() so nothing runs on a partial download.
set -euo pipefail

main() {
  REPO_URL="https://github.com/hasso5703/dgx-spark-qwen38"
  DEFAULT_DIR="$HOME/dgx-spark-qwen38"
  command -v git >/dev/null || { echo "ERROR: git is required (stock on DGX OS)." >&2; exit 1; }

  # Case 1: running from inside an existing clone of this repo (any path) -> use it.
  DIR="${DIR:-}"
  if [ -z "$DIR" ] && TOP=$(git rev-parse --show-toplevel 2>/dev/null); then
    ORIGIN=$(git -C "$TOP" remote get-url origin 2>/dev/null || true)
    case "$ORIGIN" in
      *hasso5703/dgx-spark-qwen38*) DIR="$TOP" ;;
    esac
  fi

  # Case 2: default location -> reuse the clone or create it.
  if [ -z "$DIR" ]; then
    DIR="$DEFAULT_DIR"
    if [ -d "$DIR/.git" ]; then
      :
    elif [ -e "$DIR" ]; then
      echo "ERROR: $DIR exists but is not a clone of this repo. Move it, or rerun with DIR=/path/to/clone" >&2
      exit 1
    else
      echo "── Cloning $REPO_URL into $DIR"
      git clone --depth 1 "$REPO_URL" "$DIR"
    fi
  fi

  echo "── Repo: $DIR"
  git -C "$DIR" fetch -q origin main
  if [ -n "$(git -C "$DIR" status --porcelain)" ]; then
    echo "NOTE: local changes present, leaving them untouched (no pull). Delete or stash them to update."
  else
    git -C "$DIR" merge -q --ff-only origin/main 2>/dev/null \
      || echo "NOTE: local branch diverged from origin/main, leaving it as is."
    echo "── At: $(git -C "$DIR" log -1 --format='%h %s')"
  fi

  cd "$DIR"
  # exec from a real file: sudo prompts on the tty, and stdin is no longer the pipe.
  exec bash ./install.sh "$@"
}

main "$@"
