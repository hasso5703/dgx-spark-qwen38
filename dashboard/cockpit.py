#!/usr/bin/env python3
"""Spark Cockpit: the repo's web dashboard.

Single-file stdlib backend: no pip, no venv, nothing to break. Collectors, an
SSE stream, a static UI, and a supervised action surface that runs one job at a
time: unit start/stop/restart, lane switch, cache flush, abort, smoke probe.
Belts on top of that: a real generation canary (a wedged engine still answers
/health), a host memory floor, and an NVRM allocation-refusal counter.

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
VERSION = "1.0.0"
# Dry run: every mutating action and every automatic belt is logged, audited and
# shown exactly as usual, but nothing is executed. This is how the click-storm test
# (tests/monkey-check.mjs) exercises the whole UI against a second cockpit instance.
DRY_RUN = os.environ.get("COCKPIT_DRY_RUN", "0") == "1"

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
    # which image each serving container actually runs: a lane can be started outside
    # systemd (by hand, or by an older unit), and then the tag is the only way to know
    # what is really serving (field case 2026-08-31).
    for line in run(["docker", "ps", "--format", "{{.Names}}|{{.Image}}|{{.Status}}"],
                    timeout=8).splitlines():
        name, image, status = (line.split("|") + ["", ""])[:3]
        if name in CONTAINERS:
            rows.setdefault(name, {}).update(image=image, status=status)
    return {"node_id": "local", "containers": rows}


MEM_FLOOR_GIB = float(os.environ.get("COCKPIT_MEM_FLOOR_GIB", "3.0"))
MEM_FLOOR = {"last_abort": None, "aborts": 0, "last_reason": ""}


def mem_available_gib() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1048576
    except (OSError, ValueError):
        pass
    return None


def engine_abort_all(timeout: float = 8.0):
    req = urllib.request.Request(ENGINE_BASE + "/abort_request", method="POST",
                                 data=json.dumps({"abort_all": True}).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {api_key()}"})
    urllib.request.urlopen(req, timeout=timeout).read()


HEALTH = {"ts": 0.0, "ok": False}
HEALTH_RECHECK_S = float(os.environ.get("COCKPIT_HEALTH_RECHECK_S", "30"))


@guard
def collect_engine_fast():
    try:
        load = http_json(ENGINE_BASE + "/get_load", timeout=3)
    except Exception:  # noqa: BLE001
        load = None
    now = time.time()
    num_reqs, waiting = 0, 0
    try:
        row = (load or [{}])[0]
        num_reqs = int(row.get("num_reqs", 0))
        waiting = int(row.get("num_waiting_reqs", 0))
    except (TypeError, ValueError, AttributeError, IndexError):
        pass
    if load is None:
        # no HTTP layer at all (stopped, crashed, restarting): unhealthy within the second
        HEALTH["ok"], HEALTH["ts"] = False, now
    elif num_reqs + waiting > 0:
        # It answers AND it is working: that is liveness, proven without asking it to
        # generate anything. Measured 30/08: on a busy engine GET /health queues behind
        # the running request and times out every single time, which made the cockpit
        # call a serving engine "starting" and blank its own identity panel.
        HEALTH["ok"], HEALTH["ts"] = True, now
    elif now - HEALTH["ts"] >= (HEALTH_RECHECK_S if HEALTH["ok"] else 2.0):
        # In these SGLang builds GET /health is /health_generate: it runs a one-token
        # generation whenever the engine is idle (2,394 generations in the last hour of the
        # 30/08 instance at 1 Hz, and the 01:15 GPU fault happened inside one of them). So
        # it is only asked when the engine has nothing to do: every 2 s until it answers
        # 200, then every 30 s; the 90 s canary remains the probe that tells a wedged
        # scheduler from a healthy one.
        try:
            urllib.request.urlopen(ENGINE_BASE + "/health", timeout=4).read()
            HEALTH["ok"] = True
        except Exception:  # noqa: BLE001
            HEALTH["ok"] = False
        HEALTH["ts"] = now
    healthy = HEALTH["ok"]
    avail = mem_available_gib()
    abort, reason = lc.decide_mem_floor(avail_gib=avail, floor_gib=MEM_FLOOR_GIB, num_reqs=num_reqs,
                                        last_abort_ts=MEM_FLOOR["last_abort"], now=time.time())
    if abort:
        MEM_FLOOR["last_abort"] = time.time()
        MEM_FLOOR["aborts"] += 1
        MEM_FLOOR["last_reason"] = reason
        try:
            if DRY_RUN:
                raise RuntimeError("dry run: abort_all not sent")
            engine_abort_all()
            audit({"kind": "mem_floor", "avail_gib": avail, "num_reqs": num_reqs, "ok": True})
            add_event("mem_floor", f"memory floor: {reason}; every generation aborted")
        except Exception as e:  # noqa: BLE001
            audit({"kind": "mem_floor", "avail_gib": avail, "num_reqs": num_reqs, "ok": False, "err": str(e)[:120]})
            add_event("mem_floor", f"memory floor: {reason}; abort failed: {str(e)[:80]}")
    return {"node_id": "local", "load": load, "healthy": healthy,
            "mem_floor": {"gib": MEM_FLOOR_GIB, "avail_gib": avail, "aborts": MEM_FLOOR["aborts"],
                          "last_abort": MEM_FLOOR["last_abort"]}}


KERNEL_LAST = {"count": None}


@guard
def collect_kernel():
    """GPU driver allocation refusals (NVRM NV_ERR_NO_MEMORY) in the kernel log, last
    hour: measured 29/08 to precede the livelock edge (prefill of a 150k+ prompt at
    fraction 0.81). Read-only via the sudoers line for journalctl -k."""
    out = run(["sudo", "-n", "/usr/bin/journalctl", "-k", "--since", "-1h", "--no-pager", "-o", "short-iso"], timeout=8)
    lines = [ln for ln in out.splitlines() if "NV_ERR_NO_MEMORY" in ln]
    last = lines[-1].split()[0] if lines else None
    count = len(lines)
    if KERNEL_LAST["count"] is not None and count > KERNEL_LAST["count"]:
        add_event("kernel", f"GPU driver refused {count - KERNEL_LAST['count']} allocation(s): memory edge during a prefill")
    KERNEL_LAST["count"] = count
    return {"node_id": "local", "nvrm_oom_1h": count, "nvrm_last": last}


@guard
def collect_engine_info():
    # 10 s, not 6: SGLang's event loop stalls under a long prefill and a late answer is
    # far better than none (measured 30/08: three timeouts in a row at 6 s while the same
    # endpoint answered in 0.19 s between two requests).
    info = http_json(ENGINE_BASE + "/get_server_info", timeout=10)
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
    # Which switch target these weights are: the selector must show what is serving,
    # not the first option in the list. Derived from the registry pins, so a new pin
    # is a new target with no extra table to keep in step.
    served_target = None
    for var, repo in rg.PIN_MODELS.items():
        if repo == slim.get("model_path") and var in VAR2TARGET:
            served_target = VAR2TARGET[var]
            break
    # the proxy's absolute prompt ceiling, as deployed in the keepalive unit (0 = pool share only)
    ceiling = 0
    env = run(["systemctl", "show", "qwen38-keepalive.service", "-p", "Environment"], timeout=5)
    m = re.search(r"PROMPT_CEILING_TOKENS=(\d+)", env or "")
    if m:
        ceiling = int(m.group(1))
    return {"node_id": "local", "info": slim, "prompt_ceiling_tokens": ceiling,
            "served_target": served_target}


CANARY: dict = {"fails": 0, "last_ok": None, "last_err": "", "latency": None}
# Off by default, deliberately. This repo spends a README section on the ways a
# GB10 box freezes, so an engine restarting itself is never something an install
# should decide for the operator: arm it with COCKPIT_AUTOHEAL=1 once you know
# what a wedge looks like on your box.
AUTOHEAL = os.environ.get("COCKPIT_AUTOHEAL", "0") == "1"
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
# The guard says what it did and what it cost, once per streak: it used to write one
# event per attempt, and a scheduler busy with a prefill made it retry every 38 s
# (20 identical "flush failed: timed out" lines in the 30/08 journal).
POOL_GUARD_STATE: dict = {"flushes": 0, "last": None, "fails": 0, "last_err": "", "last_fail": None}
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
    # a dry-run instance exists to exercise the UI: it must not make the real engine
    # generate anything, not even two tokens (a second cockpit shares the same box).
    if DRY_RUN or not ready or JOB_LOCK.locked() or busy or recent:
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


@guard
def collect_feed():
    """Last requests seen by the keepalive proxy: client, path, size, outcome, guard detail."""
    raw = run(["journalctl", "-u", "qwen38-keepalive.service", "-n", "200",
               "--no-pager", "-o", "short-iso"], timeout=6)
    return {"node_id": "local", "rows": lc.parse_feed(raw)}


@guard
def collect_opencode():
    """What the installer and the switch act on: the --no-opencode marker, the config
    opencode really reads (default model, per-lane limits), the launcher and its cap."""
    off = CONFIG_DIR / "opencode.off"
    real = Path.home() / ".config/opencode/opencode.json"
    art = CONFIG_DIR / "opencode.json"
    launcher = Path.home() / ".local/bin/oc"
    out = {"node_id": "local", "enabled": not off.exists(),
           "off_note": off.read_text(errors="replace").strip()[:100] if off.exists() else None,
           "launcher": {"present": launcher.exists(), "ours": False, "cap": None},
           "real": {"present": real.exists(), "default": None, "limits": {}},
           "artifact": {"present": art.exists(), "default": None}}
    if launcher.exists():
        txt = launcher.read_text(errors="replace")
        out["launcher"]["ours"] = "dgx-spark-qwen38" in txt
        m = re.search(r"OUTPUT_TOKEN_MAX[^0-9]*?(\d{4,7})", txt)
        out["launcher"]["cap"] = int(m.group(1)) if m else None
    for key, path in (("real", real), ("artifact", art)):
        if not path.exists():
            continue
        try:
            cfg = json.loads(path.read_text())
        except (ValueError, OSError) as e:  # noqa: PERF203
            out[key]["error"] = str(e)[:80]
            continue
        out[key]["default"] = cfg.get("model")
        if key == "real":
            for prov, pv in (cfg.get("provider") or {}).items():
                if prov not in ("qwen38", "flashnext"):
                    continue
                for mid, mv in (pv.get("models") or {}).items():
                    lim = mv.get("limit") or {}
                    out["real"]["limits"][f"{prov}/{mid}"] = {"context": lim.get("context"), "output": lim.get("output")}
    with LIFE_LOCK:
        states = dict(LIFE.get("states", {}))
    ok, why = lc.opencode_default_follows(out["real"]["default"], states)
    out["follows"] = ok
    out["why"] = why
    # Do the declared limits fit the pool the engine actually booted with? A limit
    # larger than the pool does not fail early: the conversation grows until the
    # proxy refuses a prompt mid-session.
    with STATE_LOCK:
        info = ((STATE.get("engine_info") or {}).get("data") or {}).get("info") or {}
    pool = int(info.get("max_total_num_tokens") or 0)
    served = info.get("served_model_name") or ""
    out["fit"] = None
    if pool and served:
        prov = {"qwen3.8-27b": "qwen38", "qwen3.8-flash-next": "flashnext"}.get(served)
        lim = (out["real"]["limits"] or {}).get(f"{prov}/{served}") if prov else None
        if lim and lim.get("context"):
            ctx, outp = int(lim["context"]), int(lim.get("output") or 0)
            usable = int(pool * USABLE_FRAC)
            # two independent constraints: the prompt alone must pass the proxy (which
            # also applies the lane's absolute ceiling), and prompt plus answer must fit
            # the pool. The flash lane fails only the first, the FP8 lane only the second.
            ceiling = ((STATE.get("engine_info") or {}).get("data") or {}).get("prompt_ceiling_tokens") or 0
            prompt_cap = min(usable, ceiling) if ceiling else usable
            why = ("the prompt alone exceeds what the proxy relays" if ctx > prompt_cap
                   else "prompt plus answer exceeds the pool" if ctx + outp > usable else "")
            out["fit"] = {"pool": pool, "worst": ctx + outp, "usable": usable, "served": served,
                          "prompt_cap": prompt_cap, "ok": not why, "why": why,
                          "context": ctx, "output": outp}
    return out


@guard
def collect_repo():
    def g(*args):
        return run(["git", "-C", str(REPO_DIR), *args]).strip()
    # keepalive proxy: version of the deployed file and whether it is the repo's copy
    proxy = {"version": None, "same_as_repo": None}
    try:
        deployed = (CONFIG_DIR / "keepalive-proxy.py").read_bytes()
        m = re.search(rb"\nv(\d+\.\d+):", deployed[:4000])
        proxy["version"] = "v" + m.group(1).decode() if m else None
        repo_copy = (REPO_DIR / "keepalive-proxy.py").read_bytes()
        proxy["same_as_repo"] = hashlib.sha256(deployed).hexdigest() == hashlib.sha256(repo_copy).hexdigest()
    except OSError:
        pass
    return {"node_id": "local",
            "head": g("log", "-1", "--format=%h %s"),
            "branch": g("branch", "--show-current"),
            "tag": g("describe", "--tags", "--abbrev=0"),
            "dirty": bool(g("status", "--porcelain")),
            "proxy": proxy}


def _guard_failed(err: str):
    """One event per failure streak, then silence: the next line is the recovery."""
    POOL_GUARD_STATE["fails"] += 1
    POOL_GUARD_STATE["last_err"] = err
    POOL_GUARD_STATE["last_fail"] = time.time()
    if POOL_GUARD_STATE["fails"] == 1:
        add_event("guard", f"pool guard: could not flush the prefix cache ({err}); "
                           f"backing off, it will try again when the engine is quiet")


VAR2TARGET = {"STOCK_REV": "stock", "UNC_REV": "uncensored", "FP8_REV": "fp8",
              "UNCFP8_REV": "uncensored-fp8", "FLASH_REV": "flash"}
UNIT_PATHS = {"qwen38-sglang.service": Path("/etc/systemd/system/qwen38-sglang.service"),
              "qwen38-flash.service": CONFIG_DIR / "launch-flash.sh"}
UNIT_TARGET_CACHE: dict = {}


def unit_target(unit: str) -> dict:
    """{model, target} the unit is configured for, from its own file. Cached on mtime."""
    path = UNIT_PATHS.get(unit)
    if not path:
        return {"model": None, "target": None}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {"model": None, "target": None}
    hit = UNIT_TARGET_CACHE.get(unit)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {"model": None, "target": None}
    m = re.search(r"--model-path\s+(\S+)", text)
    model = m.group(1) if m else None
    target = None
    for var, repo in rg.PIN_MODELS.items():
        if repo == model and var in VAR2TARGET:
            target = VAR2TARGET[var]
            break
    out = {"model": model, "target": target}
    UNIT_TARGET_CACHE[unit] = (mtime, out)
    return out


def containers_now() -> dict:
    with STATE_LOCK:
        return ((STATE.get("containers") or {}).get("data") or {}).get("containers") or {}


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
                   "ActiveState,SubState,ActiveEnterTimestampMonotonic,StateChangeTimestampMonotonic"])
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
            # A failed attempt backs off further and further instead of retrying at a
            # fixed 30 s, and the engine must also be quiet in its own log: /get_load
            # can read zero between two turns while the scheduler is still working.
            quiet = not LAST_PROGRESS["ts"] or time.time() - LAST_PROGRESS["ts"] > 10
            cooldown = 30.0 * min(8, 1 + POOL_GUARD_STATE["fails"])
            if (POOL_GUARD and IDLE_SINCE["ts"] and time.time() - IDLE_SINCE["ts"] > 3 and quiet
                    and (LAST_USAGE["value"] > POOL_GUARD_THRESHOLD
                         or LAST_USAGE["mamba"] >= MAMBA_GUARD_THRESHOLD)
                    and time.time() - LAST_FLUSH["ts"] > cooldown):
                held, mamba = LAST_USAGE["value"], LAST_USAGE["mamba"]
                try:
                    if DRY_RUN:
                        raise urllib.error.HTTPError(ENGINE_BASE, 400, "dry run: flush not sent", None, None)
                    req = urllib.request.Request(ENGINE_BASE + "/flush_cache", method="POST",
                                                 data=b"", headers={"Authorization": f"Bearer {api_key()}"})
                    urllib.request.urlopen(req, timeout=8).read()
                    POOL_GUARD_STATE.update(flushes=POOL_GUARD_STATE["flushes"] + 1,
                                            last=time.time(), fails=0, last_err="")
                    add_event("guard", f"pool guard: prefix cache flushed while the engine was idle "
                                       f"({held:.0%} of the pool held, {mamba:.0%} of the mamba slots); "
                                       f"the next long prompt prefills from scratch")
                    audit({"kind": "pool_guard", "usage": held, "mamba": mamba})
                except urllib.error.HTTPError as e:
                    # 400 = "pending requests": the engine is not idle after all
                    # (a queued request the load endpoint does not show); stand down.
                    if e.code != 400:
                        _guard_failed(f"HTTP {e.code}")
                    IDLE_SINCE["ts"] = None
                except Exception as e:  # noqa: BLE001
                    _guard_failed(str(e)[:80])
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
                    dump = "" if DRY_RUN else run(["sudo", "-n", "/usr/local/bin/qwen38-pyspy-scheduler"],
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
                              f"probes failed, {num_reqs} req): restarting it"
                              + (" (dry run: not really)" if DRY_RUN else ""))
                    code, out = start_action("unit", {"verb": "restart", "unit": unit}, origin="autoheal")
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
        state_elapsed = None
        try:
            chg_us = int(d.get("StateChangeTimestampMonotonic", "0"))
            if chg_us > 0:
                state_elapsed = max(0.0, monotonic_now() - chg_us / 1e6)
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
                         **unit_target(unit),
                         "stage_done": boot.get("done", []),
                         "elapsed": round(elapsed, 1) if elapsed else None,
                         "state_elapsed": round(state_elapsed, 1) if state_elapsed is not None else None,
                         "eta": eta, "overdue": overdue,
                         "boots": history.get(unit, [])[-5:],
                         "boots_rebuild": history.get(f"{unit}:rebuild", [])[-3:],
                         "pools": lc.pool_spread(history, unit, unit_target(unit).get("target"))}
        states[unit] = st["state"]
        # transitions: events + boot-duration learning
        was = prev.get(unit)
        if was and was != st["state"]:
            add_event("state", f"{unit}: {was} -> {st['state']}")
            if st["state"] == "ready" and elapsed and witnessed \
                    and was in lc.TRANSITIONAL:
                history = lc.record_boot(history, unit, elapsed, rebuild)
                # The KV pool this boot won, kept per target: it is a lottery and
                # it decides whether the declared opencode limits can be served.
                with STATE_LOCK:
                    _info = ((STATE.get("engine_info") or {}).get("data") or {}).get("info") or {}
                history = lc.record_pool(history, unit, unit_target(unit).get("target"),
                                         int(_info.get("max_total_num_tokens") or 0))
                save_history(history)
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
    # A container serving while its unit is not active: the cockpit's buttons act on the
    # unit, so it must say so instead of reporting "no engine" over a live engine.
    orphans = []
    for unit, cont in UNIT2CONT.items():
        if states.get(unit) == "stopped" and run(["docker", "ps", "-q", "-f", f"name=^{cont}$"]).strip():
            orphans.append({"unit": unit, "container": cont,
                            "image": (containers_now().get(cont) or {}).get("image")})
    return {"node_id": "local", "engines": engines, "orphans": orphans,
            "keepalive": states.get("qwen38-keepalive.service", "stopped"),
            "pool_guard": {"enabled": POOL_GUARD, "threshold": POOL_GUARD_THRESHOLD, **POOL_GUARD_STATE},
            "blocked": blocked, "events": ev}


# ── Snapshot store + background sampling ────────────────────────────────────
STATE: dict[str, dict] = {}
STATE_LOCK = threading.Lock()
# Share of the KV pool one prompt may use: the same knob and default as the keepalive
# proxy's oversize guard (OVERSIZE_MARGIN_FRAC), so the reservoir tick and the proxy's
# refusals tell one story.
USABLE_FRAC = round(1.0 - float(os.environ.get("OVERSIZE_MARGIN_FRAC", "0.08")), 3)
EVENT = threading.Condition()

def collect_jobs():
    """1 s tier: the running job (whoever started it) and the recent ones."""
    return job_snapshot()


TIERS = [
    (1.0, {"machine": collect_machine, "engine_fast": collect_engine_fast, "job": collect_jobs}),
    (2.0, {"lifecycle": collect_lifecycle}),
    (90.0, {"canary": collect_canary}),
    (3.0, {"gpu": collect_gpu, "decode": collect_decode_telemetry}),
    (5.0, {"units": collect_units, "containers": collect_containers,
           "feed": collect_feed}),
    (30.0, {"engine_info": collect_engine_info, "repo": collect_repo, "kernel": collect_kernel,
            "opencode": collect_opencode}),
]


# expected refresh period per collector, so the UI can tell a stale panel from a slow one
PERIODS = {name: period for period, cols in TIERS for name in cols}
STATE["config"] = {"data": {"usable_frac": USABLE_FRAC, "version": VERSION, "dry_run": DRY_RUN,
                            "repo_dir": str(REPO_DIR), "periods": PERIODS,
                            "terminal_only": {"update_stack": f"cd {REPO_DIR} && ./install.sh"}},
                   "ts": time.time()}


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
        "params": {"target": ["stock", "uncensored", "fp8", "uncensored-fp8", "flash"]},
        "argv": lambda p: ["bash", str(REPO_DIR / "switch-model.sh"), p["target"]],
        "timeout": 1800,
    },
    # engine cache flush (harmless, engine-level)
    "flush_cache": {
        "danger": "low",
        "params": {},
        "argv": None,  # python job, see JOB_FUNCS
        "timeout": 20,
    },
    # NOTE: no "update_stack" here. install.sh needs an interactive sudo (units, cp,
    # sed) that a service without a tty cannot give; a half-applied install from a
    # button is the one failure this cockpit must never cause. The Setup tab shows
    # the exact terminal command instead (STATE config: terminal_only).
    # abort every in-flight generation (orphans left by vanished clients)
    "abort_all": {
        "danger": "medium",
        "params": {},
        "argv": None,
        "timeout": 10,
    },
    # make opencode ask for no more than the served engine can hold
    "fit_opencode": {
        "danger": "low",
        "params": {},
        "argv": lambda p: ["python3", str(REPO_DIR / "oc-fit-limits.py")],
        "timeout": 60,
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
    def __init__(self, action: str, argv: list[str] | None, timeout: float,
                 params: dict | None = None, fn=None, origin: str = "ui"):
        self.id = secrets.token_hex(8)
        self.action = action
        self.argv = argv
        self.fn = fn                     # python job (flush, abort, smoke, bundle)
        self.params = params or {}
        self.origin = origin             # ui | autoheal
        self.timeout = timeout
        self.lines: list[str] = []
        self.status = "running"
        self.rc: int | None = None
        self.started = time.time()
        self.ended: float | None = None
        self.result: dict = {}

    def append(self, line: str):
        self.lines.append(line[:500])
        if len(self.lines) > 2000:  # bounded memory, always
            del self.lines[:500]

    def summary(self, tail: int = 0) -> dict:
        d = {"id": self.id, "action": self.action, "params": self.params, "origin": self.origin,
             "status": self.status, "rc": self.rc, "started": self.started, "ended": self.ended,
             "elapsed": round((self.ended or time.time()) - self.started, 1),
             "argv": self.argv, "result": self.result, "dry_run": DRY_RUN}
        if tail:
            d["lines"] = self.lines[-tail:]
        return d


JOBS: dict[str, Job] = {}
JOBS_KEEP = 50                # bounded: the audit log is the long-term record
JOB_LOCK = threading.Lock()   # one mutating job at a time, ever
JOB_CURRENT: dict = {"id": None}


def job_snapshot() -> dict:
    """What every browser tab sees, whoever started the job: the running job with its
    log tail, and the last finished ones. A reload or a second tab never loses a job."""
    cur = JOBS.get(JOB_CURRENT["id"]) if JOB_CURRENT["id"] else None
    if cur and cur.status != "running":
        cur = None
    hist = sorted((j for j in JOBS.values() if j.status != "running"),
                  key=lambda j: j.started, reverse=True)[:5]
    return {"node_id": "local", "current": cur.summary(tail=40) if cur else None,
            "recent": [j.summary() for j in hist], "locked": JOB_LOCK.locked()}


def _prune_jobs():
    if len(JOBS) <= JOBS_KEEP:
        return
    for j in sorted(JOBS.values(), key=lambda j: j.started)[:len(JOBS) - JOBS_KEEP]:
        if j.status != "running":
            JOBS.pop(j.id, None)


def audit(entry: dict):
    entry["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def run_job(job: Job):
    try:
        if DRY_RUN:
            job.append(f"[dry run] would run: {' '.join(job.argv) if job.argv else job.action}")
            time.sleep(2.0)
            job.rc = 0
            job.status = "done"
            job.result = {"ok": True, "dry_run": True}
        elif job.fn is not None:
            ok, lines, result = job.fn(job)
            for ln in lines:
                job.append(ln)
            job.result = result
            job.rc = 0 if ok else 1
            job.status = "done" if ok else "failed"
        else:
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
        job.ended = time.time()
        audit({"kind": "job_end", "action": job.action, "rc": job.rc,
               "status": job.status, "id": job.id, "dry_run": DRY_RUN})
        add_event("job", f"{job.action} {job.status}" + (f" (rc={job.rc})" if job.argv else "")
                  + (" [dry run]" if DRY_RUN else ""))
        _prune_jobs()
        JOB_LOCK.release()
        with EVENT:
            EVENT.notify_all()


# ── python jobs (they used to run inline in the HTTP handler, unlocked) ──────
def served_model_name() -> str:
    """The name the served engine answers to (never a hardcoded lane)."""
    with STATE_LOCK:
        info = ((STATE.get("engine_info") or {}).get("data") or {}).get("info") or {}
    name = info.get("served_model_name")
    if name:
        return name
    with LIFE_LOCK:
        states = dict(LIFE.get("states", {}))
    if states.get("qwen38-flash.service") not in (None, "stopped", "failed"):
        return "qwen3.8-flash-next"
    return "qwen3.8-27b"


def job_flush_cache(job: Job):
    req = urllib.request.Request(ENGINE_BASE + "/flush_cache", method="POST", data=b"",
                                 headers={"Authorization": f"Bearer {api_key()}"})
    try:
        urllib.request.urlopen(req, timeout=8).read()
    except urllib.error.HTTPError as e:
        if e.code == 400:
            return False, ["the engine refused: requests are still running (nothing was flushed)"], {"ok": False, "reason": "busy"}
        raise
    return True, ["radix cache flushed: the next prompt starts from an empty pool"], {"ok": True}


def job_abort_all(job: Job):
    engine_abort_all()
    return True, ["every in-flight generation aborted (clients see their stream end)"], {"ok": True}


def job_smoke(job: Job):
    model = served_model_name()
    job.append(f"canary through the proxy {PROXY_BASE}, model {model}, up to 200 tokens")
    body = json.dumps({"model": model, "max_tokens": 200,
                       "chat_template_kwargs": {"enable_thinking": False},
                       "messages": [{"role": "user",
                                     "content": "Reply with exactly: COCKPIT-SMOKE-OK"}]}).encode()
    req = urllib.request.Request(PROXY_BASE + "/v1/chat/completions", body,
                                 {"Content-Type": "application/json",
                                  "Authorization": f"Bearer {api_key()}"})
    t0 = time.time()
    out = json.loads(urllib.request.urlopen(req, timeout=280).read())
    txt = (out["choices"][0]["message"].get("content") or "").strip()
    ok = "SMOKE-OK" in txt
    return ok, [f"reply in {time.time() - t0:.1f} s: {txt[:120]!r}",
                "smoke OK: the proxy, the engine and the chat template all answer" if ok
                else "smoke FAILED: the engine answered but not with the expected marker"], \
        {"ok": ok, "reply": txt[:80], "seconds": round(time.time() - t0, 1)}


def job_diag_bundle(job: Job):
    import tarfile
    import tempfile
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = Path.home() / f"qwen38-diag-{ts}.tar.gz"
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        with STATE_LOCK:
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
    size = out.stat().st_size
    add_event("action", f"diagnostics bundle written: {out.name}")
    return True, [f"bundle written: {out} ({size / 1024:.0f} KB, API key masked)"], \
        {"ok": True, "path": str(out), "bytes": size}


JOB_FUNCS = {"flush_cache": job_flush_cache, "abort_all": job_abort_all,
             "smoke": job_smoke, "diag_bundle": job_diag_bundle}


def start_action(name: str, params: dict, origin: str = "ui") -> tuple[int, dict]:
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
    warnings = []
    if name in ("unit", "switch"):
        with LIFE_LOCK:
            states = dict(LIFE.get("states", {}))
        reasons = lc.blocked_reasons(name, clean, states)
        if reasons:
            audit({"kind": "action_blocked", "action": name,
                   "params": clean, "reasons": reasons})
            return 409, {"error": "blocked", "reasons": reasons}
        warnings = lc.warn_reasons(name, clean, states)
    # one job at a time: the second click, the second tab, the autoheal, all wait
    if not JOB_LOCK.acquire(blocking=False):
        cur = JOBS.get(JOB_CURRENT["id"]) if JOB_CURRENT["id"] else None
        return 409, {"error": "busy",
                     "running": cur.summary() if cur else None,
                     "message": "another action is already running"
                                + (f": {cur.action}" if cur else "") + "; wait for it to finish"}
    try:
        argv = spec["argv"](clean) if spec["argv"] else None
        job = Job(name, argv, spec["timeout"], params=clean, fn=JOB_FUNCS.get(name), origin=origin)
        JOBS[job.id] = job
        JOB_CURRENT["id"] = job.id
        audit({"kind": "job_start", "action": name, "params": clean,
               "argv": argv, "id": job.id, "origin": origin, "dry_run": DRY_RUN})
        add_event("job", f"{name} started" + (f" ({clean})" if clean else "")
                  + (" [dry run]" if DRY_RUN else ""))
        threading.Thread(target=run_job, args=(job,), daemon=True).start()
    except Exception as e:  # noqa: BLE001 (the lock must never leak)
        JOB_LOCK.release()
        return 500, {"error": f"could not start: {type(e).__name__}: {str(e)[:120]}"}
    return 202, {"job": job.id, "argv": argv, "warnings": warnings, "dry_run": DRY_RUN}


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
        except OSError:
            models = []
        images = rg.parse_docker_images(
            run(["docker", "images", "--format",
                 "{{.Repository}}:{{.Tag}} {{.Size}} {{.ID}}"],
                timeout=10).splitlines())
        data = {"pins": pins, "managed_repos": sorted(set(rg.PIN_MODELS.values())),
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
        path, _, query = self.path.partition("?")
        fresh = "refresh=1" in query
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
                return self.send_json(upstream_snapshot(max_age=0.0 if fresh else 3600.0))
            except Exception as e:  # noqa: BLE001
                return self.send_json({"error": str(e)[:200]}, 500)
        if path == "/api/registry":
            try:
                # "scan" has to mean scan: the button sends refresh=1 and bypasses the cache
                return self.send_json(registry_snapshot(max_age=0.0 if fresh else 300.0))
            except Exception as e:  # noqa: BLE001 (isolated endpoint)
                return self.send_json({"error": str(e)[:200]}, 500)
        if path == "/api/recipes":
            try:
                if fresh:
                    registry_snapshot(max_age=0.0)   # recipes read the registry for presence
                return self.send_json(recipes_snapshot(max_age=0.0 if fresh else 60.0))
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
            return self.send_json(job.summary(tail=200))
        if path == "/api/stream":
            return self.stream()
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 65536:
            self.close_connection = True
            return self.send_json({"error": "body too large"}, 413)
        raw = self.rfile.read(length) if length else b""
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
        # a redeploy must reach every open browser at its next load: revalidate always
        self.send_header("Cache-Control", "no-cache")
        self.security_headers()
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
    print(f"Spark Cockpit {VERSION} on http://{BIND}:{PORT} (repo: {REPO_DIR})"
          + ("  [DRY RUN: nothing is executed]" if DRY_RUN else ""))
    srv.serve_forever()


if __name__ == "__main__":
    main()
