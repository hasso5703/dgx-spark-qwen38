#!/usr/bin/env bash
# Print the interpreter that has the test tooling (coverage, hypothesis), or
# python3 if the tooling is already there.
#
# The repo's RUNTIME is stdlib-only on purpose and that does not change. Only the
# measuring gates (coverage floors, property checks, mutation score) need pip
# packages, so they ask here rather than each hardcoding a path. On a GitHub
# runner the packages are pip-installed into python3 and this prints python3; on
# a developer box they live in a venv, and ci-local.sh runs every step under a
# throwaway HOME, so a venv found via $HOME would silently disappear and the gate
# would pass having measured nothing. Repo-local first for that reason.
set -u
want="${1:-coverage}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for cand in "${QWEN38_TEST_PYTHON:-}" "$REPO/.venv-test/bin/python" \
            "$REPO/.venv/bin/python" "$HOME/.local/share/qwen38-testenv/bin/python" \
            python3; do
  [ -n "$cand" ] || continue
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import $want" 2>/dev/null; then
    echo "$cand"
    exit 0
  fi
done
echo "no interpreter has $want; python3 -m venv .venv-test && .venv-test/bin/pip install coverage hypothesis" >&2
exit 1
