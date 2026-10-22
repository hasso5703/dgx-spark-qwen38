#!/usr/bin/env bash
# PreToolUse hook (Bash): a tag, a GitHub release or a push to main leaves the box only once
# ./ci-local.sh passed whole, with nothing skipped, on exactly the tree being released. ci-local.sh
# writes .cache/ci-pass (the tree's hash) when it ends on "0 echecs, 0 sautes" with a clean
# worktree; this compares. Exit 2 blocks the command and hands the reason to Claude. Reading tags,
# pushing a work branch and every other command go through.
set -u
repo="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$repo" 2>/dev/null || exit 0
# Each simple command of the line, split as the shell would, then: is it a release?
held="$(python3 -c '
import json, re, shlex, subprocess, sys
cmd = json.load(sys.stdin).get("tool_input", {}).get("command", "")
READ_TAG = ("-l", "--list", "-n", "--contains", "--no-contains", "--points-at", "--merged", "--no-merged",
            "-v", "--verify", "--sort", "--format", "--column")
VALUED = ("-o", "--push-option", "--repo", "--receive-pack", "--exec")

def branch():
    return subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True).stdout.strip()

def release(words):
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):   # VAR=value cmd
        words = words[1:]
    if words[:2] == ["gh", "release"]:
        return None if words[2:3] in (["list"], ["view"], ["download"]) else "gh release"
    if words[:1] != ["git"]:
        return None
    i = 1
    while i < len(words) and words[i] in ("-C", "-c"):
        i += 2
    sub, args = (words[i], words[i + 1:]) if i < len(words) else ("", [])
    if sub == "tag":
        return None if not args or args[0].split("=")[0] in READ_TAG else "git tag"
    if sub != "push":
        return None
    flags, rest, skip = [], [], False
    for a in args:
        if skip:
            skip = False
        elif a in VALUED:
            skip = True
        elif a.startswith("-"):
            flags.append(a)
        else:
            rest.append(a)
    if set(flags) & {"--tags", "--follow-tags", "--mirror", "--all"}:
        return "a push of tags or of every branch"
    for ref in rest[1:]:                                   # rest[0] is the remote
        dst = ref.split(":")[-1].lstrip("+")
        if dst in ("main", "refs/heads/main") or dst.startswith("refs/tags/") or re.fullmatch(r"v[0-9]+(\.[0-9]+)*", dst):
            return "a push to " + dst
        if dst == "HEAD" and branch() == "main":
            return "a push of main"
    if len(rest) <= 1 and branch() == "main":
        return "a push of main"
    return None

for part in re.split(r"&&|\|\||[;|&\n]", cmd):
    try:
        words = shlex.split(part)
    except ValueError:
        words = part.split()
    why = release(words)
    if why:
        print(why)
        break
' 2>/dev/null)" || exit 0
[ -n "$held" ] || exit 0
tree="$(git rev-parse 'HEAD^{tree}' 2>/dev/null)" || exit 0
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "blocked ($held): the worktree has uncommitted changes; commit them, run ./ci-local.sh whole, then try again" >&2
  exit 2
fi
if [ "$(cat .cache/ci-pass 2>/dev/null)" != "$tree" ]; then
  echo "blocked ($held): no full ./ci-local.sh run passed on this tree ($tree); run it to 0 echecs, 0 sautes, then try again" >&2
  exit 2
fi
exit 0
