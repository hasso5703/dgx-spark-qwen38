#!/usr/bin/env python3
"""Keepalive proxy in front of SGLang (v6.14). No content logging, and the only
rewriting is the tool-schema guard (role 4).

Four roles, nothing else:
1. fill the silences of the SSE stream (SGLang's tool-call parser buffers the
   arguments: 127 s of measured silence for a 400-line file write) by injecting
   the OFFICIAL Anthropic "ping" event every KEEPALIVE_S seconds on the
   Anthropic dialect (an SSE comment ": keepalive" KILLS Claude-family parsers:
   measured 2026-08-23, client death 10-20 s after the first comment) and an
   authentic empty chunk on the OpenAI dialect (opencode/AI SDK stall detectors
   ignore comments and drop the stream after ~140-180 s without a real chunk);
2. never leave a generation running for a client that is gone: the proxy names every
   request itself (x-override-rid), POSTs /abort_request BEFORE it closes the upstream
   socket, and drains the answer when the engine offers no rid to abort with;
3. never lull a client on a dead upstream: past MAX_SILENCE_S an EXPLICIT SSE
   error event is sent, then the stream is closed. MAX_SILENCE_S must stay
   ABOVE the worst legitimate prefill (40 min measured for 690K tokens on a
   cold cache), hence the 3600 s default;
4. keep one tool schema from killing a whole session: the 'pattern' values that
   Python's re cannot compile are dropped from tool parameter schemas, because the
   engine validates them with a Python regex and 400s the request otherwise. Nothing
   else in the body is ever touched.

v6.14: an abandoned request no longer becomes a zombie. Three holes, all measured here on
      2026-09-09 (6,582 flood lines in one day, one request decoding 6 minutes for nobody):
      a client that gave up during prefill left a request the proxy could not name, because
      it learned the rid from the first SSE event; the Anthropic dialect's id (msg_<uuid>)
      was sent to /abort_request although it names nothing the engine knows; and the abort
      was fired in a thread that raced the socket close, which is what deletes the state
      the abort needs (sglang #35255, in main since 2026-09-04 and in neither image this
      repo serves). The proxy now imposes the rid with x-override-rid on the routes that
      honour it, aborts synchronously before closing, and on /v1/messages, where the
      engine mints its own id and applies no header overrides, drains the answer to its
      end instead of orphaning it.
v6.13: Claude Code's Artifact tool carries an ECMA-262 'pattern' with Unicode property
      escapes ([^\\p{Cc}\\p{Cf}\\p{Zl}\\p{Zp}...]). SGLang validates every tool schema with
      jsonschema, whose 'regex' format check compiles patterns with Python's re, which
      raises "bad escape \\p": the engine answered 400 to EVERY request of the session
      (measured 09/09 against Claude Code v2.1.266, sglang serving_chat.py:868). The
      proxy drops the patterns Python cannot compile and forwards the rest untouched.

v6.12: the tripwire trips at 128 marker characters (48 is a plausible banner line in a
      code block; 128 is not something a model writes, and real corruption runs to
      max_tokens), and the OpenAI dialect gets "data: [DONE]" after the error event so a
      strict client sees a terminated stream, not a truncated one.
v6.11: a corruption tripwire. A decode kernel that loses its state on this hardware
      emits runs of token id 0, which is "!" in the Qwen tokenizer (sglang#36537,
      #36558, #36806, #36845). The proxy counts consecutive marker characters across
      the stream and, past CORRUPTION_RUN of them, aborts the generation upstream and
      sends the client an explicit error instead of a wall of exclamation marks. It
      reads only the delta text it already relays; nothing is logged or rewritten.
v6.10: the cached KV pool is dropped whenever the engine proves unreachable or a
      refresh read fails. It was cached for 600 s and invalidated nowhere, so after
      an engine restart the oversize guard enforced the previous engine's limit for
      up to ten minutes; the pool is a boot lottery and differs by lane (about 863k
      on 27B against 184k on flash), so a prompt accepted against a stale larger
      pool could be relayed to a smaller engine and wedge its scheduler.
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
import json, os, queue, re, socket, sys, threading, time, urllib.request, urllib.error, uuid
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
# Corruption tripwire: consecutive "!" (token id 0) that mean the decode path lost
# its state rather than the model writing prose. 0 disables the guard.
CORRUPTION_RUN = int(os.environ.get("CORRUPTION_RUN", "128") or 0)
CORRUPTION_MARK = "!"


# Tool-schema guard (v6.13): SGLang validates every tool's parameter schema with
# jsonschema (serving_chat.py: Draft202012Validator.check_schema), whose 'regex' format
# check compiles 'pattern' with Python's re. JSON Schema says 'pattern' is ECMA-262,
# which has Unicode property escapes and named groups; Python's re has neither, so ONE
# such tool makes the engine answer 400 to every request of the session. A 'pattern'
# only constrains what the model may write into an argument, so dropping the ones Python
# cannot compile costs the caller nothing and keeps the lane usable.
UNPYTHONIC_PATTERN_MARKS = (rb"\\p{", rb"\\P{", rb"(?<")
_pattern_drop_logged = set()


def _prune_patterns(node, dropped, depth=0):
    """Drop, in place, every 'pattern' Python's re refuses. Depth-bounded: a cyclic
    or absurdly nested schema must not take the proxy down with a RecursionError."""
    if depth > 48:
        return
    if isinstance(node, dict):
        pat = node.get("pattern")
        if isinstance(pat, str):
            try:
                re.compile(pat)
            except (re.error, RecursionError):
                node.pop("pattern", None)
                dropped.append(pat)
        for value in node.values():
            _prune_patterns(value, dropped, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _prune_patterns(value, dropped, depth + 1)


def _tool_param_schemas(j):
    """The tool parameter schemas of a body: Anthropic 'input_schema' and OpenAI
    'function.parameters', at request level and inside messages (the engine validates
    message-level tools too). Nothing outside these subtrees is ever visited."""
    holders = [j] + [m for m in (j.get("messages") or []) if isinstance(m, dict)]
    for holder in holders:
        tools = holder.get("tools")
        if not isinstance(tools, list):
            continue
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function")
            for schema in (tool.get("input_schema"),
                           fn.get("parameters") if isinstance(fn, dict) else None):
                if isinstance(schema, dict):
                    yield schema


def sanitize_tool_schemas(body, path):
    """The body to forward, and the patterns dropped from it. A body whose raw bytes
    carry no construct Python's re rejects is passed through untouched, without even
    being parsed: the hot path pays one substring scan."""
    if not body or not path.startswith("/v1/"):
        return body, []
    if not any(mark in body for mark in UNPYTHONIC_PATTERN_MARKS):
        return body, []
    try:
        j = json.loads(body)
    except Exception:
        return body, []                 # not JSON we can fix: let the engine decide
    if not isinstance(j, dict):
        return body, []
    dropped = []
    for schema in _tool_param_schemas(j):
        _prune_patterns(schema, dropped)
    if not dropped:
        return body, []
    return json.dumps(j).encode(), dropped


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


def invalidate_pool():
    """Forget the cached pool, because the engine that reported it is gone.

    The pool is a boot lottery (measured 863,398 / 893,479 / 913,334 for one
    checkpoint) and changes outright between lanes (about 863k on the 27B lane,
    184k on flash). Caching it for 600 s without ever dropping it meant that
    after an engine restart the guard kept enforcing the previous engine's
    limit: a prompt sized against a larger stale pool would be relayed to a
    smaller one and wedge the scheduler, which is the exact failure this guard
    exists to prevent. Anything proving the engine is not the one we measured
    drops the cache.
    """
    _POOL.update(tokens=None, ts=0.0)


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
        # A read that fails is itself evidence the engine moved: never keep
        # serving a limit measured on an engine that no longer answers.
        invalidate_pool()
    return _POOL["tokens"]
HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding"}

# Abort contract (v6.14). SGLang only aborts a request it can still FIND: abort_request()
# returns early when the rid is no longer in TokenizerManager.rid_to_state, and a client
# disconnect deletes that entry first (generate_request's "except BaseException" ->
# _discard_pending_req_states), so the AbortReq never reaches the scheduler and the
# request keeps decoding with nobody listening (sglang #35255, merged upstream
# 2026-09-04; neither image this repo serves carries it). Two consequences for the proxy:
#   - it must know the rid BEFORE the first SSE event, or a client that gives up during
#     prefill leaves a request nobody can name (measured here 2026-09-09: 4,470 flood
#     lines for one rid, ~6 min of dead decode);
#   - it must send the abort BEFORE it closes the upstream socket, or it races the
#     deletion it is trying to beat.
# The engine takes the rid from the caller on the routes below (x-override-rid,
# request_headers.py). The Anthropic route builds its own request object and never
# applies header overrides, so on /v1/messages the id the client sees (msg_<uuid>) is
# NOT the engine's rid: there is nothing to abort with, and the proxy drains instead.
RID_OVERRIDE_ROUTES = ("/v1/chat/completions", "/generate")
ABORT_TIMEOUT_S = float(os.environ.get("ABORT_TIMEOUT_S", "5"))
# Ceiling on draining an abandoned generation that cannot be aborted. Reading it to its
# natural end costs the same decode the zombie would have cost anyway, and saves the
# flood: an engine writing into a socket nobody reads is silent, an engine whose state
# was deleted logs one line per output batch.
DRAIN_MAX_S = float(os.environ.get("DRAIN_MAX_S", "900"))
_rid_override_honoured = None       # None = never observed, True/False = what the engine did

def log(msg):
    sys.stderr.write(f"[proxy] {msg}\n"); sys.stderr.flush()

def delta_text(j):
    """The visible text of one SSE event, in either dialect. Never the arguments of a
    tool call: a JSON blob of exclamation marks is not what this guard is about."""
    try:
        ch = (j.get("choices") or [{}])[0]
        d = ch.get("delta") or {}
        for k in ("content", "reasoning_content"):
            v = d.get(k)
            if isinstance(v, str) and v:
                return v
    except Exception:
        pass
    d = j.get("delta")
    if isinstance(d, dict):
        for k in ("text", "thinking"):
            v = d.get(k)
            if isinstance(v, str) and v:
                return v
    return ""


def marker_run(text, run):
    """Extend a run of marker characters across event boundaries."""
    if not text:
        return run
    stripped = text.rstrip(CORRUPTION_MARK)
    tail = len(text) - len(stripped)
    return (run + tail) if not stripped else tail


def sse_error_openai(msg):
    data = json.dumps({"error": {"type": "corrupted_output", "code": "corrupted_output",
                                 "message": f"keepalive-proxy: {msg}"}})
    return b"data: " + data.encode() + b"\n\n"


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
    def _forced_rid(self):
        """The rid this proxy imposes on the engine, so an abandoned request has a name
        before it has produced anything. A caller that sets the header itself keeps it."""
        if getattr(self, "_frid", "unset") != "unset":
            return self._frid
        if self.path.split("?")[0] in RID_OVERRIDE_ROUTES:
            self._frid = self.headers.get("x-override-rid") or uuid.uuid4().hex
        else:
            self._frid = None
        return self._frid

    def _hdrs(self):
        h = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        rid = self._forced_rid()
        if rid:
            h["X-Override-Rid"] = rid
        return h

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
        self._ended = True
        st = ""
        if getattr(self, "_bytes", 0) or getattr(self, "_first", None) is not None:
            f = f"{self._first:.1f}s" if self._first is not None else "never"
            l = f"{self._last:.1f}s" if getattr(self, "_last", None) is not None else "never"
            st = f" [{self._bytes}b relayed, first event at {f}, last at {l}]"
        log(f"{getattr(self, '_peer', '?')} {self.command} {self.path.split('?')[0]} {outcome} in {time.time()-self._t0:.1f}s{st}")

    def _note_rid(self, rec: bytes):
        """First SSE event carries the request id in both dialects. On the OpenAI dialect
        that id IS the engine's rid, so it also tells us whether the engine honoured the
        override this proxy sent; on the Anthropic dialect it is a locally minted
        msg_<uuid> and names nothing the engine knows."""
        global _rid_override_honoured
        if getattr(self, "_rid", None) is not None:
            return
        try:
            for line in rec.split(b"\n"):
                if line.startswith(b"data:"):
                    j = json.loads(line[5:].strip() or b"{}")
                    rid = j.get("id") or (j.get("message") or {}).get("id")
                    if rid and rid != "keepalive":
                        self._rid = rid
                        forced = self._forced_rid()
                        if forced and _rid_override_honoured is None:
                            _rid_override_honoured = (rid == forced)
                            log("this engine honours x-override-rid: an abandoned request "
                                "can be aborted before it produces anything"
                                if _rid_override_honoured else
                                "this engine ignores x-override-rid (start it with "
                                "SGLANG_ENABLE_REQUEST_HEADER_OVERRIDES=1): a request "
                                "abandoned before its first event can only be drained")
                    return
        except Exception:
            return

    def _abortable_rid(self):
        """The rid the engine would recognise, or None when there is none to give it.

        A rid this proxy invented is only worth sending once an answer has PROVED the
        engine takes it (SGLANG_ENABLE_REQUEST_HEADER_OVERRIDES is off by default, and an
        engine that ignores the header answers with its own rid). Sending an unproven one
        reads like a successful abort in the log and aborts nothing, which is the failure
        this version exists to remove: unproven means "no rid", and no rid means drain."""
        forced = self._forced_rid()
        observed = getattr(self, "_rid", None)
        if forced and _rid_override_honoured is True:
            return forced
        if observed and self.path.split("?")[0] in RID_OVERRIDE_ROUTES:
            return observed         # engine ignored the override: its own id is the rid
        return None                 # Anthropic dialect: msg_<uuid> is not a rid

    def _scan_corruption(self, rec: bytes) -> bool:
        """True once the stream has produced CORRUPTION_RUN marker characters in a row."""
        if not CORRUPTION_RUN:
            return False
        run = getattr(self, "_crun", 0)
        for line in rec.split(b"\n"):
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                run = marker_run(delta_text(json.loads(payload)), run)
            except Exception:
                continue
            if run >= CORRUPTION_RUN:
                self._crun = run
                return True
        self._crun = run
        return False

    def _abort_upstream(self, why: str) -> bool:
        """Tell SGLang to stop generating for a client that is gone, and say whether it
        was told. SYNCHRONOUS by contract: the engine drops the request state the moment
        this proxy closes the socket, and an abort that arrives after that is discarded
        in silence (sglang #35255). Returns False when there is no rid to abort with,
        which is the caller's signal to drain instead of orphaning the generation."""
        if getattr(self, "_aborted", False):
            return True
        rid = self._abortable_rid()
        if not rid:
            return False
        try:
            hdrs = {"Content-Type": "application/json"}
            auth = self.headers.get("Authorization")
            if auth: hdrs["Authorization"] = auth
            req = urllib.request.Request(UPSTREAM + "/abort_request",
                                         json.dumps({"rid": rid}).encode(), hdrs)
            urllib.request.urlopen(req, timeout=ABORT_TIMEOUT_S).read()
            self._aborted = True
            log(f"aborted upstream rid={rid} ({why})")
            return True
        except Exception as e:
            log(f"abort_request failed for rid={rid}: {e}")
            return False

    def _relay(self, resp):
        """A client that vanishes before the relay can start (30/08: a request that
        showed as 'in flight' forever) used to leave the upstream response open and
        unnamed. The engine had already been given the request, so it kept decoding:
        one such disconnect cost 1,742 flood lines and 3 minutes of dead decode here on
        2026-09-09. Every exit path now either aborts the generation or drains it."""
        try:
            self._relay_inner(resp)
        except BaseException:
            if not self._abort_upstream("client vanished mid-request"):
                self._drain_detached(resp)
            else:
                try: resp.close()
                except Exception: pass
            raise

    def _drain_detached(self, resp):
        """Read an abandoned response to its end in the background: see DRAIN_MAX_S."""
        t0 = time.time()
        def go():
            try:
                while time.time() - t0 < DRAIN_MAX_S:
                    if not resp.read1(65536):
                        break
            except Exception:
                pass
            finally:
                try: resp.close()
                except Exception: pass
                log(f"drained an abandoned {self.path.split('?')[0]} for {time.time()-t0:.0f}s")
        threading.Thread(target=go, daemon=True).start()

    def _relay_inner(self, resp):
        sse = "text/event-stream" in (resp.headers.get("Content-Type") or "")
        self._begin(resp.status, resp.headers)
        if not sse:
            # A non-streamed answer is relayed as it arrives, so it cannot be withheld;
            # what the guard can do here is name it in the log instead of leaving a wall
            # of exclamation marks to be explained later.
            worst = 0
            detached = False
            try:
                while True:
                    c = resp.read(65536)
                    if not c: break
                    if CORRUPTION_RUN:
                        run = 0
                        for ch in c.decode("utf-8", "ignore"):
                            run = run + 1 if ch == CORRUPTION_MARK else 0
                            if run > worst: worst = run
                    try:
                        self._chunk(c)
                    except Exception:
                        # the client is gone while the engine is still writing an answer
                        # nobody will read (a non-streamed answer is one long silence for
                        # the caller, so this is a common way to lose one)
                        if self._abort_upstream("client gone on a non-streamed answer"):
                            self._done("CLIENT GONE on write"); return
                        self._drain_detached(resp); detached = True
                        self._done("CLIENT GONE on write (draining)"); return
            except Exception:
                pass
            finally:
                if not detached:
                    try: resp.close()
                    except Exception: pass
            self._finish()
            if worst >= CORRUPTION_RUN:
                log(f"corrupted output in a non-streamed answer ({worst} marker chars)")
                self._done("ok non-sse CORRUPTED"); return
            self._done("ok non-sse"); return

        q = queue.Queue(maxsize=1024)
        stop = threading.Event()
        drain = threading.Event()
        drain_t0 = [0.0]
        def pump():
            # only COMPLETE SSE events are forwarded (boundary \n\n): a
            # keepalive can therefore never land in the middle of an event
            # (v6.4 bug: keepalive chunk injected mid-JSON-line, corrupt stream)
            buf = b""
            def put(rec):
                while not stop.is_set() and not drain.is_set():
                    try: q.put(("d", rec), timeout=2); return True
                    except queue.Full: continue
                return False
            try:
                while not stop.is_set():
                    # read1: returns as soon as bytes exist, where read(8192)
                    # blocks until 8 KB accumulate (bursty stream)
                    c = resp.read1(8192)
                    if not c: break
                    if drain.is_set():
                        # nobody is listening any more and the engine cannot be told to
                        # stop: read the answer to its end anyway. The decode costs the
                        # same as the zombie would have, the log stays clean, and the
                        # slot is released when the generation ends instead of being
                        # held by a request whose state no longer exists.
                        if time.time() - drain_t0[0] > DRAIN_MAX_S:
                            log(f"drain ceiling reached after {DRAIN_MAX_S:.0f}s, "
                                f"dropping the socket for {self.path.split('?')[0]}")
                            break
                        continue
                    buf += c
                    while True:
                        i = buf.find(b"\n\n")
                        if i < 0: break
                        rec = buf[:i+2]; buf = buf[i+2:]
                        if not put(rec): break
            except Exception as e:
                if not stop.is_set() and not drain.is_set():
                    try: q.put(("e", str(e).encode()), timeout=5)
                    except Exception: pass
            finally:
                if drain.is_set():
                    log(f"drained an abandoned {self.path.split('?')[0]} for "
                        f"{time.time() - drain_t0[0]:.0f}s")
                    try: resp.close()
                    except Exception: pass
                    return
                if buf and not stop.is_set():
                    try: q.put(("d", buf), timeout=2)
                    except Exception: pass
                while True:
                    try: q.put(("f", b""), timeout=2); break
                    except queue.Full:
                        if stop.is_set(): break
        threading.Thread(target=pump, daemon=True).start()

        def drop_upstream(why="client gone"):
            # Abort first, close second: the close is what deletes the state the abort
            # needs (sglang #35255). When there is no rid the engine would recognise,
            # closing is the one thing not to do.
            if self._abort_upstream(why):
                stop.set()
                try: resp.close()
                except Exception: pass
            else:
                drain_t0[0] = time.time()
                drain.set()

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
            if self._scan_corruption(val):
                # The decode path lost its state; everything after this point is noise.
                # Say so, rather than let the client read a wall of exclamation marks.
                log(f"corrupted output after {self._crun} marker chars, aborting rid={getattr(self, '_rid', None)}")
                drop_upstream("corrupted output")
                why = (f"the engine emitted {self._crun} consecutive '{CORRUPTION_MARK}' characters, "
                       "which is a decode-state failure, not an answer. The generation was aborted; retry.")
                try:
                    self._chunk(sse_error(why) if anthropic else sse_error_openai(why) + b"data: [DONE]\n\n")
                except Exception: pass
                self._finish(); self._done("REFUSED corrupted output"); return
            try: self._chunk(val)
            except Exception:
                drop_upstream(); self._done("CLIENT GONE on write"); return
        self._finish()
        self._done("ok" if kind == "f" else "UPSTREAM CUT")

    # ---- verbs -------------------------------------------------------------
    def _handle_inner(self, with_body):
        self._t0 = time.time()
        self._peer = f"{self.client_address[0]}:{self.client_address[1]}"
        self._bytes = 0; self._first = None; self._last = None
        n0 = self.headers.get("Content-Length") or "0"
        log(f"{self._peer} -> {self.command} {self.path.split('?')[0]} body={n0}b")
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if (with_body and n) else None
        body, dropped = sanitize_tool_schemas(body, self.path)
        for pat in dropped:                 # once per distinct pattern, not per request
            if pat not in _pattern_drop_logged:
                _pattern_drop_logged.add(pat)
                log(f"{self._peer} tool schema: dropped a 'pattern' Python's re cannot "
                    f"compile (the engine would 400 the request): {pat[:120]}")
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
                    invalidate_pool()   # this engine is restarting; its pool is not ours
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
        if cerr is not None:
            invalidate_pool()           # same: the next pool must be read fresh
            self._unavailable(cerr); self._done("503 engine unreachable"); return
        if herr is not None: self._upstream_error(herr); return
        self._relay(resp)

    def _handle(self, with_body):
        """Every request leaves exactly one end line in the journal, even when the
        client vanishes while the body is read or before the upstream answers
        (30/08: one such request showed as 'in flight' forever in the cockpit)."""
        try:
            self._handle_inner(with_body)
        finally:
            if not getattr(self, "_ended", False):
                self._t0 = getattr(self, "_t0", time.time())
                self._peer = getattr(self, "_peer", "?")
                try: self._done("no outcome (client vanished mid-request)")
                except Exception: pass

    def do_POST(self):   self._handle(True)
    def do_PUT(self):    self._handle(True)
    def do_PATCH(self):  self._handle(True)
    def do_DELETE(self): self._handle(True)

    def do_GET(self):
        try:
            self._get_inner()
        finally:
            if not getattr(self, "_ended", False):
                self._t0 = getattr(self, "_t0", time.time())
                self._peer = getattr(self, "_peer", "?")
                try: self._done("no outcome (client vanished mid-request)")
                except Exception: pass

    def _get_inner(self):
        self._t0 = time.time()
        self._peer = f"{self.client_address[0]}:{self.client_address[1]}"
        self._bytes = 0; self._first = None; self._last = None
        resp, herr, cerr = self._open(None)
        if cerr is not None:
            invalidate_pool()           # same: the next pool must be read fresh
            self._unavailable(cerr); self._done("503 engine unreachable"); return
        if herr is not None: self._upstream_error(herr); return
        try: data = resp.read()
        finally:
            try: resp.close()
            except Exception: pass
        self._plain(resp.status, dict(resp.headers), data)
        self._done("ok get")

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 30001
    log(f"v6.14 on :{port} -> {UPSTREAM} (keepalive {KEEPALIVE_S:.0f}s, max silence {MAX_SILENCE_S:.0f}s)")
    Server(("0.0.0.0", port), H).serve_forever()
