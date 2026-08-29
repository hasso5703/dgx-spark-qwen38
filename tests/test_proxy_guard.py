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

    def test_engine_unreachable_returns_none(self):
        saved = self.mod.UPSTREAM
        self.mod.UPSTREAM = "http://127.0.0.1:1"
        try:
            self.assertIsNone(self.mod.tokenize_count(json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode(), "/v1/chat/completions"))
        finally:
            self.mod.UPSTREAM = saved

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

    def test_margin_default(self):
        self.assertAlmostEqual(self.mod.OVERSIZE_MARGIN_FRAC, 0.08)
        self.assertEqual(int(178560 * (1 - self.mod.OVERSIZE_MARGIN_FRAC)), 164275)



if __name__ == "__main__":
    unittest.main()
