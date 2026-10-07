#!/usr/bin/env python3
"""The proxy refuses a request naming a model the engine does not serve (v6.34).

SGLang routes on the model it loaded, not on the model a request names: a request for a
model that is not loaded is answered by the one that IS, with no error and nothing in the
answer to notice it by. On the reference box the 27B lane was loaded at 19:44 while
opencode's default model still named flash, and every request of that hour was answered by
a model nobody had asked for (2026-10-07). A lane switch costs 6-11 minutes, so a config
naming the previous lane is what every switch leaves behind until the tooling catches up -
which is why the proxy, the one thing on the path that reads /v1/models anyway, refuses.

The unit half pins the decision table (what counts as naming a model, what an unknown truth
does, the stale cache after a switch, the escape hatches). The end-to-end half runs a real
proxy against a fake engine that lists one model, and checks what a client actually gets."""
import http.client
import http.server
import importlib.util
import io
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]


def _keep_env(cls):
    """os.environ back as this class found it when it is done: the variables set for the
    proxy under test must not leak into the modules that run after this one."""
    saved = dict(os.environ)
    cls.addClassCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))


def proxy_module(**env):
    """The proxy as a module, with these environment variables set while it loads."""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    spec = importlib.util.spec_from_file_location("kp_identity", REPO / "keepalive-proxy.py")
    mod = importlib.util.module_from_spec(spec)
    err, sys.stderr = sys.stderr, io.StringIO()
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.stderr = err
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod


class Engine(http.server.BaseHTTPRequestHandler):
    """One model listed on /v1/models, and a chat completion that answers whatever it is
    given - which is exactly the failure: it never says which model answered."""
    model = "qwen3.8-flash-next"
    listed = True
    asked = []

    def log_message(self, *a):
        pass

    def _send(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            if not Engine.listed:
                self._send(200, {"object": "list", "data": []})
            else:
                self._send(200, {"object": "list",
                                 "data": [{"id": Engine.model, "object": "model"}]})
        elif self.path == "/health":
            self._send(200, {})
        else:
            self._send(404, {"error": "no such route"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path not in ("/v1/chat/completions", "/v1/completions", "/generate"):
            self._send(404, {"error": "no such route"})
            return
        Engine.asked.append((self.path, body.get("model")))
        self._send(200, {"id": "cmpl-1", "object": "chat.completion", "model": Engine.model,
                         "choices": [{"index": 0, "message": {"role": "assistant",
                                                              "content": "ANSWERED"},
                                      "finish_reason": "stop"}]})


class WhatCountsAsNamingAModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _keep_env(cls)
        cls.mod = proxy_module()

    def test_the_bodies_that_name_a_model_and_the_ones_that_do_not(self):
        for body, want in (
                (b'{"model":"qwen3.8-27b","messages":[]}', "qwen3.8-27b"),
                (b'{"model":"  qwen3.8-27b  "}', "qwen3.8-27b"),
                (b'{"model":"qwen3.8-27b:latest"}', "qwen3.8-27b:latest"),
                (b'{"messages":[]}', None),          # no claim: relayed as it always was
                (b'{"model":null}', None),
                (b'{"model":""}', None),
                (b'{"model":7}', None),
                (b'{"model":{"name":"x"}}', None),
                (b'[]', None),
                (b'null', None),
                (b'not json', None),
                (b'', None)):
            with self.subTest(body=body[:40]):
                self.assertEqual(self.mod.model_named(body), want)


class TheDecision(unittest.TestCase):
    def setUp(self):
        self.mod = proxy_module()
        self.calls = []

    def served(self, *names):
        def f():
            self.calls.append(names)
            return tuple(names)
        return f

    def test_the_served_name_goes_through(self):
        self.mod.served_models = self.served("qwen3.8-flash-next")
        self.assertIsNone(self.mod.identity_refusal("qwen3.8-flash-next"))
        self.assertEqual(len(self.calls), 1)          # the cache read is the only cost

    def test_a_name_the_engine_does_not_serve_is_refused_with_the_names(self):
        self.mod.served_models = self.served("qwen3.8-flash-next")
        self.assertEqual(self.mod.identity_refusal("qwen3.8-27b"), ("qwen3.8-flash-next",))

    def test_a_client_tag_or_digest_on_a_served_name_goes_through(self):
        self.mod.served_models = self.served("qwen3.8-flash-next")
        for name in ("qwen3.8-flash-next:latest", "qwen3.8-flash-next@sha256:1f"):
            with self.subTest(name=name):
                self.assertIsNone(self.mod.identity_refusal(name))

    def test_an_engine_that_names_nobody_is_not_a_refusal(self):
        self.mod.served_models = self.served()       # /v1/models answered with an empty list
        self.assertIsNone(self.mod.identity_refusal("qwen3.8-27b"))

    def test_an_engine_that_does_not_answer_is_not_a_refusal(self):
        def gone():
            raise self.mod.EngineUnreachable("refused")
        self.mod.served_models = gone
        self.assertIsNone(self.mod.identity_refusal("qwen3.8-27b"))

    def test_a_miss_rereads_the_list_once_because_a_switch_made_it_stale(self):
        # The cache is up to 600 s old, and a switch is exactly when the model that has
        # just booted would be refused for it. First read: the old lane. Second: the new.
        listing = iter([("qwen3.8-flash-next",), ("qwen3.8-27b",)])
        calls = []

        def f():
            calls.append(1)
            return next(listing)
        self.mod.served_models = f
        self.mod._SERVED.update(names=("qwen3.8-flash-next",), ts=time.time())
        self.assertIsNone(self.mod.identity_refusal("qwen3.8-27b"))
        self.assertEqual(len(calls), 2)               # one forced fresh read, and only one

    def test_a_name_the_operator_admitted_goes_through(self):
        self.mod.served_models = self.served("qwen3.8-flash-next")
        self.mod.IDENTITY_EXTRA = frozenset({"lane-a"})
        self.assertIsNone(self.mod.identity_refusal("lane-a"))

    def test_the_guard_can_be_switched_off(self):
        off = proxy_module(MODEL_IDENTITY_GUARD="0")
        self.assertFalse(off.IDENTITY_GUARD)
        on = proxy_module(MODEL_IDENTITY_GUARD="1")
        self.assertTrue(on.IDENTITY_GUARD)
        self.assertTrue(proxy_module().IDENTITY_GUARD)     # on by default

    def test_the_admitted_names_are_read_from_the_environment(self):
        m = proxy_module(EXTRA_SERVED_ALIASES="lane-a, lane-b")
        self.assertEqual(m.IDENTITY_EXTRA, frozenset({"lane-a", "lane-b"}))


class TheGuardInsideTheRequestPath(unittest.TestCase):
    """The refusal where it lives, in _handle_inner, with the proxy running in this
    process: a proxy started as a subprocess is invisible to the coverage gate (the hold
    suite learned this the same way, and the CI floor comment says so)."""

    def setUp(self):
        Engine.model, Engine.listed = "qwen3.8-flash-next", True
        self.engine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=self.engine.serve_forever, daemon=True).start()
        self.addCleanup(self.engine.server_close)
        self.addCleanup(self.engine.shutdown)
        self.home = pathlib.Path(tempfile.mkdtemp(prefix="kp-identity-inproc-"))
        (self.home / ".config" / "qwen38").mkdir(parents=True)
        (self.home / ".config/qwen38/api-key").write_text("k-test\n")
        self.addCleanup(lambda: shutil.rmtree(self.home, ignore_errors=True))
        # HOME stays for the whole case, not just the import: the proxy reads the engine key
        # lazily on every upstream call, and a HOME without it makes served_models() answer
        # "nobody", which by design means no refusal (an unknown truth never refuses).
        saved_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.addCleanup(lambda: os.environ.__setitem__("HOME", saved_home))
        self.mod = proxy_module(PROXY_BIND="127.0.0.1",
                                UPSTREAM=f"http://127.0.0.1:{self.engine.server_port}")
        self.srv = self.mod.Server(("127.0.0.1", 0), self.mod.H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.port = self.srv.server_address[1]

    def post(self, payload, path="/v1/chat/completions"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.request("POST", path, json.dumps(payload),
                      {"Content-Type": "application/json", "Authorization": "Bearer k-test"})
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def test_a_generation_route_naming_another_model_is_refused(self):
        for path in ("/v1/chat/completions", "/v1/completions", "/generate", "/v1/responses"):
            with self.subTest(path=path):
                status, body = self.post({"model": "qwen3.8-27b", "messages": [],
                                          "prompt": "x"}, path=path)
                self.assertEqual(status, 400, body[:200])
                self.assertIn(b"this engine serves qwen3.8-flash-next", body)

    def test_the_served_model_still_reaches_the_engine(self):
        before = len(Engine.asked)
        status, body = self.post({"model": "qwen3.8-flash-next",
                                  "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(status, 200)
        self.assertIn("ANSWERED", body.decode())
        self.assertGreater(len(Engine.asked), before)

    def test_systemone_is_not_this_guards_business(self):
        # /v1/systemone names a Jev alias, which no engine lists: the guard must not be
        # the reason that route fails, since the proxy answers it itself.
        status, body = self.post({"state": "s", "model": "jev-latest",
                                  "questions": {"q": {"type": "noul"}}}, path="/v1/systemone")
        self.assertNotIn(b"this engine serves", body)


class ThroughARealProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _keep_env(cls)
        cls.home = pathlib.Path(tempfile.mkdtemp(prefix="kp-identity-"))
        (cls.home / ".config" / "qwen38").mkdir(parents=True)
        (cls.home / ".config/qwen38/api-key").write_text("k-test\n")
        cls.engine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=cls.engine.serve_forever, daemon=True).start()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        cls.logfile = open(cls.home / "proxy.log", "w")
        cls.proxy = subprocess.Popen(
            [sys.executable, str(REPO / "keepalive-proxy.py"), str(cls.port)],
            env={"PATH": os.environ["PATH"], "HOME": str(cls.home), "PROXY_BIND": "127.0.0.1",
                 "UPSTREAM": f"http://127.0.0.1:{cls.engine.server_port}"},
            stdout=cls.logfile, stderr=cls.logfile)
        end = time.time() + 20
        while time.time() < end:
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proxy.terminate()
        cls.proxy.wait(10)
        cls.logfile.close()
        cls.engine.shutdown()
        cls.engine.server_close()
        shutil.rmtree(cls.home, ignore_errors=True)

    def post(self, payload, path="/v1/chat/completions", port=None):
        c = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=30)
        try:
            c.request("POST", path, json.dumps(payload),
                      {"Content-Type": "application/json", "Authorization": "Bearer k-test"})
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def test_the_wrong_model_is_refused_and_the_message_names_both(self):
        before = len(Engine.asked)
        status, body = self.post({"model": "qwen3.8-27b", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(status, 400)
        said = json.loads(body)
        self.assertEqual(said["error"]["type"], "invalid_request")
        self.assertIn("qwen3.8-flash-next", said["error"]["message"])   # what it would get
        self.assertIn("qwen3.8-27b", said["error"]["message"])          # what it asked for
        self.assertIn("switch the lane", said["error"]["message"])
        self.assertEqual(len(Engine.asked), before)                     # never reached the engine

    def test_the_served_model_is_relayed(self):
        status, body = self.post({"model": "qwen3.8-flash-next",
                                  "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(status, 200)
        self.assertIn("ANSWERED", body.decode())

    def test_a_body_that_names_no_model_is_still_relayed(self):
        status, _ = self.post({"messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(status, 200)

    def test_the_refusal_is_in_the_journal(self):
        self.post({"model": "qwen3.8-27b", "messages": [{"role": "user", "content": "y"}]})
        self.logfile.flush()
        text = (self.home / "proxy.log").read_text()
        self.assertIn("REFUSED model 'qwen3.8-27b'", text)
        self.assertIn("400 model not served", text)

    def test_switching_the_guard_off_reaches_the_engine_again(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        log = open(self.home / "proxy-off.log", "w")
        proxy = subprocess.Popen(
            [sys.executable, str(REPO / "keepalive-proxy.py"), str(port)],
            env={"PATH": os.environ["PATH"], "HOME": str(self.home), "PROXY_BIND": "127.0.0.1",
                 "UPSTREAM": f"http://127.0.0.1:{self.engine.server_port}",
                 "MODEL_IDENTITY_GUARD": "0"},
            stdout=log, stderr=log)
        try:
            end = time.time() + 20
            while time.time() < end:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                    break
                except OSError:
                    time.sleep(0.1)
            before = len(Engine.asked)
            status, body = self.post({"model": "qwen3.8-27b",
                                      "messages": [{"role": "user", "content": "z"}]}, port=port)
            self.assertEqual(status, 200)                    # the old behaviour, on request
            self.assertGreater(len(Engine.asked), before)
        finally:
            proxy.terminate()
            proxy.wait(10)
            log.close()


if __name__ == "__main__":
    unittest.main()
