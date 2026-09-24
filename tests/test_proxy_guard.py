"""Offline tests for the keepalive proxy's oversize guard (v6.8): body shapes
are converted for the engine's /tokenize endpoint and the count is trusted
over the size estimate. A tiny fake /tokenize server stands in for SGLang."""
import http.server
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve()
SPEC = importlib.util.spec_from_file_location("kproxy", HERE.parents[1] / "keepalive-proxy.py")


# The proxy reads the engine key from ~/.config/qwen38/api-key, and these fixtures used to
# write "test-key" there when the file was missing, then delete it in tearDownClass. A Ctrl-C
# skips tearDownClass, the key stayed, and install.sh keeps the key it finds: the box's
# engine and cockpit then answered to a string published in this file. Where a key existed,
# the real one was read and sent to the fake engines (found in review, 2026-09-24). A proxy
# loaded here is handed its key; one started as a process gets a HOME of its own.
def key_home():
    """A throwaway HOME holding nothing but an engine key, for a proxy run as a process."""
    home = Path(tempfile.mkdtemp(prefix="proxy-key-home-"))
    (home / ".config/qwen38").mkdir(parents=True)
    (home / ".config/qwen38/api-key").write_text("test-key\n")
    return home


class FakeTokenize(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeTokenize.seen.append((self.path, body))
        if self.path not in ("/tokenize", "/v1/messages/count_tokens"):
            self.send_response(404); self.end_headers(); return
        if body.get("model") in ("__503__", "__400__"):     # engine loading / body rejected
            self.send_response(int(body["model"].strip("_"))); self.end_headers(); return
        def flat(c):
            """One word per whitespace-separated token of the text the template would see:
            text parts, a tool_result's own content, any other block as its JSON."""
            if isinstance(c, list):
                out = []
                for p in c:
                    if not isinstance(p, dict):
                        out.append(str(p))
                    elif p.get("type") == "text":
                        out.append(str(p.get("text", "")))
                    elif p.get("type") == "tool_result":
                        out.append(flat(p.get("content", "")))
                    else:
                        out.append(json.dumps(p))
                return " ".join(out)
            return str(c)
        text = body.get("prompt") or " ".join(flat(m.get("content", "")) for m in body.get("messages", []))
        if self.path == "/v1/messages/count_tokens":
            words = len((flat(body.get("system", "")) + " " + str(text)).split())
            words += len(json.dumps(body["tools"]).split()) if body.get("tools") else 0
            out = json.dumps({"input_tokens": words}).encode()
        else:
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
        cls.mod._api_key = lambda: "test-key"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

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

    def test_anthropic_body_counted_by_the_engines_own_route(self):
        """The engine converts an Anthropic body for a generation; its count route does
        the same conversion and applies the same template, so the body goes there as it
        is (v6.25), not flattened into OpenAI messages here."""
        body = {"model": "m", "system": [{"type": "text", "text": "sys one"}], "max_tokens": 9,
                "messages": [{"role": "user", "content": [{"type": "text", "text": "hello there"},
                                                          {"type": "tool_result", "tool_use_id": "x", "content": "ok"}]},
                             {"role": "assistant", "content": "fine"}]}
        n = self.mod.tokenize_count(json.dumps(body).encode(), "/v1/messages")
        path, sent = FakeTokenize.seen[-1]
        self.assertEqual(path, "/v1/messages/count_tokens")
        self.assertEqual(sent["system"], body["system"])
        self.assertEqual(sent["messages"], body["messages"])
        self.assertNotIn("max_tokens", sent)          # not a field of the count route
        self.assertEqual(n, 6)                        # sys one, hello there, ok, fine

    def test_anthropic_tools_are_counted(self):
        """Measured on the box: 8 tools, 2,709 prompt tokens served, 422 counted by v6.24,
        which sent no tools at all on this route."""
        tools = [{"name": "Read", "description": "reads a file from disk",
                  "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}]
        body = json.dumps({"model": "m", "tools": tools,
                           "messages": [{"role": "user", "content": "two words"}]}).encode()
        n = self.mod.tokenize_count(body, "/v1/messages")
        _path, sent = FakeTokenize.seen[-1]
        self.assertEqual(sent.get("tools"), tools)
        self.assertEqual(n, 2 + len(json.dumps(tools).split()))

    def test_an_image_in_a_tool_result_is_priced_not_read_as_text(self):
        """Claude Code returns the screenshots it reads inside tool_result blocks. v6.24
        sent those as JSON text, base64 included: a 384 KB screenshot counted 367,185
        tokens on the box where the engine served 5,237, enough to refuse a prompt that
        fits. The block is priced from its header and replaced by an empty text block,
        never by an empty list, which the engine's count route answers with a 500."""
        import base64
        shot = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                            "data": base64.b64encode(self._png(1280, 720)).decode() + "B" * 300_000}}
        body = json.dumps({"model": "m", "messages": [
            {"role": "user", "content": "look"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [shot]}]}]}).encode()
        n = self.mod.tokenize_count(body, "/v1/messages")
        _path, sent = FakeTokenize.seen[-1]
        self.assertNotIn("BBBB", json.dumps(sent))
        self.assertEqual(sent["messages"][2]["content"][0]["content"], [{"type": "text", "text": ""}])
        self.assertEqual(n - self.mod._image_tokens(1280, 720),
                         1 + len(json.dumps(sent["messages"][1]["content"][0]).split()))

    def test_tool_pattern_python_cannot_compile_is_dropped(self):
        """Claude Code's Artifact tool sends an ECMA-262 pattern with Unicode property
        escapes; the engine validates schemas with Python's re and 400s the whole
        session over it (09/09). The pattern goes, everything else stays."""
        ecma = r'^(?!__.*__$)[^\p{Cc}\p{Cf}\p{Zl}\p{Zp}"\\./[\]]{1,200}$'
        body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}],
                           "tools": [{"name": "Artifact", "input_schema": {"type": "object", "properties": {
                               "field": {"type": "string", "pattern": ecma},
                               "doc_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"}}}}]}).encode()
        out, dropped = self.mod.sanitize_tool_schemas(body, "/v1/messages")
        self.assertEqual(dropped, [ecma])
        props = json.loads(out)["tools"][0]["input_schema"]["properties"]
        self.assertNotIn("pattern", props["field"])
        self.assertIn("pattern", props["doc_id"])       # a pattern re accepts is kept
        self.assertEqual(json.loads(out)["messages"], [{"role": "user", "content": "hi"}])

    def test_openai_and_message_level_tools_are_reached(self):
        ecma = r"^\p{L}+$"
        body = json.dumps({"model": "m",
                           "tools": [{"type": "function", "function": {"name": "f", "parameters": {
                               "properties": {"a": {"pattern": ecma}}}}}],
                           "messages": [{"role": "system", "content": "s", "tools": [
                               {"name": "g", "input_schema": {"properties": {"b": {"pattern": ecma}}}}]}]}).encode()
        out, dropped = self.mod.sanitize_tool_schemas(body, "/v1/chat/completions")
        self.assertEqual(len(dropped), 2)
        self.assertNotIn("pattern", out.decode())

    def test_clean_body_is_forwarded_untouched(self):
        """No marker in the raw bytes: the body is not even parsed."""
        body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}],
                           "tools": [{"name": "f", "input_schema": {"properties": {
                               "a": {"pattern": "^[a-z]+$"}}}}]}).encode()
        out, dropped = self.mod.sanitize_tool_schemas(body, "/v1/messages")
        self.assertIs(out, body)
        self.assertEqual(dropped, [])

    def test_user_content_quoting_a_bad_pattern_is_left_alone(self):
        """The guard walks tool schemas only: a prompt that happens to quote one of
        these regexes must reach the engine byte for byte."""
        text = r'why does [^\p{Cc}] fail? {"pattern": "\p{L}"}'
        body = json.dumps({"model": "m", "messages": [{"role": "user", "content": text}]}).encode()
        out, dropped = self.mod.sanitize_tool_schemas(body, "/v1/messages")
        self.assertEqual(dropped, [])
        self.assertEqual(json.loads(out)["messages"][0]["content"], text)

    def test_malformed_and_bodyless_requests_survive(self):
        self.assertEqual(self.mod.sanitize_tool_schemas(rb'{"tools": [broken \p{L}', "/v1/messages")[1], [])
        self.assertEqual(self.mod.sanitize_tool_schemas(None, "/v1/messages"), (None, []))
        self.assertEqual(self.mod.sanitize_tool_schemas(rb'\p{L}', "/health")[1], [])

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
        # No readable header in that filler, so the image gets the geometric
        # ceiling rather than a flat guess, and never its base64 as text.
        self.assertEqual(n, 3 + self.mod._image_ceiling())

    def test_anthropic_image_block_counted_not_tokenized(self):
        body = json.dumps({"model": "m", "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "B" * 300_000}},
            {"type": "text", "text": "two words"}]}]}).encode()
        n = self.mod.tokenize_count(body, "/v1/messages")
        _path, sent = FakeTokenize.seen[-1]
        self.assertNotIn("BBBB", json.dumps(sent))
        self.assertEqual(n, 2 + self.mod._image_ceiling())

    # ---- media is priced, not guessed (v6.15) -----------------------------
    # The engine's numbers below are measured on the reference box (2026-09-13,
    # RadixArk/Qwen3.8-Flash-Next-NVFP4, patch 16 x merge 2): the formula matched
    # the engine's own prompt_tokens exactly at all twelve sizes tried.
    MEASURED = {(1280, 720): 882, (1400, 800): 1102, (1600, 900): 1402,
                (1680, 950): 1562, (923, 2000): 1800, (2000, 2000): 3846,
                (3840, 2160): 8162, (4000, 4000): 15627,
                (64, 64): 66, (200, 150): 72, (390, 844): 314, (4500, 4500): 16386}

    @staticmethod
    def _png(w, h):
        """A PNG that is nothing but a valid header: the parser reads no further."""
        import struct, zlib
        ihdr = struct.pack(">II", w, h) + bytes([8, 2, 0, 0, 0])
        return (b"\x89PNG\r\n\x1a\n"
                + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
                + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr)))

    def test_image_tokens_match_the_engine_at_every_measured_size(self):
        for (w, h), expected in self.MEASURED.items():
            with self.subTest(size=f"{w}x{h}"):
                self.assertEqual(self.mod._image_tokens(w, h), expected)

    def test_image_dims_read_png_jpeg_gif_webp_headers(self):
        import struct
        self.assertEqual(self.mod._image_dims(self._png(1680, 950)), (1680, 950))
        jpeg = (b"\xff\xd8"
                + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
                + b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", 950, 1680)
                + b"\x03" + b"\x00" * 9)
        self.assertEqual(self.mod._image_dims(jpeg), (1680, 950))
        self.assertEqual(self.mod._image_dims(b"GIF89a" + struct.pack("<HH", 640, 480)), (640, 480))
        webp = (b"RIFF" + b"\x00" * 4 + b"WEBPVP8X" + b"\x00" * 8
                + (1679).to_bytes(3, "little") + (949).to_bytes(3, "little"))
        self.assertEqual(self.mod._image_dims(webp), (1680, 950))

    def test_screenshot_costs_what_the_engine_charges_not_the_flat_budget(self):
        """The field failure of 2026-09-12: 24 screenshots in one conversation.

        Charged flat, they were 98,304 tokens and the request was refused at
        "200,684 prompt tokens"; the engine was serving 139,868. Counted from
        their headers they are 37,488, and the same conversation fits.
        """
        import base64
        url = "data:image/png;base64," + base64.b64encode(self._png(1680, 950)).decode()
        shots = [{"type": "image_url", "image_url": {"url": url}} for _ in range(24)]
        body = json.dumps({"model": "m", "messages": [{"role": "user", "content":
            [{"type": "text", "text": "one two three"}] + shots}]}).encode()
        n = self.mod.tokenize_count(body, "/v1/chat/completions")
        self.assertEqual(n, 3 + 24 * 1562)
        self.assertLess(n, 24 * self.mod.TOKENS_PER_MEDIA)

    def test_anthropic_image_block_priced_from_its_header(self):
        import base64
        body = json.dumps({"model": "m", "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(self._png(1280, 720)).decode()}},
            {"type": "text", "text": "two words"}]}]}).encode()
        self.assertEqual(self.mod.tokenize_count(body, "/v1/messages"), 2 + 882)

    def test_an_unreadable_image_is_charged_its_geometric_ceiling(self):
        """Nothing is ever guessed downward.

        A 3840x2160 screenshot really costs 8,162, so the old flat 4,096 would
        UNDER-count an image it could not parse, which is the direction that
        lets an oversize prompt through to the engine. An image is bounded by
        the processor's max_pixels, so the fallback is that bound.
        """
        for block in (
            {"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}},
            {"type": "image_url", "image_url": {"url": "data:image/heic;base64,AAAAAAAAAAAA"}},
            {"type": "image", "source": {"type": "url", "url": "https://example.invalid/a.png"}},
        ):
            with self.subTest(block=block["type"]):
                self.assertEqual(self.mod._media_tokens(block), self.mod._image_ceiling())
        self.assertGreater(self.mod._image_ceiling(), self.mod._image_tokens(3840, 2160))

    def test_media_that_is_not_an_image_keeps_the_flat_budget(self):
        """Audio, video and documents have no geometry to bound them."""
        for block in (
            {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}},
            {"type": "document", "source": {"type": "base64", "data": "AAAA"}},
            {"type": "video_url", "video_url": {"url": "https://example.invalid/a.mp4"}},
        ):
            with self.subTest(block=block["type"]):
                self.assertEqual(self.mod._media_tokens(block), self.mod.TOKENS_PER_MEDIA)

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
        if self.path in ("/server_info", "/get_server_info"):
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
        # This class models a loading engine whose pool IS known (200000 via
        # /get_server_info) while /tokenize still 503s. pool_tokens() needs the
        # api-key file to ask, so the proxy runs in a HOME that has one; without
        # it the pool reads as unknown and the new warmup hold answers first
        # (also 503, different type).
        cls.home = key_home()
        env = dict(os.environ, UPSTREAM=f"http://127.0.0.1:{cls.eng.server_address[1]}",
                   HOME=str(cls.home))
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
        shutil.rmtree(cls.home, ignore_errors=True)

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


class ProxyOnABoxSwitchedToImages(unittest.TestCase):
    """v6.22: with the image lane serving, the text engine is stopped on purpose and does not
    come back by itself. "Stopped, restarting or still loading (about 9 minutes)" sent a
    client to wait for it; the answer now names the image lane and the way back. systemd is
    asked through PATH, so a fake systemctl stands in for it here, in both directions."""

    def spawn(self, image_active):
        import socket, subprocess, sys, tempfile, time
        fake = Path(tempfile.mkdtemp(prefix="fake-systemctl-"))
        (fake / "systemctl").write_text(
            "#!/bin/sh\n"
            f'[ "$*" = "is-active --quiet qwen38-image.service" ] && exit {0 if image_active else 3}\n'
            "exit 3\n")
        (fake / "systemctl").chmod(0o755)
        with socket.socket() as sk:                     # a port nothing listens on: the engine is gone
            sk.bind(("127.0.0.1", 0)); dead = sk.getsockname()[1]
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]
        env = dict(os.environ, UPSTREAM=f"http://127.0.0.1:{dead}", PATH=f"{fake}:{os.environ.get('PATH', '')}")
        proc = subprocess.Popen([sys.executable, str(HERE.parents[1] / "keepalive-proxy.py"), str(port)],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.terminate(), proc.wait(timeout=5)))
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close(); break
            except OSError:
                time.sleep(0.05)
        return port

    def ask(self, port):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                                     data=json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_the_image_lane_is_named_with_the_way_back(self):
        status, raw = self.ask(self.spawn(image_active=True))
        self.assertEqual(status, 503)
        err = json.loads(raw)["error"]
        self.assertEqual(err["type"], "engine_unavailable")
        self.assertIn("serving images right now", err["message"])
        self.assertIn("switch back to a text lane", err["message"])
        self.assertNotIn("about 9 on a DGX Spark", err["message"])

    def test_without_it_the_answer_is_the_one_it_always_was(self):
        status, raw = self.ask(self.spawn(image_active=False))
        self.assertEqual(status, 503)
        msg = json.loads(raw)["error"]["message"]
        self.assertIn("stopped, restarting or still loading", msg)
        self.assertNotIn("serving images", msg)


class SmallPoolEngine(http.server.BaseHTTPRequestHandler):
    """A healthy engine with a tiny pool: it answers /get_server_info and counts
    with /tokenize, so the guard has everything it needs to refuse on size."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path in ("/server_info", "/get_server_info"):
            out = json.dumps({"max_total_num_tokens": 20000}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out)))
            self.end_headers(); self.wfile.write(out); return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        if self.path == "/tokenize":
            text = " ".join(str(m.get("content", "")) for m in body.get("messages", []))
            out = json.dumps({"count": len(text.split()), "max_model_len": 262144}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out))); self.end_headers()
            self.wfile.write(out); return
        self.send_response(404); self.end_headers()


class RefusalIsRecognisableAsOverflow(unittest.TestCase):
    """v6.15: a refusal a client cannot classify is a refusal it cannot recover from.

    opencode (and every AI-SDK client) decides "this is a context overflow, compact
    and retry" by matching the provider's message against a fixed vocabulary, or by
    reading error.code == "context_length_exceeded". The v6.14 message matched
    nothing in that vocabulary and carried no code, so the session resent the same
    prompt and got the same 400: measured on this box 2026-09-12 23:18:00 and
    23:18:03, two identical refusals 3 s apart, no compaction between them.

    The regexes below are opencode 1.18.27's own list, read out of the binary."""

    OPENCODE_OVERFLOW_VOCABULARY = [
        r"prompt is too long", r"request_too_large", r"input is too long for requested model",
        r"exceeds the context window", r"input token count.*exceeds the maximum",
        r"tokens in request more than max tokens allowed", r"maximum prompt length is \d+",
        r"reduce the length of the messages", r"maximum context length is \d+ tokens",
        r"exceeds the limit of \d+", r"exceeds the available context size",
        r"greater than the context length", r"context window exceeds limit",
        r"exceeded model token limit", r"context[_ ]length[_ ]exceeded",
        r"request entity too large", r"context length is only \d+ tokens",
        r"input length.*exceeds.*context length", r"model_context_window_exceeded",
        r"too many tokens", r"token limit exceeded",
    ]
    # The same client refuses to treat these as overflow even when the rest matches.
    OPENCODE_NOT_OVERFLOW = [r"^(throttling error|service unavailable):", r"rate limit", r"too many requests"]

    @classmethod
    def setUpClass(cls):
        import socket, subprocess, sys, time
        cls.eng = http.server.HTTPServer(("127.0.0.1", 0), SmallPoolEngine)
        threading.Thread(target=cls.eng.serve_forever, daemon=True).start()
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0)); cls.port = sk.getsockname()[1]
        cls.home = key_home()
        env = dict(os.environ, UPSTREAM=f"http://127.0.0.1:{cls.eng.server_address[1]}",
                   HOME=str(cls.home))
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
        shutil.rmtree(cls.home, ignore_errors=True)

    def _refusal(self):
        body = json.dumps({"model": "m", "messages": [
            {"role": "user", "content": "word " * 120000}]}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                self.fail(f"an oversize body was relayed: {r.status}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())["error"]

    def test_the_refusal_matches_the_client_overflow_vocabulary(self):
        import re
        _status, err = self._refusal()
        hits = [p for p in self.OPENCODE_OVERFLOW_VOCABULARY if re.search(p, err["message"], re.I)]
        self.assertTrue(hits, f"no client would classify this as an overflow: {err['message']}")
        for p in self.OPENCODE_NOT_OVERFLOW:
            self.assertIsNone(re.search(p, err["message"], re.I),
                              f"{p!r} makes the client treat an overflow as a rate limit")

    def test_the_refusal_carries_the_machine_readable_code(self):
        status, err = self._refusal()
        self.assertEqual(status, 400)
        self.assertEqual(err["code"], "context_length_exceeded")

    def test_the_refusal_keeps_its_own_type_for_needle_sh(self):
        """needle.sh reads error.type to tell a refusal from a missed needle."""
        _status, err = self._refusal()
        self.assertEqual(err["type"], "context_too_long")

    def test_the_refusal_still_says_what_and_why(self):
        _status, err = self._refusal()
        self.assertIn("prompt tokens", err["message"])
        self.assertIn("KV pool", err["message"])


class ServerInfoRoute(unittest.TestCase):
    """v6.23: the pool comes from /server_info. SGLang logs a deprecation warning for every
    call of /get_server_info, on the 27B image and the flash image alike, and says the route
    will go; the old one is asked only by an engine that answers 404 to the new one."""

    def serve(self, routes):
        seen = []

        class Engine(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(self.path)
                if self.path in routes:
                    out = json.dumps({"max_total_num_tokens": 123456}).encode()
                    self.send_response(200); self.send_header("Content-Length", str(len(out)))
                    self.end_headers(); self.wfile.write(out); return
                self.send_response(404); self.end_headers()

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        spec = importlib.util.spec_from_file_location("kproxy_route", HERE.parents[1] / "keepalive-proxy.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        m.UPSTREAM = f"http://127.0.0.1:{srv.server_address[1]}"
        m._api_key = lambda: "k"
        return m, seen

    def test_a_current_engine_is_never_asked_the_deprecated_route(self):
        m, seen = self.serve({"/server_info"})
        self.assertEqual(m.pool_tokens(), 123456)
        m.invalidate_pool()
        self.assertEqual(m.pool_tokens(), 123456)
        self.assertEqual(seen, ["/server_info", "/server_info"])

    def test_an_engine_without_the_new_route_still_gives_its_pool(self):
        m, seen = self.serve({"/get_server_info"})
        self.assertEqual(m.pool_tokens(), 123456)
        self.assertEqual(seen, ["/server_info", "/get_server_info"])
        m._POOL.update(tokens=None, ts=0.0)           # expired, same engine
        self.assertEqual(m.pool_tokens(), 123456)
        self.assertEqual(seen[2:], ["/get_server_info"], "the fallback is remembered for that engine")

    def test_a_new_engine_is_asked_the_new_route_again(self):
        m, seen = self.serve({"/get_server_info"})
        m.pool_tokens()
        m.invalidate_pool()                            # the engine moved
        self.assertEqual(m._INFO_ROUTE["path"], "/server_info")


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


class HardeningEngine(http.server.BaseHTTPRequestHandler):
    """Upstream for the v6.15 hardening gates: unknown pool (500 on
    /get_server_info), a hanging metadata route, a fast one, and a small
    SSE generation."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path in ("/server_info", "/get_server_info"):
            self.send_response(500); self.end_headers(); return
        if self.path == "/hang":
            time.sleep(5)
            try:
                self.send_response(200); self.send_header("Content-Length", "2")
                self.end_headers(); self.wfile.write(b"{}")
            except Exception:
                pass
            return
        out = b'{"ok": true}'
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        if self.path == "/tokenize":
            self.send_response(500); self.end_headers(); return
        ev = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(ev))); self.end_headers()
        self.wfile.write(ev)


class HardeningV615(unittest.TestCase):
    """v6.15: a lying Content-Length never allocates, a hanging metadata GET
    never parks a thread, and a monster arriving during a restart waits
    (503) instead of wedging the scheduler. Small requests still pass."""

    @classmethod
    def setUpClass(cls):
        import socket, subprocess
        cls.eng = http.server.ThreadingHTTPServer(("127.0.0.1", 0), HardeningEngine)
        threading.Thread(target=cls.eng.serve_forever, daemon=True).start()
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0)); cls.port = sk.getsockname()[1]
        env = dict(os.environ, UPSTREAM=f"http://127.0.0.1:{cls.eng.server_address[1]}",
                   PROMPT_CEILING_TOKENS="200000", UPSTREAM_GET_TIMEOUT_S="1",
                   MAX_BODY_BYTES="1000000")
        cls.proc = subprocess.Popen([sys.executable, str(HERE.parents[1] / "keepalive-proxy.py"), str(cls.port)],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.2).close(); break
            except OSError:
                time.sleep(0.05)
        cls.k = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(cls.k)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate(); cls.proc.wait(timeout=10)
        cls.eng.shutdown(); cls.eng.server_close()

    def _raw(self, head, body=b""):
        import socket
        sk = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            sk.sendall(head + b"\r\n\r\n" + body)
            sk.settimeout(10)
            out = b""
            while b"\r\n\r\n" not in out:
                chunk = sk.recv(4096)
                if not chunk:
                    break
                out += chunk
            return out
        finally:
            sk.close()

    def test_non_numeric_content_length_is_400(self):
        out = self._raw(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\nConnection: close")
        self.assertIn(b" 400 ", out.split(b"\r\n", 1)[0])

    def test_negative_content_length_is_400(self):
        out = self._raw(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Length: -5\r\nConnection: close")
        self.assertIn(b" 400 ", out.split(b"\r\n", 1)[0])

    def test_body_over_cap_is_413_before_any_read(self):
        out = self._raw(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Length: 2000000\r\nConnection: close")
        status = out.split(b"\r\n", 1)[0]
        self.assertIn(b" 413 ", status)

    def _post(self, body):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_monster_with_unknown_pool_is_503_not_relayed(self):
        body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "word " * 150000}]}).encode()
        self.assertGreater(len(body), 500_000)      # est ~300k tokens, over the 200k ceiling
        status, raw = self._post(body)
        self.assertEqual(status, 503)
        err = json.loads(raw)["error"]
        self.assertEqual(err["type"], "engine_warming")

    def test_small_body_with_unknown_pool_is_relayed(self):
        status, raw = self._post(json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}).encode())
        self.assertEqual(status, 200)
        self.assertIn(b"hi", raw)

    def test_hanging_metadata_get_is_503(self):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/hang", timeout=10)
            self.fail("the hanging upstream should have been cut")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 503)
            self.assertEqual(json.loads(e.read())["error"]["type"], "engine_unavailable")

    def test_deep_schema_is_left_untouched_and_reported(self):
        """Patterns nested past the depth bound are not pruned (no crash, no
        rewrite); the bound hit is reported instead of failing silently."""
        node = {"type": "object"}
        cur = node
        for _ in range(40):
            nxt = {"type": "object", "properties": {}}
            cur["properties"] = {"x": nxt}
            cur = nxt
        cur["properties"] = {"a": {"type": "string", "pattern": r"^\p{L}+$"},
                             "b": {"type": "string", "pattern": r"^\p{N}+$"}}
        body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}],
                           "tools": [{"type": "function", "function": {"name": "f", "parameters": node}}]}).encode()
        out, dropped = self.k.sanitize_tool_schemas(body, "/v1/chat/completions")
        self.assertIs(out, body)
        self.assertEqual(dropped, [])


class HardeningUnits(unittest.TestCase):
    """The v6.15 decisions as pure units: parseable lengths, the read cap,
    and the warmup hold, all without a socket."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "kproxy_units", HERE.parents[1] / "keepalive-proxy.py")
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.m.UPSTREAM = "http://127.0.0.1:1"   # nothing answers: pool stays unknown
        self.m.invalidate_pool()

    def test_parse_body_length(self):
        self.assertEqual(self.m.parse_body_length(None), 0)
        self.assertEqual(self.m.parse_body_length(""), 0)
        self.assertEqual(self.m.parse_body_length("0"), 0)
        self.assertEqual(self.m.parse_body_length("12"), 12)
        self.assertIsNone(self.m.parse_body_length("abc"))
        self.assertIsNone(self.m.parse_body_length("-5"))
        self.assertIsNone(self.m.parse_body_length("12.5"))

    def test_body_over_cap(self):
        self.m.MAX_BODY_BYTES = 100
        self.assertFalse(self.m.body_over_cap(0))
        self.assertFalse(self.m.body_over_cap(100))
        self.assertTrue(self.m.body_over_cap(101))
        self.m.MAX_BODY_BYTES = 0                  # 0 disables the cap
        self.assertFalse(self.m.body_over_cap(10 ** 12))

    def test_warmup_hold_only_delays_monsters(self):
        self.assertTrue(self.m.warmup_hold(262145, None))
        self.assertFalse(self.m.warmup_hold(262144, None))    # boundary: strictly over
        self.assertFalse(self.m.warmup_hold(10, None))
        self.assertFalse(self.m.warmup_hold(10 ** 9, 467776))  # known pool: not this gate
        self.m.PROMPT_CEILING_TOKENS = 200000
        self.assertTrue(self.m.warmup_hold(200001, None))
        self.assertFalse(self.m.warmup_hold(200000, None))


class TheProxyAgreesWithItselfAboutItsVersion(unittest.TestCase):
    """Three places in this file say which version the proxy IS, and a running box shows
    two of them: the startup line in its journal, and the history block the cockpit reads
    to report the deployed version. v1.15.0 shipped documents announcing v6.20 with a
    header that still said v6.19, and v1.15.1 fixed the header and left the startup line
    behind, so a box printed one version while the cockpit displayed another. A version
    is a fact about the file; three copies of a fact need a gate."""

    def setUp(self):
        self.src = (HERE.parents[1] / "keepalive-proxy.py").read_text()

    def versions(self):
        import re
        doc = re.search(r"in front of SGLang \(v(\d+\.\d+)\)", self.src)
        # "on {BIND}:{port}" since v6.21, "on :{port}" before it: the version is what this
        # asserts, not the shape of the line it sits in.
        banner = re.search(r"log\(f\"v(\d+\.\d+) on ", self.src)
        history = re.search(r"\nv(\d+\.\d+):", self.src)
        for name, m in (("docstring", doc), ("startup banner", banner), ("history", history)):
            self.assertTrue(m, f"the {name} no longer states a version")
        return doc.group(1), banner.group(1), history.group(1)

    def test_the_docstring_the_banner_and_the_history_say_the_same_version(self):
        doc, banner, history = self.versions()
        self.assertEqual(doc, banner, "the docstring and the startup line disagree")
        self.assertEqual(banner, history, "the startup line and the history block disagree")

    def test_the_history_block_the_cockpit_reads_is_the_newest_entry(self):
        """The cockpit takes the FIRST `\nvX.Y:` of the file, so the history has to be
        newest-first or a box reports a version it is not running."""
        import re
        found = [tuple(int(p) for p in v.split(".")) for v in re.findall(r"\nv(\d+\.\d+):", self.src)]
        self.assertGreater(len(found), 5, "the version history went missing")
        self.assertEqual(found, sorted(found, reverse=True), "the history is not newest-first")
        _doc, _banner, history = self.versions()
        self.assertEqual(tuple(int(p) for p in history.split(".")), found[0])


class PathsHttpClientCannotSend(unittest.TestCase):
    """A path this proxy decodes and then hands to http.client, which encodes it as
    ASCII. Anything outside printable ASCII raises UnicodeEncodeError, a ValueError that
    neither the relay's HTTPError nor its OSError arm catches, so the caller used to get
    an empty reply and the journal a traceback, on a route reachable before any client
    key is checked (found in review, 2026-09-21)."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "kproxy_path", HERE.parents[1] / "keepalive-proxy.py")
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def test_a_percent_encoded_non_ascii_byte_is_refused_not_relayed(self):
        for path in ("/h%C3%A9alth", "/%E2%9C%93", "/v1/mod%C3%A8ls"):
            _out, suspect = self.m.canonical_path(path)
            self.assertTrue(suspect, f"{path} would reach http.client and drop the socket")

    def test_a_raw_control_or_non_ascii_byte_in_the_query_is_refused(self):
        """The query goes upstream undecoded, so a raw byte in it reached http.client just
        the same: a control byte is InvalidURL, a byte past 0x7e (the request line is read
        as Latin-1) UnicodeEncodeError (found in review, 2026-09-24)."""
        for path in ("/v1/models?x=\x01", "/v1/models?q=\xc3\xa9", "/v1/chat/completions?a=\x7f",
                     "/v1/models?x=\x1b[2J"):
            _out, suspect = self.m.canonical_path(path)
            self.assertTrue(suspect, f"{path!r} would reach http.client and drop the socket")

    def test_what_http_client_can_send_still_goes_through_decoded(self):
        for path, want in (("/health", "/health"), ("/%76%31/models", "/v1/models"),
                           ("/v1/chat/completions?x=1", "/v1/chat/completions?x=1")):
            out, suspect = self.m.canonical_path(path)
            self.assertFalse(suspect, path)
            self.assertEqual(out, want)

    def test_the_refusal_matches_what_http_client_actually_rejects(self):
        """The gate and the library must agree: every path this says is fine has to be one
        http.client can encode, and that is asserted rather than assumed."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", 1)
        for path in ("/health", "/v1/models", "/%76%31/models", "/a~b", "/a.b-c_d",
                     "/h%C3%A9alth", "/a%20b", "/a%0ab", "/%E2%9C%93", "/a%7fb",
                     "/v1/models?x=1&y=%01", "/v1/models?x=\x01", "/v1/models?q=\xe9"):
            out, suspect = self.m.canonical_path(path)
            try:
                conn._validate_path(out)          # control characters: InvalidURL
                conn._encode_request(out)         # anything past ASCII: UnicodeEncodeError
                encodable = True
            except Exception:
                encodable = False
            if not suspect:
                self.assertTrue(encodable, f"{path} passed the gate and http.client refuses it")


class TopLogprobsCeiling(unittest.TestCase):
    """A field SGLang leaves unbounded and torch turns into a dead scheduler.

    `top_logprobs` (chat) and `logprobs` (completions) both become top_logprobs_num and
    reach `logprobs.topk(max_k, dim=-1)`; past the vocabulary that raises "selected index
    k out of range" inside the scheduler and the engine is gone for every client
    (sglang#40076). The engine cannot defend itself here, so the proxy does, and it
    refuses rather than rewrites: a silently narrowed top-k answers a different question
    than the one the client asked."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "kproxy_ceiling", HERE.parents[1] / "keepalive-proxy.py")
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def over(self, obj, path="/v1/chat/completions"):
        return self.m.top_logprobs_over_ceiling(json.dumps(obj).encode(), path)

    def test_the_chat_field_past_the_ceiling_is_named_with_what_it_asked_for(self):
        self.assertEqual(self.over({"model": "m", "top_logprobs": 1000000}),
                         ("top_logprobs", 1000000))

    def test_the_completions_endpoint_is_guarded_on_its_own_field(self):
        """/v1/completions carries the same crash under a different name: its `logprobs`
        is an int, not a bool, and serving_completions.py hands it to the same top-k."""
        self.assertEqual(self.over({"model": "m", "logprobs": 300000}, "/v1/completions"),
                         ("logprobs", 300000))
        self.assertIsNone(self.over({"model": "m", "top_logprobs": 300000}, "/v1/completions"))

    def test_the_ceiling_itself_is_relayed_and_one_past_it_is_not(self):
        self.m.TOP_LOGPROBS_CEILING = 1024
        self.assertIsNone(self.over({"model": "m", "top_logprobs": 1024}))
        self.assertEqual(self.over({"model": "m", "top_logprobs": 1025})[1], 1025)

    def test_the_chat_logprobs_boolean_is_never_judged_as_a_count(self):
        """ChatCompletionRequest.logprobs is a bool and True is an int in Python: judging
        it would refuse `{"logprobs": true, "top_logprobs": 20}`, the ordinary request."""
        self.assertIsNone(self.over({"model": "m", "logprobs": True, "top_logprobs": 20}))
        self.assertIsNone(self.over({"model": "m", "logprobs": True}, "/v1/completions"))

    def test_what_this_proxy_cannot_read_is_left_to_the_engines_own_validator(self):
        for body in (b'{"top_logprobs": 99999', b'[{"top_logprobs": 99999}]',
                     b'{"top_logprobs": "many"}', b'{"top_logprobs": null}'):
            self.assertIsNone(self.m.top_logprobs_over_ceiling(body, "/v1/chat/completions"), body)

    def test_a_body_without_the_field_is_not_parsed_at_all(self):
        """The hot path is one substring scan: every ordinary completion goes through
        this function and none of them should pay a json.loads for it."""
        called = []
        real = json.loads
        json.loads = lambda *a, **k: (called.append(1), real(*a, **k))[1]
        try:
            self.assertIsNone(self.m.top_logprobs_over_ceiling(
                b'{"model": "m", "messages": []}', "/v1/chat/completions"))
        finally:
            json.loads = real
        self.assertEqual(called, [])

    def test_the_generate_route_is_guarded_and_a_batch_is_judged_element_by_element(self):
        """/generate is relayed too (it is in RID_OVERRIDE_ROUTES) and its
        top_logprobs_num is Optional[Union[List[int], int]]: one oversized entry in a
        batch of ordinary ones is still the crash."""
        self.assertEqual(self.over({"top_logprobs_num": 10 ** 6}, "/generate"),
                         ("top_logprobs_num", 10 ** 6))
        self.assertEqual(self.over({"top_logprobs_num": [4, 10 ** 6, 8]}, "/generate")[1], 10 ** 6)
        self.assertIsNone(self.over({"top_logprobs_num": [4, 8]}, "/generate"))
        self.assertIsNone(self.over({"top_logprobs_num": None}, "/generate"))

    def test_other_routes_are_not_judged(self):
        for path in ("/v1/messages", "/v1/models", "/v1/systemone"):
            self.assertIsNone(self.over({"top_logprobs": 10 ** 6}, path), path)

    def test_a_query_string_does_not_hide_the_route(self):
        self.assertEqual(self.over({"top_logprobs": 10 ** 6}, "/v1/chat/completions?x=1")[1], 10 ** 6)

    def test_zero_disables_the_ceiling_for_an_operator_who_knows_their_build(self):
        self.m.TOP_LOGPROBS_CEILING = 0
        self.assertIsNone(self.over({"model": "m", "top_logprobs": 10 ** 9}))


class SamplingFieldsTheEngineDiesOn(unittest.TestCase):
    """The rest of the family sglang#31597 catalogued, still unbounded in the served
    release because both PRs that bounded them were closed without being merged.
    `stop_token_ids` and `input_ids` index a scatter_add_ and an embedding; `n` expands a
    list before scheduling. The vocabulary decides two of the three, so the guard stands
    down for those when it could not be learned: refusing traffic because a probe failed
    would be a worse bug than the one being prevented."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "kproxy_sampling", HERE.parents[1] / "keepalive-proxy.py")
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        # The probe reads the serving key before it sends anything, and a CI runner has
        # none: _api_key() raised, the probe never left, and the two tests below passed
        # or failed on whether the machine happened to have a key file. Stubbed, so they
        # assert the guard instead of the box (found by CI, 2026-09-22).
        self.m._api_key = lambda: "test-key"

    def refusal(self, obj, path="/v1/chat/completions", vocab=248320):
        return self.m.sampling_field_refusal(json.dumps(obj).encode(), path, vocab)

    def test_a_negative_token_id_is_refused_whatever_the_vocabulary(self):
        for vocab in (0, 248320):
            self.assertIn("negative token id", self.refusal({"stop_token_ids": [-1]}, vocab=vocab) or "")

    def test_an_id_past_the_vocabulary_is_refused_once_it_is_known(self):
        self.assertIn("248320 tokens", self.refusal({"stop_token_ids": [300000]}) or "")
        self.assertIsNone(self.refusal({"stop_token_ids": [300000]}, vocab=0),
                          "unknown vocabulary must not refuse an id it cannot judge")

    def test_the_last_real_id_is_served_and_the_first_unreal_one_is_not(self):
        self.assertIsNone(self.refusal({"stop_token_ids": [248319]}))
        self.assertIsNotNone(self.refusal({"stop_token_ids": [248320]}))

    def test_input_ids_are_judged_the_same_way(self):
        self.assertIsNone(self.refusal({"input_ids": [1, 2, 3]}))
        self.assertIsNotNone(self.refusal({"input_ids": [10 ** 9]}))

    def test_generate_carries_them_inside_sampling_params(self):
        self.assertIsNotNone(self.refusal({"sampling_params": {"stop_token_ids": [10 ** 9]}},
                                          path="/generate"))
        self.assertIsNone(self.refusal({"sampling_params": {"stop_token_ids": [7]}},
                                       path="/generate"))

    def test_n_is_bounded_at_the_value_openai_itself_allows(self):
        self.assertIsNone(self.refusal({"n": 128}))
        self.assertIn("n=129", self.refusal({"n": 129}) or "")
        self.assertIn("n=100000000", self.refusal({"n": 100000000}) or "")

    def test_a_boolean_is_never_read_as_a_count_or_an_id(self):
        self.assertIsNone(self.refusal({"n": True}))
        self.assertIsNone(self.refusal({"stop_token_ids": [True, False]}))

    def test_an_ordinary_request_is_not_parsed_at_all(self):
        called = []
        real = json.loads
        json.loads = lambda *a, **k: (called.append(1), real(*a, **k))[1]
        try:
            self.assertIsNone(self.m.sampling_field_refusal(
                b'{"model": "m", "messages": [], "max_tokens": 10}', "/v1/chat/completions", 248320))
        finally:
            json.loads = real
        self.assertEqual(called, [])

    def test_zero_disables_the_parallel_sample_bound(self):
        self.m.MAX_PARALLEL_SAMPLES = 0
        self.assertIsNone(self.refusal({"n": 10 ** 9}))

    def test_routes_that_do_not_reach_the_sampler_are_left_alone(self):
        for path in ("/v1/messages", "/v1/models", "/v1/systemone"):
            self.assertIsNone(self.refusal({"stop_token_ids": [-1]}, path=path), path)

    def test_the_vocabulary_is_learned_from_the_refusal_that_names_it(self):
        """The engine states its vocabulary in exactly one place: the message refusing an
        out-of-range logit_bias. The probe is built to be refused, so it is rejected at
        the validation boundary and never reaches the scheduler."""
        sent = {}

        class Refused(urllib.error.HTTPError):
            def __init__(self):
                super().__init__("u", 400, "Bad Request", {}, None)

            def read(self):
                return json.dumps({"object": "error", "message":
                                   "logit_bias must has keys in [0, 248319], got 999999999."}).encode()

        def fake_urlopen(req, timeout=None):
            sent["body"] = json.loads(req.data)
            raise Refused()

        real = self.m.urllib.request.urlopen
        self.m.urllib.request.urlopen = fake_urlopen
        try:
            self.m._VOCAB.update(size=0, ts=0.0)
            self.assertEqual(self.m.served_vocab(), 248320)
            self.assertEqual(sent["body"]["logit_bias"], {"999999999": 1})
            self.assertEqual(sent["body"]["max_tokens"], 1, "the probe generates nothing")
            self.m.urllib.request.urlopen = None      # a second call must use the cache
            self.assertEqual(self.m.served_vocab(), 248320)
        finally:
            self.m.urllib.request.urlopen = real
            self.m._VOCAB.update(size=0, ts=0.0)

    def test_an_engine_that_says_nothing_useful_leaves_the_vocabulary_unknown(self):
        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("nothing there")

        real = self.m.urllib.request.urlopen
        self.m.urllib.request.urlopen = fake_urlopen
        try:
            self.m._VOCAB.update(size=0, ts=0.0)
            self.assertEqual(self.m.served_vocab(), 0)
        finally:
            self.m.urllib.request.urlopen = real
            self.m._VOCAB.update(size=0, ts=0.0)

    def test_a_box_with_no_readable_key_stands_the_guard_down(self):
        """_api_key() reads a file, and a file can be missing or unreadable. The probe
        must then leave the vocabulary unknown, which stands the two id checks down,
        rather than refuse traffic it cannot judge."""
        def no_key():
            raise OSError("no key file here")

        self.m._api_key = no_key
        self.m._VOCAB.update(size=0, ts=0.0)
        self.assertEqual(self.m.served_vocab(), 0)
        self.assertIsNone(self.refusal({"stop_token_ids": [300000]},
                                       vocab=self.m.served_vocab()))
        self.assertIsNotNone(self.refusal({"stop_token_ids": [-1]},
                                          vocab=self.m.served_vocab()))

    def test_a_failed_probe_is_not_retried_on_the_next_request(self):
        """A busy engine makes the probe time out. Retrying it per request would put an
        8-second wait in front of every one of them, which is the denial of service this
        guard exists to prevent, delivered by the guard."""
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            raise urllib.error.URLError("busy")

        real = self.m.urllib.request.urlopen
        self.m.urllib.request.urlopen = fake_urlopen
        try:
            self.m._VOCAB.update(size=0, ts=0.0)
            for _ in range(5):
                self.assertEqual(self.m.served_vocab(), 0)
            self.assertEqual(len(calls), 1, "one probe, not one per request")
        finally:
            self.m.urllib.request.urlopen = real
            self.m._VOCAB.update(size=0, ts=0.0)


class ClientStringsAreNotKept(unittest.TestCase):
    """The proxy logs a dropped tool pattern and a moved reasoning_effort once per distinct
    value. It remembered the values themselves, for the life of the process, and printed
    the effort whole: five 20 MB patterns took it from 22 to 137 MiB, five 20 MB effort
    levels to 346 MiB with a 20,000,137-character journal line each (found in review,
    measured 2026-09-24)."""

    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), FakeTokenize)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        os.environ["UPSTREAM"] = f"http://127.0.0.1:{cls.srv.server_port}"
        spec = importlib.util.spec_from_file_location("kproxy_kept", HERE.parents[1] / "keepalive-proxy.py")
        cls.m = importlib.util.module_from_spec(spec)
        sys.argv = ["keepalive-proxy.py"]
        spec.loader.exec_module(cls.m)
        cls.proxy = cls.m.Server(("127.0.0.1", 0), cls.m.H)
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.proxy.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.srv.shutdown()

    def post(self, obj):
        import io
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps(obj).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        err, sys.stderr = sys.stderr, io.StringIO()
        try:
            try:
                urllib.request.urlopen(req, timeout=30).read()
            except urllib.error.HTTPError as e:
                e.read()
            time.sleep(0.1)
            return sys.stderr.getvalue()
        finally:
            sys.stderr = err

    @staticmethod
    def held(memory):
        return list(getattr(memory, "_seen", memory))

    def test_a_dropped_pattern_is_logged_once_and_not_kept(self):
        logs = ""
        for letter in "abca":
            pattern = "\\p{L}" + letter * 1_000_000
            logs += self.post({"model": "m", "messages": [{"role": "user", "content": "hi"}],
                               "tools": [{"type": "function", "function": {"name": "f", "parameters": {
                                   "type": "object", "properties": {"x": {"type": "string", "pattern": pattern}}}}}]})
        self.assertEqual(logs.count("dropped a 'pattern'"), 3, "one line per distinct pattern")
        self.assertTrue(all(len(k) <= 16 for k in self.held(self.m._pattern_drop_logged)),
                        "the proxy keeps the patterns themselves")

    def test_a_moved_effort_is_logged_short_and_not_kept(self):
        logs = ""
        for letter in "xy":
            logs += self.post({"model": "m", "messages": [{"role": "user", "content": "hi"}],
                               "reasoning_effort": letter * 1_000_000})
        lines = [ln for ln in logs.splitlines() if "reasoning_effort=" in ln]
        self.assertEqual(len(lines), 2)
        self.assertLess(max(map(len, lines)), 300, "the whole value went to the journal")
        self.assertTrue(all(len(k) <= 16 for k in self.held(self.m._effort_move_logged)),
                        "the proxy keeps the effort values themselves")

    def test_the_memory_is_bounded(self):
        once = self.m._LoggedOnce(cap=3)
        self.assertEqual([once.first(v) for v in ("a", "b", "a", "c", "d", "e")],
                         [True, True, False, True, True, True])
        self.assertEqual(len(once), 3)
        self.assertTrue(once.first("b"), "the oldest distinct value is forgotten past the cap")
        self.assertFalse(once.first("e"))


class TopLogprobsCeilingEndToEnd(unittest.TestCase):
    """The refusal on the wire, and the engine's own record of what it never received."""

    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), FakeTokenize)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        os.environ["UPSTREAM"] = f"http://127.0.0.1:{cls.srv.server_port}"
        spec = importlib.util.spec_from_file_location(
            "kproxy_ceiling_e2e", HERE.parents[1] / "keepalive-proxy.py")
        cls.m = importlib.util.module_from_spec(spec)
        sys.argv = ["keepalive-proxy.py"]
        spec.loader.exec_module(cls.m)
        cls.m._api_key = lambda: "test-key"
        cls.proxy = cls.m.Server(("127.0.0.1", 0), cls.m.H)
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.proxy.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.srv.shutdown()

    def post(self, obj, path="/v1/chat/completions"):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(obj).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer t"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_the_request_is_refused_400_and_the_engine_never_sees_it(self):
        FakeTokenize.seen = []
        status, body = self.post({"model": "m", "messages": [{"role": "user", "content": "hi"}],
                                  "logprobs": True, "top_logprobs": 1000000})
        self.assertEqual(status, 400)
        message = json.loads(body)["error"]["message"]
        self.assertIn("top_logprobs=1000000", message)
        self.assertIn("sglang#40076", message)
        self.assertEqual(FakeTokenize.seen, [], "nothing reached the upstream")

    def test_a_negative_stop_token_id_is_refused_on_the_wire_too(self):
        """The one of the family that needs no vocabulary, so it holds even when the probe
        cannot run, which is the case here: the fake upstream answers no chat request.

        What does reach the upstream is the vocabulary probe, and only that: the client's
        own request never goes anywhere. The probe is a request built to be refused at the
        engine's validation boundary, so the scheduler never sees it either."""
        FakeTokenize.seen = []
        status, body = self.post({"model": "m", "messages": [{"role": "user", "content": "hi"}],
                                  "min_new_tokens": 1, "stop_token_ids": [-1]})
        self.assertEqual(status, 400)
        message = json.loads(body)["error"]["message"]
        self.assertIn("negative token id", message)
        self.assertIn("sglang#31597", message)
        for _path, sent in FakeTokenize.seen:
            self.assertEqual(sent.get("model"), "probe", f"the client's request was relayed: {sent}")
            self.assertEqual(sent.get("max_tokens"), 1)

    def test_an_oversized_n_is_refused_on_the_wire_too(self):
        FakeTokenize.seen = []
        status, body = self.post({"model": "m", "messages": [{"role": "user", "content": "hi"}],
                                  "n": 100000000})
        self.assertEqual(status, 400)
        self.assertIn("n=100000000", json.loads(body)["error"]["message"])
        self.assertEqual(FakeTokenize.seen, [])

    def test_an_ordinary_n_still_reaches_the_engine(self):
        """The guard must not become the thing that refuses `n: 1`, which many clients
        send on every request."""
        status, _body = self.post({"model": "m", "messages": [{"role": "user", "content": "hi"}],
                                   "n": 1})
        self.assertNotEqual(status, 400, "an ordinary n was refused")


if __name__ == "__main__":
    unittest.main()
