#!/usr/bin/env python3
"""Spark Cockpit: the repo's web dashboard (BETA, branch webapp).

Single-file stdlib backend: no pip, no venv, nothing to break.
Read-only phase: collectors + SSE stream + static UI. The action registry
exists but every mutating action returns 501 until the actions phase lands.

Run (dev):  python3 dashboard/cockpit.py
Then open http://127.0.0.1:30090 and paste the API key from
~/.config/qwen38/api-key at the login prompt.
"""
from __future__ import annotations

import hashlib
import hmac
import http.cookies
import http.server
import json
import os
import re
import secrets
import shutil
import socketserver
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from collections import deque

import lifecycle as lc
import registry as rg
import recipes as rp

# ── Configuration (env-overridable, safe defaults) ──────────────────────────
HERE = Path(__file__).resolve().parent
REPO_DIR = Path(os.environ.get("COCKPIT_REPO_DIR", HERE.parent))
CONFIG_DIR = Path(os.environ.get("COCKPIT_CONFIG_DIR", Path.home() / ".config/qwen38"))
BIND = os.environ.get("COCKPIT_BIND", "127.0.0.1")
PORT = int(os.environ.get("COCKPIT_PORT", "30090"))
ENGINE_BASE = os.environ.get("COCKPIT_ENGINE", "http://127.0.0.1:30000")
PROXY_BASE = os.environ.get("COCKPIT_PROXY", "http://127.0.0.1:30001")
STATIC_DIR = HERE / "static"
VERSION = "0.1.0-beta"

# Fields from get_server_info that must never reach a browser.
MASKED_FIELDS = {"api_key", "admin_api_key"}

UNITS = ("qwen38-sglang.service", "qwen38-flash.service", "qwen38-keepalive.service")
CONTAINERS = ("qwen38-sglang", "qwen38-flash")

UNIT2CONT = {"qwen38-sglang.service": "qwen38-sglang",
             "qwen38-flash.service": "qwen38-flash"}
HISTORY_FILE_NAME = "cockpit-history.json"

EVENTS: deque = deque(maxlen=200)
EVENTS_LOCK = threading.Lock()
LIFE: dict = {"states": {}, "enter": {}, "witnessed": {}}
LIFE_LOCK = threading.Lock()


EVENTS_FILE_NAME = "cockpit-events.jsonl"


def add_event(kind: str, msg: str):
    ev = {"ts": time.time(), "kind": kind, "msg": msg[:300]}
    with EVENTS_LOCK:
        EVENTS.append(ev)
    try:  # the timeline survives cockpit restarts (append-only, bounded read)
        with open(CONFIG_DIR / EVENTS_FILE_NAME, "a") as f:
            f.write(json.dumps(ev) + "\n")
    except OSError:
        pass


def load_events():
    try:
        lines = (CONFIG_DIR / EVENTS_FILE_NAME).read_text().splitlines()[-100:]
    except OSError:
        return
    with EVENTS_LOCK:
        for ln in lines:
            try:
                EVENTS.append(json.loads(ln))
            except json.JSONDecodeError:
                pass


def load_history() -> dict:
    try:
        return json.loads((CONFIG_DIR / HISTORY_FILE_NAME).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_history(h: dict):
    try:
        tmp = CONFIG_DIR / (HISTORY_FILE_NAME + ".tmp")
        tmp.write_text(json.dumps(h))
        tmp.replace(CONFIG_DIR / HISTORY_FILE_NAME)
    except OSError:
        pass


def api_key() -> str:
    try:
        return (CONFIG_DIR / "api-key").read_text().strip()
    except OSError:
        return ""


def run(argv: list[str], timeout: float = 5.0, merge_err: bool = False) -> str:
    """Fixed-argv runner: never a shell, never client input.

    merge_err folds stderr into the result: docker logs streams the
    container's stderr (where SGLang actually speaks) to its own stderr,
    so every docker-logs reader MUST pass merge_err=True (field bug: the
    stage parser and decode telemetry read empty stdout for a night).
    """
    try:
        if merge_err:
            out = subprocess.run(argv, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True,
                                 timeout=timeout)
        else:
            out = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=timeout)
        return out.stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def http_json(url: str, timeout: float = 4.0):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key()}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ── Collectors ───────────────────────────────────────────────────────────────
# Each returns a plain dict and NEVER raises: failures become {"error": ...}
# so one broken source never darkens the rest of the cockpit.

def guard(fn):
    def inner(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001 (isolation by design)
            return {"error": f"{type(e).__name__}: {e}"}
    return inner


class CpuTracker:
    def __init__(self):
        self.prev: dict[str, tuple[int, int]] = {}

    def sample(self):
        rows = {}
        for line in Path("/proc/stat").read_text().splitlines():
            if not line.startswith("cpu"):
                break
            parts = line.split()
            name = parts[0]
            vals = [int(x) for x in parts[1:]]
            idle = vals[3] + vals[4]
            total = sum(vals)
            p_total, p_idle = self.prev.get(name, (total, idle))
            dt, di = total - p_total, idle - p_idle
            rows[name] = round(100.0 * (dt - di) / dt, 1) if dt > 0 else 0.0
            self.prev[name] = (total, idle)
        return rows


CPU = CpuTracker()


@guard
def collect_machine():
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree", "Cached", "Dirty"):
            mem[k] = int(v.strip().split()[0]) * 1024
    load1, load5, load15 = os.getloadavg()
    disks = {}
    for label, path in (("home", str(Path.home())), ("docker", "/var/lib/docker")):
        try:
            st = os.statvfs(path)
            disks[label] = {"total": st.f_frsize * st.f_blocks,
                            "free": st.f_frsize * st.f_bavail}
        except OSError:
            pass
    return {"node_id": "local", "mem": mem, "cpu_pct": CPU.sample(),
            "load": [load1, load5, load15], "disks": disks, "ts": time.time()}


@guard
def collect_gpu():
    # GB10 traps (measured): memory.used/total report [N/A]; utilization.gpu can
    # freeze stale. Power draw, temperature and compute-apps are trustworthy.
    q = run(["nvidia-smi", "--query-gpu=power.draw,temperature.gpu",
             "--format=csv,noheader,nounits"]).strip()
    power, temp = (q.split(", ") + ["", ""])[:2] if q else ("", "")
    procs = []
    for line in run(["nvidia-smi", "--query-compute-apps=pid,used_memory",
                     "--format=csv,noheader"]).splitlines():
        pid, memtxt = [x.strip() for x in line.split(",")][:2]
        name = ""
        try:
            name = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            pass
        procs.append({"pid": int(pid), "mem": memtxt, "name": name})
    return {"node_id": "local", "power_w": float(power) if power else None,
            "temp_c": float(temp) if temp else None, "procs": procs}


@guard
def collect_units():
    out = {}
    for u in UNITS:
        raw = run(["systemctl", "show", u, "-p",
                   "ActiveState,SubState,UnitFileState,ExecMainStartTimestamp"])
        d = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        out[u] = {"active": d.get("ActiveState", "?"),
                  "sub": d.get("SubState", "?"),
                  "enabled": d.get("UnitFileState", "?"),
                  "since": d.get("ExecMainStartTimestamp", "")}
    return {"node_id": "local", "units": out}


@guard
def collect_containers():
    # docker stats errors out wholesale if ANY named container is absent, so
    # ask for everything and filter (first real bug the cockpit caught itself).
    rows = {}
    fmt = "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"
    for line in run(["docker", "stats", "--no-stream", "--format", fmt],
                    timeout=8).splitlines():
        name, cpu, memu = (line.split("|") + ["", ""])[:3]
        if name in CONTAINERS:
            rows[name] = {"cpu": cpu, "mem": memu}
    return {"node_id": "local", "containers": rows}


@guard
def collect_engine_fast():
    load = http_json(ENGINE_BASE + "/get_load", timeout=3)
    healthy = True
    try:
        urllib.request.urlopen(ENGINE_BASE + "/health", timeout=2).read()
    except Exception:  # noqa: BLE001
        healthy = False
    return {"node_id": "local", "load": load, "healthy": healthy}


@guard
def collect_engine_info():
    info = http_json(ENGINE_BASE + "/get_server_info", timeout=6)
    kept = ("model_path", "served_model_name", "revision", "quantization",
            "context_length", "mem_fraction_static", "max_running_requests",
            "chunked_prefill_size", "speculative_algorithm",
            "speculative_num_steps", "speculative_num_draft_tokens",
            "attention_backend", "decode_attention_backend",
            "prefill_attention_backend", "mamba_radix_cache_strategy",
            "ple_offload_embedding", "version", "max_total_num_tokens",
            "kv_cache_dtype", "port")
    slim = {k: info.get(k) for k in kept}
    for f in MASKED_FIELDS:
        slim.pop(f, None)
    return {"node_id": "local", "info": slim}


CANARY: dict = {"fails": 0, "last_ok": None, "last_err": "", "latency": None}
AUTOHEAL = os.environ.get("COCKPIT_AUTOHEAL", "1") == "1"
AUTOHEAL_COOLDOWN = 1800.0
AUTOHEAL_GRACE = float(os.environ.get("COCKPIT_AUTOHEAL_GRACE", "0"))   # seconds to leave a wedged engine up for forensics
WEDGED_SINCE: dict = {}
READY_SINCE: dict = {}          # per unit: when this activation first reached ready
WEDGE_MIN_READY_S = float(os.environ.get("COCKPIT_WEDGE_MIN_READY_S", "180"))
LAST_HEAL: dict = {"ts": 0.0}
LAST_PROGRESS: dict = {"ts": None}
UNHEALTHY_TICKS: dict = {}     # per unit: consecutive ticks with health down
POOL_GUARD = os.environ.get("COCKPIT_POOL_GUARD", "1") == "1"
POOL_GUARD_THRESHOLD = float(os.environ.get("COCKPIT_POOL_GUARD_THRESHOLD", "0.6"))
LAST_USAGE: dict = {"value": 0.0, "mamba": 0.0, "ts": 0.0}   # pool usage from the engine's own log lines
MAMBA_GUARD_THRESHOLD = float(os.environ.get("COCKPIT_MAMBA_GUARD_THRESHOLD", "0.5"))
IDLE_SINCE: dict = {"ts": None}
LAST_FLUSH: dict = {"ts": 0.0}
PROGRESS_RE = re.compile(r"(Prefill|Decode) batch")
USAGE_RE = re.compile(r"token usage: ([\d.]+)")
MAMBA_RE = re.compile(r"mamba usage: ([\d.]+)")


@guard
def collect_canary():
    """A real 2-token generation, only when an engine claims ready and no job
    runs: the only probe that tells a wedged scheduler from a healthy one."""
    with LIFE_LOCK:
        states = dict(LIFE.get("states", {}))
    ready = [u for u in lc.ENGINE_UNITS if states.get(u) in ("ready", "wedged")]
    with STATE_LOCK:
        load = ((STATE.get("engine_fast") or {}).get("data", {}).get("load") or [{}])[0]
    busy = int(load.get("num_reqs") or 0) + int(load.get("num_waiting_reqs") or 0) > 0
    # a request seen in the last 60 s means a client is active: stay out of its way
    recent = LAST_PROGRESS["ts"] and time.time() - LAST_PROGRESS["ts"] < 60
    if not ready or JOB_LOCK.locked() or busy or recent:
        # never queue a probe behind a user's request (max-running-requests 1)
        return {"node_id": "local", **CANARY, "skipped": True}
    body = json.dumps({"model": "canary", "max_tokens": 2, "temperature": 0,
                       "messages": [{"role": "user", "content": "Say OK"}],
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(ENGINE_BASE + "/v1/chat/completions", body,
                                 {"Content-Type": "application/json",
                                  "Authorization": f"Bearer {api_key()}"})
    t0 = time.time()
    try:
        urllib.request.urlopen(req, timeout=25).read()
        CANARY.update(fails=0, last_ok=time.time(), last_err="",
                      latency=round(time.time() - t0, 2))
    except Exception as e:  # noqa: BLE001
        CANARY["fails"] += 1
        CANARY["last_err"] = f"{type(e).__name__}: {str(e)[:80]}"
        if CANARY["fails"] == 1:
            add_event("canary", f"generation probe failed: {CANARY['last_err']}")
    return {"node_id": "local", **CANARY, "skipped": False}


DECODE_RE = re.compile(
    r"#running-req: (\d+).*?token usage: ([\d.]+).*?accept len: ([\d.]+)")


@guard
def collect_decode_telemetry():
    """Parse the newest scheduler lines from the serving container's log."""
    active = None
    for c in CONTAINERS:
        if run(["docker", "ps", "-q", "-f", f"name=^{c}$"]).strip():
            active = c
            break
    if not active:
        return {"node_id": "local", "lane": None}
    tail = run(["docker", "logs", "--since", "30s", active], timeout=6,
               merge_err=True)[-8000:]
    last = None
    for line in tail.splitlines():
        if PROGRESS_RE.search(line):
            LAST_PROGRESS["ts"] = time.time()
        um = USAGE_RE.search(line)
        if um:
            LAST_USAGE.update(value=float(um.group(1)), ts=time.time())
        mm = MAMBA_RE.search(line)
        if mm:
            LAST_USAGE["mamba"] = float(mm.group(1))
        m = DECODE_RE.search(line)
        if m:
            last = {"running": int(m.group(1)),
                    "token_usage": float(m.group(2)),
                    "accept_len": float(m.group(3))}
    return {"node_id": "local", "lane": active, "decode": last,
            "usage": {"tokens": LAST_USAGE["value"], "mamba": LAST_USAGE["mamba"],
                      "age": round(time.time() - LAST_USAGE["ts"], 1) if LAST_USAGE["ts"] else None}}


FEED_START = re.compile(r"\[proxy\] (\S+) -> (POST|GET) (\S+) body=(\d+)b")
FEED_END = re.compile(r"\[proxy\] (\S+) (POST|GET) (\S+) (.+?) in ([\d.]+)s")


@guard
def collect_feed():
    """Last requests seen by the keepalive proxy: client, path, size, outcome."""
    raw = run(["journalctl", "-u", "qwen38-keepalive.service", "-n", "160",
               "--no-pager", "-o", "short-iso"], timeout=6)
    reqs: dict[str, dict] = {}
    order: list[str] = []
    for ln in raw.splitlines():
        ts = ln[:19]
        m = FEED_START.search(ln)
        if m:
            peer = m.group(1)
            reqs[peer] = {"ts": ts, "peer": peer, "path": m.group(3),
                          "bytes": int(m.group(4)), "outcome": "in flight",
                          "secs": None}
            order.append(peer)
            continue
        m = FEED_END.search(ln)
        if m and m.group(1) in reqs:
            r = reqs[m.group(1)]
            r["outcome"] = m.group(4)[:40]
            r["secs"] = float(m.group(5))
    rows = [reqs[p] for p in order[-25:]]
    return {"node_id": "local", "rows": rows}


@guard
def collect_repo():
    def g(*args):
        return run(["git", "-C", str(REPO_DIR), *args]).strip()
    return {"node_id": "local",
            "head": g("log", "-1", "--format=%h %s"),
            "branch": g("branch", "--show-current"),
            "tag": g("describe", "--tags", "--abbrev=0"),
            "dirty": bool(g("status", "--porcelain"))}


def monotonic_now() -> float:
    return float(Path("/proc/uptime").read_text().split()[0])


@guard
def collect_lifecycle():
    """Explicit per-engine state + progress + ETA + action gates (2s tier)."""
    with STATE_LOCK:
        healthy = bool((STATE.get("engine_fast") or {})
                       .get("data", {}).get("healthy"))
    history = load_history()
    engines = {}
    prev = dict(LIFE.get("states", {}))
    states = {}
    for unit in lc.ENGINE_UNITS + ("qwen38-keepalive.service",):
        raw = run(["systemctl", "show", unit, "-p",
                   "ActiveState,SubState,ActiveEnterTimestampMonotonic"])
        d = dict(ln.split("=", 1) for ln in raw.splitlines() if "=" in ln)
        active = d.get("ActiveState", "?")
        if unit not in lc.ENGINE_UNITS:
            states[unit] = "ready" if active == "active" else "stopped"
            continue
        cont = UNIT2CONT[unit]
        running = bool(run(["docker", "ps", "-q", "-f",
                            f"name=^{cont}$"]).strip())
        boot = {"stage": None, "fired_up": False, "done": []}
        rebuild = False
        if running and not healthy:
            # A mature server's tail is pure decode noise: only read logs
            # while health is down (boot or trouble), where markers live.
            tail = run(["docker", "logs", "--tail", "300", cont],
                       timeout=6, merge_err=True).splitlines()
            boot = lc.parse_boot_log(tail)
        # Hysteresis: a 2 s health probe times out under a heavy prefill.
        # Leaving ready needs 3 consecutive misses AND no fresh progress line;
        # a single 200 restores it at once.
        if healthy:
            UNHEALTHY_TICKS[unit] = 0
        else:
            UNHEALTHY_TICKS[unit] = UNHEALTHY_TICKS.get(unit, 0) + 1
        progressing = LAST_PROGRESS["ts"] and time.time() - LAST_PROGRESS["ts"] < 30
        sticky_ready = (prev.get(unit) in ("ready", "wedged") and running and not healthy
                        and (UNHEALTHY_TICKS[unit] < 3 or progressing))
        st = lc.derive_state(unit_active=active, unit_sub=d.get("SubState", "?"),
                             container_running=running,
                             healthy=(healthy or sticky_ready) and running, boot=boot,
                             rebuild=False)
        # degraded means "WAS serving, lost health", not "health probe has
        # not caught up yet": right after fired-up, stay warming-up unless
        # we had already reached ready in this activation.
        if st["state"] == "degraded" and prev.get(unit) not in ("ready",
                                                                "degraded"):
            st["state"] = "warming-up"
        if st["state"] == "ready":
            with STATE_LOCK:
                load = ((STATE.get("engine_fast") or {}).get("data", {})
                        .get("load") or [{}])[0]
            num_reqs = int(load.get("num_reqs") or 0)
            # Pool guard (field cases 29/08, twice): the scheduler hangs when it
            # must evict cached prefixes to make room (pool at 0.89 and 0.98).
            # When the engine goes idle with the pool still mostly held by
            # cache, flush it so the next giant prompt never needs eviction.
            waiting = int(load.get("num_waiting_reqs") or 0)
            if num_reqs + waiting == 0:
                IDLE_SINCE["ts"] = IDLE_SINCE["ts"] or time.time()
            else:
                IDLE_SINCE["ts"] = None
            if (POOL_GUARD and IDLE_SINCE["ts"] and time.time() - IDLE_SINCE["ts"] > 3
                    and (LAST_USAGE["value"] > POOL_GUARD_THRESHOLD
                         or LAST_USAGE["mamba"] >= MAMBA_GUARD_THRESHOLD)
                    and time.time() - LAST_FLUSH["ts"] > 30):
                try:
                    req = urllib.request.Request(ENGINE_BASE + "/flush_cache", method="POST",
                                                 data=b"", headers={"Authorization": f"Bearer {api_key()}"})
                    urllib.request.urlopen(req, timeout=8).read()
                    add_event("guard", f"pool guard: cache flushed (tokens {LAST_USAGE['value']:.0%}, "
                                       f"mamba slots {LAST_USAGE['mamba']:.0%}), engine idle")
                    audit({"kind": "pool_guard", "usage": LAST_USAGE["value"], "mamba": LAST_USAGE["mamba"]})
                except urllib.error.HTTPError as e:
                    # 400 = "pending requests": the engine is not idle after all
                    # (a queued request the load endpoint does not show); stand down.
                    if e.code != 400:
                        add_event("guard", f"pool guard flush failed: HTTP {e.code}")
                    IDLE_SINCE["ts"] = None
                except Exception as e:  # noqa: BLE001
                    add_event("guard", f"pool guard flush failed: {str(e)[:80]}")
                LAST_USAGE["value"] = 0.0
                LAST_USAGE["mamba"] = 0.0
                LAST_FLUSH["ts"] = time.time()
            age = (time.time() - LAST_PROGRESS["ts"]) if LAST_PROGRESS["ts"] else None
            READY_SINCE.setdefault(unit, time.time())
            settled = time.time() - READY_SINCE[unit] >= WEDGE_MIN_READY_S
            decided = settled and lc.decide_wedge(health_ok=True, canary_fails=CANARY["fails"],
                                                  num_reqs=num_reqs, progress_age=age)
            plan = lc.wedge_plan(decided=decided, prev_state=prev.get(unit),
                                 wedged_since=WEDGED_SINCE.get(unit), now=time.time(),
                                 grace=AUTOHEAL_GRACE, autoheal=AUTOHEAL,
                                 cooldown_ok=time.time() - LAST_HEAL["ts"] > AUTOHEAL_COOLDOWN,
                                 job_running=JOB_LOCK.locked())
            if plan["state"] == "wedged":
                st["state"] = "wedged"
                WEDGED_SINCE[unit] = plan["since"]
                if plan["first"]:
                    audit({"kind": "wedge", "unit": unit, "canary_fails": CANARY["fails"],
                           "num_reqs": num_reqs, "progress_age": age})
                    # forensics before any restart: the scheduler's Python stacks
                    dump = run(["sudo", "-n", "/usr/local/bin/qwen38-pyspy-scheduler"],
                               timeout=40, merge_err=True)
                    if dump.strip():
                        f = CONFIG_DIR / f"wedge-{time.strftime('%Y%m%d-%H%M%S')}.txt"
                        try:
                            f.write_text(dump)
                            add_event("forensics", f"scheduler stacks saved: {f.name}")
                        except OSError:
                            pass
                if plan["restart"]:
                    LAST_HEAL["ts"] = time.time()
                    add_event("autoheal", f"{unit} wedged (health ok, {CANARY['fails']} "
                              f"probes failed, {num_reqs} req): restarting it")
                    code, out = start_action("unit", {"verb": "restart", "unit": unit})
                    audit({"kind": "autoheal", "unit": unit, "code": code, "out": out})
            else:
                WEDGED_SINCE.pop(unit, None)
        if st["state"] in lc.TRANSITIONAL and running:
            jl = run(["journalctl", "-u", unit, "-n", "40", "--no-pager",
                      "-o", "cat"], timeout=6).splitlines()
            rebuild = lc.journal_flags(jl)["rebuild"]
            st["rebuild"] = rebuild
        elapsed = None
        try:
            mono_us = int(d.get("ActiveEnterTimestampMonotonic", "0"))
            if mono_us > 0 and active == "active":
                elapsed = max(0.0, monotonic_now() - mono_us / 1e6)
        except ValueError:
            pass
        # Learning guard: only record a boot we actually witnessed from its
        # start. A cockpit (re)start facing an already-warm engine must not
        # mistake 'first time I see it' for 'it just booted'.
        enter_key = d.get("ActiveEnterTimestampMonotonic", "0")
        with LIFE_LOCK:
            prev_enter = LIFE["enter"].get(unit)
            LIFE["enter"][unit] = enter_key
            if prev_enter is not None and enter_key != prev_enter \
                    and enter_key != "0":
                LIFE["witnessed"][unit] = True
                # a new life: nothing from the previous one may count against it
                CANARY.update(fails=0, last_err="")
                WEDGED_SINCE.pop(unit, None)
                LAST_PROGRESS["ts"] = None
                READY_SINCE.pop(unit, None)
            witnessed = LIFE["witnessed"].get(unit, False)
        eta = lc.eta_for(history, unit, rebuild)
        overdue = bool(eta and elapsed and st["state"] in lc.TRANSITIONAL
                       and elapsed > 2 * eta)
        engines[unit] = {"state": st["state"], "rebuild": st.get("rebuild", False),
                         "stage_done": boot.get("done", []),
                         "elapsed": round(elapsed, 1) if elapsed else None,
                         "eta": eta, "overdue": overdue,
                         "boots": history.get(unit, [])[-5:],
                         "boots_rebuild": history.get(f"{unit}:rebuild", [])[-3:]}
        states[unit] = st["state"]
        # transitions: events + boot-duration learning
        was = prev.get(unit)
        if was and was != st["state"]:
            add_event("state", f"{unit}: {was} -> {st['state']}")
            if st["state"] == "ready" and elapsed and witnessed \
                    and was in lc.TRANSITIONAL:
                save_history(lc.record_boot(history, unit, elapsed, rebuild))
                with LIFE_LOCK:
                    LIFE["witnessed"][unit] = False
    with LIFE_LOCK:
        LIFE["states"] = states
    blocked = {}
    for unit in lc.ENGINE_UNITS:
        r = lc.blocked_reasons("unit", {"unit": unit, "verb": "start"}, states)
        if r:
            blocked[f"unit:start:{unit}"] = r
            blocked[f"unit:restart:{unit}"] = r
    for act in ("switch", "update_stack"):
        r = lc.blocked_reasons(act, {}, states)
        if r:
            blocked[act] = r
    with EVENTS_LOCK:
        ev = list(EVENTS)[-30:]
    return {"node_id": "local", "engines": engines,
            "keepalive": states.get("qwen38-keepalive.service", "stopped"),
            "blocked": blocked, "events": ev}


# ── Snapshot store + background sampling ────────────────────────────────────
STATE: dict[str, dict] = {}
STATE_LOCK = threading.Lock()
# Share of the KV pool one prompt may use: the same knob and default as the keepalive
# proxy's oversize guard (OVERSIZE_MARGIN_FRAC), so the reservoir tick and the proxy's
# refusals tell one story.
USABLE_FRAC = round(1.0 - float(os.environ.get("OVERSIZE_MARGIN_FRAC", "0.08")), 3)
STATE["config"] = {"data": {"usable_frac": USABLE_FRAC, "version": VERSION}, "ts": time.time()}
EVENT = threading.Condition()

TIERS = [
    (1.0, {"machine": collect_machine, "engine_fast": collect_engine_fast}),
    (2.0, {"lifecycle": collect_lifecycle}),
    (90.0, {"canary": collect_canary}),
    (3.0, {"gpu": collect_gpu, "decode": collect_decode_telemetry}),
    (5.0, {"units": collect_units, "containers": collect_containers,
           "feed": collect_feed}),
    (30.0, {"engine_info": collect_engine_info, "repo": collect_repo}),
]


def sampler(period: float, collectors: dict):
    while True:
        t0 = time.time()
        for name, fn in collectors.items():
            snap = fn()
            with STATE_LOCK:
                STATE[name] = {"data": snap, "ts": time.time()}
        with EVENT:
            EVENT.notify_all()
        time.sleep(max(0.2, period - (time.time() - t0)))


# ── Actions: fixed argv registry + supervised jobs ───────────────────────────
# Every mutating capability is a registry entry with a FIXED argv (or an argv
# template whose every parameter is validated against a closed enum). The UI
# renders from this registry; the backend never builds commands from free text.
AUDIT_LOG = CONFIG_DIR / "cockpit-audit.log"

SERVING_UNITS = {"qwen38-sglang.service", "qwen38-flash.service",
                 "qwen38-keepalive.service"}
UNIT_VERBS = {"start", "stop", "restart"}

ACTIONS = {
    # systemd control (privileged: requires the sudoers drop-in, see
    # dashboard/sudoers-cockpit.template)
    "unit": {
        "danger": "medium",
        "params": {"verb": sorted(UNIT_VERBS), "unit": sorted(SERVING_UNITS)},
        "argv": lambda p: ["sudo", "-n", "/usr/bin/systemctl", p["verb"], p["unit"]],
        "timeout": 90,
    },
    # model switch (repo script, itself never restarts anything)
    "switch": {
        "danger": "medium",
        "params": {"target": ["stock", "uncensored", "flash"]},
        "argv": lambda p: ["bash", str(REPO_DIR / "switch-model.sh"), p["target"]],
        "timeout": 1800,
    },
    # engine cache flush (harmless, engine-level)
    "flush_cache": {
        "danger": "low",
        "params": {},
        "argv": None,  # HTTP action, handled inline
        "timeout": 10,
    },
    # supervised converging upgrade of the serving stack
    "update_stack": {
        "danger": "high",
        "params": {},
        "argv": lambda p: ["bash", str(REPO_DIR / "install.sh")],
        "timeout": 3600,
    },
    # abort every in-flight generation (orphans left by vanished clients)
    "abort_all": {
        "danger": "medium",
        "params": {},
        "argv": None,
        "timeout": 10,
    },
    # diagnostics bundle for issue reports (logs, state, versions; key masked)
    "diag_bundle": {
        "danger": "low",
        "params": {},
        "argv": None,
        "timeout": 60,
    },
    # health smoke: one canary through the proxy
    "smoke": {
        "danger": "low",
        "params": {},
        "argv": None,
        "timeout": 300,
    },
}


class Job:
    def __init__(self, action: str, argv: list[str], timeout: float):
        self.id = secrets.token_hex(8)
        self.action = action
        self.argv = argv
        self.timeout = timeout
        self.lines: list[str] = []
        self.status = "running"
        self.rc: int | None = None
        self.started = time.time()

    def append(self, line: str):
        self.lines.append(line[:500])
        if len(self.lines) > 2000:  # bounded memory, always
            del self.lines[:500]


JOBS: dict[str, Job] = {}
JOB_LOCK = threading.Lock()   # one mutating job at a time, ever


def audit(entry: dict):
    entry["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def run_job(job: Job):
    try:
        proc = subprocess.Popen(job.argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                cwd=str(REPO_DIR))
        assert proc.stdout is not None
        deadline = time.time() + job.timeout
        for line in proc.stdout:
            job.append(line.rstrip())
            if time.time() > deadline:
                proc.kill()
                job.append("[cockpit] job timeout, killed")
                break
        job.rc = proc.wait(timeout=30)
        job.status = "done" if job.rc == 0 else "failed"
    except Exception as e:  # noqa: BLE001
        job.status = "failed"
        job.append(f"[cockpit] {type(e).__name__}: {e}")
    finally:
        audit({"kind": "job_end", "action": job.action, "rc": job.rc,
               "status": job.status, "id": job.id})
        add_event("job", f"{job.action} {job.status} (rc={job.rc})")
        JOB_LOCK.release()


def start_action(name: str, params: dict) -> tuple[int, dict]:
    spec = ACTIONS.get(name)
    if not spec:
        return 404, {"error": "unknown action"}
    # closed-enum validation of every parameter
    clean = {}
    for key, allowed in spec["params"].items():
        val = params.get(key)
        if val not in allowed:
            return 400, {"error": f"invalid {key}"}
        clean[key] = val
    # lifecycle gates: the server refuses what the UI also disables
    if name in ("unit", "switch", "update_stack"):
        with LIFE_LOCK:
            states = dict(LIFE.get("states", {}))
        reasons = lc.blocked_reasons(name, clean, states)
        if reasons:
            audit({"kind": "action_blocked", "action": name,
                   "params": clean, "reasons": reasons})
            return 409, {"error": "blocked", "reasons": reasons}
        warnings = lc.warn_reasons(name, clean, states)
    else:
        warnings = []
    if name == "flush_cache":
        try:
            req = urllib.request.Request(ENGINE_BASE + "/flush_cache",
                                         method="POST", data=b"",
                                         headers={"Authorization": f"Bearer {api_key()}"})
            urllib.request.urlopen(req, timeout=8).read()
            audit({"kind": "action", "action": name, "ok": True})
            return 200, {"ok": True}
        except Exception as e:  # noqa: BLE001
            return 502, {"error": str(e)[:200]}
    if name == "diag_bundle":
        try:
            import tarfile
            import tempfile
            ts = time.strftime("%Y%m%d-%H%M%S")
            out = Path.home() / f"qwen38-diag-{ts}.tar.gz"
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                (tdp / "cockpit-state.json").write_text(json.dumps(STATE, default=str, indent=1))
                (tdp / "journal-flash.txt").write_text(run(["journalctl", "-u", "qwen38-flash.service", "-n", "400", "--no-pager", "-o", "short-iso"], timeout=15))
                (tdp / "journal-sglang.txt").write_text(run(["journalctl", "-u", "qwen38-sglang.service", "-n", "200", "--no-pager", "-o", "short-iso"], timeout=15))
                (tdp / "journal-keepalive.txt").write_text(run(["journalctl", "-u", "qwen38-keepalive.service", "-n", "300", "--no-pager", "-o", "short-iso"], timeout=15))
                for cont in CONTAINERS:
                    (tdp / f"docker-{cont}.txt").write_text(run(["docker", "logs", "--tail", "600", cont], timeout=15, merge_err=True))
                (tdp / "nvidia-smi.txt").write_text(run(["nvidia-smi"], timeout=10))
                (tdp / "system.txt").write_text(run(["uname", "-a"]) + run(["free", "-g"]) + run(["df", "-h", str(Path.home())]) + run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}} {{.Size}} {{.ID}}"], timeout=10))
                (tdp / "units.txt").write_text("".join(run(["systemctl", "show", u, "--no-pager"], timeout=5) + "\n" for u in UNITS))
                try:
                    info = http_json(ENGINE_BASE + "/get_server_info", timeout=6)
                    for f in MASKED_FIELDS:
                        info.pop(f, None)
                    (tdp / "server-info.json").write_text(json.dumps(info, default=str, indent=1))
                except Exception as e:  # noqa: BLE001
                    (tdp / "server-info.json").write_text(json.dumps({"error": str(e)[:200]}))
                launch = CONFIG_DIR / "launch-flash.sh"
                if launch.exists():
                    txt = re.sub(r'--api-key "\$\(cat [^)]*\)"', '--api-key <masked>', launch.read_text())
                    (tdp / "launch-flash.sh").write_text(txt)
                # scrub the key VALUE everywhere: SGLang prints api_key=... in its
                # ServerArgs banner, which lands in docker logs and the journal
                key = api_key()
                for f in tdp.iterdir():
                    txt = f.read_text(errors="replace")
                    if key and key in txt:
                        f.write_text(txt.replace(key, "<masked>"))
                with tarfile.open(out, "w:gz") as tar:
                    for f in sorted(tdp.iterdir()):
                        tar.add(f, arcname=f"qwen38-diag-{ts}/{f.name}")
            audit({"kind": "action", "action": name, "ok": True, "path": str(out)})
            add_event("action", f"diagnostics bundle written: {out.name}")
            return 200, {"ok": True, "path": str(out), "bytes": out.stat().st_size}
        except Exception as e:  # noqa: BLE001
            return 500, {"error": str(e)[:200]}
    if name == "abort_all":
        try:
            req = urllib.request.Request(ENGINE_BASE + "/abort_request", method="POST",
                                         data=json.dumps({"abort_all": True}).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": f"Bearer {api_key()}"})
            urllib.request.urlopen(req, timeout=8).read()
            audit({"kind": "action", "action": name, "ok": True})
            add_event("action", "abort_all: every in-flight generation aborted")
            return 200, {"ok": True}
        except Exception as e:  # noqa: BLE001
            return 502, {"error": str(e)[:200]}
    if name == "smoke":
        try:
            body = json.dumps({"model": "qwen3.8-flash-next", "max_tokens": 200,
                               "messages": [{"role": "user",
                                             "content": "Reply with exactly: COCKPIT-SMOKE-OK"}]}).encode()
            req = urllib.request.Request(PROXY_BASE + "/v1/chat/completions", body,
                                         {"Content-Type": "application/json",
                                          "Authorization": f"Bearer {api_key()}"})
            out = json.loads(urllib.request.urlopen(req, timeout=280).read())
            txt = (out["choices"][0]["message"].get("content") or "").strip()
            audit({"kind": "action", "action": name, "ok": "SMOKE-OK" in txt})
            return 200, {"ok": "SMOKE-OK" in txt, "reply": txt[:80]}
        except Exception as e:  # noqa: BLE001
            return 502, {"error": str(e)[:200]}
    if not JOB_LOCK.acquire(blocking=False):
        return 409, {"error": "another job is already running"}
    job = Job(name, spec["argv"](clean), spec["timeout"])
    JOBS[job.id] = job
    audit({"kind": "job_start", "action": name, "params": clean,
           "argv": job.argv, "id": job.id})
    add_event("job", f"{name} started ({job.id})")
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return 202, {"job": job.id, "argv": job.argv, "warnings": warnings}


# ── Sessions / auth ──────────────────────────────────────────────────────────
def _session_secret() -> bytes:
    """Persist the HMAC secret (0600) so a cockpit restart keeps sessions.

    Field lesson: an in-memory secret logged every browser out at each
    service restart. Falls back to an ephemeral secret if the config dir
    is unwritable (degraded, never broken).
    """
    f = CONFIG_DIR / "cockpit-secret"
    try:
        raw = f.read_bytes()
        if len(raw) >= 32:
            return raw[:32]
    except OSError:
        pass
    raw = secrets.token_bytes(32)
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
    except OSError:
        pass
    return raw


SESSION_SECRET = _session_secret()

UPSTREAM_CACHE: dict = {"ts": 0.0, "data": None}
UPSTREAM_LOCK = threading.Lock()


def _get_json(url: str, timeout: float = 5.0):
    req = urllib.request.Request(url, headers={
        "User-Agent": "spark-cockpit/" + VERSION, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def upstream_snapshot(max_age: float = 3600.0) -> dict:
    """Are our pins behind their upstreams? Cached 1h, offline-tolerant:
    every remote failure degrades to status offline for that row only."""
    with UPSTREAM_LOCK:
        if UPSTREAM_CACHE["data"] and time.time() - UPSTREAM_CACHE["ts"] < max_age:
            return UPSTREAM_CACHE["data"]
        reg = registry_snapshot()
        rows = []
        for var, model_id in rg.PIN_MODELS.items():
            pin = reg["pins"].get(var)
            if not pin:
                continue
            row = {"model": model_id, "pin": pin[:10], "var": var}
            try:
                j = _get_json("https://huggingface.co/api/models/"
                              f"{model_id}/revision/main")
                sha = j.get("sha", "")
                row["upstream"] = sha[:10]
                row["status"] = "same" if sha == pin else "moved"
            except Exception as e:  # noqa: BLE001
                row["status"] = "offline"
                row["detail"] = str(e)[:80]
            rows.append(row)
        rel = {"local": run(["git", "-C", str(REPO_DIR), "describe",
                             "--tags", "--abbrev=0"]).strip()}
        try:
            j = _get_json("https://api.github.com/repos/hasso5703/"
                          "dgx-spark-qwen38/releases/latest")
            rel["latest"] = j.get("tag_name", "")
        except Exception as e:  # noqa: BLE001
            try:
                tags = _get_json("https://api.github.com/repos/hasso5703/"
                                 "dgx-spark-qwen38/tags")
                rel["latest"] = tags[0]["name"] if tags else ""
            except Exception:  # noqa: BLE001
                rel["latest"] = None
                rel["detail"] = str(e)[:80]
        data = {"models": rows, "release": rel, "ts": time.time()}
        UPSTREAM_CACHE.update(ts=time.time(), data=data)
        return data


RECIPES_CACHE: dict = {"ts": 0.0, "data": None}
RECIPES_LOCK = threading.Lock()
INSTALLED_INVOCATION = {"27b": Path("/etc/systemd/system/qwen38-sglang.service"),
                        "flash": CONFIG_DIR / "launch-flash.sh"}


def recipes_snapshot(max_age: float = 60.0) -> dict:
    """Built-in recipes (from the repo's own pins and templates), custom ones
    (JSON files), and for each: what is on the box and where the installed
    invocation drifts from it. Read-only; cached briefly."""
    with RECIPES_LOCK:
        if RECIPES_CACHE["data"] and time.time() - RECIPES_CACHE["ts"] < max_age:
            return RECIPES_CACHE["data"]
        assigns = rp.parse_assignments((REPO_DIR / "install.sh").read_text())
        templates = rp.load_templates(REPO_DIR)
        installed = {}
        for lane, path in INSTALLED_INVOCATION.items():
            try:
                installed[lane] = rp.profile_from_text(path.read_text())
            except OSError:
                installed[lane] = None
        reg = registry_snapshot()

        def enrich(rec):
            inst = installed.get(rec["lane"])
            return {"recipe": rec, "presence": rp.presence(rec, reg),
                    "drift": rp.drift(rec, inst) if inst else None,
                    "installed": bool(inst) and inst["model"].get("revision") == rec["model"]["revision"]
                    and inst["model"].get("repo") == rec["model"]["repo"]}

        builtin = [enrich(r) for r in rp.builtins(assigns, templates)]
        custom = []
        for item in rp.load_custom(CONFIG_DIR / "recipes"):
            row = {"file": item["file"], "errors": item["errors"]}
            if item["recipe"] and not item["errors"]:
                row.update(enrich(item["recipe"]))
            else:
                row["recipe"] = item["recipe"]
            custom.append(row)
        data = {"builtin": builtin, "custom": custom, "installed": installed,
                "custom_dir": str(CONFIG_DIR / "recipes"), "ts": time.time()}
        RECIPES_CACHE.update(ts=time.time(), data=data)
        return data


REGISTRY_CACHE: dict = {"ts": 0.0, "data": None}
REGISTRY_LOCK = threading.Lock()


def registry_snapshot(max_age: float = 300.0) -> dict:
    """Local registry, cached; the scan is metadata-only but not free."""
    with REGISTRY_LOCK:
        if REGISTRY_CACHE["data"] and time.time() - REGISTRY_CACHE["ts"] < max_age:
            return REGISTRY_CACHE["data"]
        pins = {}
        for f in ("switch-model.sh", "run.sh", "install.sh"):
            try:
                pins.update(rg.parse_pins((REPO_DIR / f).read_text()))
            except OSError:
                pass
        try:
            models = rg.classify(rg.scan_hf_cache(
                Path.home() / ".cache/huggingface/hub"), pins)
        except OSError as e:
            models = []
        images = rg.parse_docker_images(
            run(["docker", "images", "--format",
                 "{{.Repository}}:{{.Tag}} {{.Size}} {{.ID}}"],
                timeout=10).splitlines())
        data = {"pins": pins,
                "models": [m for m in models if m["managed"]],
                "other_models": [{"repo_id": m["repo_id"],
                                  "disk_bytes": m["disk_bytes"]}
                                 for m in models if not m["managed"]],
                "images": [i for i in images if i["engine"]],
                "ts": time.time()}
        REGISTRY_CACHE.update(ts=time.time(), data=data)
        return data
LOGIN_FAILS: dict[str, list] = {}


def make_token(kind: str) -> str:
    raw = f"{kind}:{int(time.time())}"
    sig = hmac.new(SESSION_SECRET, raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}:{sig}"


def check_token(tok: str, kind: str, max_age: int = 12 * 3600) -> bool:
    try:
        k, ts, sig = tok.split(":")
        good = hmac.new(SESSION_SECRET, f"{k}:{ts}".encode(),
                        hashlib.sha256).hexdigest()[:32]
        return (k == kind and hmac.compare_digest(sig, good)
                and time.time() - int(ts) < max_age)
    except (ValueError, AttributeError):
        return False


# ── HTTP layer ───────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "SparkCockpit/" + VERSION
    protocol_version = "HTTP/1.1"

    # ---- plumbing ----
    def log_message(self, fmt, *args):  # quiet by default, errors still surface
        pass

    def security_headers(self):
        # Zero external origins by construction; say so to the browser too.
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'self' 'unsafe-inline'; "
                         "script-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    def authed(self) -> bool:
        c = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        tok = c.get("cockpit")
        return bool(tok and check_token(tok.value, "sess"))

    # ---- routes ----
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            return self.send_json({"ok": True, "version": VERSION})
        if path == "/login" or path == "/favicon.ico" or path.startswith("/static/"):
            return self.serve_static(path)
        if not self.authed():
            if path == "/":
                return self.serve_static("/login")
            return self.send_json({"error": "auth"}, 401)
        if path == "/":
            return self.serve_static("/index")
        if path == "/api/state":
            with STATE_LOCK:
                return self.send_json(STATE)
        if path == "/api/actions":
            return self.send_json({n: {"danger": s["danger"],
                                       "params": s["params"]}
                                   for n, s in ACTIONS.items()})
        if path == "/api/upstream":
            try:
                return self.send_json(upstream_snapshot())
            except Exception as e:  # noqa: BLE001
                return self.send_json({"error": str(e)[:200]}, 500)
        if path == "/api/registry":
            try:
                return self.send_json(registry_snapshot())
            except Exception as e:  # noqa: BLE001 (isolated endpoint)
                return self.send_json({"error": str(e)[:200]}, 500)
        if path == "/api/recipes":
            try:
                return self.send_json(recipes_snapshot())
            except Exception as e:  # noqa: BLE001 (isolated endpoint)
                return self.send_json({"error": str(e)[:200]}, 500)
        if path == "/api/inventory":
            # uninstall.sh --list is read-only by design; parse its rows.
            out = run(["bash", str(REPO_DIR / "uninstall.sh"), "--list"],
                      timeout=30)
            items = []
            for line in out.splitlines():
                m = re.match(r"\s{2}(\S+)\s+(.*)", line)
                if m and m.group(1) in ("unit", "drop-ins", "backup", "config",
                                        "legacy", "launcher", "image",
                                        "weights", "ple-file"):
                    items.append({"kind": m.group(1), "what": m.group(2)})
            return self.send_json({"items": items})
        if path.startswith("/api/logs/"):
            name = path.rsplit("/", 1)[1]
            if name in CONTAINERS:
                txt = run(["docker", "logs", "--tail", "120", name],
                          timeout=8, merge_err=True)
            elif name in UNITS:
                txt = run(["journalctl", "-u", name, "-n", "120",
                           "--no-pager", "-o", "cat"], timeout=8)
            else:
                return self.send_json({"error": "unknown source"}, 404)
            return self.send_json({"name": name,
                                   "lines": txt.splitlines()[-120:]})
        if path.startswith("/api/jobs/"):
            job = JOBS.get(path.rsplit("/", 1)[1])
            if not job:
                return self.send_json({"error": "no such job"}, 404)
            return self.send_json({"id": job.id, "action": job.action,
                                   "status": job.status, "rc": job.rc,
                                   "started": job.started,
                                   "lines": job.lines[-200:]})
        if path == "/api/stream":
            return self.stream()
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(min(length, 65536)) if length else b""
        if path == "/api/login":
            # Rate limit: after 5 failures from one address, lock 60 s.
            ip = self.client_address[0]
            now = time.time()
            fails = [t for t in LOGIN_FAILS.get(ip, []) if now - t < 60]
            if len(fails) >= 5:
                LOGIN_FAILS[ip] = fails
                return self.send_json({"error": "too many attempts, wait a minute"}, 429)
            try:
                key = json.loads(raw or b"{}").get("key", "")
            except json.JSONDecodeError:
                key = ""
            expected = api_key()
            if not (expected and hmac.compare_digest(key, expected)):
                fails.append(now)
                LOGIN_FAILS[ip] = fails
                audit({"kind": "login_fail", "ip": ip})
                return self.send_json({"error": "bad key"}, 403)
            if True:
                tok = make_token("sess")
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie",
                                 f"cockpit={tok}; HttpOnly; SameSite=Strict; Path=/")
                self.end_headers()
                self.wfile.write(body)
                return
            return self.send_json({"error": "bad key"}, 403)
        if not self.authed():
            return self.send_json({"error": "auth"}, 401)
        # CSRF: any mutating POST must echo the token bound to the session.
        if path == "/api/csrf":
            return self.send_json({"token": make_token("csrf")})
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self.send_json({"error": "bad json"}, 400)
        if not check_token(payload.get("csrf", ""), "csrf", max_age=3600):
            return self.send_json({"error": "csrf"}, 403)
        if path == "/api/action":
            code, out = start_action(str(payload.get("name", "")),
                                     payload.get("params") or {})
            return self.send_json(out, code)
        return self.send_json({"error": "not found"}, 404)

    # ---- helpers ----
    def serve_static(self, path: str):
        name = {"/login": "login.html", "/index": "index.html",
                "/favicon.ico": "favicon.svg"}.get(path)
        if name is None and path.startswith("/static/"):
            name = path[len("/static/"):]
        target = (STATIC_DIR / (name or "")).resolve()
        if not name or not str(target).startswith(str(STATIC_DIR.resolve())) \
                or not target.is_file():
            return self.send_json({"error": "not found"}, 404)
        ctype = {"html": "text/html; charset=utf-8", "css": "text/css",
                 "js": "text/javascript", "svg": "image/svg+xml"}.get(
                     target.suffix.lstrip("."), "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                with EVENT:
                    EVENT.wait(timeout=2.0)
                with STATE_LOCK:
                    payload = json.dumps(STATE)
                self.wfile.write(b"data: " + payload.encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    load_events()
    if not shutil.which("nvidia-smi"):
        print("note: nvidia-smi not found, GPU panel will degrade")
    for period, cols in TIERS:
        threading.Thread(target=sampler, args=(period, cols), daemon=True).start()
    srv = Server((BIND, PORT), Handler)
    print(f"Spark Cockpit {VERSION} on http://{BIND}:{PORT} (repo: {REPO_DIR})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
