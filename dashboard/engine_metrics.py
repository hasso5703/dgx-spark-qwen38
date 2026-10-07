"""What the serving engine's own counters say about the last minutes (its /metrics).

SGLang serves Prometheus counters on the engine's port: both text lanes start with
--enable-metrics, and the endpoint is exempt from the API key. Per FINISHED request it adds
the request's prompt tokens, how many of them the radix cache held, and its generated
tokens; for every output after a request's first, the time since the previous one and the
tokens it carried (inter_token_latency); per request, the time to its first output, which
is its first token when it streams (agents do); a request that does not stream has its
first output when its length lands on a multiple of 50 tokens, or at its end. Measured on
the reference box (2026-10-07) against the client's own view: prompt and generated tokens
to the unit as the answer's usage says them, a decode speed of 115.35 against the client's
115.3 tok/s, a first token at 56.103 s against 56.106 s (within 4 ms on every request). The
difference of two reads is then exact: each request counted once, and no idle time in a
speed. The scheduler's log lines carry "throughput" figures too, and those are not used
here: each is a token count over the time since the previous line of its kind, idle time
included (the v1.22.12 notes in CHANGELOG.md have the measurements).

Two things are not the clients': the cockpit's own canary, a real two-token chat request
every 90 s while nothing else runs, which SGLang counts like any other, and the /health
probe, which it does not count (log_metrics=False). The canary is taken out exactly: the
cockpit reads the counters just before and just after it, and the difference is its own as
long as nothing else moved meanwhile; when something did, the window says its numbers are
approximate for as long as that canary lies inside it. A read of the window's own that ran while a
canary was in flight may or may not hold it, so it is dropped: every read kept is wholly
before or wholly after each canary, and the canary is taken out of exactly the windows that
hold it. Counters start again from zero with every engine start, so a new engine, or
counters that went back, start the window again.
"""
from __future__ import annotations

import math
import re
from collections import deque

NAMES = {
    "requests": "sglang:num_requests_total",
    "prompt": "sglang:prompt_tokens_total",
    "cached": "sglang:cached_tokens_total",
    "generated": "sglang:generation_tokens_total",
    "ttft_sum": "sglang:time_to_first_token_seconds_sum",
    "ttft_count": "sglang:time_to_first_token_seconds_count",
    "itl_sum": "sglang:inter_token_latency_seconds_sum",
    "itl_count": "sglang:inter_token_latency_seconds_count",
}
_BY_NAME = {v: k for k, v in NAMES.items()}
_SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^\n]*\})?[ \t]+(\S+)(?:[ \t]+\S+)?$')


def parse(text: str) -> dict | None:
    """Each counter summed over its label sets (a model, a cache source, streaming or not).
    A counter not on the page yet is zero: prometheus_client writes a labelled series only
    once it was first observed, and an engine just ready lists no cached tokens and no
    inter-token time (its own warm-up request had neither). None when the page has no
    SGLang metric at all: not an SGLang engine's /metrics."""
    out = dict.fromkeys(NAMES, 0.0)
    sglang = False
    for line in text.splitlines():
        if "sglang:" in line:
            sglang = True
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE.match(line.strip())
        if not m or m.group(1) not in _BY_NAME:
            continue
        try:
            v = float(m.group(3))
        except ValueError:
            continue
        if math.isfinite(v):
            out[_BY_NAME[m.group(1)]] += v
    return out if sglang else None


def delta(a: dict, b: dict) -> dict:
    return {k: b[k] - a[k] for k in NAMES}


def isolated(d: dict) -> bool:
    """A difference that holds the canary and nothing else: one finished request, one first
    token, and at most the one inter-token interval of its two tokens."""
    return d["requests"] == 1 and d["ttft_count"] == 1 and d["itl_count"] <= 1


class Window:
    """The last `span` seconds of one engine's counters, read every few seconds."""

    def __init__(self, span: float = 300.0):
        self.span = span
        self.key = None
        self.samples: deque = deque()     # (t, values)
        self.canaries: deque = deque()    # (t, difference) of the cockpit's own canary
        self.started = None               # the first read of this engine's counters
        self.unseparated: deque = deque() # when each canary not taken out exactly ended
        self.inflight = None              # when the canary in flight began, if one is
        self.spans: deque = deque()       # (began, ended) of each canary since the oldest read

    def reset(self, key, t):
        self.key, self.started = key, t
        self.samples.clear()
        self.canaries.clear()
        self.spans.clear()
        self.unseparated.clear()

    def add(self, t: float, key, values: dict, started: float | None = None) -> bool:
        """One read of the counters of the engine `key` (its unit and its start), which began
        at `started` and ended at `t`. False when it is dropped: it ran across a canary."""
        started = t if started is None else started
        # a canary that began over a minute ago is over, whatever became of its end (its
        # request gives up at 25 s): never let one lost end blind the window
        if self.inflight is not None and t - self.inflight < 60:
            return False
        if any(started <= ended and t >= began for began, ended in self.spans):
            return False
        if key != self.key or (self.samples and any(values[k] < self.samples[-1][1][k] for k in NAMES)):
            self.reset(key, t)
        self.samples.append((t, values))
        # one read at or before the window's start is kept: the difference starts there
        while len(self.samples) > 2 and self.samples[1][0] <= t - self.span:
            self.samples.popleft()
        while self.canaries and self.canaries[0][0] <= self.samples[0][0]:
            self.canaries.popleft()
        while self.spans and self.spans[0][1] < self.samples[0][0]:
            self.spans.popleft()
        while self.unseparated and self.unseparated[0] <= self.samples[0][0]:
            self.unseparated.popleft()
        return True

    def canary_begins(self, t: float):
        """The cockpit's canary is about to read the counters, then send its request."""
        self.inflight = t

    def canary(self, t: float, key, before: dict | None, after: dict | None):
        """The cockpit's canary ran, between two reads of this engine's counters: the first
        began at canary_begins(), the second ended now, at `t`."""
        began = self.inflight if self.inflight is not None else t
        self.inflight = None
        self.spans.append((began, t))
        if key != self.key:
            return
        if before is not None and after is not None and isolated(delta(before, after)):
            self.canaries.append((t, delta(before, after)))
        else:
            self.unseparated.append(t)

    def stats(self, now: float) -> dict:
        if not self.samples:
            return {"watched_s": 0.0, "requests": None}
        t0, a = self.samples[0]
        t1, b = self.samples[-1]
        d = delta(a, b)
        n_canary = 0
        for tc, dc in self.canaries:
            if t0 < tc <= t1:
                n_canary += 1
                for k in NAMES:
                    d[k] -= dc[k]
        covered = t1 - t0
        out = {
            "watched_s": round(min(self.span, covered), 1),
            "requests": int(round(d["requests"])),
            "prompt_tokens": int(round(d["prompt"])),
            "cached_tokens": int(round(d["cached"])),
            "generated_tokens": int(round(d["generated"])),
            "reuse": d["cached"] / d["prompt"] if d["prompt"] > 0 else None,
            "decode_tps": d["itl_count"] / d["itl_sum"] if d["itl_sum"] > 0 and d["itl_count"] > 0 else None,
            "ttft_s": d["ttft_sum"] / d["ttft_count"] if d["ttft_count"] > 0 else None,
            "throughput_tps": d["generated"] / covered if covered > 0 else None,
            "canaries_out": n_canary,
            # a canary that could not be taken out exactly is counted in this window as a
            # request: said for as long as it lies between the window's two reads
            "approximate": any(t0 < tc <= t1 for tc in self.unseparated),
        }
        return out
