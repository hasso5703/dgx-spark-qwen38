#!/usr/bin/env python3
"""The proxy unit's MemoryMax holds the bodies the proxy accepts at once (2026-10-18).

BODY_BUDGET_BYTES (384 MiB) bounds the bytes of the bodies in flight, and MemoryMax=1G with
MemorySwapMax=0 the proxy's memory, so that a proxy past any real use is stopped alone. But a
body costs the proxy about four times its bytes while the guards work on it: the
reasoning_effort route parses it and writes it again. Measured in a cloud container (x86_64,
Python 3.13.16): one body of 250 MiB, under MAX_BODY_BYTES, with reasoning_effort "lean", took
the proxy from 32 to 1034 MiB, and under systemd 255 a unit capped at 1G was killed by the
kernel for it ("Memory cgroup out of memory"), with every stream in flight; the client got an
empty reply (found in a cloud review, not yet on the box).

This measures that rate on the machine that runs it, on the real proxy as a process, with a
24 MiB body of the costliest shape measured (an image as base64, ASCII), and holds the unit to
it: the idle proxy plus the budget at that rate fits under MemoryMax, with a tenth to spare.
"""
import http.client
import http.server
import json
import os
import pathlib
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
BODY_MIB = 24


class FakeEngine(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def reply(self, obj, code=200):
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            return self.reply({"object": "list", "data": [{"id": "qwen3.8-27b", "object": "model"}]})
        return self.reply({})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        while n > 0:
            chunk = self.rfile.read(min(n, 1 << 20))
            if not chunk:
                break
            n -= len(chunk)
        self.reply({"id": "c", "object": "chat.completion", "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})


class Engine(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def peak_mib(pid):
    """The process's highest resident set so far (VmHWM), in MiB."""
    with open(f"/proc/{pid}/status") as f:
        return next(int(ln.split()[1]) for ln in f if ln.startswith("VmHWM:")) / 1024


def unit_bytes(name):
    """A size directive of the proxy's unit template, in bytes (systemd's K, M, G are 1024's)."""
    text = (REPO / "qwen38-keepalive.service.template").read_text()
    m = re.search(rf"^{name}=(\d+)([KMGT]?)$", text, re.M)
    if not m:
        raise AssertionError(f"the proxy unit has no {name}= the test can read")
    return int(m.group(1)) * 1024 ** " KMGT".index(m.group(2) or " ")


def proxy_default(name):
    """A size default of keepalive-proxy.py, as its source spells it (a product of whole numbers)."""
    text = (REPO / "keepalive-proxy.py").read_text()
    m = re.search(rf'^{name} = int\(os\.environ\.get\("{name}", str\(([\d *]+)\)\)', text, re.M)
    if not m:
        raise AssertionError(f"keepalive-proxy.py no longer spells the {name} default the way this test reads it")
    value = 1
    for factor in m.group(1).split("*"):
        value *= int(factor)
    return value


class TheCeilingHoldsTheBudget(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Engine(("127.0.0.1", 0), FakeEngine)
        threading.Thread(target=cls.engine.serve_forever, daemon=True).start()
        cls.home = pathlib.Path(tempfile.mkdtemp(prefix="kp-ceiling-"))
        (cls.home / ".config" / "qwen38").mkdir(parents=True)
        (cls.home / ".config" / "qwen38" / "api-key").write_text("k-test\n")
        cls.port = free_port()
        env = {"PATH": os.environ["PATH"], "HOME": str(cls.home), "PROXY_BIND": "127.0.0.1",
               "UPSTREAM": f"http://127.0.0.1:{cls.engine.server_address[1]}", "PROXY_HOLD_UNITS": ""}
        cls.proxy = subprocess.Popen([sys.executable, str(REPO / "keepalive-proxy.py"), str(cls.port)], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        cls.engine.shutdown()
        cls.engine.server_close()
        shutil.rmtree(cls.home, ignore_errors=True)

    def send(self, doc):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        try:
            c.request("POST", "/v1/chat/completions", body=json.dumps(doc).encode(),
                      headers={"Content-Type": "application/json", "Authorization": "Bearer k-test"})
            r = c.getresponse()
            r.read()
            return r.status
        finally:
            c.close()

    def test_the_idle_proxy_and_the_budget_at_the_measured_rate_fit_under_memorymax(self):
        self.send({"model": "qwen3.8-27b", "messages": [{"role": "user", "content": "2+2?"}]})
        idle = peak_mib(self.proxy.pid)
        body = {"model": "qwen3.8-27b", "reasoning_effort": "lean", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * (BODY_MIB << 20)}}]}]}
        self.assertIsNotNone(self.send(body))
        self.assertIsNone(self.proxy.poll(), "the proxy died on the body")
        rate = (peak_mib(self.proxy.pid) - idle) / BODY_MIB
        self.assertGreater(rate, 1, "the body did not reach the guards: the measurement measured nothing")
        budget = proxy_default("BODY_BUDGET_BYTES") / 2**20
        ceiling = unit_bytes("MemoryMax") / 2**20
        need = idle + rate * max(budget, proxy_default("MAX_BODY_BYTES") / 2**20)
        self.assertLessEqual(need, 0.9 * ceiling,
                             f"idle {idle:.0f} MiB + {rate:.1f} x a budget of {budget:.0f} MiB = {need:.0f} MiB, "
                             f"over nine tenths of MemoryMax ({ceiling:.0f} MiB): the kernel kills the proxy first")

    def test_the_ceiling_is_not_moved_to_swap(self):
        # with swap on the box, MemoryMax alone moves the excess to swap (measured 2026-10-08)
        self.assertEqual(unit_bytes("MemorySwapMax"), 0)

    def test_the_readers_read_what_is_there(self):
        # so a spelling that changed fails here, and not as a ceiling of zero
        self.assertEqual(proxy_default("BODY_BUDGET_BYTES"), 384 * 1024 * 1024)
        self.assertEqual(proxy_default("MAX_BODY_BYTES"), 256 * 1024 * 1024)
        self.assertGreaterEqual(unit_bytes("MemoryMax"), 1024 ** 3)


if __name__ == "__main__":
    unittest.main()
