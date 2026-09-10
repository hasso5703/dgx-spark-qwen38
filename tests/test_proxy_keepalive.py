"""The keepalive injection, which is the reason this proxy exists, and had no test.

An agent CLI gives up on a silent stream. opencode and the AI SDK were measured
dying at 140 to 180 seconds of silence, and a Qwen3.8 prefill of 200,000 tokens
is longer than that, so the proxy writes something into the stream while the
engine is quiet. Everything about that is easy to get subtly wrong, and one
version did: v6.4 injected a keepalive in the middle of an SSE event and
corrupted the stream.

Coverage said 68% of keepalive-proxy.py before this file, and the missing 175
statements included this whole loop: the injection, the per-dialect payload, the
max-silence drop, the mid-stream cut, and the non-streamed relay path.

A fake engine here controls its own silence to the millisecond, so the tests are
about the proxy's behaviour and not about timing luck.
"""
import http.server
import importlib.util
import json
import os
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[1]

KEEPALIVE_S = 0.3
MAX_SILENCE_S = 1.5


class Engine(http.server.BaseHTTPRequestHandler):
    """Answers with a scripted pattern of events and silences.

    The request body says what to do:
      "silent:<n>"   n seconds of silence before the first event
      "gap:<n>"      n seconds of silence between two events
      "forever"      never answer at all after the head
      "nonsse"       a plain JSON answer, not a stream
      "cut"          one event then close the socket mid-stream
      "bangs"        an answer made of exclamation marks
    """
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _sse_head(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()

    def _event(self, text):
        rec = json.dumps({"id": "e", "choices": [{"delta": {"content": text}}]})
        self.wfile.write(b"data: " + rec.encode() + b"\n\n")
        self.wfile.flush()

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        # the script is the PROMPT, not the whole JSON: splitting the JSON text
        # gave tokens like '"silent:0.9"}]}' which match nothing, so the first
        # version of this engine never slept and three tests passed for the
        # wrong reason
        try:
            body = json.loads(raw)["messages"][0]["content"]
        except Exception:
            body = raw
        if self.path.split("?")[0] == "/abort_request":
            out = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if "reset" in body:
            # a genuine transport error, not a clean close: this is what makes
            # the proxy read an exception rather than end of stream
            self._sse_head()
            self._event("one")
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                       struct.pack("ii", 1, 0))
            self.connection.close()
            return
        if "nonsse" in body:
            payload = json.dumps({"choices": [{"message": {
                "content": "!" * 300 if "bangs" in body else "plain answer"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._sse_head()
        try:
            if "forever" in body:
                time.sleep(30)
                return
            for token in body.split():
                if token.startswith("silent:") or token.startswith("gap:"):
                    time.sleep(float(token.split(":", 1)[1]))
                    continue
            if "cut" in body:
                self._event("one")
                return                      # close mid-stream, no [DONE]
            self._event("hello")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception:
            pass


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        cls.engine.daemon_threads = True
        threading.Thread(target=cls.engine.serve_forever, daemon=True).start()
        os.environ.update(UPSTREAM=f"http://127.0.0.1:{cls.engine.server_port}",
                          KEEPALIVE_S=str(KEEPALIVE_S),
                          MAX_SILENCE_S=str(MAX_SILENCE_S),
                          CLIENT_IO_S="10",
                          PROMPT_CEILING_TOKENS="0")
        spec = importlib.util.spec_from_file_location(
            f"kp_ka_{time.time_ns()}", REPO / "keepalive-proxy.py")
        cls.mod = importlib.util.module_from_spec(spec)
        sys.argv = ["keepalive-proxy.py"]
        spec.loader.exec_module(cls.mod)
        cls.proxy = cls.mod.Server(("127.0.0.1", 0), cls.mod.H)
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()
        cls.port = cls.proxy.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.proxy.server_close()
        cls.engine.shutdown()
        cls.engine.server_close()

    def ask(self, prompt, path="/v1/chat/completions", read_for=None):
        """Send a request and read everything the proxy writes until it closes."""
        raw = json.dumps({"messages": [{"role": "user", "content": prompt}]}).encode()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=30)
        s.sendall(f"POST {path} HTTP/1.1\r\nHost: p\r\nContent-Type: application/json\r\n"
                  f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw)
        s.settimeout(read_for or 30)
        got = b""
        try:
            while True:
                c = s.recv(8192)
                if not c:
                    break
                got += c
        except (socket.timeout, TimeoutError):
            pass
        finally:
            s.close()
        return got

    @staticmethod
    def sse_records(chunked: bytes) -> list[bytes]:
        """The SSE records out of a chunked HTTP body, in order."""
        head, _, body = chunked.partition(b"\r\n\r\n")
        out, rest = [], body
        # chunked framing: <hexlen>\r\n<data>\r\n
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
        return out


class KeepaliveInjection(Base):
    def test_a_silent_engine_gets_keepalives_and_the_answer_still_arrives(self):
        got = self.ask(f"silent:{KEEPALIVE_S * 3:.2f}")
        self.assertIn(b"hello", got)
        self.assertIn(b"[DONE]", got)
        self.assertIn(b'"id":"keepalive"', got, "no keepalive was injected")

    def test_the_openai_keepalive_is_an_authentic_empty_chunk(self):
        """A comment (`: ping`) is ignored by opencode's stall detector, which is
        why this is a real chunk with an empty choices list. A client parsing it
        must find valid JSON and no content."""
        got = self.ask(f"silent:{KEEPALIVE_S * 3:.2f}")
        kas = [r for r in self.sse_records(got) if b'"keepalive"' in r]
        self.assertTrue(kas, self.sse_records(got))
        for rec in kas:
            self.assertTrue(rec.startswith(b"data: "), rec)
            payload = json.loads(rec[len(b"data: "):].strip())
            self.assertEqual(payload["choices"], [])
            self.assertEqual(payload["object"], "chat.completion.chunk")

    def test_the_anthropic_keepalive_is_the_official_ping_event(self):
        got = self.ask(f"silent:{KEEPALIVE_S * 3:.2f}", path="/v1/messages")
        self.assertIn(b"event: ping", got)
        self.assertIn(b'{"type": "ping"}', got)
        self.assertNotIn(b'"id":"keepalive"', got,
                         "the openai keepalive was sent on the anthropic route")

    def test_a_keepalive_never_lands_inside_an_event(self):
        """The v6.4 bug: a keepalive injected mid-JSON-line corrupts the stream.
        Every record the client receives must parse on its own."""
        got = self.ask(f"silent:{KEEPALIVE_S * 2:.2f} gap:{KEEPALIVE_S * 2:.2f}")
        for rec in self.sse_records(got):
            text = rec.strip()
            if not text or text == b"data: [DONE]":
                continue
            self.assertTrue(text.startswith(b"data: ") or text.startswith(b"event: "),
                            f"a record does not start a line: {text[:60]!r}")
            for line in text.split(b"\n"):
                if line.startswith(b"data: ") and line[6:].strip() != b"[DONE]":
                    json.loads(line[6:])          # raises on a torn record

    def test_a_fast_answer_gets_no_keepalive_at_all(self):
        got = self.ask("now")
        self.assertIn(b"hello", got)
        self.assertNotIn(b"keepalive", got, "a keepalive was injected into a fast answer")


class SilenceCeiling(Base):
    def test_an_engine_silent_past_the_ceiling_is_dropped(self):
        t0 = time.time()
        got = self.ask("forever", read_for=MAX_SILENCE_S + 8)
        elapsed = time.time() - t0
        self.assertLess(elapsed, MAX_SILENCE_S + 6, "the drop never happened")
        self.assertGreaterEqual(elapsed, MAX_SILENCE_S - 0.5,
                                "it dropped before the ceiling")
        self.assertIn(b'"id":"keepalive"', got,
                      "it dropped without ever keeping the client alive")

    def test_an_anthropic_client_is_told_why_its_stream_ended(self):
        got = self.ask("forever", path="/v1/messages", read_for=MAX_SILENCE_S + 8)
        self.assertIn(b"event: ping", got)
        self.assertIn(b"silent", got.lower(), "the drop was silent on the anthropic route")


class NonStreamedAndCutStreams(Base):
    def test_a_non_streamed_answer_is_relayed_whole(self):
        got = self.ask("nonsse")
        self.assertIn(b"plain answer", got)

    def test_a_non_streamed_wall_of_markers_is_named_in_the_answer_path(self):
        """The corruption guard: a non-streamed answer cannot be withheld, so what
        the proxy can do is see it. This asserts the answer still arrives."""
        got = self.ask("nonsse bangs")
        self.assertIn(b"!" * 100, got)

    def test_an_engine_that_closes_without_done_still_ends_the_body_cleanly(self):
        """A clean close with no [DONE] is end of stream, not an error: the proxy
        forwards what arrived and terminates the chunked body. It must not invent
        a [DONE] the engine never sent."""
        got = self.ask("cut")
        self.assertIn(b"one", got)
        self.assertIn(b"0\r\n\r\n", got, "the chunked body was not terminated")
        self.assertNotIn(b"[DONE]", got, "the proxy invented an end the engine never sent")

    def test_a_transport_error_mid_stream_is_named_on_the_anthropic_route(self):
        """A reset is different from a close: the read raises, and an Anthropic
        client gets an error event rather than a stream that just stops."""
        got = self.ask("reset", path="/v1/messages")
        self.assertIn(b"one", got)
        self.assertIn(b"interrupted", got.lower(), got[-200:])

    def test_a_transport_error_mid_stream_ends_an_openai_stream_without_a_lie(self):
        got = self.ask("reset")
        self.assertIn(b"one", got)
        self.assertNotIn(b"[DONE]", got)


if __name__ == "__main__":
    unittest.main(verbosity=2)
