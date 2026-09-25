#!/usr/bin/env python3
"""tools-check.py must tell four failures apart, because they are four repairs.

A model that emits nothing, one whose call the parser cannot turn into
`tool_calls`, one whose arguments are not JSON, and one that calls the right
function with the wrong values all read as "tool calling is broken" and all need
different work. So do the two directions: a lane that never calls and a lane
that calls on small talk.

A fake engine here plays each of those, and the gates read the probe's buckets
back. They also check the happy path on two cases, so a probe that scores
nothing correct cannot pass by being uniformly pessimistic.
"""
import http.server
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import unittest

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(REPO_DIR, "tools-check.py")
MODE = ["correct"]          # set per test, read by the handler


def call(name, arguments, *, as_json=True):
    return {"id": "c1", "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(arguments) if as_json else arguments}}


class Fake(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            return self._send(200, {"data": [{"id": "fake-model"}]})
        self._send(404, {})

    def _message(self, ask):
        mode = MODE[0]
        if mode == "refuse":
            return None
        if mode == "silent":
            return {"role": "assistant", "content": "Here is an answer with no call."}
        if mode == "unparsed":
            return {"role": "assistant",
                    "content": '<tool_call>{"name": "get_weather", "arguments": {}}</tool_call>'}
        if mode == "badjson":
            # Truncated mid string: what a cut-off generation actually looks like.
            return {"role": "assistant", "content": None,
                    "tool_calls": [call("get_weather", '{"city": "Oslo", "unit": ', as_json=False)]}
        if mode == "weirdshape":
            # Right name, JSON that parses, values of a type no checker expects:
            # numbers where the schema promised strings, which is what makes a
            # substring test raise rather than simply come out false.
            return {"role": "assistant", "content": None,
                    "tool_calls": [call("get_weather", {"city": 3, "unit": 4,
                                                        "path": 3, "content": 5})]}
        if mode == "empty":
            # a call whose arguments came out empty: nothing can run it
            return {"role": "assistant", "content": None,
                    "tool_calls": [call("get_weather", "", as_json=False)]}
        if mode in ("duplicate", "twocities") and "Oslo and in Bergen" in ask:
            cities = ("Oslo", "Oslo") if mode == "duplicate" else ("Oslo", "Bergen")
            return {"role": "assistant", "content": None,
                    "tool_calls": [call("get_weather", {"city": c, "unit": "celsius"}) for c in cities]}
        if mode in ("wrongb", "rightb") and "divided by 7" in ask:
            return {"role": "assistant", "content": None,
                    "tool_calls": [call("calculator", {"a": 4891, "b": 8 if mode == "wrongb" else 7, "op": "divide"})]}
        if mode == "always":
            return {"role": "assistant", "content": None,
                    "tool_calls": [call("get_weather", {"city": "Oslo", "unit": "celsius"})]}
        # "correct": right on the two cases this fake knows, wrong values elsewhere,
        # and quiet on everything that asks for no tool.
        if "Reykjavik" in ask:
            return {"role": "assistant", "content": None,
                    "tool_calls": [call("get_weather", {"city": "Reykjavik", "unit": "celsius"})]}
        if "multiply 1723" in ask:
            return {"role": "assistant", "content": None,
                    "tool_calls": [call("calculator", {"a": 1723, "b": 4891, "op": "multiply"})]}
        if any(q in ask for q in ("KV cache", "capital of Iceland", "Say hello")):
            return {"role": "assistant", "content": "No tool needed."}
        return {"role": "assistant", "content": None,
                "tool_calls": [call("get_weather", {"city": "nowhere", "unit": "kelvin"})]}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        ask = body["messages"][0]["content"]
        if MODE[0] == "hang":
            time.sleep(3)                   # past the probe's --timeout 1
            return self._send(200, {"choices": []})
        if MODE[0] == "reset":
            self.close_connection = True    # the engine went away mid-request
            return
        msg = self._message(ask)
        if msg is None:
            return self._send(400, {"error": {"message": "refused"}})
        self._send(200, {"choices": [{"index": 0, "message": msg, "finish_reason": "stop"}]})


class ToolsCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def probe(self, mode, *extra):
        MODE[0] = mode
        return subprocess.run(
            [sys.executable, PROBE, "--port", str(self.port), "--timeout", "20", *extra],
            capture_output=True, text=True, timeout=120,
            env=dict(os.environ, QWEN38_API_KEY="not-the-real-key"))

    def bucket(self, out, name):
        for line in out.splitlines():
            if line.startswith(name):
                return line.split()[-1]
        raise AssertionError(f"no {name!r} bucket in:\n{out}")

    def test_a_lane_that_never_calls_scores_zero_calls_and_full_restraint(self):
        """Inert is not the same as undisciplined, and the summary must not hide it."""
        r = self.probe("silent")
        self.assertEqual(self.bucket(r.stdout, "called"), "0/12")
        self.assertEqual(self.bucket(r.stdout, "restraint"), "3/3")
        self.assertIn("emitted no call", r.stdout)

    def test_a_call_the_parser_did_not_parse_is_named_as_such(self):
        """This is the failure clients show to the user as an answer full of JSON."""
        r = self.probe("unparsed")
        self.assertIn("emitted a call the parser did not parse", r.stdout)
        self.assertEqual(self.bucket(r.stdout, "called"), "0/12")

    def test_arguments_that_are_not_json_fail_the_well_formed_bucket_only(self):
        """The call arrived. Nothing can execute it."""
        r = self.probe("badjson")
        self.assertEqual(self.bucket(r.stdout, "called"), "12/12")
        self.assertEqual(self.bucket(r.stdout, "well formed"), "0/12")
        self.assertEqual(self.bucket(r.stdout, "arguments"), "0/12")

    def test_calling_on_small_talk_costs_restraint_not_the_other_buckets(self):
        """Over-triggering is its own defect: every agent turn pays for it."""
        r = self.probe("always")
        self.assertEqual(self.bucket(r.stdout, "restraint"), "0/3")
        self.assertEqual(self.bucket(r.stdout, "called"), "12/12")

    def test_right_values_score_and_wrong_values_do_not(self):
        """Without this, a probe that fails everything would pass every gate above."""
        r = self.probe("correct")
        self.assertEqual(self.bucket(r.stdout, "called"), "12/12")
        self.assertEqual(self.bucket(r.stdout, "well formed"), "12/12")
        self.assertEqual(self.bucket(r.stdout, "arguments"), "2/12")
        self.assertEqual(self.bucket(r.stdout, "restraint"), "3/3")
        self.assertIn("TOOLS SUMMARY: 5/15", r.stdout)

    def test_arguments_of_the_wrong_type_fail_one_case_and_not_the_probe(self):
        """Fourteen other cases still have to run after a checker meets a surprise."""
        r = self.probe("weirdshape")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.bucket(r.stdout, "well formed"), "12/12")
        self.assertEqual(self.bucket(r.stdout, "arguments"), "0/12")
        self.assertIn("TOOLS SUMMARY", r.stdout)

    def test_a_refusal_is_not_a_score(self):
        """A lane that refuses has not been measured, and must not read as a zero."""
        r = self.probe("refuse")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertNotIn("TOOLS SUMMARY", r.stdout)

    def case_line(self, out, name):
        return next(ln for ln in out.splitlines() if name in ln)

    def test_two_identical_calls_are_not_two_calls(self):
        # found in review, 2026-09-24: Oslo asked twice passed the Oslo-and-Bergen case
        bad = self.probe("duplicate")
        self.assertIn("FAIL", self.case_line(bad.stdout, "two calls in one turn"), bad.stdout)
        good = self.probe("twocities")
        self.assertIn("ok", self.case_line(good.stdout, "two calls in one turn"), good.stdout)

    def test_the_second_operand_is_checked(self):
        bad = self.probe("wrongb")
        self.assertIn("FAIL", self.case_line(bad.stdout, "the right tool out of two"), bad.stdout)
        good = self.probe("rightb")
        self.assertIn("ok", self.case_line(good.stdout, "the right tool out of two"), good.stdout)

    def test_empty_arguments_are_not_well_formed(self):
        r = self.probe("empty")
        self.assertEqual(self.bucket(r.stdout, "well formed"), "0/12", r.stdout)

    def test_an_engine_that_stops_answering_is_exit_3_not_a_traceback(self):
        for mode, extra in (("hang", ["--timeout", "1"]), ("reset", [])):
            MODE[0] = mode
            r = subprocess.run([sys.executable, PROBE, "--port", str(self.port), *extra],
                               capture_output=True, text=True, timeout=120,
                               env=dict(os.environ, QWEN38_API_KEY="not-the-real-key"))
            self.assertEqual(r.returncode, 3, f"{mode}: {r.stdout}{r.stderr}")
            self.assertNotIn("Traceback", r.stderr, mode)

    def test_min_turns_the_probe_into_a_gate(self):
        """Same measurement, non-zero exit, so a caller can fail a run on it."""
        ok = self.probe("correct", "--min", "5")
        bad = self.probe("correct", "--min", "6")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)


class SqlEscaping(unittest.TestCase):
    """A correct answer escapes the apostrophe. The checker must know that.

    The first run of this probe against a real engine failed a model that wrote
    `name = 'O''Brien'`, which is how SQL puts an apostrophe inside a quoted
    string. The checker was demanding the bare form, so it scored better SQL as
    a defect (2026-09-18). Only a query that lost the name is wrong.
    """

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("tools_check", PROBE)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)
        cls.case = [c for c in cls.mod.CASES if "apostrophe" in c["name"]][0]

    def test_every_way_of_keeping_the_apostrophe_passes(self):
        for query in ("SELECT * FROM users WHERE name = 'O''Brien';",
                      "SELECT * FROM users WHERE name = 'O\\'Brien'",
                      "select * from users where name = 'O'Brien'"):
            self.assertEqual(self.case["check"]({"query": query}), [], query)

    def test_a_query_that_dropped_the_apostrophe_still_fails(self):
        self.assertTrue(self.case["check"]({"query": "SELECT * FROM users WHERE name = 'OBrien'"}))

    def test_a_query_that_forgot_the_table_still_fails(self):
        self.assertTrue(self.case["check"]({"query": "SELECT * FROM people WHERE name = 'O''Brien'"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
