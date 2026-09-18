#!/usr/bin/env python3
"""Offline tests for the proxy's System One endpoint (v6.19): POST /v1/systemone,
the wire contract of TypeSafe's Jev, answered by the lane behind the proxy from
one single-token chat completion per question and the top_logprobs it returns.

A fake engine stands in for SGLang. It answers /v1/chat/completions with a scripted
first-token distribution, so every number a test asserts is a number the test chose,
and it records each request it receives, so the tests can check what the model was
actually shown (the state, the letters, never the question ids) and in which order
(the state warms the cache alone before the questions fan out).

The bugs these tests name, in the words of what would go wrong:
- a response that is not byte-for-byte the Jev shape is a response the TypeSafe SDK
  refuses, so the shape is asserted key by key, and the SDK itself is run against the
  proxy when it is importable;
- a probability computed over the wrong tokens: " B" (leading space) and "b" are the
  model choosing B, "The" is the prompt failing, and a label the engine did not list
  is a label with no probability, not a crash;
- a request refused with the wrong status, or relayed to the engine when it should
  have been refused (Jev says 422 and names the field);
- a dead engine turned into a size refusal, which is the bug the relay path fixed
  on 2026-08-30 and this path must not reintroduce;
- a label list that is not single-token on the tokenizer the lanes serve: checked
  against the local tokenizer.json when the Hugging Face cache holds one, skipped
  on a runner that does not.
"""
import http.server
import importlib.util
import json
import math
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[1]

QUICKSTART_STATE = ("Hi, I've been trying to connect my Stripe account for 3 days and it keeps "
                    "failing. I'm losing sales. Please help ASAP.")
QUICKSTART_QUESTIONS = {
    "department": {"type": "choice", "instructions": "Which team should handle this",
                   "criteria": {"billing": "Payment or subscription issues",
                                "technical": "Bugs or integration problems",
                                "sales": "Pricing or account questions"}},
    "frustration": {"type": "score", "instructions": "How frustrated the customer appears",
                    "criteria": ["Calm, just stating facts", "Frustrated but civil", "Very angry, strong language"]},
    "is_urgent": {"type": "noul", "instructions": "The message conveys urgency or time-sensitivity"},
}


class Engine(http.server.BaseHTTPRequestHandler):
    """The fake SGLang. `script` maps a substring of the user turn to the first-token
    distribution to answer with; `default` answers everything else."""
    protocol_version = "HTTP/1.1"
    lock = threading.Lock()
    seen = []                 # dicts: t0, t1, body (parsed), auth
    script = {}
    default = {"A": 0.70, " B": 0.20, "C": 0.05, "The": 0.05}
    fail_status = None        # when set, every chat completion answers this status
    no_logprobs = False
    delay = 0.0
    cached_tokens = None
    model = "qwen3.8-test"

    def log_message(self, *a):
        pass

    def _send(self, status, obj, extra=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": Engine.model, "object": "model"}]})
        elif self.path == "/get_server_info":
            self._send(200, {"max_total_num_tokens": 100000})
        elif self.path == "/health":
            self._send(200, {})
        else:
            self._send(404, {"error": "no such route"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or "0")
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/tokenize":
            text = " ".join(str(m.get("content", "")) for m in body.get("messages", []))
            self._send(200, {"tokens": [], "count": len(text.split())})
            return
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": "no such route"})
            return
        t0 = time.time()
        if Engine.delay:
            time.sleep(Engine.delay)
        user = body["messages"][-1]["content"]
        if Engine.fail_status:
            self._send(Engine.fail_status, {"error": {"message": f"engine says {Engine.fail_status}",
                                                        "type": "invalid_request_error"}})
            return
        dist = Engine.default
        for needle, d in Engine.script.items():
            if needle in user:
                dist = d
                break
        top = [{"token": tok, "logprob": math.log(p), "bytes": None} for tok, p in dist.items()]
        first = max(dist, key=dist.__getitem__)
        logprobs = None if Engine.no_logprobs else {"content": [
            {"token": first, "logprob": math.log(dist[first]), "bytes": None, "top_logprobs": top}]}
        usage = {"prompt_tokens": len(user) // 4, "completion_tokens": 1, "total_tokens": len(user) // 4 + 1}
        if Engine.cached_tokens is not None:
            usage["prompt_tokens_details"] = {"cached_tokens": Engine.cached_tokens}
        out = {"id": "chatcmpl-test", "object": "chat.completion", "model": Engine.model,
               "choices": [{"index": 0, "message": {"role": "assistant", "content": first},
                            "logprobs": logprobs, "finish_reason": "length"}],
               "usage": usage}
        with Engine.lock:
            Engine.seen.append({"t0": t0, "t1": time.time(), "body": body,
                                "auth": self.headers.get("Authorization")})
        self._send(200, out)


def _serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class SystemOne(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = _serve(Engine)
        os.environ["UPSTREAM"] = f"http://127.0.0.1:{cls.engine.server_port}"
        os.environ["SYSTEMONE_WARM_CHARS"] = "400"
        os.environ.pop("QWEN38_CLIENT_KEYS_FILE", None)
        spec = importlib.util.spec_from_file_location("kproxy_systemone", REPO / "keepalive-proxy.py")
        cls.mod = importlib.util.module_from_spec(spec)
        sys.argv = ["keepalive-proxy.py"]
        spec.loader.exec_module(cls.mod)
        cls.keyfile = Path.home() / ".config/qwen38/api-key"
        cls.had_key = cls.keyfile.exists()
        if not cls.had_key:
            cls.keyfile.parent.mkdir(parents=True, exist_ok=True)
            cls.keyfile.write_text("test-key\n")
        cls.proxy = cls.mod.Server(("127.0.0.1", 0), cls.mod.H)
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.proxy.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.engine.shutdown()
        if not cls.had_key:
            cls.keyfile.unlink()

    def setUp(self):
        Engine.seen = []
        Engine.script = {}
        Engine.default = {"A": 0.70, " B": 0.20, "C": 0.05, "The": 0.05}
        Engine.fail_status = None
        Engine.no_logprobs = False
        Engine.delay = 0.0
        Engine.cached_tokens = None
        Engine.model = "qwen3.8-test"
        self.mod._SERVED.update(name=None, ts=0.0)

    def post(self, obj, raw=None, headers=None):
        data = raw if raw is not None else json.dumps(obj).encode()
        hdrs = {"Content-Type": "application/json", "Authorization": "Bearer client-token"}
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + "/v1/systemone", data=data, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, dict(r.headers), json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raw_body = e.read()
            try:
                body = json.loads(raw_body.decode())
            except Exception:
                body = raw_body
            return e.code, dict(e.headers), body

    def quickstart(self, model="jev-latest"):
        return {"state": QUICKSTART_STATE, "model": model, "questions": QUICKSTART_QUESTIONS}

    # ---- the contract ------------------------------------------------------
    def test_quickstart_answers_have_exactly_the_jev_shape(self):
        status, headers, out = self.post(self.quickstart())
        self.assertEqual(status, 200, out)
        self.assertEqual(set(out), {"model", "answers", "usage"})
        self.assertEqual(set(out["answers"]), set(QUICKSTART_QUESTIONS))
        dept = out["answers"]["department"]
        self.assertEqual(set(dept), {"type", "choice", "probabilities", "confidence"})
        self.assertEqual(dept["type"], "choice")
        self.assertEqual(list(dept["probabilities"]), ["billing", "technical", "sales"])
        self.assertAlmostEqual(sum(dept["probabilities"].values()), 1.0, places=5)
        self.assertEqual(dept["choice"], max(dept["probabilities"], key=dept["probabilities"].__getitem__))
        fr = out["answers"]["frustration"]
        self.assertEqual(set(fr), {"type", "score", "legend", "probabilities", "confidence"})
        self.assertEqual(fr["legend"], {"0": "Calm, just stating facts", "1": "Frustrated but civil",
                                        "2": "Very angry, strong language"})
        self.assertEqual(list(fr["probabilities"]), ["0", "1", "2"])
        self.assertTrue(0.0 <= fr["score"] <= 2.0)
        urg = out["answers"]["is_urgent"]
        self.assertEqual(set(urg), {"type", "noul"})
        self.assertTrue(0.0 <= urg["noul"] <= 1.0)
        self.assertEqual(set(out["usage"]), {"input_tokens", "output_tokens"})
        self.assertIsInstance(out["usage"]["input_tokens"], int)
        self.assertEqual(out["usage"]["output_tokens"], 3)
        self.assertEqual(headers["x-systemone-branches"], "3")

    def test_the_readout_is_the_renormalized_label_mass(self):
        # A 0.70, " B" 0.20 (a leading space is still B), C 0.05, "The" 0.05 is not a label
        status, headers, out = self.post({"state": "s", "model": "m", "questions": {
            "q": {"type": "choice", "instructions": "i", "criteria": {"x": None, "y": None, "z": None}}}})
        self.assertEqual(status, 200, out)
        p = out["answers"]["q"]["probabilities"]
        self.assertAlmostEqual(p["x"], 0.70 / 0.95, places=5)
        self.assertAlmostEqual(p["y"], 0.20 / 0.95, places=5)
        self.assertAlmostEqual(p["z"], 0.05 / 0.95, places=5)
        self.assertEqual(out["answers"]["q"]["choice"], "x")
        self.assertEqual(headers["x-systemone-label-mass"], "0.9500")

    def test_case_space_and_punctuation_variants_count_for_their_label(self):
        Engine.default = {"A": 0.10, " a": 0.20, "a.": 0.10, " A:": 0.10, "B": 0.50}
        status, _, out = self.post({"state": "s", "model": "m", "questions": {
            "q": {"type": "choice", "instructions": "i", "criteria": {"x": None, "y": None}}}})
        self.assertEqual(status, 200, out)
        self.assertAlmostEqual(out["answers"]["q"]["probabilities"]["x"], 0.5, places=5)
        self.assertAlmostEqual(out["answers"]["q"]["probabilities"]["y"], 0.5, places=5)

    def test_a_label_the_engine_did_not_list_has_zero_probability_not_an_error(self):
        Engine.default = {"A": 0.9, "The": 0.1}
        status, _, out = self.post({"state": "s", "model": "m", "questions": {
            "q": {"type": "choice", "instructions": "i", "criteria": {"x": None, "y": None, "z": None}}}})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["answers"]["q"]["probabilities"], {"x": 1.0, "y": 0.0, "z": 0.0})
        self.assertEqual(out["answers"]["q"]["confidence"], 1.0)

    def test_no_probability_on_any_label_is_a_502_not_a_guess(self):
        Engine.default = {"The": 0.6, "<think>": 0.4}
        status, _, out = self.post({"state": "s", "model": "m", "questions": {
            "q": {"type": "noul", "instructions": "i"}}})
        self.assertEqual(status, 502, out)
        self.assertIn("no probability on any option label", out["error"]["message"])

    def test_noul_is_the_probability_of_the_first_label_which_is_yes(self):
        Engine.default = {"A": 0.3, "B": 0.6, "junk": 0.1}
        status, _, out = self.post({"state": "s", "model": "m", "questions": {
            "q": {"type": "noul", "instructions": "Does it?", "criteria": {"true": "yes it does", "false": "no"}}}})
        self.assertEqual(status, 200, out)
        self.assertAlmostEqual(out["answers"]["q"]["noul"], 0.3 / 0.9, places=5)
        shown = Engine.seen[0]["body"]["messages"][-1]["content"]
        self.assertIn("A. yes: yes it does", shown)
        self.assertIn("B. no: no", shown)

    def test_score_is_the_probability_weighted_level_index(self):
        Engine.default = {"A": 0.05, "B": 0.30, "C": 0.65}
        status, _, out = self.post({"state": "s", "model": "m", "questions": {
            "q": {"type": "score", "instructions": "i", "criteria": ["Calm", "Frustrated", "Very angry"]}}})
        self.assertEqual(status, 200, out)
        self.assertAlmostEqual(out["answers"]["q"]["score"], 0.30 + 2 * 0.65, places=5)
        self.assertEqual(out["answers"]["q"]["probabilities"], {"0": 0.05, "1": 0.3, "2": 0.65})
        shown = Engine.seen[0]["body"]["messages"][-1]["content"]
        self.assertIn("OPTIONS, ordered from the lowest level to the highest", shown)
        self.assertIn("A. Calm\nB. Frustrated\nC. Very angry", shown)

    def test_confidence_follows_the_live_jev_formulas_not_the_docs(self):
        """Pairs read off the hosted jev-1.13.0 on 2026-09-18 (probabilities as it
        publishes them, two decimals). The docs' normalized entropy fits none of
        these; the two formulas below fit all 166 pairs collected within the
        rounding of the published probabilities."""
        choice, score = self.mod.systemone_choice_confidence, self.mod.systemone_score_confidence
        for probs, conf in [([0.67, 0.33, 0.0], 0.50), ([0.74, 0.26], 0.47), ([0.55, 0.2, 0.12, 0.09, 0.04], 0.43),
                            ([0.97, 0.03, 0.0, 0.0, 0.0], 0.96), ([0.48, 0.52], 0.03), ([0.39, 0.31, 0.30], 0.08)]:
            self.assertAlmostEqual(choice(probs), conf, delta=0.02, msg=probs)
        for probs, conf in [([0.0, 0.25, 0.75, 0.0, 0.0], 0.79), ([0.21, 0.05, 0.72, 0.02, 0.0], 0.58),
                            ([0.56, 0.44, 0.0], 0.33), ([0.05, 0.95, 0.0], 0.92), ([0.07, 0.79, 0.14, 0.0], 0.80),
                            ([0.02, 0.32, 0.35, 0.28, 0.03, 0.0, 0.0], 0.59)]:
            self.assertAlmostEqual(score(probs), conf, delta=0.02, msg=probs)
        # the worst of the 120 Score pairs, a spread over ten levels with a shallow mode
        self.assertAlmostEqual(score([0.36, 0.15, 0.12, 0.1, 0.15, 0.11, 0.01, 0.0, 0.0, 0.0]), 0.21, delta=0.035)
        # the limits both formulas share
        for f in (choice, score):
            self.assertEqual(f([1.0, 0.0, 0.0]), 1.0)
            self.assertEqual(f([0.25, 0.25, 0.25, 0.25]), 0.0)
            self.assertEqual(f([0.8, 0.2]), 0.6)          # at N=2 the Score formula is the Choice formula

    def test_answer_keys_come_in_the_order_the_hosted_api_writes_them(self):
        status, _, out = self.post(self.quickstart())
        self.assertEqual(status, 200, out)
        self.assertEqual(list(out["answers"]["department"]), ["type", "choice", "confidence", "probabilities"])
        self.assertEqual(list(out["answers"]["frustration"]), ["type", "score", "confidence", "legend", "probabilities"])
        self.assertEqual(list(out["answers"]["is_urgent"]), ["type", "noul"])

    def test_a_label_mass_floor_refuses_a_confused_answer_when_set(self):
        Engine.default = {"A": 0.10, "The": 0.90}                 # a tenth of the mass on a label
        status, _, out = self.post({"state": "s", "model": "m", "questions": {"q": {"type": "noul", "instructions": "i"}}})
        self.assertEqual(status, 200, out)                        # default floor 0: answered, reported
        old = self.mod.SYSTEMONE_MIN_LABEL_MASS
        self.mod.SYSTEMONE_MIN_LABEL_MASS = 0.5
        try:
            status, _, out = self.post({"state": "s", "model": "m", "questions": {"q": {"type": "noul", "instructions": "i"}}})
        finally:
            self.mod.SYSTEMONE_MIN_LABEL_MASS = old
        self.assertEqual(status, 502, out)
        self.assertIn("0.100", out["error"]["message"])
        self.assertIn("SYSTEMONE_MIN_LABEL_MASS", out["error"]["message"])

    # ---- what the model is shown --------------------------------------------
    def test_every_branch_is_one_token_with_logprobs_and_thinking_off(self):
        status, _, out = self.post(self.quickstart(model="qwen3.8-test"))
        self.assertEqual(status, 200, out)
        self.assertEqual(len(Engine.seen), 3)
        for rec in Engine.seen:
            b = rec["body"]
            self.assertEqual(b["max_tokens"], 1)
            self.assertIs(b["logprobs"], True)
            self.assertGreaterEqual(b["top_logprobs"], 3)
            self.assertIs(b["stream"], False)
            self.assertEqual(b["chat_template_kwargs"], {"enable_thinking": False})
            self.assertEqual(b["model"], "qwen3.8-test")
            self.assertEqual(b["messages"][0]["role"], "system")
            self.assertEqual(rec["auth"], "Bearer client-token")

    def test_all_branches_share_the_state_prefix_byte_for_byte(self):
        self.post(self.quickstart())
        users = [r["body"]["messages"][-1]["content"] for r in Engine.seen]
        prefix = "STATE\n" + QUICKSTART_STATE + "\n\nQUESTION\n"
        for u in users:
            self.assertTrue(u.startswith(prefix), u[:80])
        self.assertEqual(len({u for u in users}), 3, "three different questions, three different suffixes")

    def test_question_ids_never_reach_the_model_but_option_names_do(self):
        self.post(self.quickstart())
        shown = "\n".join(r["body"]["messages"][-1]["content"] for r in Engine.seen)
        for qid in QUICKSTART_QUESTIONS:
            self.assertNotIn(qid, shown, f"question id {qid!r} leaked into the prompt")
        self.assertIn("A. billing: Payment or subscription issues", shown)
        self.assertIn("C. sales: Pricing or account questions", shown)

    def test_structured_state_instructions_and_criteria_are_rendered_as_json(self):
        req = {"state": {"ticket": {"subject": "Duplicate charge",
                                    "messages": [{"from": "customer", "text": "Charged twice for A-104."}]},
                         "refund_policy": "Duplicate charges are eligible for a refund."},
               "model": "m",
               "questions": {"q": {"type": "choice",
                                   "instructions": {"question": "Which team?", "focus": "primary request"},
                                   "criteria": {"billing": {"what": "charges", "not_for": "delivery"},
                                                "orders": None}}}}
        status, _, out = self.post(req)
        self.assertEqual(status, 200, out)
        shown = Engine.seen[0]["body"]["messages"][-1]["content"]
        self.assertIn('"subject": "Duplicate charge"', shown)
        self.assertIn('"refund_policy": "Duplicate charges are eligible for a refund."', shown)
        self.assertIn('{"question": "Which team?", "focus": "primary request"}', shown)
        self.assertIn('A. billing: {"what": "charges", "not_for": "delivery"}', shown)
        self.assertIn("B. orders\n", shown)

    def test_a_multiline_description_stays_under_its_label(self):
        self.post({"state": "s", "model": "m", "questions": {"q": {"type": "choice", "instructions": "i",
                   "criteria": {"x": "first line\nsecond line", "y": None}}}})
        shown = Engine.seen[0]["body"]["messages"][-1]["content"]
        self.assertIn("A. x: first line\n   second line\nB. y", shown)

    def test_non_ascii_survives_unescaped_in_both_directions(self):
        Engine.default = {"A": 0.8, "B": 0.2}
        status, _, out = self.post({"state": "Le client a été débité deux fois.", "model": "m",
                                    "questions": {"q": {"type": "choice", "instructions": "Quelle équipe ?",
                                                        "criteria": {"facturation": "Paiements", "livraison": None}}}})
        self.assertEqual(status, 200, out)
        self.assertIn("Le client a été débité deux fois.", Engine.seen[0]["body"]["messages"][-1]["content"])
        self.assertEqual(out["answers"]["q"]["choice"], "facturation")

    # ---- the fan-out and the cache -------------------------------------------
    def test_a_long_state_is_sent_alone_before_the_questions_fan_out(self):
        Engine.delay = 0.15
        state = "x" * 600                                   # over SYSTEMONE_WARM_CHARS=400
        qs = {f"q{i}": {"type": "noul", "instructions": f"question {i}"} for i in range(3)}
        status, _, out = self.post({"state": state, "model": "m", "questions": qs})
        self.assertEqual(status, 200, out)
        seen = sorted(Engine.seen, key=lambda r: r["t0"])
        self.assertEqual(len(seen), 3)
        self.assertGreaterEqual(seen[1]["t0"], seen[0]["t1"] - 0.01, "second branch started before the first finished")
        self.assertGreaterEqual(seen[2]["t0"], seen[0]["t1"] - 0.01)
        self.assertEqual(seen[0]["body"]["messages"][-1]["content"].count("question 0"), 1)

    def test_a_short_state_fans_out_at_once(self):
        Engine.delay = 0.15
        qs = {f"q{i}": {"type": "noul", "instructions": f"question {i}"} for i in range(3)}
        status, _, out = self.post({"state": "short", "model": "m", "questions": qs})
        self.assertEqual(status, 200, out)
        seen = sorted(Engine.seen, key=lambda r: r["t0"])
        self.assertLess(seen[2]["t0"], seen[0]["t1"], "the branches did not overlap")

    def test_usage_sums_every_branch_and_counts_one_output_token_per_question(self):
        Engine.cached_tokens = 7
        status, headers, out = self.post(self.quickstart())
        self.assertEqual(status, 200, out)
        expected = sum(r["body"]["messages"][-1]["content"].__len__() // 4 for r in Engine.seen)
        self.assertEqual(out["usage"], {"input_tokens": expected, "output_tokens": 3})
        self.assertEqual(headers["x-systemone-cached-tokens"], "21")

    def test_cached_tokens_header_is_absent_when_the_engine_does_not_report_them(self):
        status, headers, _ = self.post(self.quickstart())
        self.assertEqual(status, 200)
        self.assertNotIn("x-systemone-cached-tokens", headers)

    # ---- the model field -----------------------------------------------------
    def test_jev_aliases_resolve_to_the_served_model_and_the_answer_names_it(self):
        for alias in ("jev-latest", "jev-preview", "jev-1.13.0", "JEV-latest"):
            Engine.seen = []
            self.mod._SERVED.update(name=None, ts=0.0)
            status, _, out = self.post(self.quickstart(model=alias))
            self.assertEqual(status, 200, out)
            self.assertEqual({r["body"]["model"] for r in Engine.seen}, {"qwen3.8-test"}, alias)
            self.assertEqual(out["model"], "qwen3.8-test")

    def test_any_other_model_name_goes_to_the_engine_as_given(self):
        status, _, out = self.post(self.quickstart(model="qwen3.8-27b"))
        self.assertEqual(status, 200, out)
        self.assertEqual({r["body"]["model"] for r in Engine.seen}, {"qwen3.8-27b"})

    # ---- refusals: 422 with the field ----------------------------------------
    def test_validation_refusals_are_422_and_name_the_field(self):
        good = self.quickstart()
        cases = [
            ("not json", None, b"{not json", None),
            ("a list", None, b"[]", None),
            ("no state", {k: v for k, v in good.items() if k != "state"}, None, "state"),
            ("state is a number", {**good, "state": 3}, None, "state"),
            ("no model", {k: v for k, v in good.items() if k != "model"}, None, "model"),
            ("empty model", {**good, "model": " "}, None, "model"),
            ("no questions", {k: v for k, v in good.items() if k != "questions"}, None, "questions"),
            ("empty questions", {**good, "questions": {}}, None, "questions"),
            ("question not an object", {**good, "questions": {"q": "x"}}, None, "questions.q"),
            ("unknown type", {**good, "questions": {"q": {"type": "rank", "instructions": "i"}}}, None, "questions.q.type"),
            ("choice without criteria", {**good, "questions": {"q": {"type": "choice", "instructions": "i"}}}, None, "questions.q.criteria"),
            ("choice with one option", {**good, "questions": {"q": {"type": "choice", "instructions": "i", "criteria": {"x": None}}}}, None, "questions.q.criteria"),
            ("choice with an empty option name", {**good, "questions": {"q": {"type": "choice", "instructions": "i", "criteria": {"": None, "y": None}}}}, None, "questions.q.criteria"),
            ("choice description of the wrong kind", {**good, "questions": {"q": {"type": "choice", "instructions": "i", "criteria": {"x": 4, "y": None}}}}, None, "questions.q.criteria.x"),
            ("score with one level", {**good, "questions": {"q": {"type": "score", "instructions": "i", "criteria": ["a"]}}}, None, "questions.q.criteria"),
            ("score with eleven levels", {**good, "questions": {"q": {"type": "score", "instructions": "i", "criteria": list("abcdefghijk")}}}, None, "questions.q.criteria"),
            ("score criteria as an object", {**good, "questions": {"q": {"type": "score", "instructions": "i", "criteria": {"0": "a", "1": "b"}}}}, None, "questions.q.criteria"),
            ("noul criteria with a stray key", {**good, "questions": {"q": {"type": "noul", "instructions": "i", "criteria": {"true": "t", "maybe": "m"}}}}, None, "questions.q.criteria"),
            ("instructions of the wrong kind", {**good, "questions": {"q": {"type": "noul", "instructions": 12}}}, None, "questions.q.instructions"),
        ]
        for name, obj, raw, param in cases:
            with self.subTest(name):
                status, _, out = self.post(obj, raw=raw)
                self.assertEqual(status, 422, (name, out))
                self.assertEqual(out["error"]["type"], "invalid_request")
                self.assertTrue(out["error"]["message"].startswith("keepalive-proxy: "))
                self.assertEqual(out["error"].get("param"), param, name)
        self.assertEqual(Engine.seen, [], "a refused request must never reach the engine")

    def test_too_many_questions_are_refused_before_any_reaches_the_engine(self):
        qs = {f"q{i}": {"type": "noul", "instructions": "i"} for i in range(self.mod.SYSTEMONE_MAX_QUESTIONS + 1)}
        status, _, out = self.post({"state": "s", "model": "m", "questions": qs})
        self.assertEqual(status, 422, out)
        self.assertEqual(Engine.seen, [])

    def test_more_options_than_the_cap_are_refused(self):
        crit = {f"opt{i}": None for i in range(self.mod.SYSTEMONE_MAX_OPTIONS + 1)}
        status, _, out = self.post({"state": "s", "model": "m", "questions": {
            "q": {"type": "choice", "instructions": "i", "criteria": crit}}})
        self.assertEqual(status, 422, out)
        self.assertIn(str(self.mod.SYSTEMONE_MAX_OPTIONS), out["error"]["message"])

    def test_the_cap_itself_is_served_with_two_letter_labels(self):
        n = self.mod.SYSTEMONE_MAX_OPTIONS
        Engine.default = {"AB": 0.5, "A": 0.5}
        crit = {f"opt{i}": None for i in range(n)}
        status, _, out = self.post({"state": "s", "model": "m", "questions": {
            "q": {"type": "choice", "instructions": "i", "criteria": crit}}})
        self.assertEqual(status, 200, out)
        labels = self.mod.SYSTEMONE_LABELS[:n]
        shown = Engine.seen[0]["body"]["messages"][-1]["content"]
        self.assertIn(f"{labels[-1]}. opt{n - 1}", shown)
        self.assertGreaterEqual(Engine.seen[0]["body"]["top_logprobs"], n)
        self.assertEqual(out["answers"]["q"]["probabilities"]["opt0"], 0.5)
        self.assertEqual(out["answers"]["q"]["probabilities"][f"opt{labels.index('AB')}"], 0.5)

    # ---- the engine's own failures -------------------------------------------
    def test_an_engine_401_is_relayed_as_401(self):
        Engine.fail_status = 401
        status, _, out = self.post(self.quickstart())
        self.assertEqual(status, 401, out)
        self.assertIn("engine says 401", json.dumps(out))

    def test_an_engine_5xx_is_the_relay_path_503_never_a_size_refusal(self):
        Engine.fail_status = 503
        status, headers, out = self.post(self.quickstart())
        self.assertEqual(status, 503, out)
        self.assertEqual(out["error"]["type"], "engine_unavailable")
        self.assertIn("NOT refused for its size", out["error"]["message"])
        self.assertEqual(headers.get("Retry-After"), "30")

    def test_an_engine_without_logprobs_is_a_502_that_says_so(self):
        Engine.no_logprobs = True
        status, _, out = self.post(self.quickstart())
        self.assertEqual(status, 502, out)
        self.assertIn("no logprobs", out["error"]["message"])

    def test_a_dead_engine_is_a_503_with_retry_after(self):
        os.environ["UPSTREAM"] = "http://127.0.0.1:9"            # nothing listens on the discard port
        old = self.mod.UPSTREAM
        self.mod.UPSTREAM = "http://127.0.0.1:9"
        try:
            status, headers, out = self.post(self.quickstart(model="m"))
        finally:
            self.mod.UPSTREAM = old
        self.assertEqual(status, 503, out)
        self.assertEqual(out["error"]["type"], "engine_unavailable")
        self.assertEqual(headers.get("Retry-After"), "30")

    # ---- the two levers, off by default ------------------------------------
    def test_two_presentations_average_back_in_the_given_order(self):
        old = self.mod.SYSTEMONE_PERMUTATIONS
        self.mod.SYSTEMONE_PERMUTATIONS = 2
        try:
            status, headers, out = self.post({"state": "s", "model": "m", "questions": {
                "q": {"type": "choice", "instructions": "i", "criteria": {"x": None, "y": None, "z": None}},
                "n": {"type": "noul", "instructions": "yes?"}}})
        finally:
            self.mod.SYSTEMONE_PERMUTATIONS = old
        self.assertEqual(status, 200, out)
        self.assertEqual(headers["x-systemone-branches"], "4")
        self.assertEqual(out["usage"]["output_tokens"], 4)
        shown = sorted(r["body"]["messages"][-1]["content"] for r in Engine.seen if "OPTIONS\nA. x" in r["body"]["messages"][-1]["content"]
                       or "OPTIONS\nA. z" in r["body"]["messages"][-1]["content"])
        self.assertEqual(len(shown), 2)
        self.assertIn("A. x\nB. y\nC. z", shown[0])
        self.assertIn("A. z\nB. y\nC. x", shown[1])
        # engine: A 0.70, " B" 0.20, C 0.05 in each presentation (renormalized over labels: .7368 .2105 .0526)
        p = out["answers"]["q"]["probabilities"]
        self.assertAlmostEqual(p["x"], (0.70 + 0.05) / 0.95 / 2, places=5)
        self.assertAlmostEqual(p["y"], 0.20 / 0.95, places=5)
        self.assertAlmostEqual(p["z"], (0.05 + 0.70) / 0.95 / 2, places=5)
        # noul: given order yes/no gives A=yes .70/.90; reversed gives A=no .70/.90 so yes=.20/.90; averaged
        self.assertAlmostEqual(out["answers"]["n"]["noul"], (0.70 / 0.90 + 0.20 / 0.90) / 2, places=5)

    def test_temperature_flattens_or_sharpens_the_readout(self):
        old = self.mod.SYSTEMONE_TEMPERATURE
        try:
            self.mod.SYSTEMONE_TEMPERATURE = 2.0
            _, _, hot = self.post({"state": "s", "model": "m", "questions": {
                "q": {"type": "choice", "instructions": "i", "criteria": {"x": None, "y": None, "z": None}}}})
            self.mod.SYSTEMONE_TEMPERATURE = 0.5
            _, _, cold = self.post({"state": "s", "model": "m", "questions": {
                "q": {"type": "choice", "instructions": "i", "criteria": {"x": None, "y": None, "z": None}}}})
        finally:
            self.mod.SYSTEMONE_TEMPERATURE = old
        r = [0.70 ** 0.5, 0.20 ** 0.5, 0.05 ** 0.5]; t = sum(r)
        self.assertAlmostEqual(hot["answers"]["q"]["probabilities"]["x"], r[0] / t, places=4)
        self.assertAlmostEqual(hot["answers"]["q"]["probabilities"]["z"], r[2] / t, places=4)
        r = [0.70 ** 2, 0.20 ** 2, 0.05 ** 2]; t = sum(r)
        self.assertAlmostEqual(cold["answers"]["q"]["probabilities"]["x"], r[0] / t, places=4)
        self.assertGreater(cold["answers"]["q"]["confidence"], hot["answers"]["q"]["confidence"])

    # ---- pure pieces ---------------------------------------------------------
    def test_the_label_list_is_588_distinct_capital_labels_in_product_order(self):
        labels = self.mod.SYSTEMONE_LABELS
        self.assertEqual(len(labels), 588)
        self.assertEqual(len(set(labels)), 588)
        self.assertEqual(labels[:26], list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
        self.assertEqual(labels[26:29], ["AA", "AB", "AC"])
        self.assertNotIn("BQ", labels)
        self.assertEqual(len(self.mod._SYSTEMONE_UNTOKENED_PAIRS), 114)
        self.assertTrue(all(len(x) == 2 and x.isupper() for x in self.mod._SYSTEMONE_UNTOKENED_PAIRS))

    def test_labels_are_single_tokens_in_the_served_tokenizer(self):
        """Vocabulary membership is exactly single-token encoding for these labels
        (checked with the tokenizers library on all 676 pairs, 0 disagreements), so
        the json vocab is enough here and no tokenizer library is needed. Skipped
        on a machine whose Hugging Face cache holds no Qwen3.8 checkpoint."""
        hub = Path.home() / ".cache/huggingface/hub"
        files = sorted(hub.glob("models--*Qwen3.8*/snapshots/*/tokenizer.json")) if hub.is_dir() else []
        if not files:
            self.skipTest("no Qwen3.8 tokenizer.json in the local Hugging Face cache")
        vocab = json.loads(files[0].read_text())["model"]["vocab"]
        missing = [L for L in self.mod.SYSTEMONE_LABELS if L not in vocab]
        self.assertEqual(missing, [], "labels that are not one vocabulary entry")
        present = [p for p in self.mod._SYSTEMONE_UNTOKENED_PAIRS if p in vocab]
        self.assertEqual(present, [], "pairs excluded as multi-token that the vocabulary does hold")
        self.assertTrue(all(("Ġ" + L) in vocab for L in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
                        "the leading-space variants the readout folds in are single tokens too")

    def test_top_k_covers_the_labels_and_leaves_room_for_variants(self):
        k = self.mod.systemone_top_k
        self.assertEqual(k(2), 8)
        self.assertEqual(k(3), 9)
        self.assertEqual(k(60), min(66, self.mod.SYSTEMONE_TOP_K_MAX))
        self.assertGreaterEqual(k(self.mod.SYSTEMONE_MAX_OPTIONS), self.mod.SYSTEMONE_MAX_OPTIONS)

    def test_read_ignores_malformed_top_logprob_entries(self):
        answer = {"choices": [{"logprobs": {"content": [{"token": "A", "logprob": -0.1, "top_logprobs": [
            {"token": "A", "logprob": math.log(0.5)}, {"token": None, "logprob": -1.0},
            {"token": "B", "logprob": "nan"}, {"token": "B", "logprob": math.log(0.25)}]}]}}],
                  "usage": {"prompt_tokens": 12}, "model": "m"}
        mass, total, ptoks, cached, model = self.mod.systemone_read(answer, ["A", "B"])
        self.assertAlmostEqual(mass[0], 0.5)
        self.assertAlmostEqual(mass[1], 0.25)
        self.assertAlmostEqual(total, 0.75)
        self.assertEqual((ptoks, cached, model), (12, None, "m"))

    # ---- the official SDK ----------------------------------------------------
    def test_the_typesafe_sdk_round_trips_against_the_proxy(self):
        try:
            from typesafe_sdk import Choice, Noul, Score, TypeSafeClient
        except ImportError:
            self.skipTest("typesafe-sdk is not installed in this interpreter")
        with TypeSafeClient(api_key="client-token", base_url=self.base) as client:
            response = client.system_one(
                state={"document": "I was charged twice. Please fix this ASAP."},
                questions={
                    "billing": Noul(instructions="Is this ticket about billing?"),
                    "tone": Choice(instructions="What is the customer's tone?",
                                   criteria={"calm": None, "frustrated": None, "angry": None}),
                    "urgency": Score(instructions="How urgent is this ticket?",
                                     criteria=["can wait", "this week", "today"]),
                })
        # two labels for a noul: A 0.70 and " B" 0.20 are label mass, "C" and "The" are not
        self.assertAlmostEqual(response.nouls["billing"].noul, 0.70 / 0.90, places=4)
        self.assertEqual(response.choices["tone"].choice, "calm")
        self.assertTrue(0.0 <= response.scores["urgency"].score <= 2.0)
        self.assertEqual(response.model, "qwen3.8-test")
        self.assertEqual(response.usage.output_tokens, 3)


if __name__ == "__main__":
    unittest.main(verbosity=1)
