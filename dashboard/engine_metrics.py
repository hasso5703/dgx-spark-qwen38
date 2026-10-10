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

The prefill numbers come from the engine's own counters too: `realtime_tokens_total` with
`mode="prefill_compute"` counts only prompt tokens it actually computed (cache-served ones
are another mode), and `per_stage_req_latency_seconds` with `stage="prefill_forward"` is
the engine's stopwatch of a request's whole prefill, from its first forward to the last
chunk, so a difference of two reads holds no idle time either. The `chunked_prefill` stage
times the same chunks from the same start, inside `prefill_forward`, so it is not summed
in: on a 27B lane an 80,400-token prompt in ten 8,192-token chunks ran 61.8 s of
prefill_forward, 1,301 tok/s, which counting the chunks as well would have halved to 651.
Measured by this view's author (2026-10-08, a flash lane): a cold 1,908-token prompt took
0.955 s of the stopwatch, 1,998 tok/s, the scale the benchmarks table's ~2,250 prefill says for a
saturated lane. `queue_time_seconds` is the waiting before the engine starts a request,
`scheduler_idle_seconds_total` the time it had nothing runnable, and
`num_aborted_requests_total` the aborts it accepted (the proxy's abandonment, landed).
The idle share is only shown when both reads of a window carry a data line for
`scheduler_idle_seconds_total`: some SGLang builds do not publish it (a registered
counter still prints its HELP line), and a missing counter would read as a fully busy
engine.
Of the drafter: `spec_verify_calls_total` counts a request's verifications when it
finishes, and the window's generated tokens over its difference is that window's accept
length, what one step netted (its one token plus the drafts it accepted). Measured by this
view's author (2026-10-08): a 700-token request spent 194 verifications, 3.61 a step at
4 drafts, while the `spec_accept_length` gauge sat at 2.7, an average since the boot, and
the scheduler prints its own only per a log window of its choice.
The levels (`full_token_usage`, `num_running_reqs`, `num_queue_reqs`) are not counters: no
delta is possible, so the window keeps its own peaks, honest lower bounds of what peaked.

Two things are not the clients': the cockpit's own canary, a real two-token chat request
every 90 s while nothing else runs, which SGLang counts like any other, and the /health
probe, which it does not count (log_metrics=False). The canary's request counters are taken
out exactly: the cockpit reads them just before and just after it, and the difference is
the canary's own as long as nothing else moved meanwhile (the idle wall clock is the
engine's, not the canary's, so it is not subtracted); when something did move, the window
says its numbers are
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
    "requests": ("sglang:num_requests_total", None),
    "prompt": ("sglang:prompt_tokens_total", None),
    "cached": ("sglang:cached_tokens_total", None),
    "generated": ("sglang:generation_tokens_total", None),
    "ttft_sum": ("sglang:time_to_first_token_seconds_sum", None),
    "ttft_count": ("sglang:time_to_first_token_seconds_count", None),
    "itl_sum": ("sglang:inter_token_latency_seconds_sum", None),
    # the drafter's verifications: generated tokens over them is the accept length
    "ver": ("sglang:spec_verify_calls_total", None),
    "itl_count": ("sglang:inter_token_latency_seconds_count", None),
    # what the engine computed (not read from its cache) across every prompt, and the
    # scheduler's own stopwatch of prefill: per-request stage times, so prefill speed holds
    # no idle time and no cache-served token inflates it
    "pc": ("sglang:realtime_tokens_total", 'mode="prefill_compute"'),
    "fwd": ("sglang:per_stage_req_latency_seconds_sum", 'stage="prefill_forward"'),
    "q_sum": ("sglang:queue_time_seconds_sum", None),
    "q_count": ("sglang:queue_time_seconds_count", None),
    "idle": ("sglang:scheduler_idle_seconds_total", None),
    "aborted": ("sglang:num_aborted_requests_total", None),
}
# instantaneous levels, not counters: no delta, the window's own peaks of them
GAUGES = {
    "running": ("sglang:num_running_reqs", None),
    "queued": ("sglang:num_queue_reqs", None),
    "pool": ("sglang:full_token_usage", None),
}
# each label set is one scheduler: within a DP group the ranks are state-synchronous and
# only the group leader logs stats, so the sets on a page are independent schedulers and
# the request counts add; the pool's fullness is a fraction of one pool, and a fleet's
# pool is only as full as its fullest
_MAX_LEVELS = {"pool"}


def _by(spec):
    out = {}
    for key, (name, label) in spec.items():
        out.setdefault(name, []).append((key, label))
    return out


_BY_NAME = _by(NAMES)
_GA_BY_NAME = _by(GAUGES)
_IDLE_LINE = re.compile(
    r'(?m)^sglang:scheduler_idle_seconds_total(?:\{[^}\n]*\})?[ \t]+(\S+)(?:[ \t]+\S+)?$')
_SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^\n]*\})?[ \t]+(\S+)(?:[ \t]+\S+)?$')


def _idle_seen(text: str) -> bool:
    """Whether a data line of the idle counter carries a value the reader accepts: a
    counter that was registered but never observed prints only its HELP and TYPE lines,
    and a NaN is not a value; neither may read as an engine that is fully busy."""
    for m in _IDLE_LINE.finditer(text):
        try:
            if math.isfinite(float(m.group(1))):
                return True
        except ValueError:
            pass
    return False


def _read(text: str, spec_by_name, keys, max_keys=()):
    """Sum the listed families over their label sets (a model, a cache source, streaming
    or not), taking only the label sets whose filter appears in the line (the modes of the
    realtime tokens, the stages of the per-request stopwatch). Keys in `max_keys` keep
    their highest value across label sets instead of adding: the pool's fullness is a
    fraction, and a fleet's pool is only as full as its fullest rank. A name not on the
    page yet maps to None: prometheus_client writes a labelled series only once it was
    first observed, and an engine just ready lists no cached tokens and no inter-token
    time (its own warm-up request had neither). Zero-filling of the absent names is tied
    to the engine's counter names: a caller that passes any other spec keeps its Nones
    and must not difference them. None when the page has no SGLang metric at all: not an
    SGLang engine's /metrics."""
    out = dict.fromkeys(keys, None)
    sglang = False
    for line in text.splitlines():
        if "sglang:" in line:
            sglang = True
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE.match(line.strip())
        cands = spec_by_name.get(m.group(1)) if m else None
        if not cands:
            continue
        try:
            v = float(m.group(3))
        except ValueError:
            continue
        if not math.isfinite(v):
            continue
        labels = m.group(2) or ""
        for key, label in cands:
            if label is None or label in labels:
                if out[key] is None:
                    out[key] = v
                elif key in max_keys:
                    out[key] = max(out[key], v)
                else:
                    out[key] += v
    if not sglang:
        return None
    return {k: (0.0 if v is None else v) if spec_by_name is _BY_NAME else v
            for k, v in out.items()}


def parse(text: str) -> dict | None:
    """The counters of a /metrics page, each summed as `_read` says; a counter not on the
    page yet is zero, so a difference of two reads is the window's own. `idle_seen` says
    whether a data line of the idle counter carried a value the reader accepts: some
    SGLang builds never publish it, and an absent counter must not read as a busy engine.
    Its HELP and TYPE lines, and a NaN line, are not values."""
    out = _read(text, _BY_NAME, NAMES)
    if out is not None:
        out["idle_seen"] = _idle_seen(text)
    return out


def parse_gauges(text: str) -> dict | None:
    """The instantaneous levels of a /metrics page, or None when a name is not on it: a
    peak is only claimed for values the cockpit actually read. The pool keeps its fullest
    rank; the request counts add across ranks."""
    return _read(text, _GA_BY_NAME, GAUGES, _MAX_LEVELS)


def delta(a: dict, b: dict) -> dict:
    return {k: b[k] - a[k] for k in NAMES}


def isolated(d: dict) -> bool:
    """A difference that holds the canary and nothing else: one finished request, one first
    token, and at most the one inter-token interval of its two tokens. The drafter's
    verification count moves only when a request finishes, where the first two already
    moved, so it adds nothing to this gate."""
    return d["requests"] == 1 and d["ttft_count"] == 1 and d["itl_count"] <= 1


class Window:
    """The last `span` seconds of one engine's counters, read every few seconds."""

    def __init__(self, span: float = 300.0):
        self.span = span
        self.key = None
        self.samples: deque = deque()     # (t, values, gauges) of each kept read
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

    def add(self, t: float, key, values: dict, started: float | None = None,
            gauges: dict | None = None) -> bool:
        """One read of the counters of the engine `key` (its unit and its start), which began
        at `started` and ended at `t`, with the instantaneous levels it showed alongside (the
        window keeps their peaks). False when it is dropped: it ran across a canary."""
        started = t if started is None else started
        # a canary that began over a minute ago is over, whatever became of its end (its
        # request gives up at 25 s): never let one lost end blind the window
        if self.inflight is not None and t - self.inflight < 60:
            return False
        if any(started <= ended and t >= began for began, ended in self.spans):
            return False
        if key != self.key or (self.samples and any(values[k] < self.samples[-1][1][k] for k in NAMES)):
            self.reset(key, t)
        self.samples.append((t, values, gauges))
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
        t0, a, _ = self.samples[0]
        t1, b, _ = self.samples[-1]
        d = delta(a, b)
        n_canary = 0
        for tc, dc in self.canaries:
            if t0 < tc <= t1:
                n_canary += 1
                for k in NAMES:
                    # the idle wall clock is the engine's, not the canary's: the idle it
                    # kept while the canary ran was real idle time, and stays in the share
                    if k != "idle":
                        d[k] -= dc[k]
        covered = t1 - t0
        gs = [g for _, _, g in self.samples if g]

        def peak(name, whole=False):
            vs = [g[name] for g in gs if g[name] is not None]
            if not vs:
                return None
            return int(round(max(vs))) if whole else max(vs)

        out = {
            "watched_s": round(min(self.span, covered), 1),
            "requests": int(round(d["requests"])),
            "prompt_tokens": int(round(d["prompt"])),
            "cached_tokens": int(round(d["cached"])),
            "generated_tokens": int(round(d["generated"])),
            "reuse": d["cached"] / d["prompt"] if d["prompt"] > 0 else None,
            # computed prompt tokens over the engine's own prefill stopwatch: cache-served
            # tokens and idle time are both out of it, the same way the decode speed is.
            # The stopwatch's prefill_forward stage spans a chunked prompt's whole prefill,
            # so the chunked_prefill stage inside it never counts again
            "prefill_tps": d["pc"] / d["fwd"] if d["pc"] > 0 and d["fwd"] > 0 else None,
            "queue_s": d["q_sum"] / d["q_count"] if d["q_count"] > 0 else None,
            "ttft_s": d["ttft_sum"] / d["ttft_count"] if d["ttft_count"] > 0 else None,
            "decode_tps": d["itl_count"] / d["itl_sum"] if d["itl_sum"] > 0 and d["itl_count"] > 0 else None,
            # finished tokens over the drafter's verifications: what a step netted, its own
            # count bearing no idle time; a lane without a drafter calls verify never
            "acc_len": d["generated"] / d["ver"] if d["ver"] > 0 else None,
            "throughput_tps": d["generated"] / covered if covered > 0 else None,
            # the window's idle share of its span, only when both ends saw the idle
            # counter: an engine that does not publish it must not read as fully busy
            "idle_share": min(1.0, d["idle"] / covered)
            if covered > 0 and a.get("idle_seen") and b.get("idle_seen") else None,
            "aborted": int(round(d["aborted"])),
            "pool_max": peak("pool"),
            "running_max": peak("running", whole=True),
            "queued_max": peak("queued", whole=True),
            "canaries_out": n_canary,
            # a canary that could not be taken out exactly is counted in this window as a
            # request: said for as long as it lies between the window's two reads
            "approximate": any(t0 < tc <= t1 for tc in self.unseparated),
        }
        return out
