#!/usr/bin/env python3
"""A lane switch is followed at once, request or not (keepalive-proxy v6.35).

The pool, the window and the names the proxy reads from the engine were kept 600 s and
dropped only by a request that found the engine gone. After a switch made while no request
came, a lane that boots in less than 600 s served its first minutes under the stopped lane's:
the 27B (about 7 min to boot on the reference box) refused every prompt past the flash lane's
250,000 ceiling and /v1/systemone answered as the flash (found 2026-10-08); on a box where the
flash lane boots that fast, it would go without its ceiling. The proxy now asks systemd for the
invocation id of the units it names in PROXY_HOLD_UNITS when it reads those facts, at most
every 2 s, and a new id, which systemd gives every start, drops them.

Every test runs the real proxy in this process (the coverage floor reads these lines)
against an engine this file serves, and a systemctl that answers from files, in the format
of the real one (`systemctl show A B -p InvocationID --value` prints "idA\\n\\nidB\\n"): the
units of the box that runs the suite are never asked."""
import http.client
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
from pathlib import Path

HERE = Path(__file__).resolve()
PROXY = HERE.parents[1] / "keepalive-proxy.py"
UNITS = "qwen38-sglang.service qwen38-flash.service"
KEY = "restart-test-key"
# The two lanes as their engines report themselves on the reference box (2026-10-08).
FLASH = {"pool": 466112, "window": 262138, "name": "qwen3.8-flash-next"}
B27 = {"pool": 863644, "window": 863638, "name": "qwen3.8-27b"}
# 260,000 words of three bytes, 780 kB: counted by this file's engine as 260,000 tokens,
# past the flash lane's 250,000 ceiling and well inside the 27B's 794,552 usable.
LONG = "xy " * 260000


class Engine(http.server.BaseHTTPRequestHandler):
    """The lane that serves (server.lane) as the proxy reads it: its info route, its names, a
    count of words on /tokenize, and an answer to any generation, recorded in server.relayed."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj):
        out = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def do_GET(self):
        lane = self.server.lane
        if self.path in ("/server_info", "/get_server_info"):
            return self._json({"max_total_num_tokens": lane["pool"], "max_req_input_len": lane["window"]})
        if self.path == "/v1/models":
            return self._json({"object": "list", "data": [{"id": lane["name"], "object": "model"}]})
        self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/tokenize":
            return self._json({"count": sum(len(str(m.get("content", "")).split()) for m in body.get("messages", []))})
        self.server.relayed.append(self.path)
        self._json({"id": "x", "object": "chat.completion", "model": self.server.lane["name"],
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})


class Box:
    """One engine, the proxy in this process, and a systemctl that prints the ids file for
    `show`, logs every call, and exits 1 while the fail file exists."""

    def __init__(self, test, units=UNITS, lane=FLASH):
        self.t = Path(tempfile.mkdtemp(prefix="proxy-restarts-"))
        test.addCleanup(shutil.rmtree, self.t, True)
        (self.t / "home/.config/qwen38").mkdir(parents=True)
        (self.t / "home/.config/qwen38/api-key").write_text(KEY + "\n")
        (self.t / "bin").mkdir()
        self.ids_file, self.calls_file, self.fail_file = self.t / "ids", self.t / "calls", self.t / "fail"
        self.ids("sglang-1", "flash-1")
        (self.t / "bin/systemctl").write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> "{self.calls_file}"\n'
            f'[ -e "{self.fail_file}" ] && exit 1\n'
            f'if [ "$1" = show ]; then cat "{self.ids_file}"; exit 0; fi\n'
            "exit 3\n")
        (self.t / "bin/systemctl").chmod(0o755)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        self.srv.lane, self.srv.relayed = lane, []
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        test.addCleanup(self.srv.server_close)
        test.addCleanup(self.srv.shutdown)
        env = dict(os.environ, UPSTREAM=f"http://127.0.0.1:{self.srv.server_address[1]}",
                   HOME=str(self.t / "home"), PATH=f"{self.t / 'bin'}:{os.environ.get('PATH', '')}",
                   PROMPT_CEILING_TOKENS="0", FLASH_PROMPT_CEILING_TOKENS="250000")
        env.pop("PROXY_HOLD_UNITS", None)
        if units:
            env["PROXY_HOLD_UNITS"] = units
        saved_env, saved_argv = dict(os.environ), list(sys.argv)
        test.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved_env)))
        test.addCleanup(setattr, sys, "argv", saved_argv)
        os.environ.clear(); os.environ.update(env)
        spec = importlib.util.spec_from_file_location(f"kp_restarts_{time.time_ns()}", PROXY)
        self.m = importlib.util.module_from_spec(spec)
        sys.argv = ["keepalive-proxy.py"]
        spec.loader.exec_module(self.m)
        self.lines = []
        self.m.log = lambda msg: self.lines.append(msg)
        self.proxy = self.m.Server(("127.0.0.1", 0), self.m.H)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        test.addCleanup(self.proxy.server_close)
        test.addCleanup(self.proxy.shutdown)

    def ids(self, sglang, flash):
        """What systemd says of the two units: an id each, a blank line between, as it prints them."""
        self.ids_file.write_text(f"{sglang}\n\n{flash}\n")

    def switch(self, lane, sglang, flash):
        """The engine behind the port is now `lane`, and systemd says which unit started."""
        self.srv.lane = lane
        self.ids(sglang, flash)

    def calls(self):
        return self.calls_file.read_text().splitlines() if self.calls_file.exists() else []

    def chat(self, content):
        body = json.dumps({"model": "m", "max_tokens": 1, "messages": [{"role": "user", "content": content}]}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_address[1], timeout=60)
        conn.request("POST", "/v1/chat/completions", body=body,
                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
        r = conn.getresponse()
        out = r.read()
        conn.close()
        return r.status, out

    def past_the_last_look(self):
        """systemd is asked at most every 2 s: wait that long, as a real client would have."""
        time.sleep(2.2)


class ASwitchIsFollowedAtOnce(unittest.TestCase):
    def test_to_the_27b_lane_the_flash_ceiling_goes_with_the_flash(self):
        box = Box(self, lane=FLASH)
        self.assertEqual(box.chat("hello")[0], 200)                 # the flash lane's facts, read
        box.switch(B27, sglang="sglang-2", flash="flash-1")         # the 27B unit started
        box.past_the_last_look()
        status, out = box.chat(LONG)
        self.assertEqual(status, 200, out[:300])
        self.assertEqual(box.srv.relayed, ["/v1/chat/completions"] * 2)
        self.assertIn("an engine unit started since the engine's pool and names were read", "\n".join(box.lines))

    def test_to_the_flash_lane_its_ceiling_applies_at_once(self):
        box = Box(self, lane=B27)
        self.assertEqual(box.chat("hello")[0], 200)                 # the 27B lane's facts, read
        box.switch(FLASH, sglang="sglang-1", flash="flash-2")       # the flash unit started
        box.past_the_last_look()
        status, out = box.chat(LONG)
        self.assertEqual(status, 400, out[:300])
        msg = json.loads(out)["error"]["message"]
        self.assertIn("260000 prompt tokens (counted by the engine)", msg)
        self.assertIn("this lane serves at most 250000 prompt tokens (KV pool 466112 tokens, one-prompt ceiling 250000)", msg)
        self.assertEqual(box.srv.relayed, ["/v1/chat/completions"], "the long prompt never reached the flash lane")

    def test_system_one_names_the_lane_that_serves(self):
        box = Box(self, lane=FLASH)
        self.assertEqual(box.m.systemone_model("jev-latest"), "qwen3.8-flash-next")
        box.switch(B27, sglang="sglang-2", flash="flash-1")
        box.past_the_last_look()
        self.assertEqual(box.m.systemone_model("jev-latest"), "qwen3.8-27b")

    def test_a_restart_of_the_same_lane_reads_its_new_pool(self):
        """The pool is a boot lottery (863,398 / 893,479 / 913,334 measured for one checkpoint):
        a restart of the lane that serves is a new engine too."""
        box = Box(self, lane=B27)
        self.assertEqual(box.m.pool_tokens(), 863644)
        box.switch(dict(B27, pool=813334), sglang="sglang-2", flash="flash-1")
        box.past_the_last_look()
        self.assertEqual(box.m.pool_tokens(), 813334)


class WhatSystemdDoesNotSayIsNotGuessed(unittest.TestCase):
    """With no new start in systemd's word, the facts stand their 600 s, as they did before:
    the switch below is real, so these also show what v6.34 did after one."""

    def test_the_same_ids_keep_the_facts(self):
        box = Box(self, lane=FLASH)
        self.assertEqual(box.chat("hello")[0], 200)
        box.srv.lane = B27                                          # the engine moved, systemd says nothing
        box.past_the_last_look()
        status, out = box.chat(LONG)
        self.assertEqual(status, 400, out[:300])
        self.assertIn("at most 250000 prompt tokens", json.loads(out)["error"]["message"])
        self.assertEqual(box.m.systemone_model("jev-latest"), "qwen3.8-flash-next")

    def test_a_proxy_that_names_no_unit_asks_nothing(self):
        box = Box(self, units="", lane=FLASH)
        self.assertEqual(box.chat("hello")[0], 200)
        box.switch(B27, sglang="sglang-2", flash="flash-1")
        box.past_the_last_look()
        self.assertEqual(box.chat(LONG)[0], 400)
        self.assertEqual(box.calls(), [], "a proxy run by hand or by a test asks systemd nothing")

    def test_a_systemd_that_fails_keeps_the_last_ids_it_gave(self):
        box = Box(self, lane=FLASH)
        self.assertEqual(box.m.pool_tokens(), 466112)
        box.fail_file.touch()
        box.switch(B27, sglang="sglang-2", flash="flash-1")
        box.past_the_last_look()
        self.assertEqual(box.m.pool_tokens(), 466112, "no answer from systemd is no news")
        box.fail_file.unlink()
        box.past_the_last_look()
        self.assertEqual(box.m.pool_tokens(), 863644, "the start it could not say then, it says now")

    def test_no_systemctl_at_all_is_no_news(self):
        box = Box(self, lane=FLASH)
        self.assertEqual(box.m.pool_tokens(), 466112)
        (box.t / "bin/systemctl").unlink()
        os.environ["PATH"] = str(box.t / "bin")                     # and none further down the PATH
        box.switch(B27, sglang="sglang-2", flash="flash-1")
        box.past_the_last_look()
        self.assertFalse(box.m.follow_engine_restarts())
        self.assertEqual(box.m.pool_tokens(), 466112)


class TheLook(unittest.TestCase):
    """follow_engine_restarts itself: what it asks, how often, and what it drops."""

    def test_it_asks_for_the_named_units_ids_and_nothing_else(self):
        box = Box(self)
        box.m.follow_engine_restarts()
        self.assertEqual(box.calls(), [f"show {UNITS} -p InvocationID --value"])

    def test_the_first_look_only_learns(self):
        box = Box(self)
        box.m._POOL.update(tokens=466112, window=262138, ts=time.time())
        self.assertFalse(box.m.follow_engine_restarts())
        self.assertEqual(box.m._POOL["tokens"], 466112)

    def test_systemd_is_asked_at_most_every_2_s(self):
        box = Box(self)
        for _ in range(5):
            box.m.follow_engine_restarts()
        self.assertEqual(len(box.calls()), 1)
        box.switch(B27, sglang="sglang-2", flash="flash-1")
        self.assertFalse(box.m.follow_engine_restarts(), "asked again within 2 s")
        box.past_the_last_look()
        self.assertTrue(box.m.follow_engine_restarts())
        self.assertEqual(len(box.calls()), 2)

    def test_a_new_start_drops_every_fact_once(self):
        box = Box(self)
        box.m.follow_engine_restarts()
        box.m._POOL.update(tokens=466112, window=262138, ts=time.time())
        box.m._SERVED.update(names=("qwen3.8-flash-next",), ts=time.time())
        box.m._INFO_ROUTE["path"] = "/get_server_info"
        box.switch(B27, sglang="sglang-2", flash="flash-1")
        box.past_the_last_look()
        self.assertTrue(box.m.follow_engine_restarts())
        self.assertEqual((box.m._POOL["tokens"], box.m._POOL["window"]), (None, None))
        self.assertEqual(box.m._SERVED["names"], ())
        self.assertEqual(box.m._INFO_ROUTE["path"], "/server_info")
        box.past_the_last_look()
        self.assertFalse(box.m.follow_engine_restarts(), "the same start is not news twice")


if __name__ == "__main__":
    unittest.main(verbosity=2)
