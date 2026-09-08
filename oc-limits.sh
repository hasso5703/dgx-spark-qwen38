#!/usr/bin/env bash
# The opencode limits a target deserves: one table, three callers.
#
# install.sh writes these limits at install time, switch-model.sh rewrites them
# at switch time, and CI asserts them. They are not decoration: opencode compacts
# a conversation when it reaches `limit.context`, and if that number is larger
# than what the lane can serve, the keepalive proxy refuses the request with a
# 400 BEFORE opencode ever decides to compact. The user then sees a hard failure
# in the middle of a session instead of a compaction. A switch from a 27B box in
# 1M mode (700,000 context) to the flash lane (200,000 one-prompt ceiling) is
# exactly that shape, and until 2026-09-08 the switch left the old numbers in
# place because only the installer knew this table.
#
# Duplicating it in each caller would have been the easy fix, and is how two
# numbers drift apart. So there is one table and it lives here.
#
#   ./oc-limits.sh <choice> <tier-or-mode>        # explicit: what install.sh knows
#   ./oc-limits.sh <choice> --from <invocation>   # derived: what a switch can read
#
# `choice` is a target name. The second argument is the flash serving tier
# (context | concurrency | throughput) for a flash target, or the 27B context
# mode (native | 1m) for the others. With `--from`, the answer is derived from
# what the box actually serves instead: the tier from the launcher's
# --max-running-requests, the context mode from the unit's --context-length.
#
# Prints "CTX OUT LABEL". Every number is measured; the measurements are in
# BENCHMARKS.md and the reasoning is in the comments below.
set -uo pipefail

usage() {
  printf 'usage: %s <choice> <tier-or-mode>\n       %s <choice> --from <invocation-file>\n' \
    "$0" "$0" >&2
  exit 2
}

CHOICE="${1:-}"
[ -n "$CHOICE" ] || usage
SECOND="${2:-}"
INVOCATION=""
SELECTOR=""

case "$SECOND" in
  --from)
    INVOCATION="${3:-}"
    [ -n "$INVOCATION" ] || usage
    ;;
  "")   ;;                       # nothing given: fall back to the defaults below
  *)    SELECTOR="$SECOND" ;;
esac

flag() {  # $1 flag name -> its value in the invocation, or empty
  [ -n "$INVOCATION" ] && [ -f "$INVOCATION" ] || return 0
  grep -oE -- "$1 [0-9]+" "$INVOCATION" 2>/dev/null | awk '{print $2}' | head -1
}

case "$CHOICE" in
  flash|flash-nvda|flash-uncensored)
    TIER="$SELECTOR"
    if [ -z "$TIER" ]; then
      # Derived, or defaulted: the tier is what the launcher pins as concurrency.
      case "$(flag '--max-running-requests')" in
        24) TIER=throughput ;;
        8)  TIER=concurrency ;;
        *)  TIER=context ;;
      esac
    fi
    # The budget is min(the proxy's one-prompt ceiling, what the tier's KV pool
    # holds alongside the answer).
    case "$TIER" in
      context)     CTX=190000; OUT=64000 ;;   # 254,000 <= the pool, and 190,000 <= the 200,000 ceiling
      concurrency) CTX=100000; OUT=16000 ;;   # 116,000 <= the 129,792-token pool, 100,000 <= its 119,408 proxy share
      throughput)  CTX=110000; OUT=32000 ;;   # a big pool, but 24 requests share it
      *) printf 'oc-limits: unknown flash tier "%s"\n' "$TIER" >&2; exit 2 ;;
    esac
    LABEL="local"
    ;;
  stock|uncensored|fp8|uncensored-fp8)
    MODE="$SELECTOR"
    if [ -z "$MODE" ]; then
      CTX_LEN="$(flag '--context-length')"
      if [ "${CTX_LEN:-0}" -gt 262144 ] 2>/dev/null; then MODE=1m; else MODE=native; fi
    fi
    case "$MODE" in
      1m)
        case "$CHOICE" in
          fp8|uncensored-fp8)
            # FP8 weights cost about 92,000 tokens of KV pool (measured, same 1M
            # unit: 863,398 on NVFP4, 771,139 on FP8), and the NVFP4 numbers do
            # not transfer: 680,000 of compaction plus 200,000 of output is an
            # 880,000 worst case against a 771,139 pool.
            CTX=480000; OUT=160000 ;;
          *)
            CTX=700000; OUT=200000 ;;
        esac
        LABEL="local, 1M"
        ;;
      native) CTX=194048; OUT=64000; LABEL="local" ;;
      *) printf 'oc-limits: unknown context mode "%s"\n' "$MODE" >&2; exit 2 ;;
    esac
    ;;
  *) printf 'oc-limits: unknown target "%s"\n' "$CHOICE" >&2; exit 2 ;;
esac

printf '%s %s %s\n' "$CTX" "$OUT" "$LABEL"
