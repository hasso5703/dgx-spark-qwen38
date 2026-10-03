"""A stream before the engine answers (proxy v6.31).

SGLang sends a stream's status and headers with its first chunk, so only after the request
was queued and its prompt computed (serving_chat.py, _handle_streaming_request). The proxy
relayed those headers when they came, wrote nothing before, and did not watch the caller.
On 2026-10-03 eight agents overflowed the KV pool, waited longer than opencode's 300 s for
headers, gave up and sent again, and each request they gave up on stayed queued in the engine
until its turn: 704 in a night, and 163 requests in the queue at the end, for eight agents.

The fake engine here does what SGLang does: it holds its headers back for as long as the
request says, and it records when the proxy closes the connection while it waits, which is
how SGLang sees a caller gone (its disconnect check, every 4 s, aborts the request).

    "head:<n>"     n seconds before the headers, then one event and [DONE]
    "refuse:<n>"   n seconds before an HTTP 400 with a JSON error
    "gone:<n>"     n seconds before an HTTP 503 with no body, as SGLang answers while it stops
    "never"        no headers at all
"""
import http.client
import http.server
import importlib.util
import io
import json
import os
import select
import socket
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[1]


def _keep_env(cls):
    saved = dict(os.environ)
    cls.addClassCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))


class Engine(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen = {}                     # prompt -> {"start": t, "closed": t or None, "answered": t or None}

    def log_message(self, *a):
        pass

    def _wait(self, secs, rec):
        """Sleep, unless the proxy closes its side first: then record it, as SGLang would."""
        end = time.time() + secs
        while time.time() < end:
            r, _, _ = select.select([self.connection], [], [], 0.02)
            if r:
                try:
                    if not self.connection.recv(1, socket.MSG_PEEK):
                        rec["closed"] = time.time()
                        return False
                except OSError:
                    rec["closed"] = time.time()
                    return False
        return True

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        if self.path.split("?")[0] == "/abort_request":
            out = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        try:
            prompt = json.loads(raw)["messages"][0]["content"]
        except Exception:
            prompt = raw
        rec = {"start": time.time(), "closed": None, "answered": None, "rid": self.headers.get("x-override-rid")}
        Engine.seen[prompt] = rec
        kind, _, n = prompt.partition(":")
        secs = 3600.0 if kind == "never" else float(n or 0)
        if not self._wait(secs, rec):
            return
        rec["answered"] = time.time()
        if kind == "gone":
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if kind == "refuse":
            out = json.dumps({"object": "error", "message": "bad request from the engine", "code": 400}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if '"stream": false' in raw:
            out = json.dumps({"choices": [{"message": {"content": "whole answer"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Engine", "1")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("X-Engine", "1")
        self.send_header("Connection", "close")
        self.end_headers()
        rec_ = json.dumps({"id": "e", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "hello"}}]})
        self.wfile.write(b"data: " + rec_.encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class Base(unittest.TestCase):
    KEEPALIVE_S = 0.3
    MAX_SILENCE_S = 30.0

    @classmethod
    def setUpClass(cls):
        _keep_env(cls)
        cls.engine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        cls.engine.daemon_threads = True
        threading.Thread(target=cls.engine.serve_forever, daemon=True).start()
        os.environ.update(UPSTREAM=f"http://127.0.0.1:{cls.engine.server_port}",
                          KEEPALIVE_S=str(cls.KEEPALIVE_S), MAX_SILENCE_S=str(cls.MAX_SILENCE_S),
                          CLIENT_IO_S="10", PROMPT_CEILING_TOKENS="0")
        spec = importlib.util.spec_from_file_location(f"kp_head_{time.time_ns()}", REPO / "keepalive-proxy.py")
        cls.mod = importlib.util.module_from_spec(spec)
        sys.argv = ["keepalive-proxy.py"]
        spec.loader.exec_module(cls.mod)
        cls.log = io.StringIO()
        cls.proxy = cls.mod.Server(("127.0.0.1", 0), cls.mod.H)
        cls.err = redirect_stderr(cls.log)
        cls.err.__enter__()
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()
        cls.port = cls.proxy.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.err.__exit__(None, None, None)
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.engine.shutdown()
        cls.engine.server_close()

    def send(self, prompt, stream=True, path="/v1/chat/completions"):
        body = {"model": "m", "messages": [{"role": "user", "content": prompt}], "stream": stream}
        raw = json.dumps(body).encode()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=30)
        s.sendall(f"POST {path} HTTP/1.1\r\nHost: p\r\nContent-Type: application/json\r\n"
                  f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw)
        return s

    @staticmethod
    def read_all(s, limit=30):
        s.settimeout(limit)
        got, marks, t0 = b"", [], time.time()
        try:
            while True:
                c = s.recv(65536)
                if not c:
                    break
                marks.append((round(time.time() - t0, 2), c))
                got += c
        except (socket.timeout, TimeoutError):
            pass
        finally:
            s.close()
        return got, marks

    @staticmethod
    def records(chunked):
        head, _, body = chunked.partition(b"\r\n\r\n")
        out, rest = [], body
        while True:
            line, _, rest = rest.partition(b"\r\n")
            if not line:
                break
            try:
                n = int(line.split(b";")[0], 16)
            except ValueError:
                break
            if n == 0:
                break
            out.append(rest[:n])
            rest = rest[n + 2:]
        return head, out

    def end_line(self, prompt_peer_hint=None):
        return [ln for ln in self.log.getvalue().splitlines() if " in " in ln and "POST /v1/chat/completions" in ln]


class AStreamHearsFromTheProxyBeforeTheEngine(Base):
    def test_a_waiting_stream_gets_its_headers_and_keepalives_then_the_answer(self):
        got, marks = self.read_all(self.send("head:1.5"))
        head, recs = self.records(got)
        self.assertTrue(head.startswith(b"HTTP/1.1 200"), head[:60])
        self.assertLess(marks[0][0], 1.0, "the first bytes came with the engine's, not before")
        keep = [r for r in recs if b'"id":"keepalive"' in r]
        real = [r for r in recs if b'"hello"' in r]
        self.assertGreaterEqual(len(keep), 2, recs)
        self.assertEqual(len(real), 1, recs)
        self.assertLess(recs.index(keep[0]), recs.index(real[0]))
        self.assertIn(b"data: [DONE]", b"".join(recs))
        for r in recs:
            self.assertTrue(r.startswith(b"data: ") and r.endswith(b"\n\n"), r)
        self.assertIn("stream opened, keepalives until it does", self.log.getvalue())

    def test_a_stream_the_engine_answers_quickly_is_the_engines(self):
        got, _ = self.read_all(self.send("head:0.05"))
        head, recs = self.records(got)
        self.assertIn(b"X-Engine: 1", head, "the engine's own headers")
        self.assertFalse([r for r in recs if b'"id":"keepalive"' in r], recs)

    def test_a_quick_refusal_keeps_its_status(self):
        got, _ = self.read_all(self.send("refuse:0.05"))
        self.assertTrue(got.startswith(b"HTTP/1.1 400"), got[:80])
        self.assertIn(b"bad request from the engine", got)

    def test_a_late_refusal_is_an_error_event_of_the_opened_stream(self):
        got, _ = self.read_all(self.send("refuse:1.2"))
        head, recs = self.records(got)
        self.assertTrue(head.startswith(b"HTTP/1.1 200"), head[:60])
        errs = [r for r in recs if b'"error"' in r]
        self.assertEqual(len(errs), 1, recs)
        self.assertIn(b"bad request from the engine", errs[0])

    def test_an_engine_that_stops_meanwhile_is_an_error_event_of_the_opened_stream(self):
        got, _ = self.read_all(self.send("gone:1.2"))
        head, recs = self.records(got)
        self.assertTrue(head.startswith(b"HTTP/1.1 200"), head[:60])
        self.assertEqual(head.count(b"HTTP/1.1"), 1, "one status line, the stream's")
        errs = [r for r in recs if b'"error"' in r]
        self.assertEqual(len(errs), 1, recs)
        self.assertIn(b"engine_unavailable", errs[0])

    def test_a_non_streamed_request_is_not_opened_early(self):
        got, marks = self.read_all(self.send("head:1.0", stream=False))
        self.assertTrue(got.startswith(b"HTTP/1.1 200"), got[:60])
        self.assertIn(b"X-Engine: 1", got)
        self.assertIn(b"whole answer", got)
        self.assertGreaterEqual(marks[0][0], 0.9, "a non-streamed answer comes whole, nothing before it")


class ACallerWhoLeavesBeforeTheEngineAnswers(Base):
    def _leave(self, prompt, after):
        s = self.send(prompt)
        time.sleep(after)
        s.close()
        rec = None
        for _ in range(100):
            rec = Engine.seen.get(prompt)
            if rec and rec["closed"]:
                break
            time.sleep(0.05)
        return rec

    def test_after_the_stream_was_opened_the_upstream_is_ended(self):
        rec = self._leave("head:6", 0.8)
        self.assertIsNotNone(rec["closed"], "the engine never saw the caller go")
        self.assertLess(rec["closed"] - rec["start"], 2.5, rec)
        self.assertIsNone(rec["answered"])
        for _ in range(50):
            if any("CLIENT GONE before the engine answered" in ln for ln in self.end_line()):
                break
            time.sleep(0.05)
        self.assertTrue(any("CLIENT GONE before the engine answered" in ln for ln in self.end_line()), self.end_line())


class ACallerWhoLeavesBeforeAnyKeepalive(Base):
    KEEPALIVE_S = 5.0

    def test_the_upstream_is_ended_without_waiting_for_a_keepalive(self):
        s = self.send("head:6.5")
        time.sleep(0.3)
        s.close()
        rec = None
        for _ in range(60):
            rec = Engine.seen.get("head:6.5")
            if rec and rec["closed"]:
                break
            time.sleep(0.05)
        self.assertIsNotNone(rec and rec["closed"], "the engine never saw the caller go")
        self.assertLess(rec["closed"] - rec["start"], 2.0, rec)


class AnEngineSilentBeforeItsHead(Base):
    MAX_SILENCE_S = 1.5

    def test_the_request_is_dropped_past_max_silence(self):
        t0 = time.time()
        got, _ = self.read_all(self.send("never"), limit=10)
        took = time.time() - t0
        head, recs = self.records(got)
        self.assertTrue(head.startswith(b"HTTP/1.1 200"), head[:60])
        self.assertLess(took, self.MAX_SILENCE_S + 3, "the drop never happened")
        rec = Engine.seen.get("never")
        for _ in range(60):
            if rec and rec["closed"]:
                break
            time.sleep(0.05)
        self.assertIsNotNone(rec["closed"], "the engine still holds the request")
        self.assertTrue(any("DROPPED upstream silent" in ln for ln in self.end_line()), self.end_line())

    def test_an_anthropic_stream_is_told_why(self):
        got, _ = self.read_all(self.send("never", path="/v1/messages"), limit=10)
        head, recs = self.records(got)
        self.assertTrue(any(b"event: error" in r and b"upstream silent" in r for r in recs), recs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
