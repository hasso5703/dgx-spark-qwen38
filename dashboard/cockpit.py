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
import urllib.request
from pathlib import Path

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


def api_key() -> str:
    try:
        return (CONFIG_DIR / "api-key").read_text().strip()
    except OSError:
        return ""


def run(argv: list[str], timeout: float = 5.0) -> str:
    """Fixed-argv runner: never a shell, never client input."""
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
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
    rows = {}
    fmt = "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"
    for line in run(["docker", "stats", "--no-stream", "--format", fmt,
                     *CONTAINERS], timeout=8).splitlines():
        name, cpu, memu = (line.split("|") + ["", ""])[:3]
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
    tail = run(["docker", "logs", "--since", "30s", active], timeout=6)[-8000:]
    last = None
    for line in tail.splitlines():
        m = DECODE_RE.search(line)
        if m:
            last = {"running": int(m.group(1)),
                    "token_usage": float(m.group(2)),
                    "accept_len": float(m.group(3))}
    return {"node_id": "local", "lane": active, "decode": last}


@guard
def collect_repo():
    def g(*args):
        return run(["git", "-C", str(REPO_DIR), *args]).strip()
    return {"node_id": "local",
            "head": g("log", "-1", "--format=%h %s"),
            "branch": g("branch", "--show-current"),
            "tag": g("describe", "--tags", "--abbrev=0"),
            "dirty": bool(g("status", "--porcelain"))}


# ── Snapshot store + background sampling ────────────────────────────────────
STATE: dict[str, dict] = {}
STATE_LOCK = threading.Lock()
EVENT = threading.Condition()

TIERS = [
    (1.0, {"machine": collect_machine, "engine_fast": collect_engine_fast}),
    (3.0, {"gpu": collect_gpu, "decode": collect_decode_telemetry}),
    (5.0, {"units": collect_units, "containers": collect_containers}),
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


# ── Sessions / auth ──────────────────────────────────────────────────────────
SESSION_SECRET = secrets.token_bytes(32)


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

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
        if path == "/api/stream":
            return self.stream()
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(min(length, 65536)) if length else b""
        if path == "/api/login":
            try:
                key = json.loads(raw or b"{}").get("key", "")
            except json.JSONDecodeError:
                key = ""
            expected = api_key()
            if expected and hmac.compare_digest(key, expected):
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
        # Action registry: present, intentionally inert in the read-only phase.
        return self.send_json({"error": "actions land in the next phase"}, 501)

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
    if not shutil.which("nvidia-smi"):
        print("note: nvidia-smi not found, GPU panel will degrade")
    for period, cols in TIERS:
        threading.Thread(target=sampler, args=(period, cols), daemon=True).start()
    srv = Server((BIND, PORT), Handler)
    print(f"Spark Cockpit {VERSION} on http://{BIND}:{PORT} (repo: {REPO_DIR})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
