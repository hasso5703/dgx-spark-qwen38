#!/usr/bin/env python3
"""A System One answer names the model that produced it, never the caller's alias.

The answer takes the engine's model name, and fell back on the name the caller sent when
the engine gave none: `jev-latest`, which no lane on this box serves, although the name
the alias resolved to was known by then (found in review, 2026-09-24)."""
import importlib.util
import json
import pathlib
import unittest

HERE = pathlib.Path(__file__).resolve()
SPEC = importlib.util.spec_from_file_location("kproxy_so_name", HERE.parents[1] / "keepalive-proxy.py")
PROXY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROXY)


def answer(engine_name):
    req = PROXY.systemone_parse(json.dumps({"state": "s", "model": "jev-latest", "questions": {
        "q": {"type": "noul", "instructions": "i"}}}).encode())
    plan = PROXY.systemone_plan(req)
    reads = [([0.9] + [0.1] * (len(order) - 1), 1.0, 10, None, engine_name, 0)
             for (_, _, _, _, order) in plan["branches"]]
    _, _, out, _ = PROXY.systemone_response(req, plan, reads, "qwen3.8-27b")
    return json.loads(out)["model"]


class TheModelName(unittest.TestCase):
    def test_the_engines_own_name_first(self):
        self.assertEqual(answer("qwen3.8-flash-next"), "qwen3.8-flash-next")

    def test_without_it_the_resolved_name_not_the_alias(self):
        self.assertEqual(answer(None), "qwen3.8-27b")

    def test_the_handler_passes_the_resolved_name(self):
        text = (HERE.parents[1] / "keepalive-proxy.py").read_text()
        self.assertIn("systemone_response(req, plan, reads, model)", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
