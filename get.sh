#!/usr/bin/env bash
# One-liner bootstrap: first install AND update, same command, any directory:
#   curl -fsSL https://raw.githubusercontent.com/hasso5703/dgx-spark-qwen38/main/get.sh | bash
# NO sudo in front, ever: install.sh calls sudo itself for the steps that need
# it, and under sudo every path it writes moves to /root (refused below).
# It installs the whole box, engine and proxy and cockpit, and prints the
# cockpit URL when it is done.
# Pass install.sh options after `bash -s --`, e.g.:
#   curl -fsSL .../get.sh | bash -s -- --no-service
# Env overrides pass through (PORT=, HF_CACHE=, DIR= for a custom clone location).
# Everything is wrapped in main() so nothing runs on a partial download.
set -euo pipefail

main() {
  # ── This does not run under sudo, and the refusal comes before the clone ──
  # Piped into `sudo bash`, every path below moves to /root: the clone lands in
  # /root/dgx-spark-qwen38 and install.sh then writes /root/.config/qwen38 with
  # a brand new API key, leaving a healthy engine that answers 401 to every
  # client reading ~/.config/qwen38/api-key. That happened on the reference box
  # on 2026-09-13. install.sh calls sudo itself, for the steps that need it.
  # install.sh refuses again on its own; this one exists so the refusal lands
  # before anything is written, and names the command that was actually typed.
  if [ "$(id -u)" = "0" ]; then
    if [ -n "${SUDO_USER:-}" ]; then
      printf '\n\033[1;31mERROR:\033[0m do not pipe this into "sudo bash" (your login is %s).\n\n' "$SUDO_USER" >&2
      printf '  run : curl -fsSL %s/raw/main/get.sh | bash\n' "https://github.com/hasso5703/dgx-spark-qwen38" >&2
      printf '        (no sudo in front: the installer calls sudo itself, for the steps that need it)\n\n' >&2
      printf 'Under sudo, HOME is /root: the clone and the API key land there, the units point\n' >&2
      printf 'at them, and every client reading ~/.config/qwen38/api-key gets 401 from an engine\n' >&2
      printf 'that installed, started and served perfectly well.\n' >&2
      exit 1
    fi
    if [ "${ALLOW_ROOT:-0}" != "1" ]; then
      printf '\n\033[1;31mERROR:\033[0m run this as the user who will use the box, not as root.\n' >&2
      printf '  everything it installs is addressed from $HOME, which as root is /root.\n' >&2
      printf '  if this box genuinely has no other user: ALLOW_ROOT=1 before the pipe.\n' >&2
      exit 1
    fi
  fi

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
      git clone "$REPO_URL" "$DIR"
    fi
  fi

  # Whatever resolved DIR: it must be a clone of THIS repo before we touch it.
  DIR_ORIGIN=$(git -C "$DIR" remote get-url origin 2>/dev/null || true)
  case "$DIR_ORIGIN" in
    *hasso5703/dgx-spark-qwen38*) : ;;
    *) echo "ERROR: $DIR is not a clone of this repo (origin: ${DIR_ORIGIN:-none}). Refusing to touch it." >&2; exit 1 ;;
  esac

  echo "── Repo: $DIR"
  git -C "$DIR" fetch -q origin main

  # Must be on main (a detached HEAD or a side branch would silently pin an old version).
  CUR=$(git -C "$DIR" symbolic-ref -q --short HEAD || echo DETACHED)
  if [ "$CUR" != "main" ]; then
    if [ "${FORCE_UPDATE:-0}" = "1" ]; then
      echo "── FORCE_UPDATE=1: switching from '$CUR' to main (your branch is kept)"
      git -C "$DIR" checkout -q main 2>/dev/null || git -C "$DIR" checkout -qb main origin/main
    else
      echo "ERROR: $DIR is on '$CUR', not 'main'; refusing to install from it." >&2
      echo "  rerun with FORCE_UPDATE=1 to switch to main (your branch/commit is kept)" >&2
      exit 1
    fi
  fi

  # Invariant: if this script completes, you ARE on the latest origin/main.
  # Only TRACKED modifications can make the checkout stale or altered. Untracked
  # files cannot, and `git reset --hard` below leaves them alone, so blocking on
  # them turned an unrelated notes file into a failed install and, under
  # FORCE_UPDATE, swept it into a stash the user never asked for.
  if [ -n "$(git -C "$DIR" status --porcelain --untracked-files=no)" ]; then
    if [ "${FORCE_UPDATE:-0}" = "1" ]; then
      echo "── FORCE_UPDATE=1: stashing your tracked changes (recoverable: git stash list)"
      git -C "$DIR" stash push -m "get.sh auto-stash before update"
    else
      echo "ERROR: $DIR has modified tracked files, refusing to install a stale or altered version." >&2
      git -C "$DIR" status --short --untracked-files=no | head -10 >&2
      echo "Fix with ONE of:" >&2
      echo "  keep your changes aside :  FORCE_UPDATE=1  then rerun this command (recover later: git -C $DIR stash pop)" >&2
      echo "  discard your changes    :  git -C $DIR checkout -- . && git -C $DIR clean -fd  then rerun" >&2
      exit 1
    fi
  fi
  AHEAD=$(git -C "$DIR" rev-list --count origin/main..HEAD 2>/dev/null || echo 1)
  if [ "$AHEAD" != "0" ]; then
    if [ "${FORCE_UPDATE:-0}" = "1" ]; then
      BK="backup-$(git -C "$DIR" rev-parse --short HEAD)"
      git -C "$DIR" branch -f "$BK" >/dev/null
      echo "── FORCE_UPDATE=1: local commits kept on branch '$BK', resetting to origin/main"
    else
      echo "ERROR: $DIR has local commits not on origin/main, refusing to install a stale version." >&2
      echo "  keep your commits aside :  FORCE_UPDATE=1  then rerun (they stay on a backup branch)" >&2
      exit 1
    fi
  fi
  # Untracked files are kept, but say so: if one of them is a path that origin/main
  # now tracks, the reset below writes over it.
  UNTRACKED=$(git -C "$DIR" ls-files --others --exclude-standard)
  if [ -n "$UNTRACKED" ]; then
    echo "── Untracked files kept in place (not part of the update):"
    printf '%s\n' "$UNTRACKED" | head -5 | sed 's/^/     /'
  fi
  # Tree verified clean and no local commits (or backed up): hard sync is lossless
  # and, unlike ff-only, immune to shallow-clone ancestry gaps.
  git -C "$DIR" reset -q --hard origin/main
  echo "── At: $(git -C "$DIR" log -1 --format='%h %s')"

  cd "$DIR"
  # exec from a real file: sudo prompts on the tty, and stdin is no longer the pipe.
  exec bash ./install.sh "$@"
}

main "$@"
