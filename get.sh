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

  # Invariant: if this script completes, you ARE on the latest origin/main.
  if [ -n "$(git -C "$DIR" status --porcelain)" ]; then
    if [ "${FORCE_UPDATE:-0}" = "1" ]; then
      echo "── FORCE_UPDATE=1: stashing your local changes (recoverable: git stash list)"
      git -C "$DIR" stash push -u -m "get.sh auto-stash before update"
    else
      echo "ERROR: $DIR has local modifications, refusing to install a stale or altered version." >&2
      git -C "$DIR" status --short | head -10 >&2
      echo "Fix with ONE of:" >&2
      echo "  keep your changes aside :  FORCE_UPDATE=1  then rerun this command (recover later: git -C $DIR stash pop)" >&2
      echo "  discard your changes    :  git -C $DIR checkout -- . && git -C $DIR clean -fd  then rerun" >&2
      exit 1
    fi
  fi
  if ! git -C "$DIR" merge -q --ff-only origin/main 2>/dev/null; then
    if [ "${FORCE_UPDATE:-0}" = "1" ]; then
      BK="backup-$(git -C "$DIR" rev-parse --short HEAD)"
      git -C "$DIR" branch -f "$BK" >/dev/null
      echo "── FORCE_UPDATE=1: local branch diverged; kept as branch '$BK', resetting to origin/main"
      git -C "$DIR" reset -q --hard origin/main
    else
      echo "ERROR: local branch diverged from origin/main, refusing to install a stale version." >&2
      echo "  keep your commits aside :  FORCE_UPDATE=1  then rerun (they stay on a backup branch)" >&2
      exit 1
    fi
  fi
  echo "── At: $(git -C "$DIR" log -1 --format='%h %s')"

  cd "$DIR"
  # exec from a real file: sudo prompts on the tty, and stdin is no longer the pipe.
  exec bash ./install.sh "$@"
}

main "$@"
