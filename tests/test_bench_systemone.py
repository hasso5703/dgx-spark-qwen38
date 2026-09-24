#!/usr/bin/env python3
"""bench-systemone.py scores a target on all its rows, from one model, or not at all.

Its contract (the module's docstring, BENCHMARKS.md): "the report refuses to score a target
that is missing rows: a denominator that quietly shrank would be a number about nothing".
The classification and gdpr reports kept it; the public report scored whatever rows were
there, so a run that died after 5 of 76 nodes published a line about 5 nodes. And a run
resumed after a lane switch appended the other lane's answers to the same "ours" file: every
row records the model that answered it, and nothing read it back, so one table line
described two models at once. These call the report functions as written, on items shaped
like the prepared tasks.
"""
import importlib.util
import io
import json
import pathlib
import re
import shutil
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bench_systemone", REPO / "bench-systemone.py")
bs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bs)


def public_items():
    items = []
    for n in range(3):
        qs = {f"q{n}a": {"type": "noul", "instructions": "Is it paid?"},
              f"q{n}b": {"type": "choice", "instructions": "Which team?", "criteria": {"x": "", "y": ""}}}
        refs = {f"q{n}a": {"sets": [{"value": True}, {"value": True}]},
                f"q{n}b": {"sets": [{"value": "x"}, {"value": "x"}]}}
        saved = {"typesafe": {f"q{n}a": {"type": "noul", "noul": 0.9},
                              f"q{n}b": {"type": "choice", "choice": "x", "probabilities": {"x": 0.8, "y": 0.2}}}}
        items.append({"id": f"invoice_processing/c{n}/node{n}", "workflow": "invoice_processing",
                      "payload": {"state": "doc", "questions": qs}, "gold": {"references": refs, "saved": saved}})
    return items


def answered(item, model="qwen3.8-27b", drop=None):
    answers = {}
    for qid, q in item["payload"]["questions"].items():
        if qid == drop:
            continue
        answers[qid] = ({"type": "noul", "noul": 0.8} if q["type"] == "noul" else
                        {"type": "choice", "choice": "x", "probabilities": {"x": 0.7, "y": 0.3}})
    return {"id": item["id"], "target": "ours", "answers": answers, "model": model, "latency_s": 1.0}


def boolq_items(n=4):
    return [{"id": f"b{i}", "payload": {"state": "p", "questions": {"answer": {"type": "noul"}}},
             "gold": {"answer": i % 2 == 0}} for i in range(n)]


def boolq_row(item, model):
    return {"id": item["id"], "target": "ours", "answers": {"answer": {"type": "noul", "noul": 0.9}},
            "model": model, "latency_s": 0.5, "usage": {"input_tokens": 10}}


def line_of(report, name):
    return next(ln for ln in report.splitlines() if ln.startswith(f"| {name} |"))


class ThePublicReport(unittest.TestCase):
    def test_a_whole_run_is_scored(self):
        items = public_items()
        rep = bs.report_public(items, {"ours": {it["id"]: answered(it) for it in items}})
        self.assertNotIn("not scored", line_of(rep, "ours"))
        self.assertIn("| ours | 6 |", rep)

    def test_a_run_missing_a_node_is_not_scored(self):
        items = public_items()
        rep = bs.report_public(items, {"ours": {it["id"]: answered(it) for it in items[:2]}})
        self.assertIn("not scored", line_of(rep, "ours"), rep)
        self.assertNotIn("| ours | invoice_processing |", rep)

    def test_a_node_answered_in_part_is_not_scored(self):
        items = public_items()
        rows = {it["id"]: answered(it) for it in items}
        rows[items[1]["id"]] = answered(items[1], drop="q1b")
        self.assertIn("not scored", line_of(bs.report_public(items, {"ours": rows}), "ours"))

    def test_the_saved_models_are_still_scored_without_a_run(self):
        rep = bs.report_public(public_items(), {})
        self.assertNotIn("not scored", line_of(rep, "saved typesafe"))


class ARunOfTwoModels(unittest.TestCase):
    def test_the_classification_report_refuses_a_mix(self):
        items = boolq_items()
        rows = {it["id"]: boolq_row(it, "qwen3.8-27b" if i < 2 else "qwen3.8-flash-next")
                for i, it in enumerate(items)}
        line = line_of(bs.report_classification("boolq", items, {"ours": rows}), "ours")
        self.assertIn("not scored", line)
        self.assertIn("qwen3.8-flash-next", line)

    def test_one_model_is_scored(self):
        items = boolq_items()
        rows = {it["id"]: boolq_row(it, "qwen3.8-27b") for it in items}
        self.assertNotIn("not scored", line_of(bs.report_classification("boolq", items, {"ours": rows}), "ours"))

    def test_the_public_report_refuses_a_mix(self):
        items = public_items()
        rows = {it["id"]: answered(it, "qwen3.8-27b" if i else "qwen3.8-flash-next") for i, it in enumerate(items)}
        self.assertIn("not scored", line_of(bs.report_public(items, {"ours": rows}), "ours"))

    def test_the_gdpr_report_refuses_a_mix(self):
        def answer(q):
            if q["type"] == "noul":
                return {"type": "noul", "noul": 0.9}
            if q["type"] == "choice":
                first = next(iter(q["criteria"]))
                return {"type": "choice", "choice": first, "probabilities": {k: float(k == first) for k in q["criteria"]}}
            return {"type": "score", "score": 1, "legend": q["criteria"]}
        items = [{"id": f"g{i}", "mode": "batched" if i % 2 else "single"} for i in range(4)]
        rows = {it["id"]: {"id": it["id"], "answers": {k: answer(q) for k, q in bs.GDPR_QUESTIONS.items()},
                           "model": "jev-1.13.0" if i else "jev-1.14.0", "latency_s": 1.0,
                           "usage": {"input_tokens": 100}} for i, it in enumerate(items)}
        rep = bs.report_gdpr(items, {"jev": rows})
        self.assertIn("not scored", rep)
        self.assertIn("jev-1.14.0", rep)
        # the same rows from one model are scored
        for r in rows.values():
            r["model"] = "jev-1.13.0"
        self.assertNotIn("not scored", bs.report_gdpr(items, {"jev": rows}))


class TheFanout(unittest.TestCase):
    """A first call is cold only when the engine says nothing was cached: every question
    count after the first reuses the state the one before sent."""

    def test_only_a_first_call_with_nothing_cached_is_cold(self):
        data = pathlib.Path(tempfile.mkdtemp(prefix="bench-so-fan-"))
        self.addCleanup(shutil.rmtree, data, ignore_errors=True)
        (data / "raw").mkdir()
        (data / "raw" / f"gdpr-{bs.GDPR_REVISION}.txt").write_text("GDPR " * 200)
        calls = []

        def fake_post(target, payload, model, timeout):
            calls.append(payload)
            cached = "0" if len(calls) == 1 else "40"       # only the very first call is cold
            return {"usage": {"input_tokens": 60}}, 0.1, {"x-systemone-cached-tokens": cached}, 0

        args = types.SimpleNamespace(data=str(data), target="ours", state_chars="100", questions="1,4",
                                     repeats=2, offset=0, model="jev-latest", timeout=5.0)
        out = io.StringIO()
        with mock.patch.object(bs, "post_systemone", fake_post), redirect_stdout(out):
            bs.cmd_fanout(args)
        rows = [ln.split("|")[1:4] for ln in out.getvalue().splitlines() if ln.startswith("| 100 |")]
        labels = {(q.strip(), run.strip()) for _, q, run in rows}
        self.assertIn(("1", "cold"), labels, out.getvalue())
        self.assertIn(("4", "first"), labels, out.getvalue())
        self.assertNotIn(("4", "cold"), labels, out.getvalue())


class ThePublishedMethod(unittest.TestCase):
    def test_the_log_loss_floor_benchmarks_md_states_is_the_one_the_code_uses(self):
        text = (REPO / "BENCHMARKS.md").read_text()
        floors = re.findall(r"log loss of the probability\s+given to the gold answer \(clipped at ([0-9.e-]+)", text)
        self.assertEqual(floors, [repr(bs.LOGLOSS_FLOOR)], "BENCHMARKS.md states another floor than LOGLOSS_FLOOR")


class TheGdprReport(unittest.TestCase):
    """It exists to say whether batching moves an answer, and for a choice it showed the
    top probability only, which does not say which option was chosen."""

    def rows(self, single_choice):
        def answer(key, q, mode):
            if q["type"] == "noul":
                return {"type": "noul", "noul": 0.9}
            if q["type"] == "choice":
                pick = single_choice if (mode == "single" and key == "instrument_type") else next(iter(q["criteria"]))
                return {"type": "choice", "choice": pick,
                        "probabilities": {k: (0.9 if k == pick else 0.1 / (len(q["criteria"]) - 1)) for k in q["criteria"]}}
            return {"type": "score", "score": 1, "legend": q["criteria"]}
        items = [{"id": f"g{i}", "mode": "batched" if i % 2 else "single"} for i in range(10)]
        rows = {it["id"]: {"id": it["id"], "model": "jev-1.13.0", "latency_s": 1.0, "usage": {"input_tokens": 100},
                           "answers": {k: answer(k, q, it["mode"]) for k, q in bs.GDPR_QUESTIONS.items()}}
                for it in items}
        return items, rows

    def test_a_choice_that_batching_moves_is_reported(self):
        items, rows = self.rows("Directive")
        rep = bs.report_gdpr(items, {"jev": rows})
        line = next(ln for ln in rep.splitlines() if ln.startswith("| instrument_type |"))
        self.assertIn("Directive", line, rep)
        self.assertIn("moved", rep)
        self.assertIn("1 of 13: instrument_type", rep)

    def test_nothing_moved_says_so(self):
        items, rows = self.rows("Regulation")
        self.assertIn("0 of 13", bs.report_gdpr(items, {"jev": rows}))

    def test_a_resume_on_another_model_stops_before_writing_its_answers(self):
        items = boolq_items(6)
        data = pathlib.Path(tempfile.mkdtemp(prefix="bench-so-"))
        self.addCleanup(shutil.rmtree, data, ignore_errors=True)
        (data / "tasks").mkdir()
        (data / "tasks" / "boolq.jsonl").write_text("".join(json.dumps(it) + "\n" for it in items))
        runs = data / "runs" / "boolq"
        runs.mkdir(parents=True)
        (runs / "ours.jsonl").write_text("".join(json.dumps(boolq_row(it, "qwen3.8-27b")) + "\n" for it in items[:2]))
        key = data / "key"
        key.write_text("k\n")
        sent = []
        lock = threading.Lock()

        def fake_post(target, payload, model, timeout):
            with lock:
                sent.append(payload)
            return ({"model": "qwen3.8-flash-next", "answers": {"answer": {"type": "noul", "noul": 0.9}},
                     "usage": {}}, 0.1, {}, 0)

        args = types.SimpleNamespace(data=str(data), task="boolq", limit=0, target="ours", concurrency=1,
                                     model="jev-latest", timeout=5.0)
        targets = {"ours": {"url": "http://127.0.0.1:9", "key": key, "concurrency": 1}}
        with mock.patch.object(bs, "post_systemone", fake_post), mock.patch.dict(bs.TARGETS, targets), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as stop:
                bs.cmd_run(args)
        self.assertIn("qwen3.8-flash-next", str(stop.exception.code))
        self.assertEqual(len(sent), 1, "it went on sending after the first answer named another model")
        done = bs.load_done(runs / "ours.jsonl")
        self.assertEqual({r["model"] for r in done.values()}, {"qwen3.8-27b"})
        self.assertEqual(len(done), 2)


if __name__ == "__main__":
    unittest.main()
