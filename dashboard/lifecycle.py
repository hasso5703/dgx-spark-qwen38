"""Engine lifecycle: pure logic, no I/O.

Turns raw facts (systemd state, container presence, boot-log lines, health)
into ONE explicit state plus progress, ETA and blocked-action reasons.
Every function here is pure so the whole module is unit-testable offline.

States (a strict superset of what the UI shows):
  stopped, failed, starting, loading-weights, loading-draft, allocating-kv,
  capturing-graphs, warming-up, ready, degraded, stopping
Flags: rebuild (PLE table rebuild in progress), overdue (stage took > 2x ETA).
"""
from __future__ import annotations

import re
import statistics

# ── Boot-log stage markers (real lines, see tests/test_lifecycle.py) ─────────
_TS = r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] "
MARKERS = [
    ("init",       re.compile(_TS + r"server_args=ServerArgs\(")),
    ("weights",    re.compile(_TS + r"Load weight begin\.")),
    ("ple",        re.compile(_TS + r"PLE table -> ")),
    ("weights_end", re.compile(_TS + r"Load weight end\. elapsed=([\d.]+) s, type=(\w+)")),
    ("kv",         re.compile(_TS + r"KV Cache is allocated\.")),
    ("graphs",     re.compile(_TS + r"Capture .* CUDA graph begin\.")),
    ("graphs_end", re.compile(_TS + r"Capture .* CUDA graph end\.")),
    ("ready",      re.compile(_TS + r"The server is fired up and ready to roll!")),
]
REBUILD_RE = re.compile(r"rebuilding the PLE table")

# Boot stages in order; parse_boot_log maps marker hits onto these.
STAGES = ("init", "loading-weights", "loading-draft", "allocating-kv",
          "capturing-graphs", "warming-up")


def parse_boot_log(lines: list[str]) -> dict:
    """Reduce a container log tail to {stage, done[], fired_up, weight_loads}.

    weight_loads counts completed 'Load weight end' lines: the first is the
    target model, the second the draft/MTP head. That distinction is what
    separates loading-weights from loading-draft.
    """
    fired = False
    saw_init = False
    weight_begins = 0
    weight_ends = 0
    kv = False
    graphs_begun = False
    graphs_done = False
    ple = False
    for ln in lines:
        for name, rx in MARKERS:
            m = rx.match(ln)
            if not m:
                continue
            if name == "init":
                # A fresh ServerArgs line means a fresh boot: reset everything
                # (docker log tails can span a previous life of the container).
                fired = saw_init = kv = graphs_begun = graphs_done = ple = False
                weight_begins = weight_ends = 0
                saw_init = True
            elif name == "weights":
                weight_begins += 1
            elif name == "weights_end":
                weight_ends += 1
            elif name == "ple":
                ple = True
            elif name == "kv":
                kv = True
            elif name == "graphs":
                graphs_begun = True
            elif name == "graphs_end":
                graphs_done = True
            elif name == "ready":
                fired = True
            break
    if fired:
        stage = "warming-up"          # fired up; health flips it to ready
    elif graphs_begun:
        stage = "capturing-graphs"
    elif kv:
        stage = "allocating-kv"
    elif weight_ends >= 2 or (weight_ends >= 1 and weight_begins >= 2):
        stage = "loading-draft"
    elif weight_begins >= 1:
        stage = "loading-weights"
    elif saw_init:
        stage = "init"
    else:
        stage = None                  # no boot evidence in this tail
    done = []
    if stage in STAGES:            # no marker evidence => nothing is "done"
        for s in STAGES:
            if s == stage:
                break
            done.append(s)
    return {"stage": stage, "done": done, "fired_up": fired,
            "ple_mmap": ple, "weight_ends": weight_ends,
            "graphs_done": graphs_done}


def journal_flags(lines: list[str]) -> dict:
    """Flags derivable only from the unit journal (launcher speaks there)."""
    return {"rebuild": any(REBUILD_RE.search(ln) for ln in lines)}


def derive_state(*, unit_active: str, unit_sub: str, container_running: bool,
                 healthy: bool, boot: dict, rebuild: bool = False) -> dict:
    """The single source of truth the UI renders. Pure function of facts."""
    flags = {"rebuild": bool(rebuild)}
    if unit_active == "failed":
        return {"state": "failed", **flags}
    if unit_active == "deactivating":
        return {"state": "stopping", **flags}
    if unit_active in ("inactive", "dead", "?", ""):
        return {"state": "stopped", **flags}
    # unit is active/activating from here on
    if not container_running:
        return {"state": "starting", **flags}
    if healthy:
        return {"state": "ready", **flags}
    stage = boot.get("stage")
    if boot.get("fired_up"):
        # fired up but health probe failing right now
        return {"state": "degraded", **flags}
    if stage in (None, "init"):
        return {"state": "starting", **flags}
    mapping = {"loading-weights": "loading-weights",
               "loading-draft": "loading-draft",
               "allocating-kv": "allocating-kv",
               "capturing-graphs": "capturing-graphs",
               "warming-up": "warming-up"}
    return {"state": mapping.get(stage, "starting"), **flags}


# States during which an engine occupies (or is about to occupy) the GPU pool.
BUSY_STATES = {"starting", "loading-weights", "loading-draft", "allocating-kv",
               "capturing-graphs", "warming-up", "ready", "degraded",
               "stopping", "wedged"}
TRANSITIONAL = BUSY_STATES - {"ready", "degraded"}
ENGINE_UNITS = ("qwen38-sglang.service", "qwen38-flash.service")


def blocked_reasons(action: str, params: dict, states: dict) -> list[str]:
    """Why an action must NOT run now. Empty list = allowed.

    states: {unit_name: state_string} for the two engine units.
    The cardinal rule on 128 GB unified memory: never two engines at once.
    """
    reasons = []
    def st(u):
        return states.get(u, "stopped")
    if action == "unit":
        unit, verb = params.get("unit", ""), params.get("verb", "")
        if unit in ENGINE_UNITS and verb in ("start", "restart"):
            other = [u for u in ENGINE_UNITS if u != unit][0]
            if st(other) in BUSY_STATES:
                reasons.append(
                    f"{other} is {st(other)}: two engines never run at once "
                    f"on unified memory (stop it first)")
        if unit in ENGINE_UNITS and verb in ("stop", "restart") \
                and st(unit) in TRANSITIONAL:
            # allowed, but the caller should surface warn_reasons instead
            pass
    elif action in ("switch", "update_stack"):
        for u in ENGINE_UNITS:
            if st(u) in TRANSITIONAL:
                reasons.append(f"{u} is {st(u)}: wait for it to settle")
    return reasons


def warn_reasons(action: str, params: dict, states: dict,
                 lane_of_unit: dict | None = None) -> list[str]:
    """Truthful warnings for allowed-but-consequential actions."""
    warns = []
    if action == "unit" and params.get("verb") in ("stop", "restart"):
        unit = params.get("unit", "")
        state = states.get(unit, "stopped")
        if unit == "qwen38-flash.service" and state in TRANSITIONAL:
            warns.append("stopping the flash lane mid-boot marks the PLE "
                         "table dirty: the NEXT boot rebuilds it (~12 min)")
        elif state == "ready":
            warns.append("clients on :30001 will get errors until an engine "
                         "is back")
    return warns


# ── Stage timing: history + ETA (median of real observed durations) ─────────
def eta_for(history: dict, unit: str, rebuild: bool) -> float | None:
    """Median full-boot duration for this unit (seconds), rebuild-aware."""
    key = f"{unit}:rebuild" if rebuild else unit
    vals = [v for v in history.get(key, []) if isinstance(v, (int, float))]
    if not vals:
        vals = [v for v in history.get(unit, [])
                if isinstance(v, (int, float))]
    return statistics.median(vals) if vals else None


def record_boot(history: dict, unit: str, seconds: float,
                rebuild: bool, keep: int = 12) -> dict:
    key = f"{unit}:rebuild" if rebuild else unit
    lst = list(history.get(key, []))
    lst.append(round(float(seconds), 1))
    history[key] = lst[-keep:]
    return history


# ── Wedge detection: health says yes, generation says no ────────────────────
def decide_wedge(*, health_ok: bool, canary_fails: int, num_reqs: int,
                 progress_age: float | None, threshold: int = 3,
                 stall_after: float = 300.0) -> bool:
    """True when the frontend answers but nothing is being generated.

    Two routes, never mixed:
    - idle route: no request running, yet threshold consecutive generation
      probes failed (field case 29/08: scheduler spinning, /get_load 0 requests);
    - busy route: requests running but no prefill/decode progress line for
      stall_after seconds. Probes are never sent while requests run (they
      would just queue behind a legitimate long generation), so a slow user
      request can never be mistaken for a wedge.
    """
    if not health_ok:
        return False
    if num_reqs == 0:
        return canary_fails >= threshold
    return progress_age is not None and progress_age > stall_after


def wedge_plan(*, decided: bool, prev_state: str | None, wedged_since: float | None,
               now: float, grace: float, autoheal: bool, cooldown_ok: bool,
               job_running: bool) -> dict:
    """What to do this tick about a wedge decision. Pure, so it is testable:
    the 29/08 regression had the forensics and the restart in the wrong branch.

    Returns {"state": "wedged"|None, "first": bool, "since": float|None, "restart": bool}
    """
    if not decided:
        return {"state": None, "first": False, "since": None, "restart": False}
    since = wedged_since if wedged_since is not None else now
    first = prev_state != "wedged"
    restart = bool(autoheal and cooldown_ok and (now - since) >= grace and not job_running)
    return {"state": "wedged", "first": first, "since": since, "restart": restart}


def decide_mem_floor(*, avail_gib: float | None, floor_gib: float, num_reqs: int,
                     last_abort_ts: float | None, now: float,
                     cooldown_s: float = 60.0) -> tuple[bool, str]:
    """Memory-floor belt: abort every in-flight generation when host MemAvailable
    drops under the floor while requests run (unified memory: a prefill's
    activations can take the box to the livelock edge, measured 29/08 at 0.8 GiB
    after a 177k prompt). Pure decision: (abort?, reason). Never aborts twice
    within the cooldown, never when nothing runs, never on a missing reading."""
    if avail_gib is None:
        return False, "no reading"
    if avail_gib >= floor_gib:
        return False, "above floor"
    if num_reqs <= 0:
        return False, f"below floor ({avail_gib:.1f} GiB) but nothing running"
    if last_abort_ts is not None and now - last_abort_ts < cooldown_s:
        return False, "cooldown"
    return True, f"MemAvailable {avail_gib:.1f} GiB under the {floor_gib:.1f} GiB floor with {num_reqs} running"
