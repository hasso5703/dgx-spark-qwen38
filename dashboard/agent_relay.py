#!/usr/bin/env python3
"""Agent relay: opencode's web interface behind the cockpit's own login.

`opencode serve` listens on loopback with a Basic password that never leaves the
box. This relay is the only thing that reaches it. It binds one address (the
tailnet address by default), accepts a request only with a valid cockpit session
cookie, presents the Basic credentials upstream, and streams the answer back:
plain responses, server-sent events, and the WebSocket upgrade the terminal
panel uses. The browser sees one host for the cockpit and the relay (cookies
ignore ports), so the Agent tab frames the interface with no second login and
no Basic-auth prompt, which Chrome would block inside a cross-origin frame.

Stdlib only, like the cockpit. The pure helpers (header hygiene, origin check,
address resolution) are kept apart from the socket code so the tests can pin
them without a browser.
"""
from __future__ import annotations

import base64
import http.client
import http.server
import json
import re
import select
import socket
import socketserver
import subprocess
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

RELAY_VERSION = "1.0"

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailer", "transfer-encoding", "upgrade"}
# Request headers the relay never forwards: the cockpit session must not reach
# opencode, the client's own Authorization is replaced by ours, Host names the
# upstream, lengths are re-set, and the referrer says nothing about the cockpit.
REQUEST_DROP = HOP_BY_HOP | {"host", "cookie", "authorization", "content-length", "referer"}
# Response headers the relay owns (Date and Server come from this server).
RESPONSE_DROP = HOP_BY_HOP | {"content-length", "date", "server"}
SESSION_COOKIE = "cockpit"
# Fetched by browsers with no cookie (a manifest link omits credentials): the app's
# name and icons, nothing else, so it may pass without a session (GET only).
PUBLIC_PATHS = {"/site.webmanifest"}
MAX_BODY = 64 * 1024 * 1024       # request body ceiling (attachments included)
CHUNK = 64 * 1024
CONNECT_TIMEOUT = 5.0
HANDSHAKE_LIMIT = 64 * 1024


# ── pure helpers ─────────────────────────────────────────────────────────────
def host_name(host_header: str | None) -> str:
    """The host part of a Host header: `a.b:30090` -> `a.b`, `[::1]:1` -> `[::1]`."""
    h = (host_header or "").strip()
    if not h:
        return ""
    if h.startswith("["):
        return h.split("]", 1)[0] + "]"
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def origin_allowed(origin: str | None, host_header: str | None) -> bool:
    """A browser names its origin on every POST and every WebSocket handshake. The
    interface is served by this relay, so the only legitimate origin is the relay's
    own host as the browser typed it. No header (curl, the cockpit's own probes) is
    fine: those requests still need the session cookie. `null` is never fine."""
    if origin is None:
        return True
    o = origin.strip().lower()
    host = (host_header or "").strip().lower()
    return bool(host) and o in (f"http://{host}", f"https://{host}")


def basic_auth(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode("ascii")


def read_credentials(path: Path) -> tuple[str, str] | None:
    """The env file the opencode unit reads (KEY=VALUE lines, optional quotes)."""
    try:
        text = Path(path).read_text()
    except OSError:
        return None
    kv = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        kv[k.strip()] = v.strip().strip('"').strip("'")
    pw = kv.get("OPENCODE_SERVER_PASSWORD", "")
    if not pw:
        return None
    return kv.get("OPENCODE_SERVER_USERNAME") or "opencode", pw


def forward_request_headers(items, upstream_host: str, auth: str,
                            websocket: bool = False) -> list[tuple[str, str]]:
    out = [(k, v) for k, v in items if k.lower() not in REQUEST_DROP]
    out.append(("Host", upstream_host))
    out.append(("Authorization", auth))
    if websocket:
        out.append(("Connection", "Upgrade"))
        out.append(("Upgrade", "websocket"))
    else:
        out.append(("Connection", "close"))
    return out


def forward_response_headers(items) -> list[tuple[str, str]]:
    out = []
    for k, v in items:
        kl = k.lower()
        if kl in RESPONSE_DROP:
            continue
        # a cookie named like the cockpit session would shadow the real one on this host
        if kl == "set-cookie" and v.strip().lower().startswith(SESSION_COOKIE + "="):
            continue
        out.append((k, v))
    return out


def frame_policy(host_header: str | None, cockpit_port: int) -> str:
    """Only the cockpit on the same host may frame the interface (clickjacking
    guard). The upstream policy has no frame-ancestors, so this one governs."""
    name = host_name(host_header) or "127.0.0.1"
    return f"frame-ancestors 'self' http://{name}:{cockpit_port}"


def split_upstream(url: str) -> tuple[str, int]:
    u = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
    return u.hostname or "127.0.0.1", u.port or 80


def tailscale_ipv4(run=subprocess.run) -> str | None:
    """The tailnet IPv4 of this box: the interface first (kernel state, no daemon
    socket needed), the CLI as a fallback."""
    for argv in (["ip", "-4", "-o", "addr", "show", "dev", "tailscale0"], ["tailscale", "ip", "-4"]):
        try:
            out = run(argv, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        m = re.search(r"\b(100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b", out or "")
        if m:
            return m.group(1)
    return None


def resolve_bind(spec: str, run=subprocess.run) -> str | None:
    """`tailscale` resolves to the tailnet address (None while it is not up);
    anything else is taken as an address literal."""
    spec = (spec or "").strip()
    if spec.lower() == "tailscale":
        return tailscale_ipv4(run)
    return spec or None


# ── configuration ────────────────────────────────────────────────────────────
@dataclass
class RelayConfig:
    upstream: tuple[str, int]                              # opencode serve, loopback
    credentials: Callable[[], tuple[str, str] | None]      # (user, password) or None
    is_authed: Callable[[str | None], bool]                # Cookie header -> valid session?
    cockpit_port: int                                      # for frame-ancestors and the sign-in hint

    @property
    def upstream_host(self) -> str:
        return f"{self.upstream[0]}:{self.upstream[1]}"


def health(cfg: RelayConfig, timeout: float = 3.0) -> dict:
    """GET /global/health on the upstream with the credentials: what the Agent
    panel shows as the server state."""
    creds = cfg.credentials()
    if not creds:
        return {"healthy": False, "error": "no credentials file"}
    conn = http.client.HTTPConnection(*cfg.upstream, timeout=timeout)
    try:
        conn.request("GET", "/global/health", headers={"Authorization": basic_auth(*creds)})
        resp = conn.getresponse()
        raw = resp.read(4096)
        if resp.status == 401:
            return {"healthy": False, "error": "credentials refused (restart opencode-web.service after editing the env file)"}
        if resp.status != 200:
            return {"healthy": False, "error": f"HTTP {resp.status}"}
        data = json.loads(raw or b"{}")
        return {"healthy": bool(data.get("healthy")), "version": data.get("version")}
    except (OSError, http.client.HTTPException, ValueError) as e:
        return {"healthy": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}
    finally:
        conn.close()


# ── the relay ────────────────────────────────────────────────────────────────
SIGN_IN_PAGE = """<!doctype html><meta charset="utf-8"><title>Spark Cockpit</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;background:#0e1014;color:#ece9e1;display:grid;place-items:center;min-height:100vh;margin:0}}
main{{max-width:44ch;padding:32px;text-align:center}}a{{color:#d9b45e}}</style>
<main><h1 style="font-size:18px">Cockpit session required</h1>
<p>This is the agent relay of the Spark Cockpit. Sign in to the cockpit at
<a href="{cockpit}">{cockpit}</a>, then come back to the Agent tab.</p></main>"""


def make_handler(cfg: RelayConfig) -> type[http.server.BaseHTTPRequestHandler]:
    class Relay(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "SparkCockpitRelay/" + RELAY_VERSION
        sys_version = ""

        def log_message(self, fmt, *args):   # quiet: a chatty relay hides the real journal lines
            pass

        # every method goes through the same gate and the same forwarder
        def do_GET(self):
            self.relay()

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = do_GET

        # ---- gate ----
        def relay(self):
            host = self.headers.get("Host")
            if not origin_allowed(self.headers.get("Origin"), host):
                return self.refuse(403, "origin not allowed")
            public = self.command == "GET" and self.path.split("?", 1)[0] in PUBLIC_PATHS
            if not public and not cfg.is_authed(self.headers.get("Cookie")):
                return self.refuse(401, "cockpit session required")
            creds = cfg.credentials()
            if not creds:
                return self.refuse(503, "agent credentials missing on the box (run dashboard/install-agent.sh)")
            auth = basic_auth(*creds)
            if self.headers.get("Upgrade", "").lower() == "websocket":
                return self.tunnel(auth)
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                return self.refuse(411, "length required")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self.refuse(400, "bad content length")
            if length < 0 or length > MAX_BODY:
                return self.refuse(413, "body too large")
            body = self.rfile.read(length) if length else b""
            self.forward(auth, body)

        def refuse(self, code: int, why: str):
            # never try to keep a connection whose body was not consumed
            self.close_connection = True
            wants_page = self.command == "GET" and "text/html" in (self.headers.get("Accept") or "")
            if wants_page and code == 401:
                cockpit = f"http://{host_name(self.headers.get('Host')) or '127.0.0.1'}:{cfg.cockpit_port}/"
                payload = SIGN_IN_PAGE.format(cockpit=cockpit).encode()
                ctype = "text/html; charset=utf-8"
            else:
                payload = json.dumps({"error": why}).encode()
                ctype = "application/json"
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.own_headers()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def own_headers(self):
            self.send_header("Content-Security-Policy", frame_policy(self.headers.get("Host"), cfg.cockpit_port))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")

        # ---- plain HTTP, streamed ----
        def forward(self, auth: str, body: bytes):
            conn = http.client.HTTPConnection(*cfg.upstream, timeout=CONNECT_TIMEOUT)
            try:
                conn.connect()
                # a long generation or an idle event stream must not time out here;
                # a vanished client shows up as a write error on our side instead
                conn.sock.settimeout(None)
                conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                for k, v in forward_request_headers(self.headers.items(), cfg.upstream_host, auth):
                    conn.putheader(k, v)
                if body or self.command in ("POST", "PUT", "PATCH"):
                    conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(body if body else None)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                return self.refuse(502, f"agent server unreachable ({type(e).__name__}); is opencode-web.service running?")
            try:
                self.send_response(resp.status, resp.reason)
                for k, v in forward_response_headers(resp.getheaders()):
                    self.send_header(k, v)
                self.own_headers()
                bodyless = self.command == "HEAD" or resp.status in (204, 304) or resp.status < 200
                if bodyless:
                    if self.command == "HEAD" and resp.getheader("Content-Length") is not None:
                        self.send_header("Content-Length", resp.getheader("Content-Length"))
                    self.end_headers()
                    return
                if resp.length is not None and not resp.chunked:
                    self.send_header("Content-Length", str(resp.length))
                    self.end_headers()
                    remaining = resp.length
                    while remaining > 0:
                        data = resp.read(min(CHUNK, remaining))
                        if not data:
                            break
                        self.wfile.write(data)
                        remaining -= len(data)
                    self.wfile.flush()
                    return
                # unknown length (server-sent events, chunked answers): re-chunk for a
                # 1.1 client, close-delimit for a 1.0 one; flush every piece so an
                # event reaches the browser the moment opencode emits it
                chunked = self.request_version >= "HTTP/1.1"
                if chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.close_connection = True
                self.end_headers()
                while True:
                    data = resp.read1(CHUNK)
                    if not data:
                        break
                    if chunked:
                        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
                    else:
                        self.wfile.write(data)
                    self.wfile.flush()
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
            except (OSError, http.client.HTTPException):
                # the browser went away (tab closed, stream cancelled) or opencode
                # dropped the connection: nothing to answer to anymore
                self.close_connection = True
            finally:
                conn.close()

        # ---- WebSocket (the terminal panel) ----
        def tunnel(self, auth: str):
            try:
                up = socket.create_connection(cfg.upstream, timeout=CONNECT_TIMEOUT)
            except OSError as e:
                return self.refuse(502, f"agent server unreachable ({type(e).__name__})")
            self.close_connection = True
            try:
                up.settimeout(CONNECT_TIMEOUT)
                lines = [f"{self.command} {self.path} HTTP/1.1"]
                lines += [f"{k}: {v}" for k, v in forward_request_headers(
                    self.headers.items(), cfg.upstream_host, auth, websocket=True)]
                up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
                head = b""
                while b"\r\n\r\n" not in head:
                    piece = up.recv(4096)
                    if not piece:
                        raise OSError("upstream closed during the handshake")
                    head += piece
                    if len(head) > HANDSHAKE_LIMIT:
                        raise OSError("handshake too large")
                head, rest = head.split(b"\r\n\r\n", 1)
                # the handshake answer goes back verbatim (101, or the refusal)
                self.wfile.write(head + b"\r\n\r\n")
                self.wfile.flush()
                if not head.startswith(b"HTTP/1.1 101"):
                    if rest:
                        self.wfile.write(rest)
                        self.wfile.flush()
                    return
                up.settimeout(None)
                client = self.connection
                if rest:
                    client.sendall(rest)
                pump(client, up)
            except OSError:
                pass
            finally:
                up.close()

    return Relay


def pump(a: socket.socket, b: socket.socket):
    """Bytes both ways until either side closes."""
    peer = {a: b, b: a}
    while True:
        try:
            ready, _, broken = select.select([a, b], [], [a, b])
        except (OSError, ValueError):
            return
        if broken:
            return
        for s in ready:
            try:
                data = s.recv(CHUNK)
            except OSError:
                return
            if not data:
                return
            try:
                peer[s].sendall(data)
            except OSError:
                return


class RelayServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def serve(cfg: RelayConfig, bind: str, port: int) -> RelayServer:
    """Bind now (raises OSError if the address is not ours yet); the caller runs
    serve_forever in its own thread."""
    return RelayServer((bind, port), make_handler(cfg))


if __name__ == "__main__":   # dev: python3 agent_relay.py 127.0.0.1 30091 (no session check)
    import sys
    cfg = RelayConfig(upstream=split_upstream(sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:4096"),
                      credentials=lambda: read_credentials(Path.home() / ".config/qwen38/opencode-web.env"),
                      is_authed=lambda cookie: True, cockpit_port=30090)
    srv = serve(cfg, sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1", int(sys.argv[2]) if len(sys.argv) > 2 else 30091)
    print(f"relay (UNAUTHENTICATED dev mode) on {srv.server_address} -> {cfg.upstream_host}", flush=True)
    srv.serve_forever()
