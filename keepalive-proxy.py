#!/usr/bin/env python3
"""Keepalive proxy in front of SGLang (v6.9). No content logging, no rewriting.

Three roles, nothing else:
1. fill the silences of the SSE stream (SGLang's tool-call parser buffers the
   arguments: 127 s of measured silence for a 400-line file write) by injecting
   the OFFICIAL Anthropic "ping" event every KEEPALIVE_S seconds on the
   Anthropic dialect (an SSE comment ": keepalive" KILLS Claude-family parsers:
   measured 2026-08-23, client death 10-20 s after the first comment) and an
   authentic empty chunk on the OpenAI dialect (opencode/AI SDK stall detectors
   ignore comments and drop the stream after ~140-180 s without a real chunk);
2. close the upstream as soon as the client goes away AND explicitly POST
   /abort_request for the request id (v6.7: closing the socket alone left
   orphan generations decoding to max_tokens, measured 29/08), so SGLang aborts the
   generation instead of running a zombie;
3. never lull a client on a dead upstream: past MAX_SILENCE_S an EXPLICIT SSE
   error event is sent, then the stream is closed. MAX_SILENCE_S must stay
   ABOVE the worst legitimate prefill (40 min measured for 690K tokens on a
   cold cache), hence the 3600 s default.

v6.9: an engine that does not answer (stopped, crashed, restarting, loading) gets a 503
      engine_unavailable with Retry-After on EVERY path, never a false context_too_long:
      on 30/08 the size fallback refused a 68k-token request as "~409k tokens" while the
      engine was restarting after a GPU fault. Unknown request shapes still refuse on size.
v6.8: oversize guard counts with the engine tokenizer (size only nominates), 8 percent margin,
      absolute per-lane prompt ceiling (PROMPT_CEILING_TOKENS).
v6.7: abort_request on client loss. v6.6: upstream reads via read1() and TCP_NODELAY on the client side.
resp.read(8192) on chunked HTTP BLOCKS until 8 KB (~30 SSE events) accumulate
before relaying them at once: measured 2026-08-23, median inter-event gap 0 ms
/ max 1307 ms through the proxy vs a steady 118 ms direct. read1() returns as
soon as bytes are available, so the stream stays token by token.
"""
import json, os, queue, socket, sys, threading, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class EngineUnreachable(Exception):
    """The engine did not answer /tokenize: stopped, crashed, restarting or still loading."""

UPSTREAM      = os.environ.get("UPSTREAM", "http://127.0.0.1:30000")
KEEPALIVE_S   = float(os.environ.get("KEEPALIVE_S", "10"))
MAX_SILENCE_S = float(os.environ.get("MAX_SILENCE_S", "3600"))
CLIENT_IO_S   = float(os.environ.get("CLIENT_IO_S", "900"))   # write blocked toward a frozen client
# Oversize guard (v6.7): a prompt longer than the engine's KV pool is not rejected by
# this SGLang build, it wedges the scheduler (measured 29/08). The proxy learns the
# pool size from /get_server_info once the upstream is healthy and refuses, with a
# clear 400, bodies whose most optimistic token estimate still exceeds it.
CHARS_PER_TOKEN_MIN = float(os.environ.get("CHARS_PER_TOKEN_MIN", "2.5"))
# Usable share of the pool for one prompt: the rest is room for the answer and the
# scheduler's own buffers (README: with a 178,560-token pool a single prompt tops out
# near 165K, that is 92 percent).
OVERSIZE_MARGIN_FRAC = float(os.environ.get("OVERSIZE_MARGIN_FRAC", "0.08"))
# Absolute ceiling for one prompt, in tokens (0 = none). The KV pool is not the only limit:
# on the flash lane the prefill of a long prompt grows the engine's footprint by ~0.27 GiB
# per 1k tokens beyond ~90k (measured 29/08), so the ceiling that keeps the box away from
# the memory edge is a token count set per lane by install.sh, not a share of the pool.
PROMPT_CEILING_TOKENS = int(os.environ.get("PROMPT_CEILING_TOKENS", "0") or 0)


def prompt_limit(pool):
    """Usable prompt tokens: the pool share, capped by the absolute ceiling when set."""
    limit = int(pool * (1.0 - OVERSIZE_MARGIN_FRAC))
    if PROMPT_CEILING_TOKENS > 0:
        limit = min(limit, PROMPT_CEILING_TOKENS)
    return limit


MEDIA_BLOCKS = ("image", "image_url", "input_audio", "video_url", "document", "audio_url")
TOKENS_PER_MEDIA = int(os.environ.get("TOKENS_PER_MEDIA", "4096"))   # generous per image/audio part


def _anthropic_as_openai(j, media):
    """Anthropic /v1/messages body -> OpenAI-shaped messages for /tokenize.
    Text blocks are kept, media blocks are counted in media[0] (their base64 is
    not prompt text), other blocks (tool_use, tool_result) go through their JSON,
    close enough for a guard that keeps an 8 percent margin."""
    def flat(content):
        if isinstance(content, str):
            return content
        parts = []
        for b in content or []:
            if not isinstance(b, dict):
                parts.append(str(b))
            elif b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            elif b.get("type") in MEDIA_BLOCKS:
                media[0] += 1          # counted as a fixed token budget, never as base64 text
            else:
                parts.append(json.dumps(b, ensure_ascii=False))
        return "\n".join(parts)
    msgs = []
    if j.get("system"):
        msgs.append({"role": "system", "content": flat(j["system"])})
    for m in j.get("messages") or []:
        role = m.get("role") if m.get("role") in ("user", "assistant", "system") else "user"
        msgs.append({"role": role, "content": flat(m.get("content"))})
    return msgs


def _strip_media(messages, media):
    """OpenAI messages with content parts: keep text parts, count media parts."""
    out = []
    for m in messages:
        if not isinstance(m, dict) or not isinstance(m.get("content"), list):
            out.append(m)
            continue
        parts = []
        for part in m["content"]:
            if isinstance(part, dict) and part.get("type") in MEDIA_BLOCKS:
                media[0] += 1
            else:
                parts.append(part)
        out.append({**m, "content": parts or ""})
    return out


def tokenize_count(body, path):
    """Exact prompt length from the engine's /tokenize endpoint (chat template
    applied to messages). None when nothing exact is possible for THIS body
    (malformed, unknown shape, rejected by the engine with a 4xx): the caller
    then refuses on size. Raises EngineUnreachable when the engine itself does
    not answer (connection refused, reset, timeout, 5xx): that is not a size
    problem and the caller must say so instead of refusing."""
    try:
        j = json.loads(body)
        if not isinstance(j, dict):
            return None
        req = {"model": j.get("model") or "default"}
        media = [0]
        if path.startswith("/v1/messages"):
            req["messages"] = _anthropic_as_openai(j, media)
        elif isinstance(j.get("messages"), list):
            req["messages"] = _strip_media(j["messages"], media)
            if j.get("tools"):
                req["tools"] = j["tools"]
        elif isinstance(j.get("prompt"), (str, list)):
            req["prompt"] = j["prompt"]
        else:
            return None
        payload = json.dumps(req).encode()
        key = open(os.path.expanduser("~/.config/qwen38/api-key")).read().strip()
    except Exception:
        return None
    try:
        r = urllib.request.Request(UPSTREAM + "/tokenize", data=payload,
                                   headers={"Authorization": f"Bearer {key}",
                                            "Content-Type": "application/json"})
        raw = urllib.request.urlopen(r, timeout=20).read()
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            raise EngineUnreachable(f"/tokenize answered HTTP {e.code}") from e
        return None                       # 4xx: this body cannot be counted, size decides
    except (urllib.error.URLError, OSError) as e:   # refused, reset, timeout: no engine there
        raise EngineUnreachable(str(getattr(e, "reason", None) or e)) from e
    except Exception:
        return None
    try:
        n = int(json.loads(raw.decode()).get("count", -1))
    except Exception:
        return None
    return n + media[0] * TOKENS_PER_MEDIA if n >= 0 else None


_POOL = {"tokens": None, "ts": 0.0}


def pool_tokens():
    if _POOL["tokens"] and time.time() - _POOL["ts"] < 600:
        return _POOL["tokens"]
    try:
        key = open(os.path.expanduser("~/.config/qwen38/api-key")).read().strip()
        req = urllib.request.Request(UPSTREAM + "/get_server_info", headers={"Authorization": f"Bearer {key}"})
        info = json.loads(urllib.request.urlopen(req, timeout=4).read().decode())
        n = int(info.get("max_total_num_tokens") or 0)
        if n > 0:
            _POOL.update(tokens=n, ts=time.time())
    except Exception:
        pass
    return _POOL["tokens"]
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

    def _unavailable(self, exc):
        """The engine is not there (stopped, crashed, restarting, loading): say exactly that,
        503 with Retry-After, never a size refusal (30/08: a restarting engine made the size
        fallback call a 68k-token request "~409k tokens")."""
        log(f"engine unreachable: {exc}")
        msg = (f"keepalive-proxy: the engine behind {UPSTREAM} is not answering ({exc}). It is stopped, "
               f"restarting or still loading (a restart takes minutes, about 9 on a DGX Spark); this "
               f"request was NOT refused for its size. Retry it unchanged once GET {UPSTREAM}/health "
               f"answers 200.")
        body = json.dumps({"type": "error", "error": {"type": "engine_unavailable", "message": msg}}).encode()
        try: self._plain(503, {"Content-Type": "application/json", "Retry-After": "30"}, body)
        except Exception: pass

    def _upstream_error(self, herr):
        """Relay the engine's own error, except its 5xx: SGLang answers 503 with an empty
        body while starting or shutting down, which a client cannot read. Say it instead."""
        raw = herr.read()
        try: herr.close()
        except Exception: pass
        if herr.code in (502, 503, 504):
            self._unavailable(f"it answered HTTP {herr.code}, as it does while starting or shutting down")
            self._done(f"503 engine unreachable (upstream {herr.code})"); return
        try: self._plain(herr.code, dict(herr.headers), raw)
        except Exception: pass
        self._done(f"{herr.code} upstream")

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

    def _note_rid(self, rec: bytes):
        """First SSE event carries the request id in both dialects."""
        if getattr(self, "_rid", None) is not None:
            return
        try:
            for line in rec.split(b"\n"):
                if line.startswith(b"data:"):
                    j = json.loads(line[5:].strip() or b"{}")
                    rid = j.get("id") or (j.get("message") or {}).get("id")
                    if rid and rid != "keepalive":
                        self._rid = rid
                    return
        except Exception:
            return

    def _abort_upstream(self, why: str):
        """Tell SGLang to stop generating for a client that is gone."""
        rid = getattr(self, "_rid", None)
        if not rid:
            return
        def go():
            try:
                hdrs = {"Content-Type": "application/json"}
                auth = self.headers.get("Authorization")
                if auth: hdrs["Authorization"] = auth
                req = urllib.request.Request(UPSTREAM + "/abort_request",
                                             json.dumps({"rid": rid}).encode(), hdrs)
                urllib.request.urlopen(req, timeout=5).read()
                log(f"aborted upstream rid={rid} ({why})")
            except Exception as e:
                log(f"abort_request failed for rid={rid}: {e}")
        threading.Thread(target=go, daemon=True).start()

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

        def drop_upstream(why="client gone"):
            self._abort_upstream(why)
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
                    drop_upstream("upstream silent")
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
            self._note_rid(val)
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
        if body and self.path.startswith("/v1/") and len(body) > 200_000:
            pool = pool_tokens()
            est = len(body) / CHARS_PER_TOKEN_MIN     # optimistic: fewest tokens the body could be
            if pool and est > prompt_limit(pool):
                # v6.8: the size estimate only nominates; the engine's tokenizer decides
                # (a 140k-token English prompt is 479 KB, which the 2.5 chars/token bound
                # called 192k tokens and refused although the pool served it).
                limit = prompt_limit(pool)
                try:
                    count = tokenize_count(body, self.path)
                except EngineUnreachable as e:
                    self._unavailable(e); self._done("503 engine unreachable"); return
                if count is None:
                    reason = (f"at least ~{int(est)} tokens by size (a shape the engine's tokenizer "
                              f"cannot count, so the size decides)")
                elif count > limit:
                    reason = f"{count} prompt tokens (counted by the engine)"
                else:
                    reason = None
                    log(f"{self._peer} oversize check: {count} tokens fit ({limit} usable of pool {pool})")
                if reason:
                    ceil = f", one-prompt ceiling {PROMPT_CEILING_TOKENS}" if PROMPT_CEILING_TOKENS > 0 else ""
                    msg = (f"keepalive-proxy: this request is {reason}; this lane serves at most {limit} "
                           f"prompt tokens (KV pool {pool} tokens{ceil}) and the engine would hang instead "
                           f"of refusing it. The engine itself is up: shorten the context (compaction) "
                           f"or serve a larger pool.")
                    log(f"{self._peer} REFUSED oversize ({len(body)}b, {reason}, limit {limit})")
                    self._plain(400, {"Content-Type": "application/json"},
                                json.dumps({"error": {"type": "context_too_long", "message": msg}}).encode())
                    self._done("400 oversize refused"); return
        resp, herr, cerr = self._open(body)
        if cerr is not None: self._unavailable(cerr); self._done("503 engine unreachable"); return
        if herr is not None: self._upstream_error(herr); return
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
        if cerr is not None: self._unavailable(cerr); self._done("503 engine unreachable"); return
        if herr is not None: self._upstream_error(herr); return
        try: data = resp.read()
        finally:
            try: resp.close()
            except Exception: pass
        self._plain(resp.status, dict(resp.headers), data)
        self._done("ok get")

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 30001
    log(f"v6.9 on :{port} -> {UPSTREAM} (keepalive {KEEPALIVE_S:.0f}s, max silence {MAX_SILENCE_S:.0f}s)")
    Server(("0.0.0.0", port), H).serve_forever()
