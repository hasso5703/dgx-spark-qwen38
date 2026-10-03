#!/usr/bin/env python3
"""selftest.py: seven real requests the way clients send them, and a verdict.

The fake server answers like the proxy in front of a healthy lane, and each test breaks one
thing: a wrong answer, a stream with no [DONE], no tool call, a passphrase not found, one of
four concurrent answers wrong, the Anthropic route missing, a busy engine, nobody listening."""
import http.server
import importlib.util
import io
import json
import re
import socket
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("selftest", REPO / "selftest.py")
selftest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(selftest)


class Fake(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        s = self.server
        s.seen.append(("GET", self.path, self.headers.get("Authorization")))
        if self.path == "/metrics":
            return self._send(200, f"sglang:num_running_reqs{{a=\"b\"}} {s.busy}\nsglang:num_queue_reqs 0.0\n", "text/plain")
        if self.path == "/v1/models":
            if "models500" in s.broken:
                return self._send(500, "Internal Server Error", "text/plain")
            if "modelsgarbage" in s.broken:
                return self._send(200, {"object": "list"})
            return self._send(200, {"object": "list", "data": [{"id": "qwen3.8-flash-next"}]})
        self._send(404, {"error": "no"})

    def do_POST(self):
        s = self.server
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        s.seen.append(("POST", self.path, self.headers.get("Authorization")))
        if "everything503" in s.broken:
            return self._send(503, {"error": {"type": "engine_unavailable"}})
        if self.path == "/v1/messages":
            if "anthropic" in s.broken:
                return self._send(404, {"error": "no such route"})
            if "anthropicgarbage" in s.broken:
                return self._send(200, {"type": "error"})
            return self._send(200, {"type": "message", "content": [{"type": "text", "text": "42"}]})
        content = body["messages"][0]["content"]
        if body.get("tools"):
            if "tool" in s.broken:
                return self._send(200, {"choices": [{"message": {"content": "It is sunny.", "tool_calls": []}}]})
            args = '{"city": "Par' if "toolargs" in s.broken else '{"city": "Paris"}'
            call = {"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": args}}
            return self._send(200, {"choices": [{"message": {"content": None, "tool_calls": [call]}}]})
        m = re.search(r"The passphrase is (\S+)\.", content)
        if m:
            said = "nothing" if "needle" in s.broken else m.group(1)
            if "nousage" in s.broken:
                return self._send(200, {"choices": [{"message": {"content": said}}]})
            return self._send(200, {"choices": [{"message": {"content": said}}], "usage": {"prompt_tokens": 14211}})
        m = re.search(r"What is (\d+) \+ (\d+)\?", content)
        total = int(m.group(1)) + int(m.group(2))
        if "concurrent" in s.broken and total == 11 + 23 + 7 * 2 + 5 * 2:
            total += 1
        if "answer" in s.broken:
            total += 1
        if body.get("stream"):
            chunks = [{"choices": [{"index": 0, "delta": {"content": str(total)[:1]}, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {"content": str(total)[1:]}, "finish_reason": "stop"}]}]
            text = ": a comment line\n\ndata: not json\n\n" + "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
            if "streamerror" in s.broken:
                text = 'data: {"error": {"message": "the engine stopped"}}\n\n'
            if "stream503" in s.broken:
                return self._send(503, "starting", "text/plain")
            if "stream" not in s.broken:
                text += "data: [DONE]\n\n"
            return self._send(200, text, "text/event-stream")
        if "answerhtml" in s.broken:
            return self._send(200, "<html>proxy page</html>", "text/html")
        self._send(200, {"choices": [{"message": {"content": str(total)}}]})


class Base(unittest.TestCase):
    BROKEN = ()
    BUSY = 0

    def setUp(self):
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        self.srv.daemon_threads = True
        self.srv.broken, self.srv.busy, self.srv.seen = set(self.BROKEN), self.BUSY, []
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        port = self.srv.server_address[1]
        self.client = selftest.Client(port, "the-key", engine_port=port)

    def run_main(self, *args):
        out = io.StringIO()
        rc = selftest.main(list(args), client=self.client, out=out)
        return rc, out.getvalue()


class AHealthyLane(Base):
    def test_every_check_passes_and_says_what_it_got(self):
        rc, out = self.run_main()
        self.assertEqual(rc, 0, out)
        self.assertIn("All 7 checks passed.", out)
        for want in ("qwen3.8-flash-next", "answered '42'", "[DONE] seen", 'get_weather({"city": "Paris"})',
                     "found in 14,211 prompt tokens", "4 of 4 right"):
            self.assertIn(want, out)

    def test_the_key_goes_in_the_header_and_nowhere_in_the_output(self):
        rc, out = self.run_main("--report")
        self.assertEqual(rc, 0)
        self.assertNotIn("the-key", out)
        self.assertTrue(all(auth == "Bearer the-key" for _, _, auth in self.srv.seen))
        self.assertIn("| tool call | ok |", out)

    def test_it_asks_the_engine_whether_it_is_busy_first(self):
        self.run_main()
        self.assertEqual(self.srv.seen[0][:2], ("GET", "/metrics"))


class EachThingThatCanGoWrong(Base):
    def broken(self, what):
        self.srv.broken = {what}
        rc, out = self.run_main()
        self.assertEqual(rc, 1, out)
        return out

    def test_a_wrong_answer(self):
        out = self.broken("answer")
        self.assertIn("FAIL  answer", out)

    def test_a_stream_without_done(self):
        self.assertIn("[DONE] missing", self.broken("stream"))

    def test_no_tool_call(self):
        self.assertIn("no tool call; content 'It is sunny.'", self.broken("tool"))

    def test_the_passphrase_not_found(self):
        self.assertIn("not found in 14,211 prompt tokens", self.broken("needle"))

    def test_one_concurrent_answer_wrong(self):
        self.assertIn("3 of 4 right", self.broken("concurrent"))

    def test_the_anthropic_route_missing(self):
        out = self.broken("anthropic")
        self.assertIn("FAIL  Anthropic dialect", out)
        self.assertIn("1 of 7 checks failed: Anthropic dialect.", out)


    def test_the_model_list_failing_or_empty(self):
        self.assertIn("HTTP 500: Internal Server Error", self.broken("models500"))
        self.assertIn("not a model list", self.broken("modelsgarbage"))

    def test_an_answer_that_is_not_json(self):
        out = self.broken("answerhtml")
        self.assertIn("HTTP 200: <html>proxy page</html>", out)

    def test_a_stream_that_ends_on_an_error_event_or_never_starts(self):
        self.assertIn("error event: {'message': 'the engine stopped'}", self.broken("streamerror"))
        self.assertIn("HTTP 503: starting", self.broken("stream503"))

    def test_tool_arguments_that_do_not_parse(self):
        self.assertIn("arguments that are not JSON", self.broken("toolargs"))

    def test_a_passphrase_found_without_a_usage_block_still_counts(self):
        self.srv.broken = {"nousage"}
        rc, out = self.run_main()
        self.assertEqual(rc, 0, out)
        self.assertIn("found in 2,200 words", out)

    def test_an_anthropic_answer_that_is_not_a_message(self):
        self.assertIn("not a message", self.broken("anthropicgarbage"))

    def test_an_engine_that_answers_503_everywhere(self):
        self.srv.broken = {"everything503"}
        rc, out = self.run_main()
        self.assertEqual(rc, 1)
        self.assertIn("6 of 7 checks failed", out, "the model list is a GET and still answers")


class ABusyEngine(Base):
    BUSY = 3

    def test_it_refuses_and_says_why(self):
        rc, out = self.run_main()
        self.assertEqual(rc, 3)
        self.assertIn("serving 3 request(s)", out)
        self.assertEqual([p for _, p, _ in self.srv.seen], ["/metrics"], "nothing else was sent")

    def test_force_runs_it_anyway(self):
        rc, out = self.run_main("--force")
        self.assertEqual(rc, 0, out)


class NobodyListening(unittest.TestCase):
    def test_every_check_fails_with_the_reason(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        out = io.StringIO()
        rc = selftest.main([], client=selftest.Client(port, "k", engine_port=port), out=out)
        self.assertEqual(rc, 1)
        self.assertIn("ConnectionRefusedError", out.getvalue())
        self.assertIn("7 of 7 checks failed", out.getvalue())


class TheCommandLine(unittest.TestCase):
    def test_help_usage_and_a_missing_key(self):
        out = io.StringIO()
        self.assertEqual(selftest.main(["--help"], out=out), 0)
        self.assertIn("--force", out.getvalue())
        self.assertEqual(selftest.main(["--bogus"], out=io.StringIO()), 2)
        self.assertEqual(selftest.main(["--port", "x"], out=io.StringIO()), 2)
        saved = selftest.KEY_FILE
        selftest.KEY_FILE = "/nonexistent/api-key"
        try:
            out = io.StringIO()
            self.assertEqual(selftest.main([], out=out), 3)
            self.assertIn("No API key", out.getvalue())
        finally:
            selftest.KEY_FILE = saved

    def test_a_metrics_page_that_says_nothing_is_not_busy(self):
        class C(selftest.Client):
            def get(self, path, port=None):
                return 200, "# nothing here\n"
        self.assertIsNone(C(1, "k").busy())

        class D(selftest.Client):
            def get(self, path, port=None):
                raise ConnectionRefusedError()
        self.assertIsNone(D(1, "k").busy())

        class E(selftest.Client):
            def get(self, path, port=None):
                return 401, ""
        self.assertIsNone(E(1, "k").busy())


if __name__ == "__main__":
    unittest.main(verbosity=2)
