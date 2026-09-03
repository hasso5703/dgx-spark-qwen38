"""Offline tests for the keepalive proxy's oversize guard (v6.8): body shapes
are converted for the engine's /tokenize endpoint and the count is trusted
over the size estimate. A tiny fake /tokenize server stands in for SGLang."""
import http.server
import importlib.util
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve()
SPEC = importlib.util.spec_from_file_location("kproxy", HERE.parents[1] / "keepalive-proxy.py")


class FakeTokenize(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeTokenize.seen.append((self.path, body))
        if self.path != "/tokenize":
            self.send_response(404); self.end_headers(); return
        if body.get("model") in ("__503__", "__400__"):     # engine loading / body rejected
            self.send_response(int(body["model"].strip("_"))); self.end_headers(); return
        def flat(c):
            if isinstance(c, list):
                return " ".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in c)
            return str(c)
        text = body.get("prompt") or " ".join(flat(m.get("content", "")) for m in body.get("messages", []))
        out = json.dumps({"tokens": [], "count": len(str(text).split()), "max_model_len": 262144}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)


class ProxyGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), FakeTokenize)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        os.environ["UPSTREAM"] = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.mod = importlib.util.module_from_spec(SPEC)
        sys.argv = ["keepalive-proxy.py"]
        SPEC.loader.exec_module(cls.mod)
        cls.keyfile = Path.home() / ".config/qwen38/api-key"
        cls.had_key = cls.keyfile.exists()
        if not cls.had_key:
            cls.keyfile.parent.mkdir(parents=True, exist_ok=True)
            cls.keyfile.write_text("test-key\n")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        if not cls.had_key:
            cls.keyfile.unlink()

    def test_openai_chat_counted_with_tools(self):
        body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "one two three"}],
                           "tools": [{"type": "function", "function": {"name": "f"}}]}).encode()
        self.assertEqual(self.mod.tokenize_count(body, "/v1/chat/completions"), 3)
        path, sent = FakeTokenize.seen[-1]
        self.assertEqual(path, "/tokenize")
        self.assertIn("tools", sent)

    def test_completions_prompt(self):
        body = json.dumps({"model": "m", "prompt": "a b c d"}).encode()
        self.assertEqual(self.mod.tokenize_count(body, "/v1/completions"), 4)

    def test_anthropic_shape_converted(self):
        body = json.dumps({"model": "m", "system": [{"type": "text", "text": "sys one"}],
                           "messages": [{"role": "user", "content": [{"type": "text", "text": "hello there"},
                                                                     {"type": "tool_result", "tool_use_id": "x", "content": "ok"}]},
                                        {"role": "assistant", "content": "fine"}]}).encode()
        n = self.mod.tokenize_count(body, "/v1/messages")
        _path, sent = FakeTokenize.seen[-1]
        self.assertEqual([m["role"] for m in sent["messages"]], ["system", "user", "assistant"])
        self.assertIn("sys one", sent["messages"][0]["content"])
        self.assertIn("tool_result", sent["messages"][1]["content"])  # non-text blocks counted via their JSON
        self.assertGreaterEqual(n, 5)

    def test_unknown_shapes_return_none(self):
        self.assertIsNone(self.mod.tokenize_count(b"not json", "/v1/chat/completions"))
        self.assertIsNone(self.mod.tokenize_count(json.dumps([1, 2]).encode(), "/v1/chat/completions"))
        self.assertIsNone(self.mod.tokenize_count(json.dumps({"model": "m", "input": "embeddings"}).encode(), "/v1/embeddings"))

    def test_engine_unreachable_raises(self):
        # no engine at all (stopped, crashed, restarting): not a size problem, so not None
        saved = self.mod.UPSTREAM
        self.mod.UPSTREAM = "http://127.0.0.1:1"
        try:
            with self.assertRaises(self.mod.EngineUnreachable):
                self.mod.tokenize_count(json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode(), "/v1/chat/completions")
        finally:
            self.mod.UPSTREAM = saved

    def test_engine_5xx_raises_4xx_counts_by_size(self):
        body = {"model": "__503__", "messages": [{"role": "user", "content": "x"}]}
        with self.assertRaises(self.mod.EngineUnreachable):      # engine still loading
            self.mod.tokenize_count(json.dumps(body).encode(), "/v1/chat/completions")
        body["model"] = "__400__"                                  # engine rejected this body
        self.assertIsNone(self.mod.tokenize_count(json.dumps(body).encode(), "/v1/chat/completions"))

    def test_openai_image_parts_counted_not_tokenized(self):
        big = "A" * 400_000  # a base64 image is body size, not prompt text
        body = json.dumps({"model": "m", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "describe this one"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + big}}]}]}).encode()
        n = self.mod.tokenize_count(body, "/v1/chat/completions")
        _path, sent = FakeTokenize.seen[-1]
        self.assertNotIn("image_url", json.dumps(sent))
        self.assertEqual(n, 3 + self.mod.TOKENS_PER_MEDIA)

    def test_anthropic_image_block_counted_not_tokenized(self):
        body = json.dumps({"model": "m", "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "B" * 300_000}},
            {"type": "text", "text": "two words"}]}]}).encode()
        n = self.mod.tokenize_count(body, "/v1/messages")
        _path, sent = FakeTokenize.seen[-1]
        self.assertNotIn("BBBB", json.dumps(sent))
        self.assertEqual(n, 2 + self.mod.TOKENS_PER_MEDIA)

    def test_prompt_limit_pool_share_and_ceiling(self):
        saved = self.mod.PROMPT_CEILING_TOKENS
        try:
            self.mod.PROMPT_CEILING_TOKENS = 0
            self.assertEqual(self.mod.prompt_limit(184384), 169633)
            self.mod.PROMPT_CEILING_TOKENS = 135000
            self.assertEqual(self.mod.prompt_limit(184384), 135000)
            self.assertEqual(self.mod.prompt_limit(100000), 92000)   # ceiling above the share: share wins
        finally:
            self.mod.PROMPT_CEILING_TOKENS = saved

    def test_margin_default(self):
        self.assertAlmostEqual(self.mod.OVERSIZE_MARGIN_FRAC, 0.08)
        self.assertEqual(int(178560 * (1 - self.mod.OVERSIZE_MARGIN_FRAC)), 164275)



class LoadingEngine(http.server.BaseHTTPRequestHandler):
    """Answers /get_server_info (so the proxy knows the pool) but is still loading:
    503 on /tokenize and on generations, like SGLang between 'Started' and 'ready'."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/get_server_info":
            out = json.dumps({"max_total_num_tokens": 200000}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out)))
            self.end_headers(); self.wfile.write(out); return
        self.send_response(503); self.end_headers()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(503); self.end_headers()


class ProxyInFrontOfLoadingEngine(unittest.TestCase):
    """v6.9: a body the size guard nominates must come back 503 engine_unavailable while the
    engine is down, never 400 context_too_long (live on 2026-08-30 01:15: '~409k tokens' for a
    68,626-token request during an engine restart)."""

    @classmethod
    def setUpClass(cls):
        import socket, subprocess, sys, time
        cls.eng = http.server.HTTPServer(("127.0.0.1", 0), LoadingEngine)
        threading.Thread(target=cls.eng.serve_forever, daemon=True).start()
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0)); cls.port = sk.getsockname()[1]
        env = dict(os.environ, UPSTREAM=f"http://127.0.0.1:{cls.eng.server_address[1]}")
        cls.proc = subprocess.Popen([sys.executable, str(HERE.parents[1] / "keepalive-proxy.py"), str(cls.port)],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.2).close(); break
            except OSError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate(); cls.proc.wait(timeout=5); cls.eng.shutdown()

    def _post(self, body):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_big_body_is_503_engine_unavailable_not_400(self):
        body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "word " * 300000}]}).encode()
        self.assertGreater(len(body), 1_000_000)
        status, headers, raw = self._post(body)
        self.assertEqual(status, 503)
        err = json.loads(raw)["error"]
        self.assertEqual(err["type"], "engine_unavailable")
        self.assertIn("NOT refused for its size", err["message"])
        self.assertEqual(headers.get("Retry-After"), "30")

    def test_small_body_is_503_too(self):
        status, _headers, raw = self._post(json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}).encode())
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw)["error"]["type"], "engine_unavailable")


class PoolCacheInvalidation(unittest.TestCase):
    """The cached pool must never outlive the engine that reported it.

    The pool is a boot lottery and changes outright between lanes (about 863k on
    the 27B lane, 184k on flash). Before this, pool_tokens() cached for 600 s and
    dropped the value nowhere, so for ten minutes after an engine restart the
    guard enforced the previous engine's limit: a prompt sized against a larger
    stale pool would be relayed to a smaller one and wedge the scheduler, which
    is the failure the guard exists to prevent."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "kproxy_pool", HERE.parents[1] / "keepalive-proxy.py")
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def test_invalidate_drops_a_cached_pool(self):
        self.m._POOL.update(tokens=913334, ts=self.m.time.time())
        self.assertEqual(self.m.pool_tokens(), 913334, "fresh cache should be used")
        self.m.invalidate_pool()
        self.assertIsNone(self.m._POOL["tokens"], "invalidate must clear the value")
        self.assertEqual(self.m._POOL["ts"], 0.0, "invalidate must clear the timestamp")

    def test_failed_read_does_not_keep_a_stale_pool(self):
        # No engine answers on this port, so the refresh read fails. The old
        # behaviour returned the stale number; the guard would then size prompts
        # against an engine that is not there any more.
        self.m.UPSTREAM = "http://127.0.0.1:1"
        self.m._POOL.update(tokens=913334, ts=0.0)      # cached, but expired
        self.assertIsNone(self.m.pool_tokens(),
                          "a failed refresh must not fall back on the previous engine's pool")

    def test_a_smaller_pool_is_picked_up_after_invalidation(self):
        # 27B pool cached, then the box switches to the flash lane. Whatever the
        # cache said, the limit must follow the engine that is actually serving.
        self.m._POOL.update(tokens=863398, ts=self.m.time.time())
        big = self.m.prompt_limit(self.m.pool_tokens())
        self.m.invalidate_pool()
        self.m._POOL.update(tokens=184384, ts=self.m.time.time())
        small = self.m.prompt_limit(self.m.pool_tokens())
        self.assertLess(small, big, "the flash lane must not inherit the 27B limit")




class CorruptionRun(unittest.TestCase):
    """v6.11 tripwire: runs of token id 0 ("!") are a decode-state failure, not prose."""

    @classmethod
    def setUpClass(cls):
        sys.argv = ["keepalive-proxy.py"]
        cls.k = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(cls.k)

    def setUp(self):
        self.k = type(self).k

    def test_a_run_grows_across_events(self):
        run = 0
        for _ in range(10):
            run = self.k.marker_run("!", run)
        self.assertEqual(run, 10)

    def test_any_real_character_resets_the_run(self):
        run = self.k.marker_run("!!!!", 0)
        self.assertEqual(run, 4)
        self.assertEqual(self.k.marker_run(" hello", run), 0)

    def test_a_trailing_run_survives_its_own_event(self):
        self.assertEqual(self.k.marker_run("wait!!!", 5), 3)

    def test_prose_exclamations_never_reach_the_threshold(self):
        run = 0
        for word in ("Done", "!", " Great", "!", "!", " Ship it", "!"):
            run = self.k.marker_run(word, run)
        self.assertLess(run, self.k.CORRUPTION_RUN)

    def test_delta_text_reads_both_dialects(self):
        self.assertEqual(self.k.delta_text(
            {"choices": [{"delta": {"content": "hi"}}]}), "hi")
        self.assertEqual(self.k.delta_text(
            {"choices": [{"delta": {"reasoning_content": "think"}}]}), "think")
        self.assertEqual(self.k.delta_text(
            {"type": "content_block_delta", "delta": {"text": "hey"}}), "hey")
        self.assertEqual(self.k.delta_text({"choices": [{"delta": {}}]}), "")

    def test_tool_arguments_are_not_scanned(self):
        # a JSON blob of exclamation marks inside tool arguments is the model's
        # business, not a decode failure
        self.assertEqual(self.k.delta_text(
            {"choices": [{"delta": {"tool_calls": [{"function": {"arguments": "!" * 80}}]}}]}), "")

    def test_scan_trips_only_past_the_threshold(self):
        h = self.k.H.__new__(self.k.H)
        ev = b'data: {"choices":[{"delta":{"content":"!"}}]}\n\n'
        fired = [h._scan_corruption(ev) for _ in range(self.k.CORRUPTION_RUN)]
        self.assertEqual(fired.count(True), 1)
        self.assertTrue(fired[-1])
        self.assertFalse(any(fired[:-1]))

    def test_scan_ignores_done_and_keepalive_events(self):
        h = self.k.H.__new__(self.k.H)
        self.assertFalse(h._scan_corruption(b"data: [DONE]\n\n"))
        self.assertFalse(h._scan_corruption(
            b'data: {"id":"keepalive","choices":[]}\n\n'))
        self.assertEqual(getattr(h, "_crun", 0), 0)

    def test_the_error_event_names_the_cause_in_both_dialects(self):
        oa = self.k.sse_error_openai("boom").decode()
        self.assertIn("corrupted_output", oa)
        self.assertTrue(oa.startswith("data: "))
        an = self.k.sse_error("boom").decode()
        self.assertIn("event: error", an)


class CorruptingEngine(http.server.BaseHTTPRequestHandler):
    """Streams the failure this guard exists for: one "!" per SSE event, forever.
    /abort_request records that the proxy asked it to stop."""
    aborted = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b"{}"
        if self.path == "/abort_request":
            CorruptingEngine.aborted.append(json.loads(body or b"{}").get("rid"))
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers()
            self.wfile.write(b"{}"); return
        if self.path == "/tokenize":
            out = json.dumps({"tokens": [1, 2, 3]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out))); self.end_headers()
            self.wfile.write(out); return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        first = json.dumps({"id": "rid-corrupt", "choices": [{"delta": {"content": "Here goes"}}]})
        self.wfile.write(b"data: " + first.encode() + b"\n\n"); self.wfile.flush()
        bang = json.dumps({"id": "rid-corrupt", "choices": [{"delta": {"content": "!"}}]})
        try:
            for _ in range(4000):
                self.wfile.write(b"data: " + bang.encode() + b"\n\n"); self.wfile.flush()
        except Exception:
            pass


class CorruptionTripwireEndToEnd(unittest.TestCase):
    """The whole path: a stream that degenerates into token id 0 is cut, the client is told
    why, and the engine is asked to stop generating."""

    @classmethod
    def setUpClass(cls):
        import socket, subprocess, time
        CorruptingEngine.aborted = []
        cls.eng = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CorruptingEngine)
        threading.Thread(target=cls.eng.serve_forever, daemon=True).start()
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0)); cls.port = sk.getsockname()[1]
        env = dict(os.environ, UPSTREAM=f"http://127.0.0.1:{cls.eng.server_address[1]}",
                   CORRUPTION_RUN="48", PROMPT_CEILING_TOKENS="0")
        cls.proc = subprocess.Popen([sys.executable, str(HERE.parents[1] / "keepalive-proxy.py"), str(cls.port)],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.2).close(); break
            except OSError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate(); cls.proc.wait(timeout=10)
        cls.eng.shutdown(); cls.eng.server_close()

    def test_the_stream_is_cut_and_the_client_is_told_why(self):
        import time
        body = json.dumps({"model": "m", "stream": True,
                           "messages": [{"role": "user", "content": "hi"}]}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions",
                                     data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8", "ignore")
        self.assertIn("corrupted_output", text)
        self.assertIn("decode-state failure", text)
        self.assertTrue(text.rstrip().endswith("data: [DONE]"), "the OpenAI stream must end with [DONE]")
        # the guard cut the stream well before the engine's 4000 events
        self.assertLess(text.count('"!"'), 400, "the wall of exclamation marks was relayed")
        for _ in range(40):                     # the abort is fired on its own thread
            if CorruptingEngine.aborted: break
            time.sleep(0.05)
        self.assertEqual(CorruptingEngine.aborted, ["rid-corrupt"])


if __name__ == "__main__":
    unittest.main()
