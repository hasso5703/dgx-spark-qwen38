#!/usr/bin/env python3
"""Keepalive proxy in front of SGLang (v6.6). No content logging, no rewriting.

Three roles, nothing else:
1. fill the silences of the SSE stream (SGLang's tool-call parser buffers the
   arguments: 127 s of measured silence for a 400-line file write) by injecting
   the OFFICIAL Anthropic "ping" event every KEEPALIVE_S seconds on the
   Anthropic dialect (an SSE comment ": keepalive" KILLS Claude-family parsers:
   measured 2026-08-23, client death 10-20 s after the first comment) and an
   authentic empty chunk on the OpenAI dialect (opencode/AI SDK stall detectors
   ignore comments and drop the stream after ~140-180 s without a real chunk);
2. close the upstream as soon as the client goes away, so SGLang aborts the
   generation instead of running a zombie;
3. never lull a client on a dead upstream: past MAX_SILENCE_S an EXPLICIT SSE
   error event is sent, then the stream is closed. MAX_SILENCE_S must stay
   ABOVE the worst legitimate prefill (40 min measured for 690K tokens on a
   cold cache), hence the 3600 s default.

v6.6: upstream reads via read1() and TCP_NODELAY on the client side.
resp.read(8192) on chunked HTTP BLOCKS until 8 KB (~30 SSE events) accumulate
before relaying them at once: measured 2026-08-23, median inter-event gap 0 ms
/ max 1307 ms through the proxy vs a steady 118 ms direct. read1() returns as
soon as bytes are available, so the stream stays token by token.
"""
import json, os, queue, socket, sys, threading, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM      = os.environ.get("UPSTREAM", "http://127.0.0.1:30000")
KEEPALIVE_S   = float(os.environ.get("KEEPALIVE_S", "10"))
MAX_SILENCE_S = float(os.environ.get("MAX_SILENCE_S", "3600"))
CLIENT_IO_S   = float(os.environ.get("CLIENT_IO_S", "900"))   # write blocked toward a frozen client
HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding"}

def log(msg):
    sys.stderr.write(f"[proxy] {msg}\n"); sys.stderr.flush()

def sse_error(msg):
    data = json.dumps({"type": "error",
                       "error": {"type": "api_error", "message": f"keepalive-proxy: {msg}"}})
    return b"event: error\ndata: " + data.encode() + b"\n\n"

class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def handle_one_request(self):
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, BrokenPipeError, socket.timeout, TimeoutError):
            self.close_connection = True

    def setup(self):
        BaseHTTPRequestHandler.setup(self)
        # a write blocked for CLIENT_IO_S (frozen client, laptop asleep) raises
        # socket.timeout instead of parking the thread forever
        self.connection.settimeout(CLIENT_IO_S)
        # small frequent SSE events: without NODELAY, Nagle batches them (Tailscale)
        try: self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError: pass

    # ---- upstream ----------------------------------------------------------
    def _hdrs(self):
        return {k: v for k, v in self.headers.items() if k.lower() not in HOP}

    def _open(self, body):
        req = urllib.request.Request(UPSTREAM + self.path, data=body,
                                     headers=self._hdrs(), method=self.command)
        try:
            return urllib.request.urlopen(req, timeout=None), None, None
        except urllib.error.HTTPError as e:
            return None, e, None
        except (urllib.error.URLError, OSError) as e:
            return None, None, e

    # ---- client side -------------------------------------------------------
    def _plain(self, status, headers, body):
        self.close_connection = True
        self.send_response(status)
        for k, v in headers.items():
            if k.lower() not in HOP: self.send_header(k, v)
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bad_gateway(self, exc):
        log(f"upstream unreachable: {exc}")
        body = json.dumps({"type": "error", "error": {"type": "api_error",
                "message": f"keepalive-proxy: upstream {UPSTREAM} unreachable ({exc})"}}).encode()
        try: self._plain(502, {"Content-Type": "application/json"}, body)
        except Exception: pass

    def _begin(self, status, headers):
        self.close_connection = True
        self.send_response(status)
        for k, v in headers.items():
            if k.lower() not in HOP: self.send_header(k, v)
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _chunk(self, b):
        if b:
            self.wfile.write(b"%x\r\n" % len(b) + b + b"\r\n"); self.wfile.flush()

    def _finish(self):
        try: self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
        except Exception: pass
        try: self.connection.shutdown(socket.SHUT_WR)   # clean EOF toward the client
        except Exception: pass

    # ---- relay -------------------------------------------------------------
    def _done(self, outcome):
        st = ""
        if getattr(self, "_bytes", 0) or getattr(self, "_first", None) is not None:
            f = f"{self._first:.1f}s" if self._first is not None else "never"
            l = f"{self._last:.1f}s" if getattr(self, "_last", None) is not None else "never"
            st = f" [{self._bytes}b relayed, first event at {f}, last at {l}]"
        log(f"{getattr(self, '_peer', '?')} {self.command} {self.path.split('?')[0]} {outcome} in {time.time()-self._t0:.1f}s{st}")

    def _relay(self, resp):
        sse = "text/event-stream" in (resp.headers.get("Content-Type") or "")
        self._begin(resp.status, resp.headers)
        if not sse:
            try:
                while True:
                    c = resp.read(65536)
                    if not c: break
                    self._chunk(c)
            except Exception:
                pass
            finally:
                try: resp.close()
                except Exception: pass
            self._finish(); self._done("ok non-sse"); return

        q = queue.Queue(maxsize=1024)
        stop = threading.Event()
        def pump():
            # only COMPLETE SSE events are forwarded (boundary \n\n): a
            # keepalive can therefore never land in the middle of an event
            # (v6.4 bug: keepalive chunk injected mid-JSON-line, corrupt stream)
            buf = b""
            def put(rec):
                while not stop.is_set():
                    try: q.put(("d", rec), timeout=2); return True
                    except queue.Full: continue
                return False
            try:
                while not stop.is_set():
                    # read1: returns as soon as bytes exist, where read(8192)
                    # blocks until 8 KB accumulate (bursty stream)
                    c = resp.read1(8192)
                    if not c: break
                    buf += c
                    while True:
                        i = buf.find(b"\n\n")
                        if i < 0: break
                        rec = buf[:i+2]; buf = buf[i+2:]
                        if not put(rec): break
            except Exception as e:
                if not stop.is_set():
                    try: q.put(("e", str(e).encode()), timeout=5)
                    except Exception: pass
            finally:
                if buf and not stop.is_set():
                    try: q.put(("d", buf), timeout=2)
                    except Exception: pass
                while True:
                    try: q.put(("f", b""), timeout=2); break
                    except queue.Full:
                        if stop.is_set(): break
        threading.Thread(target=pump, daemon=True).start()

        def drop_upstream():
            stop.set()
            try: resp.close()
            except Exception: pass

        anthropic = self.path.startswith("/v1/messages")
        # anthropic dialect: official ping event. openai dialect: an AUTHENTIC
        # empty chunk (choices: []), because client stall detectors
        # (opencode/AI SDK, deaths measured at ~140-180 s) ignore comments
        ka_bytes = (b'event: ping\ndata: {"type": "ping"}\n\n' if anthropic
                    else b'data: {"id":"keepalive","object":"chat.completion.chunk","created":0,"model":"keepalive","choices":[]}\n\n')
        silence = 0.0
        while True:
            try:
                kind, val = q.get(timeout=KEEPALIVE_S)
                silence = 0.0
            except queue.Empty:
                silence += KEEPALIVE_S
                if silence >= MAX_SILENCE_S:
                    log(f"upstream silent for {silence:.0f}s, dropping the request")
                    drop_upstream()
                    if anthropic:
                        try: self._chunk(sse_error(f"upstream silent for {silence:.0f}s, request dropped"))
                        except Exception: pass
                    self._finish(); self._done("DROPPED upstream silent"); return
                try: self._chunk(ka_bytes)
                except Exception:
                    drop_upstream(); self._done("CLIENT GONE during keepalive"); return
                continue
            if kind == "f":
                break
            if kind == "e":
                log(f"upstream cut mid-stream: {val.decode(errors='replace')[:120]}")
                if anthropic:
                    try: self._chunk(sse_error("upstream stream interrupted, retry"))
                    except Exception: pass
                break
            now = time.time() - self._t0
            if self._first is None: self._first = now
            self._last = now; self._bytes += len(val)
            try: self._chunk(val)
            except Exception:
                drop_upstream(); self._done("CLIENT GONE on write"); return
        self._finish()
        self._done("ok" if kind == "f" else "UPSTREAM CUT")

    # ---- verbs -------------------------------------------------------------
    def _handle(self, with_body):
        self._t0 = time.time()
        self._peer = f"{self.client_address[0]}:{self.client_address[1]}"
        self._bytes = 0; self._first = None; self._last = None
        n0 = self.headers.get("Content-Length") or "0"
        log(f"{self._peer} -> {self.command} {self.path.split('?')[0]} body={n0}b")
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if (with_body and n) else None
        resp, herr, cerr = self._open(body)
        if cerr is not None: self._bad_gateway(cerr); self._done("502 upstream unreachable"); return
        if herr is not None:
            raw = herr.read()
            try: herr.close()
            except Exception: pass
            try: self._plain(herr.code, dict(herr.headers), raw)
            except Exception: pass
            self._done(f"{herr.code} upstream"); return
        self._relay(resp)

    def do_POST(self):   self._handle(True)
    def do_PUT(self):    self._handle(True)
    def do_PATCH(self):  self._handle(True)
    def do_DELETE(self): self._handle(True)

    def do_GET(self):
        self._t0 = time.time()
        self._peer = f"{self.client_address[0]}:{self.client_address[1]}"
        self._bytes = 0; self._first = None; self._last = None
        resp, herr, cerr = self._open(None)
        if cerr is not None: self._bad_gateway(cerr); self._done("502 upstream unreachable"); return
        if herr is not None:
            raw = herr.read()
            try: herr.close()
            except Exception: pass
            try: self._plain(herr.code, dict(herr.headers), raw)
            except Exception: pass
            self._done(f"{herr.code} upstream"); return
        try: data = resp.read()
        finally:
            try: resp.close()
            except Exception: pass
        self._plain(resp.status, dict(resp.headers), data)
        self._done("ok get")

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 30001
    log(f"v6.6 on :{port} -> {UPSTREAM} (keepalive {KEEPALIVE_S:.0f}s, max silence {MAX_SILENCE_S:.0f}s)")
    Server(("0.0.0.0", port), H).serve_forever()
