#!/usr/bin/env bash
# Dump the Python stacks of the serving engine's scheduler process (root only,
# installed to /usr/local/bin by install-dashboard.sh and allowed in sudoers with
# NO arguments). Used by the cockpit when it declares an engine wedged, so the
# evidence exists before autoheal restarts the unit. Read-only: py-spy dump
# attaches with ptrace, prints, detaches.
set -euo pipefail
PYSPY=""
for c in /usr/local/bin/py-spy /usr/bin/py-spy /root/.local/bin/py-spy; do
  [ -x "$c" ] && PYSPY="$c" && break
done
[ -n "$PYSPY" ] || { echo "py-spy not installed (pip install py-spy, or copy the binary to /usr/local/bin/py-spy)"; exit 3; }
for cont in qwen38-flash qwen38-sglang; do
  pid="$(docker top "$cont" -o pid,comm 2>/dev/null | awk '/schedul/{print $1}' | head -1 || true)"
  if [ -n "$pid" ]; then
    echo "== $cont scheduler pid $pid, state $(awk '{print $3}' /proc/$pid/stat 2>/dev/null || echo ?) =="
    "$PYSPY" dump --pid "$pid" --nonblocking 2>&1 | sed 's/\x1b\[[0-9;]*m//g'
    exit 0
  fi
done
echo "no serving container running"; exit 4
