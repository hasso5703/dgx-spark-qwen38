#!/usr/bin/env python3
"""A request is held while its engine comes back (keepalive-proxy v6.28, issue #26).

systemd starts a crashed engine again 15 s after it stops and the boot takes 7 to 11 min.
Every request of that window got a 503 at once, and opencode 1.18.32 retried a 503 six
times on its Retry-After, then failed the session after 151 s (measured against a fake
engine, 2026-10-01). The proxy now holds a request that finds the engine not answering
while a unit named in PROXY_HOLD_UNITS is on its way back: a streamed one gets its 200 and
the keepalives at once (opencode and Claude Code both waited 400 s on that, then took the
answer), a non-streamed one waits in silence, and a hold that fails ends a stream with an
error event, which both clients answer by sending the request again.

Each test runs the real proxy against an engine this file starts and stops on one port,
and a systemctl that reads the units' state from a file."""
import http.client
import http.server
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
PROXY = HERE.parents[1] / "keepalive-proxy.py"
UNITS = "qwen38-sglang.service qwen38-flash.service"


def free_port():
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


class Engine(http.server.BaseHTTPRequestHandler):
    """SGLang as the proxy sees it: its info route, a generation in both dialects, /tokenize.
    The server's `mode` is "up" or "starting" (503, as SGLang answers while it loads) for
    its generations, `info_mode` the same for its info route; `pool` is what that route
    reports; `generations` lists what it was asked to generate."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        out = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def _empty(self, code):
        self.send_response(code); self.send_header("Content-Length", "0"); self.end_headers()

    def do_GET(self):
        if self.path in ("/server_info", "/get_server_info"):
            if self.server.info_mode == "starting":
                return self._empty(503)
            return self._json(200, {"max_total_num_tokens": self.server.pool})
        if self.server.mode == "starting":
            return self._empty(503)
        self._json(200, {"data": [{"id": "m"}]})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.server.mode == "starting":
            return self._empty(503)
        if self.path == "/tokenize":
            return self._json(200, {"count": len(body) // 4})
        self.server.generations.append(self.path)
        req = json.loads(body or b"{}")
        if self.server.mode == "plain400":
            out = b"bad request, said in plain text"
            self.send_response(400); self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out); return
        if not req.get("stream") or self.server.mode == "json-for-stream":
            return self._json(200, {"id": "x", "object": "chat.completion", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "four"}, "finish_reason": "stop"}]})
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
        def c(b):
            self.wfile.write(b"%x\r\n" % len(b) + b + b"\r\n"); self.wfile.flush()
        if self.path.startswith("/v1/messages"):
            for name, data in (("message_start", {"type": "message_start", "message": {"id": "m1", "type": "message",
                                "role": "assistant", "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}}}),
                               ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                "delta": {"type": "text_delta", "text": "four"}}),
                               ("message_stop", {"type": "message_stop"})):
                c(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        else:
            c(b'data: {"id":"g1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"four"},"finish_reason":null}]}\n\n')
            c(b'data: {"id":"g1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n')
            c(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()


class Box:
    """One engine port, one proxy, one systemctl state file. The proxy runs as its own
    process, or (inproc) in this one, where the coverage gate of CI can see its lines."""

    def __init__(self, test, units=UNITS, hold_max="30", state="activating", inproc=False):
        self.test = test
        self.t = Path(tempfile.mkdtemp(prefix="proxy-hold-"))
        test.addCleanup(shutil.rmtree, self.t, True)
        (self.t / "home/.config/qwen38").mkdir(parents=True)
        (self.t / "home/.config/qwen38/api-key").write_text("test-key\n")
        (self.t / "bin").mkdir()
        self.state_file, self.jobs_file = self.t / "state", self.t / "jobs"
        self.state(state)
        # systemctl show A B -p ActiveState --value prints one state per unit, and -p
        # InvocationID one id per unit (v6.35), the same ids all along: no unit starts again
        # in these tests; list-jobs prints the jobs file (none by default); is-active (the
        # image and video lanes' question) says inactive
        (self.t / "bin/systemctl").write_text(
            "#!/bin/sh\n"
            f'case "$*" in *InvocationID*) for u in {units}; do echo "inv-$u"; echo; done; exit 0;; esac\n'
            f'if [ "$1" = show ]; then for u in {units}; do cat "{self.state_file}"; done; exit 0; fi\n'
            f'if [ "$1" = list-jobs ]; then cat "{self.jobs_file}" 2>/dev/null; exit 0; fi\n'
            "exit 3\n")
        (self.t / "bin/systemctl").chmod(0o755)
        self.engine_port, self.port = free_port(), free_port()
        self.srv = None
        self.generations = []
        env = dict(os.environ, UPSTREAM=f"http://127.0.0.1:{self.engine_port}", HOME=str(self.t / "home"),
                   PATH=f"{self.t / 'bin'}:{os.environ.get('PATH', '')}", KEEPALIVE_S="1",
                   PROXY_HOLD_MAX_S=hold_max, PROXY_HOLD_POLL_S="0.2")
        env.pop("PROXY_HOLD_UNITS", None)
        if units:
            env["PROXY_HOLD_UNITS"] = units
        self.inproc, self.lines = inproc, []
        test.addCleanup(self.close)
        if inproc:
            saved_env, saved_argv = dict(os.environ), list(sys.argv)
            test.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved_env)))
            test.addCleanup(setattr, sys, "argv", saved_argv)
            os.environ.clear(); os.environ.update(env)
            spec = importlib.util.spec_from_file_location(f"kp_hold_{time.time_ns()}", PROXY)
            self.mod = importlib.util.module_from_spec(spec)
            sys.argv = ["keepalive-proxy.py"]
            spec.loader.exec_module(self.mod)
            self.mod.log = lambda msg: self.lines.append(f"[proxy] {msg}")
            self.proxy = self.mod.Server(("127.0.0.1", 0), self.mod.H)
            threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
            self.port = self.proxy.server_address[1]
            return
        self.log = open(self.t / "proxy.log", "w")
        self.proc = subprocess.Popen([sys.executable, str(PROXY), str(self.port)], env=env,
                                     stdout=subprocess.DEVNULL, stderr=self.log)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close(); break
            except OSError:
                time.sleep(0.05)

    def state(self, s):
        self.state_file.write_text(s + "\n")

    def jobs(self, *lines):
        """What `systemctl list-jobs --no-legend` prints: JOB UNIT TYPE STATE."""
        self.jobs_file.write_text("".join(ln + "\n" for ln in lines))

    def start_engine(self, mode="up", pool=200000, info_mode=None):
        if self.srv is None:
            self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", self.engine_port), Engine)
            self.srv.generations = self.generations
            threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.srv.mode, self.srv.pool, self.srv.info_mode = mode, pool, info_mode or mode

    def stop_engine(self):
        if self.srv is not None:
            self.srv.shutdown(); self.srv.server_close(); self.srv = None

    def close(self):
        if self.inproc:
            self.proxy.shutdown(); self.proxy.server_close()
        else:
            self.proc.terminate()
            try: self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired: self.proc.kill()
            self.log.close()
        if self.srv is not None:
            self.srv.shutdown(); self.srv.server_close()

    def journal(self):
        if self.inproc:
            return "\n".join(self.lines)
        self.log.flush()
        return (self.t / "proxy.log").read_text()

    def journal_with(self, text, within=10):
        """The journal once it holds `text`, or as it is after `within` s. The proxy writes a
        request's outcome line after the answer's last byte, so the caller can be done before
        it is written (2 runs in 36 under load read the journal too early, 2026-10-01)."""
        deadline = time.time() + within
        while text not in self.journal() and time.time() < deadline:
            time.sleep(0.05)
        return self.journal()

    def send(self, path="/v1/chat/completions", stream=True, content="What is 2+2?", timeout=60):
        """Start a request; returns a Reply that reads the answer in the background."""
        body = json.dumps({"model": "m", "stream": stream, "max_tokens": 8,
                           "messages": [{"role": "user", "content": content}]}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json",
                                                       "Authorization": "Bearer test-key"})
        return Reply(conn)


class Reply:
    def __init__(self, conn):
        self.conn, self.status, self.events, self.raw, self.err = conn, None, [], b"", None
        self.t0, self.first_at = time.time(), None
        self.done = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            r = self.conn.getresponse()
            self.status, self.ctype, self.first_at = r.status, r.getheader("Content-Type") or "", time.time() - self.t0
            buf = b""
            while True:
                c = r.read1(65536)
                if not c:
                    break
                self.raw += c; buf += c
                while b"\n\n" in buf:
                    ev, buf = buf.split(b"\n\n", 1)
                    self.events.append(ev.decode("utf-8", "replace"))
        except Exception as e:                       # noqa: BLE001 - recorded for the assertion
            self.err = e
        finally:
            self.done.set()

    def keepalives(self):
        return [e for e in self.events if '"id":"keepalive"' in e or e.startswith("event: ping")]

    def content(self):
        return [e for e in self.events if '"id":"keepalive"' not in e and not e.startswith("event: ping")]


class AStreamedRequestWaitsForItsEngine(unittest.TestCase):
    INPROC = False

    def box(self, **kw):
        return Box(self, inproc=self.INPROC, **kw)

    def test_it_hears_from_the_proxy_at_once_and_gets_the_answer_once_the_engine_is_back(self):
        box = self.box()
        reply = box.send()
        time.sleep(3.5)
        self.assertEqual(reply.status, 200, "the 200 and the stream's headers came at once")
        self.assertLess(reply.first_at, 1.5)
        self.assertIn("text/event-stream", reply.ctype)
        self.assertGreaterEqual(len(reply.keepalives()), 2, reply.events)
        self.assertEqual(reply.content(), [])
        self.assertEqual(box.generations, [], "nothing reached an engine that is not there")
        box.start_engine()
        self.assertTrue(reply.done.wait(15))
        self.assertIsNone(reply.err)
        content = reply.content()
        self.assertTrue(any('"content":"four"' in e for e in content), content)
        self.assertEqual(content[-1], "data: [DONE]")
        self.assertEqual(box.generations, ["/v1/chat/completions"], "relayed once, when the engine answered")
        log = box.journal()
        self.assertIn("holding: the engine does not answer", log)
        self.assertIn("the engine answers again after", log)
        self.assertIn("POST /v1/chat/completions ok in", box.journal_with("POST /v1/chat/completions ok in"))

    def test_an_engine_that_answers_503_while_it_starts_is_waited_for_too(self):
        box = self.box()
        box.start_engine(mode="starting")
        reply = box.send()
        time.sleep(2)
        self.assertEqual(reply.status, 200)
        self.assertEqual(box.generations, [])
        box.start_engine(mode="up")
        self.assertTrue(reply.done.wait(15))
        self.assertTrue(any('"content":"four"' in e for e in reply.content()), reply.events)

    def test_the_anthropic_dialect_waits_on_pings(self):
        box = self.box()
        reply = box.send(path="/v1/messages")
        time.sleep(2.5)
        self.assertEqual(reply.status, 200)
        self.assertTrue(reply.keepalives() and all(e.startswith("event: ping") for e in reply.keepalives()), reply.events)
        box.start_engine()
        self.assertTrue(reply.done.wait(15))
        self.assertTrue(any("message_stop" in e for e in reply.content()), reply.events)


class ANonStreamedRequestWaitsInSilence(unittest.TestCase):
    INPROC = False

    def box(self, **kw):
        return Box(self, inproc=self.INPROC, **kw)

    def test_it_gets_its_answer_when_the_engine_is_back(self):
        box = self.box()
        reply = box.send(stream=False)
        time.sleep(2)
        self.assertIsNone(reply.status, "nothing is sent before the answer exists")
        box.start_engine()
        self.assertTrue(reply.done.wait(15))
        self.assertEqual(reply.status, 200)
        self.assertEqual(json.loads(reply.raw)["choices"][0]["message"]["content"], "four")


class WhatIsNotWaitedFor(unittest.TestCase):
    INPROC = False

    def box(self, **kw):
        return Box(self, inproc=self.INPROC, **kw)

    """The v6.27 answer, at once, whenever no engine is on its way back."""

    def assert_503_at_once(self, box, path="/v1/chat/completions", stream=True):
        reply = box.send(path=path, stream=stream)
        self.assertTrue(reply.done.wait(5))
        self.assertEqual(reply.status, 503)
        self.assertLess(reply.first_at, 3)
        return json.loads(reply.raw)

    def test_a_lane_stopped_on_purpose(self):
        for state in ("inactive", "failed", "deactivating"):
            with self.subTest(state=state):
                box = self.box(state=state)
                self.assertEqual(self.assert_503_at_once(box)["error"]["type"], "engine_unavailable")

    def test_a_proxy_that_names_no_unit(self):
        box = self.box(units="")
        self.assertEqual(self.assert_503_at_once(box)["error"]["type"], "engine_unavailable")
        if not self.INPROC:                 # the startup line is the process's own
            self.assertIn("holds nothing", box.journal())

    def test_an_engine_that_answers_and_still_fails_is_here_not_coming(self):
        """Its info route answers and its generations 503: waiting would only replay the
        failure, so the answer is the one it always was."""
        box = self.box()
        box.start_engine(mode="starting", info_mode="up")
        self.assertEqual(self.assert_503_at_once(box)["error"]["type"], "engine_unavailable")
        self.assertNotIn("holding:", box.journal())


class AHoldEnds(unittest.TestCase):
    INPROC = False

    def box(self, **kw):
        return Box(self, inproc=self.INPROC, **kw)

    def test_a_stream_held_too_long_ends_with_an_error_event(self):
        box = self.box(hold_max="3")
        reply = box.send()
        self.assertTrue(reply.done.wait(15))
        self.assertEqual(reply.status, 200)
        last = json.loads(reply.content()[-1][len("data: "):])
        self.assertEqual(last["error"]["type"], "engine_unavailable")
        self.assertIn("not answering", last["error"]["message"])
        self.assertNotIn("data: [DONE]", reply.content())
        self.assertIn("held 3s and the engine is still not back", box.journal())
        self.assertIn("503 engine unreachable", box.journal_with("503 engine unreachable"))

    def test_an_anthropic_stream_ends_with_its_own_error_event(self):
        box = self.box(hold_max="3")
        reply = box.send(path="/v1/messages")
        self.assertTrue(reply.done.wait(15))
        last = reply.content()[-1]
        self.assertTrue(last.startswith("event: error\ndata: "), last)
        self.assertEqual(json.loads(last.split("data: ", 1)[1])["error"]["type"], "api_error")

    def test_a_non_streamed_request_held_too_long_gets_the_503(self):
        box = self.box(hold_max="3")
        reply = box.send(stream=False)
        self.assertTrue(reply.done.wait(15))
        self.assertEqual(reply.status, 503)
        self.assertGreaterEqual(reply.first_at, 2.5)

    def test_a_lane_stopped_while_a_request_waits_ends_the_wait(self):
        box = self.box()
        reply = box.send()
        time.sleep(1.5)
        box.state("inactive")
        self.assertTrue(reply.done.wait(15))
        self.assertIn("no longer on its way back", box.journal())

    def test_a_stop_ends_the_wait_while_the_unit_is_still_stopping(self):
        box = self.box()
        reply = box.send()
        time.sleep(1.5)
        box.state("deactivating")
        box.jobs("124 qwen38-flash.service stop running")
        self.assertTrue(reply.done.wait(15))
        self.assertIn("no longer on its way back", box.journal())

    def test_a_restart_keeps_the_wait_through_its_stop(self):
        """systemctl restart runs its stop half as "deactivating" under a restart job: the
        cockpit's restart of a zombie ended both clients' waits there (2026-10-01)."""
        box = self.box()
        reply = box.send()
        time.sleep(1.5)
        box.state("deactivating")
        box.jobs("6104 qwen38-flash.service restart running")
        time.sleep(4.5)                                 # past the 2 s systemd answers are kept
        box.state("inactive")                           # the instant between its two halves
        time.sleep(3)
        box.state("activating")
        box.jobs()
        self.assertIsNone(reply.err)
        self.assertFalse(reply.done.is_set(), reply.events)
        box.start_engine()
        self.assertTrue(reply.done.wait(15))
        self.assertTrue(any('"content":"four"' in e for e in reply.content()), reply.events)
        self.assertNotIn("no longer on its way back", box.journal())

    def test_a_caller_that_leaves_ends_the_wait_and_reaches_no_engine(self):
        for stream in (True, False):
            with self.subTest(stream=stream):
                box = self.box()
                body = json.dumps({"model": "m", "stream": stream, "messages": [{"role": "user", "content": "hi"}]}).encode()
                sk = socket.create_connection(("127.0.0.1", box.port), timeout=10)
                sk.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                           b"Authorization: Bearer test-key\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
                if stream:
                    self.assertIn(b"200", sk.recv(4096), "the held stream's 200 came first")
                time.sleep(1.5)
                sk.close()                                   # the caller gives up
                self.assertIn("CLIENT GONE during hold", box.journal_with("CLIENT GONE during hold"))
                box.start_engine()
                time.sleep(1.5)
                self.assertEqual(box.generations, [], "nobody was left to answer")

    def test_an_engine_half_back_is_retried_a_few_times_then_answered(self):
        """Its info route answers again and its generations still 503 (a boot not quite done):
        the request is sent again a bounded number of times, each after a pause, and then
        the stream says why. It spun until PROXY_HOLD_MAX_S, with no pause at all, before."""
        box = self.box()
        reply = box.send()
        time.sleep(1.2)
        box.start_engine(mode="starting", info_mode="up")
        t0 = time.time()
        self.assertTrue(reply.done.wait(20))
        self.assertLess(time.time() - t0, 10, "bounded")
        rounds = box.journal().count("the engine answers again after")
        self.assertGreaterEqual(rounds, 2)
        self.assertLessEqual(rounds, 11)
        last = json.loads(reply.content()[-1][len("data: "):])
        self.assertEqual(last["error"]["type"], "engine_unavailable")
        self.assertIn("as it does while starting or shutting down", last["error"]["message"])

    def test_an_engine_half_back_that_finishes_its_boot_serves_the_request(self):
        box = self.box()
        reply = box.send()
        time.sleep(1.2)
        box.start_engine(mode="starting", info_mode="up")
        time.sleep(0.6)
        box.srv.mode = "up"
        self.assertTrue(reply.done.wait(20))
        self.assertTrue(any('"content":"four"' in e for e in reply.content()), reply.events)

    def test_an_engine_that_comes_back_with_a_plain_text_error_is_quoted_in_the_stream(self):
        box = self.box()
        reply = box.send()
        time.sleep(1.5)
        box.start_engine(mode="plain400")
        self.assertTrue(reply.done.wait(15))
        last = json.loads(reply.content()[-1][len("data: "):])
        self.assertIn("bad request, said in plain text", last["error"]["message"])
        self.assertIn("400 upstream", box.journal_with("400 upstream"))

    def test_an_engine_that_answers_a_held_stream_without_one_is_said_so(self):
        box = self.box()
        reply = box.send()
        time.sleep(1.5)
        box.start_engine(mode="json-for-stream")
        self.assertTrue(reply.done.wait(15))
        last = json.loads(reply.content()[-1][len("data: "):])
        self.assertIn("answered this streamed request without a stream", last["error"]["message"])
        self.assertIn("UPSTREAM CUT non-sse", box.journal_with("UPSTREAM CUT non-sse"))

    def test_an_engine_lost_while_it_counted_a_long_prompt_is_waited_for(self):
        """The pool is cached from the engine that served the last long prompt; the next one
        reaches its tokenizer and finds nobody: that is a restart, not a size refusal."""
        box = self.box()
        box.start_engine(pool=100000)
        big = "word " * 60000                                 # ~300 KB: counted by the engine
        first = box.send(content=big)
        self.assertTrue(first.done.wait(15))
        self.assertEqual(first.status, 200)
        box.stop_engine()
        reply = box.send(content=big)
        time.sleep(2)
        self.assertEqual(reply.status, 200)
        box.start_engine(pool=100000)
        self.assertTrue(reply.done.wait(20))
        self.assertTrue(any('"content":"four"' in e for e in reply.content()), reply.events)
        self.assertIn("holding: the engine does not answer", box.journal())

    def test_a_prompt_too_long_for_the_lane_that_came_back_is_told_in_the_stream(self):
        """The prompt fitted the lane that went away and not the one that came back: the
        stream the proxy opened carries the overflow, with the code clients recognise."""
        box = self.box()
        reply = box.send(content="word " * 60000)            # ~300 KB, past the size guard's door
        time.sleep(2)
        box.start_engine(pool=20000)                          # the engine counts ~75,000 tokens
        self.assertTrue(reply.done.wait(20))
        last = json.loads(reply.content()[-1][len("data: "):])
        self.assertEqual((last["error"]["type"], last["error"]["code"]), ("context_too_long", "context_length_exceeded"))
        self.assertIn("the prompt is too long", last["error"]["message"])
        self.assertEqual(box.generations, [])


class TheTwoQuestionsItAsks(unittest.TestCase):
    """engine_coming() and engine_answers(), on the edges a box can put them on."""

    def load(self, **env):
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        os.environ.update(env)
        spec = importlib.util.spec_from_file_location(f"kp_q_{time.time_ns()}", PROXY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_no_systemctl_means_no_engine_coming(self):
        empty = Path(tempfile.mkdtemp(prefix="no-systemctl-"))
        self.addCleanup(shutil.rmtree, empty, True)
        mod = self.load(PROXY_HOLD_UNITS=UNITS, PATH=str(empty))
        self.assertFalse(mod.engine_coming())

    def test_a_hold_of_zero_seconds_waits_for_nothing(self):
        mod = self.load(PROXY_HOLD_UNITS=UNITS, PROXY_HOLD_MAX_S="0")
        self.assertFalse(mod.engine_coming())

    def test_an_engine_answers_without_the_key_and_with_a_401(self):
        home = Path(tempfile.mkdtemp(prefix="no-key-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        class Refusing(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                self.send_response(self.server.code); self.send_header("Content-Length", "0"); self.end_headers()
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Refusing)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        mod = self.load(HOME=str(home), UPSTREAM=f"http://127.0.0.1:{srv.server_address[1]}")
        for code, answers in ((401, True), (404, True), (500, False), (503, False)):
            srv.code = code
            self.assertEqual(mod.engine_answers(), answers, code)
        srv.shutdown(); srv.server_close()
        self.assertFalse(mod.engine_answers(), "nobody listening")

    def test_a_caller_seen_through_tls_or_a_closed_socket(self):
        import ssl
        mod = self.load()
        class Fake:
            pass
        f = Fake()
        f.connection = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).wrap_socket(socket.socket(), do_handshake_on_connect=False,
                                                                             server_hostname="x")
        self.addCleanup(f.connection.close)
        self.assertFalse(mod.H._client_left(f), "a peek through TLS is not made")
        f.connection = socket.socket(); f.connection.close()
        self.assertTrue(mod.H._client_left(f), "a socket that is gone is a caller gone")


class WhereItIsSwitchedOn(unittest.TestCase):
    def test_the_installed_proxy_names_both_text_lanes(self):
        """In a unit file, an unquoted value with a space is two assignments."""
        tpl = (HERE.parents[1] / "qwen38-keepalive.service.template").read_text()
        self.assertIn(f'Environment="PROXY_HOLD_UNITS={UNITS}"\n', tpl)

    def test_its_startup_line_says_what_it_holds(self):
        box = Box(self)
        held = "holds requests up to 30s while qwen38-sglang.service or qwen38-flash.service comes back"
        self.assertIn(held, box.journal_with(held))



# The same cases with the proxy in this process: what CI's coverage floor measures. Each is
# declared again rather than only inherited: CI counts the tests a file declares against the
# tests that ran, and an inherited case runs without being declared.
class AStreamedRequestWaitsForItsEngineInProcess(AStreamedRequestWaitsForItsEngine):
    INPROC = True

    def test_it_hears_from_the_proxy_at_once_and_gets_the_answer_once_the_engine_is_back(self):
        super().test_it_hears_from_the_proxy_at_once_and_gets_the_answer_once_the_engine_is_back()

    def test_an_engine_that_answers_503_while_it_starts_is_waited_for_too(self):
        super().test_an_engine_that_answers_503_while_it_starts_is_waited_for_too()

    def test_the_anthropic_dialect_waits_on_pings(self):
        super().test_the_anthropic_dialect_waits_on_pings()


class ANonStreamedRequestWaitsInSilenceInProcess(ANonStreamedRequestWaitsInSilence):
    INPROC = True

    def test_it_gets_its_answer_when_the_engine_is_back(self):
        super().test_it_gets_its_answer_when_the_engine_is_back()


class WhatIsNotWaitedForInProcess(WhatIsNotWaitedFor):
    INPROC = True

    def test_a_lane_stopped_on_purpose(self):
        super().test_a_lane_stopped_on_purpose()

    def test_a_proxy_that_names_no_unit(self):
        super().test_a_proxy_that_names_no_unit()

    def test_an_engine_that_answers_and_still_fails_is_here_not_coming(self):
        super().test_an_engine_that_answers_and_still_fails_is_here_not_coming()


class AHoldEndsInProcess(AHoldEnds):
    INPROC = True

    def test_a_stream_held_too_long_ends_with_an_error_event(self):
        super().test_a_stream_held_too_long_ends_with_an_error_event()

    def test_an_anthropic_stream_ends_with_its_own_error_event(self):
        super().test_an_anthropic_stream_ends_with_its_own_error_event()

    def test_a_non_streamed_request_held_too_long_gets_the_503(self):
        super().test_a_non_streamed_request_held_too_long_gets_the_503()

    def test_a_lane_stopped_while_a_request_waits_ends_the_wait(self):
        super().test_a_lane_stopped_while_a_request_waits_ends_the_wait()

    def test_a_stop_ends_the_wait_while_the_unit_is_still_stopping(self):
        super().test_a_stop_ends_the_wait_while_the_unit_is_still_stopping()

    def test_a_restart_keeps_the_wait_through_its_stop(self):
        super().test_a_restart_keeps_the_wait_through_its_stop()

    def test_a_caller_that_leaves_ends_the_wait_and_reaches_no_engine(self):
        super().test_a_caller_that_leaves_ends_the_wait_and_reaches_no_engine()

    def test_an_engine_half_back_is_retried_a_few_times_then_answered(self):
        super().test_an_engine_half_back_is_retried_a_few_times_then_answered()

    def test_an_engine_half_back_that_finishes_its_boot_serves_the_request(self):
        super().test_an_engine_half_back_that_finishes_its_boot_serves_the_request()

    def test_an_engine_that_comes_back_with_a_plain_text_error_is_quoted_in_the_stream(self):
        super().test_an_engine_that_comes_back_with_a_plain_text_error_is_quoted_in_the_stream()

    def test_an_engine_that_answers_a_held_stream_without_one_is_said_so(self):
        super().test_an_engine_that_answers_a_held_stream_without_one_is_said_so()

    def test_an_engine_lost_while_it_counted_a_long_prompt_is_waited_for(self):
        super().test_an_engine_lost_while_it_counted_a_long_prompt_is_waited_for()

    def test_a_prompt_too_long_for_the_lane_that_came_back_is_told_in_the_stream(self):
        super().test_a_prompt_too_long_for_the_lane_that_came_back_is_told_in_the_stream()


class EveryCaseHasItsInProcessTwin(unittest.TestCase):
    def test_each_twin_declares_every_case_of_its_class(self):
        for twin in (AStreamedRequestWaitsForItsEngineInProcess, ANonStreamedRequestWaitsInSilenceInProcess,
                     WhatIsNotWaitedForInProcess, AHoldEndsInProcess):
            base = twin.__bases__[0]
            cases = {n for n in vars(base) if n.startswith("test_")}
            self.assertTrue(cases, base.__name__)
            self.assertEqual({n for n in vars(twin) if n.startswith("test_")}, cases, twin.__name__)


if __name__ == "__main__":
    unittest.main()
