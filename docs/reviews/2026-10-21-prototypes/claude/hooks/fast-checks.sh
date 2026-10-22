#!/usr/bin/env bash
# PostToolUse hook (Edit|Write): the CI's static gates, on the one file just written, in a fraction
# of a second, so a dash, a shell syntax error or an unfilled placeholder is fixed in the same turn
# and not found 45 minutes later by ./ci-local.sh. Exit 2 hands what it found (stderr) to Claude,
# as Claude Code's hooks reference says of exit code 2 after a tool ran; exit 0 says nothing.
set -u
input="$(cat)"
file="$(printf '%s' "$input" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("tool_input", {}).get("file_path", ""))' 2>/dev/null || true)"
[ -n "$file" ] && [ -f "$file" ] || exit 0
repo="${CLAUDE_PROJECT_DIR:-$(git -C "$(dirname "$file")" rev-parse --show-toplevel 2>/dev/null || pwd)}"
case "$file" in "$repo"/*) ;; *) exit 0 ;; esac   # a file outside the repo is not the repo's to check
problems=()

# House typography, every tracked text file: no em or en dash, no minus sign (ci.yml, last step).
if LC_ALL=C.UTF-8 grep -IqP '[\x{2014}\x{2013}\x{2212}]' "$file" 2>/dev/null; then
  problems+=("$(LC_ALL=C.UTF-8 grep -InP '[\x{2014}\x{2013}\x{2212}]' "$file" | head -3 | sed 's/^/em or en dash: line /')")
fi

case "$file" in
  *.sh|*.bash) shell=1 ;;
  *) shell=0; head -1 "$file" 2>/dev/null | grep -qE '^#!.*\b(ba)?sh\b' && shell=1 ;;
esac
if [ "$shell" = 1 ]; then
  out="$(bash -n "$file" 2>&1)" || problems+=("bash -n: $out")
  if command -v shellcheck >/dev/null; then
    out="$(shellcheck -S warning "$file" 2>&1)" || problems+=("shellcheck -S warning:\n$(printf '%s' "$out" | head -30)")
  fi
fi

case "$file" in
  *.py)
    out="$(python3 -m py_compile "$file" 2>&1)" || problems+=("py_compile: $out")
    if command -v ruff >/dev/null; then
      out="$(ruff check --select F,E9 --quiet "$file" 2>&1)" || problems+=("ruff F,E9:\n$(printf '%s' "$out" | head -30)")
    fi ;;
  *.js|*.mjs)
    if command -v node >/dev/null; then
      out="$(node --check "$file" 2>&1)" || problems+=("node --check: $out")
    fi ;;
  *.md)
    # ci.yml "No document ships a placeholder nobody filled in"
    out="$(grep -nE '<[A-Z][A-Z0-9_]{2,}>' "$file" | head -3)"
    [ -z "$out" ] || problems+=("an unfilled placeholder:\n$out")
    # tests/test_docs_facts.py: every code block closes, no fence with an info string inside one
    out="$(python3 - "$repo" "$file" <<'PY'
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("df", pathlib.Path(sys.argv[1]) / "tests" / "test_docs_facts.py")
df = importlib.util.module_from_spec(spec)
spec.loader.exec_module(df)
print("\n".join(df.fence_problems(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))))
PY
)"
    [ -z "$out" ] || problems+=("code fences:\n$out") ;;
esac

# A lane list touched: the manifest and every list it knows must still agree (tools/lanegen.py).
case "$file" in
  *.template|*/lanes.toml|*/static/js/base.js|*/switch-model.sh)
    if [ -f "$repo/tools/lanegen.py" ]; then
      out="$(cd "$repo" && python3 tools/lanegen.py check 2>&1)" || problems+=("lanegen check:\n$(printf '%s' "$out" | head -20)")
    fi ;;
esac

[ "${#problems[@]}" -eq 0 ] && exit 0
{
  echo "fast checks on ${file#"$repo"/} (the same gates CI runs, on this file only):"
  for p in "${problems[@]}"; do printf -- '- %b\n' "$p"; done
} >&2
exit 2
