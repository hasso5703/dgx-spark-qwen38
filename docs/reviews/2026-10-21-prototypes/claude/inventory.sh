#!/usr/bin/env bash
# Phase 0: what this box's Claude Code knows about the project, and where it keeps it. Read-only:
# it prints names, sizes, counts and the first line of each description, never a setting's value,
# an environment variable or a token. Run it from the clone: bash inventory.sh > inventory.txt
set -u
repo="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
home_claude="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
section(){ printf '\n== %s\n' "$*"; }
lines(){ [ -f "$1" ] && printf '%6s lines  %s\n' "$(wc -l < "$1")" "$1"; }

section "Claude Code"
command -v claude >/dev/null && claude --version 2>/dev/null | head -1

section "Instruction files (loaded at every session start)"
for f in "$home_claude/CLAUDE.md" "$repo/CLAUDE.md" "$repo/.claude/CLAUDE.md" "$repo/CLAUDE.local.md" "$repo/AGENTS.md"; do
  lines "$f"
done
for d in "$home_claude/rules" "$repo/.claude/rules"; do
  [ -d "$d" ] && find "$d" -name '*.md' -print0 | while IFS= read -r -d '' f; do lines "$f"; done
done

section "Auto memory of this project (MEMORY.md: only its first 200 lines or 25 KB load)"
for d in "$home_claude"/projects/*/memory; do
  [ -d "$d" ] || continue
  case "$d" in *"$(basename "$repo")"*) ;; *) continue ;; esac
  lines "$d/MEMORY.md"
  find "$d" -name '*.md' ! -name MEMORY.md -printf '%TY-%Tm-%Td  %6s bytes  %f\n' | sort
done

section "Skills"
for d in "$home_claude/skills" "$repo/.claude/skills"; do
  [ -d "$d" ] && for s in "$d"/*/SKILL.md; do
    [ -f "$s" ] && printf '%s: %s\n' "$s" "$(grep -m1 '^description:' "$s" | cut -c1-140)"
  done
done

section "Subagents"
for d in "$home_claude/agents" "$repo/.claude/agents"; do
  [ -d "$d" ] && for a in "$d"/*.md; do
    [ -f "$a" ] && printf '%s: %s\n' "$a" "$(grep -m1 '^description:' "$a" | cut -c1-140)"
  done
done

section "Hooks (event and matcher only, never the command's arguments)"
for f in "$home_claude/settings.json" "$repo/.claude/settings.json" "$repo/.claude/settings.local.json"; do
  [ -f "$f" ] || continue
  printf '%s\n' "$f"
  python3 - "$f" <<'PY'
import json, sys
try:
    hooks = json.load(open(sys.argv[1])).get("hooks", {})
except (OSError, ValueError) as e:
    print("  unreadable:", e); sys.exit(0)
for event, groups in hooks.items():
    for g in groups:
        for h in g.get("hooks", []):
            cmd = str(h.get("command", "")).split()
            print(f"  {event:18s} matcher={g.get('matcher', '*')!s:14s} {h.get('type')} {cmd[0] if cmd else ''}")
PY
done

section "Git hooks of the clone (the pre-push checks a review of 2026-10-18 mentions)"
find "$repo/.git/hooks" -type f ! -name '*.sample' -printf '%f\n' 2>/dev/null
git -C "$repo" config --get core.hooksPath 2>/dev/null

section "MCP servers"
command -v claude >/dev/null && claude mcp list 2>/dev/null | sed -E 's#(https?://[^ ]*)\?[^ ]*#\1?...#'

section "What the repository versions of all this"
ls -d "$repo/.claude" "$repo/CLAUDE.md" 2>/dev/null || echo "nothing: no .claude/ and no CLAUDE.md in the repository"
