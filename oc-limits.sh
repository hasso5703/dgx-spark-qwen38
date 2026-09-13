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
#   ./oc-limits.sh --max-out                      # the ceiling every caller shares
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
  printf 'usage: %s <choice> <tier-or-mode>\n       %s <choice> --from <invocation-file>\n       %s --max-out\n' \
    "$0" "$0" "$0" >&2
  exit 2
}

CHOICE="${1:-}"
[ -n "$CHOICE" ] || usage

# --max-out: the largest output any target can ask for.
#
# OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX is a CEILING, not a per-target number:
# opencode sends max_tokens = min(limit.output, that ceiling). limit.output is
# the number that follows a switch (rewritten from this table), so the ceiling
# only has to sit above every value the table can produce. Deriving it from the
# INSTALLED target instead is the bug this exists to prevent: a box installed on
# the flash lane wrote a 64,000 ceiling, the switch to a 27B box in 1M mode
# raised limit.output to 200,000, the ceiling stayed, and a long turn was cut at
# 64,000 with no error and no text, which is the exact failure the 32,000
# default produces. Computed by asking this table for every pair, so it cannot
# drift from it.
if [ "$CHOICE" = "--max-out" ]; then
  MAXOUT=0
  for _c in flash flash-nvda flash-uncensored; do
    for _t in context concurrency throughput; do
      _o="$("$0" "$_c" "$_t" | awk '{print $2}')"
      [ -n "$_o" ] || { printf 'oc-limits: --max-out could not read %s %s\n' "$_c" "$_t" >&2; exit 2; }
      [ "$_o" -gt "$MAXOUT" ] && MAXOUT="$_o"
    done
  done
  for _c in stock uncensored fp8 uncensored-fp8; do
    for _t in native 1m; do
      _o="$("$0" "$_c" "$_t" | awk '{print $2}')"
      [ -n "$_o" ] || { printf 'oc-limits: --max-out could not read %s %s\n' "$_c" "$_t" >&2; exit 2; }
      [ "$_o" -gt "$MAXOUT" ] && MAXOUT="$_o"
    done
  done
  [ "$MAXOUT" -gt 0 ] || { printf 'oc-limits: --max-out found no limits\n' >&2; exit 2; }
  printf '%s\n' "$MAXOUT"
  exit 0
fi
SECOND="${2:-}"
INVOCATION=""
SELECTOR=""

case "$SECOND" in
  --from)
    INVOCATION="${3:-}"
    [ -n "$INVOCATION" ] || usage
    # A missing file keeps the old contract (defaults, rc 0: switch-model.sh
    # treats "no limits" as leave-everything, mid-switch is no place to die),
    # but it says so loudly instead of silently serving another lane's numbers.
    [ -f "$INVOCATION" ] || echo "oc-limits.sh: --from $INVOCATION is not a file; using defaults" >&2
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
    #
    # Where the context-tier number comes from, since it is not the ceiling:
    # opencode does NOT estimate its context. It compacts when the LAST response's
    # reported usage reaches limit.input minus min(20,000, its output cap), so the
    # threshold is simply CTX - 20,000 (opencode 1.18.27, SessionCompaction). The
    # numbers it compares are the engine's own prompt_tokens, exact.
    #
    # What can still overrun the ceiling is the step AFTER that check: opencode
    # decides between turns, then a single turn appends its tool results and sends.
    # So the rule is threshold + one worst step <= the proxy's ceiling. Measured
    # here over 2,156 flash-lane steps (opencode's own session store, 2026-09): the
    # largest single-step prompt growth is 43,863 tokens, p99 18,512. 175,000 gives
    # 155,000 + 43,863 = 198,863 against a 200,000 ceiling. 190,000 gave 213,863 and
    # is why sessions died mid-conversation.
    #
    # The refusals of 09/09 and 10/09 that first pushed this number down were read
    # as opencode "drifting" from the engine's count. They were not: the proxy was
    # charging every image a flat 4,096 tokens where the engine charges 880 to 1,562
    # for an agent screenshot, so a session holding 24 of them was refused ~61,000
    # tokens early. Fixed in the proxy (v6.15), which is where it belonged.
    case "$TIER" in
      context)     CTX=175000; OUT=64000 ;;   # 239,000 <= the pool; 175,000 keeps one whole agent step between compaction and the 200,000 ceiling (see below)
      concurrency) CTX=100000; OUT=16000 ;;   # 116,000 worst case. Measured 2026-09-12
      # with replayssm-spec the 8-request pool came out at 468,480 (not 129,792),
      # so these limits are conservative on this box; they stay until concurrent-
      # load memory is measured (single-stream floor 13.5 GiB proves nothing
      # about eight heavy streams at once).
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
