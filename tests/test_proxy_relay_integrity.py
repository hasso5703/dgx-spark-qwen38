#!/usr/bin/env python3
"""A non-streamed answer the engine cut short reaches the client as cut short.

The proxy re-frames a non-streamed answer as chunked, so the engine's Content-Length is
gone by the time the client reads. When the engine died mid-body, the read error was
swallowed and the proxy wrote the terminating chunk anyway: the client got a well-framed
response carrying half a JSON document, and the journal said "ok non-sse" (found in
review, 2026-09-24). A client talking to the engine directly gets an IncompleteRead and
knows. The proxy now ends that response without its final chunk, the same fact.

A real proxy, imported fresh, in front of a fake engine that promises a body and closes
halfway through it.
"""
import http.client
import http.server
import importlib.util
import io
import json
import os
import select
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FULL = b'{"id":"x","choices":[{"message":{"content":"' + b"a" * 4000 + b'"}}]}'


class Engine(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    cut = True

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(FULL)))
        self.end_headers()
        if Engine.cut:
            self.wfile.write(FULL[:len(FULL) // 2]); self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)      # the engine dies mid-body
            self.close_connection = True
        else:
            self.wfile.write(FULL)

    def do_GET(self):
        body = b'{"max_total_num_tokens": 900000}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TheClientSeesTheCut(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=cls.engine.serve_forever, daemon=True).start()
        saved = os.environ.get("UPSTREAM")
        os.environ["UPSTREAM"] = f"http://127.0.0.1:{cls.engine.server_address[1]}"
        spec = importlib.util.spec_from_file_location("kproxy_integrity", REPO / "keepalive-proxy.py")
        cls.mod = importlib.util.module_from_spec(spec)
        err, sys.stderr = sys.stderr, io.StringIO()
        try:
            spec.loader.exec_module(cls.mod)
        finally:
            sys.stderr = err
            if saved is None:
                os.environ.pop("UPSTREAM", None)
            else:
                os.environ["UPSTREAM"] = saved
        cls.srv = cls.mod.Server(("127.0.0.1", 0), cls.mod.H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()
        cls.engine.shutdown(); cls.engine.server_close()

    def post(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/v1/chat/completions", b'{"model":"m","messages":[]}',
                  {"Content-Type": "application/json", "Authorization": "Bearer k"})
        r = c.getresponse()
        try:
            return r.status, r.read(), None
        except http.client.IncompleteRead as e:
            return r.status, e.partial, e
        finally:
            c.close()

    def test_a_body_cut_by_the_engine_is_not_relayed_as_complete(self):
        Engine.cut = True
        status, body, err = self.post()
        self.assertEqual(status, 200)
        self.assertIsNotNone(err, f"the client read {len(body)} bytes as a complete answer")
        self.assertLess(len(body), len(FULL))

    def test_a_whole_body_still_arrives_whole(self):
        Engine.cut = False
        status, body, err = self.post()
        self.assertIsNone(err)
        self.assertEqual(body, FULL)


class SlowEngine(http.server.BaseHTTPRequestHandler):
    """A non-streamed answer that takes WORK_S, written by an engine that, like SGLang,
    watches its own HTTP client meanwhile and drops the request when that client goes
    (TokenizerManager._wait_one_response, "type 1" and "type 3")."""
    protocol_version = "HTTP/1.1"
    WORK_S = 3.0
    BIG = b""                       # a whole answer written at once, for the relay test
    noticed = threading.Event()     # the engine saw its caller leave
    noticed_at = None
    finished = threading.Event()    # the engine generated the whole answer
    aborts = []                     # every /abort_request, useful or not

    def log_message(self, *a):
        pass

    def _caller_gone(self):
        r, _, _ = select.select([self.connection], [], [], 0)
        if not r:
            return False
        try:
            return self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/abort_request":
            SlowEngine.aborts.append(json.loads(body).get("rid"))
            out = b"{}"
        elif SlowEngine.BIG:
            out = SlowEngine.BIG
            SlowEngine.finished.set()
        else:
            end = time.time() + SlowEngine.WORK_S
            while time.time() < end:
                if self._caller_gone():
                    SlowEngine.noticed_at = time.time()
                    SlowEngine.noticed.set()
                    self.close_connection = True
                    return
                time.sleep(0.02)
            SlowEngine.finished.set()
            out = b'{"id":"x","choices":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class ACallerThatLeavesStopsTheGeneration(unittest.TestCase):
    """The caller of a non-streamed answer hears nothing until it is whole, so the proxy
    never wrote to it, never saw it leave, and kept the upstream open: the engine
    generated to the end for a client that was gone, and the proxy then sent an abort
    that found nothing left to abort (found in review, measured on the box 2026-09-24:
    21.6 s of decode after the caller left). The proxy now watches that caller and, when
    it goes, ends the upstream connection, which is the signal the engine itself acts on."""

    def proxy(self, honoured):
        engine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowEngine)
        engine.daemon_threads = True
        threading.Thread(target=engine.serve_forever, daemon=True).start()
        self.addCleanup(engine.server_close); self.addCleanup(engine.shutdown)
        saved = os.environ.get("UPSTREAM")
        os.environ["UPSTREAM"] = f"http://127.0.0.1:{engine.server_address[1]}"
        spec = importlib.util.spec_from_file_location("kproxy_gone_%s" % honoured, REPO / "keepalive-proxy.py")
        mod = importlib.util.module_from_spec(spec)
        err, sys.stderr = sys.stderr, io.StringIO()
        self.log = sys.stderr
        self.addCleanup(setattr, sys, "stderr", err)
        try:
            spec.loader.exec_module(mod)
        finally:
            if saved is None:
                os.environ.pop("UPSTREAM", None)
            else:
                os.environ["UPSTREAM"] = saved
        mod._rid_override_honoured = honoured       # proven by an earlier stream, or not yet
        srv = mod.Server(("127.0.0.1", 0), mod.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        SlowEngine.noticed.clear(); SlowEngine.finished.clear(); SlowEngine.noticed_at = None
        SlowEngine.aborts = []; SlowEngine.WORK_S = 3.0; SlowEngine.BIG = b""
        return srv.server_address[1]

    def connect(self, port, body):
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  b"Authorization: Bearer k\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        return s

    def leave_after(self, port, seconds, body=b'{"model":"m","messages":[],"stream":false}'):
        s = self.connect(port, body)
        time.sleep(seconds)
        left = time.time()
        s.close()
        return left

    def test_the_engine_sees_its_caller_leave(self):
        port = self.proxy(honoured=True)
        left = self.leave_after(port, 0.3)
        self.assertTrue(SlowEngine.noticed.wait(2), "the engine kept generating for a caller that was gone")
        self.assertLess(SlowEngine.noticed_at - left, 1.0)
        self.assertFalse(SlowEngine.finished.is_set())
        time.sleep(0.3)
        self.assertEqual(SlowEngine.aborts, [], "an abort was sent on top of the close")
        self.assertIn("client gone before its non-streamed answer", self.log.getvalue())
        self.assertIn("CLIENT GONE during non-sse wait", self.log.getvalue())

    def test_no_rid_is_needed(self):
        """The close carries no rid, so it works where no abort could: an override not
        proven yet, or the Anthropic route, whose ids name nothing the engine knows."""
        port = self.proxy(honoured=None)
        self.leave_after(port, 0.3)
        self.assertTrue(SlowEngine.noticed.wait(2), "without a proven rid the generation ran to its end")

    def test_a_request_without_a_stream_key_is_watched_too(self):
        port = self.proxy(honoured=True)
        self.leave_after(port, 0.3, body=b'{"model":"m","messages":[]}')
        self.assertTrue(SlowEngine.noticed.wait(2))

    def test_a_stream_is_left_to_its_relay(self):
        """Closing the upstream of a stream is what SGLang turns into a request nobody can
        stop (sglang#35255): a stream is never watched, whatever the engine answers."""
        port = self.proxy(honoured=True)
        SlowEngine.WORK_S = 1.0
        self.leave_after(port, 0.3, body=b'{"model":"m","messages":[],"stream":true}')
        self.assertTrue(SlowEngine.finished.wait(3))
        self.assertFalse(SlowEngine.noticed.is_set(), "the upstream of a stream was closed")

    def test_a_caller_that_stays_gets_its_answer(self):
        port = self.proxy(honoured=True)
        SlowEngine.WORK_S = 1.0
        s = self.connect(port, b'{"model":"m","messages":[],"stream":false}')
        r = http.client.HTTPResponse(s)
        r.begin()
        self.assertEqual((r.status, r.read()), (200, b'{"id":"x","choices":[]}'))
        s.close()
        self.assertFalse(SlowEngine.noticed.is_set())

    def test_an_answer_that_was_whole_is_not_aborted_after_the_fact(self):
        """A client that leaves while its answer is relayed leaves nothing generating: the
        engine sends a non-streamed answer once all of it exists. The abort the proxy sent
        there could only arrive after the request was over."""
        port = self.proxy(honoured=True)
        SlowEngine.BIG = b'{"id":"x","choices":[{"message":{"content":"' + b"a" * 4_000_000 + b'"}}]}'
        s = self.connect(port, b'{"model":"m","messages":[],"stream":false}')
        s.settimeout(5)
        self.assertTrue(s.recv(1))                       # the answer has started to arrive
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()                                        # RST: the proxy's next write fails
        deadline = time.time() + 5
        while "POST /v1/chat/completions CLIENT GONE" not in self.log.getvalue() and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        self.assertIn("CLIENT GONE", self.log.getvalue())
        self.assertEqual(SlowEngine.aborts, [], "an abort was sent for an answer that was already whole")


class WhatCountsAsAWholeAnswer(unittest.TestCase):
    """The flag is read on the raw bytes. Only a request every "stream" key of which says
    false, or that has none, is watched: anything else is treated as a stream."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("kproxy_flag", REPO / "keepalive-proxy.py")
        cls.mod = importlib.util.module_from_spec(spec)
        err, sys.stderr = sys.stderr, io.StringIO()
        try:
            spec.loader.exec_module(cls.mod)
        finally:
            sys.stderr = err

    def test_the_flag(self):
        whole = self.mod.answered_whole
        self.assertTrue(whole(b'{"messages":[]}'))
        self.assertTrue(whole(b'{"stream":false,"messages":[]}'))
        self.assertTrue(whole(b'{"stream" :  false}'))
        self.assertFalse(whole(b'{"stream":true}'))
        self.assertFalse(whole(b'{"stream": true}'))
        self.assertFalse(whole(b'{"stream":1}'), "SGLang reads 1 as true")
        self.assertFalse(whole(b'{"stream":"true"}'), "and the string too")
        self.assertFalse(whole(b'{"stream":false,"tools":[{"parameters":{"properties":{"stream":true}}}]}'),
                         "a nested key that says true is taken for a stream: the watch is skipped, never wrong")

    def test_text_that_mentions_the_flag_is_not_the_flag(self):
        body = json.dumps({"messages": [{"role": "user", "content": 'send {"stream": true}'}]}).encode()
        self.assertTrue(self.mod.answered_whole(body))


if __name__ == "__main__":
    unittest.main(verbosity=2)
