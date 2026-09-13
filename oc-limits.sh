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
#   ./oc-limits.sh --preserve <ctx>               # tokens a compaction keeps verbatim
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
# --preserve: how much of the recent conversation survives a compaction verbatim.
#
# opencode's default is min(15,000, max(2,000, threshold/4)), and that 15,000 cap
# is the problem on a big window: it was chosen for models with a 128K context,
# and on this lane it means a compaction at 205,000 tokens keeps 15,000 and
# summarises the other 190,000. The window is then not being used, it is being
# refilled from scratch every time.
#
# So the installed value is opencode's own intent without the cap: a quarter of
# the compaction threshold, which is limit.context minus the 20,000 opencode
# reserves. Rounded to 1,000. Sized from the INSTALLED target because the key is
# global in opencode.json while the threshold is per-target: install.sh and
# switch-model.sh rewrite it with the limits, so it always tracks the lane that
# serves. A value at or above the threshold would make select() keep everything
# and summarise the lot, which is the failure this avoids, so it is capped at
# a third of the threshold.
if [ "$CHOICE" = "--preserve" ]; then
  CTX_IN="${2:-}"
  case "$CTX_IN" in ''|*[!0-9]*) printf 'oc-limits: --preserve needs a context size\n' >&2; exit 2 ;; esac
  THRESH=$(( CTX_IN - 20000 ))
  [ "$THRESH" -gt 0 ] || { printf '0\n'; exit 0; }
  KEEP=$(( THRESH / 4 ))
  CAP=$(( THRESH / 3 ))
  [ "$KEEP" -gt "$CAP" ] && KEEP="$CAP"
  printf '%s\n' "$(( KEEP / 1000 * 1000 ))"
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
    # largest single-step prompt growth is 43,863 tokens, p99 18,512.
    #
    #     225,000 - 20,000 + 43,863 = 248,863  <=  250,000 ceiling
    #
    # The ceiling is 250,000 because the box was measured there rather than assumed
    # (2026-09-13, this engine, MemAvailable sampled through each prefill):
    #
    #     195,784 tokens -> 1.16 GiB of host headroom,  94.7 s
    #     225,051 tokens -> 1.57 GiB,                  111.3 s
    #     249,500 tokens -> 1.52 GiB,                  126.1 s, floor 7.0 GiB
    #
    # The old 128,000 and 200,000 ceilings came from a v1.5 engine whose PLE mapping
    # faulted in whole page-cache folios (~0.27 GiB per 1k tokens past 90k, and 3.5
    # GiB at 200k). v1.8 serves an engine that does not: a full-depth prefill now
    # costs about 1.5 GiB and leaves 7 GiB, so the memory argument for stopping at
    # 200,000 no longer holds and the window the model actually has is usable.
    # What does still hold is the engine's own wall, max_req_input_len = 262,138:
    # 250,000 leaves 12,138 tokens of slack under it, which is the band that keeps
    # a mis-count on the proxy's side from reaching a truncation on the engine's.
    #
    # The 175,000 this replaces, and the 190,000 before it, were both set by reading
    # proxy refusals as opencode "drifting" from the engine's count. It does not
    # drift: the proxy was charging every image a flat 4,096 tokens where the engine
    # charges 880 to 1,562 for an agent screenshot, so a session holding 24 of them
    # was refused ~61,000 tokens early. Fixed in the proxy (v6.15).
    case "$TIER" in
      context)     CTX=225000; OUT=32000 ;;   # 257,000 <= the pool; keeps one whole agent step between compaction and the 250,000 ceiling (see above)
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
