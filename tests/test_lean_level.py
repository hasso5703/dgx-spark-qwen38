#!/usr/bin/env python3
"""The lean level, end to end, offline.

Four pieces have to agree or the level exists in one place and not the next,
which is exactly how it first shipped: the generated opencode config offered
lean while the config opencode actually reads did not, so the level was
everywhere except where a user could select it.

  patch-template.py    puts the level in the template and makes it the default
  the rendered template injects the prompt for lean and nothing for medium
  oc-merge-limits.py   puts the level into an EXISTING opencode.json
  keepalive-proxy.py   relays a level SGLang's request model refuses

Qwen's own three levels must come out byte-identical. A repo that quietly
redefines `medium` makes every number anyone measures with it incomparable
with everyone else's, which is worse than not shipping the level at all.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, path))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


pt = _load("pt", "patch-template.py")

# The upstream block the patcher anchors on, reproduced from its own constants
# so this fixture cannot drift from what the script actually looks for.
STOCK = (
    "{%- set reasoning_instructions = '' %}\n"
    "{%- if enable_thinking is undefined or enable_thinking is true %}\n"
    + pt.EFFORT_ANCHOR + "\n"
    + "        {{- raise_exception('Unexpected reasoning effort ' ~ reasoning_effort ~ "
      "'. Supported types are xhigh (default), medium, and low.') }}\n"
    "    {%- endif %}\n"
    + pt.LEAN_ANCHOR
    + "        {%- set reasoning_instructions = 'Reasoning effort is set to xhigh. Please think "
      "carefully through the task, validate key assumptions, consider plausible alternatives, and "
      "prioritize correctness, consistency, and clarity in the final answer.' %}\n"
    "    {%- elif resolved_reasoning_effort == 'low' %}\n"
    "        {%- set reasoning_instructions = 'Reasoning effort is set to low. Keep your thinking "
      "brief and focused, moving directly to the conclusion without unnecessary elaboration.' %}\n"
    "    {%- endif %}\n"
    "{%- endif %}\n"
    "{%- if messages[0].role == 'system' %}\n"
    "{{- '<|im_start|>system\\n' + (reasoning_instructions + '\\n\\n' if reasoning_instructions else '') "
    "+ messages[0].content + '<|im_end|>\\n' }}\n"
    "{%- elif reasoning_instructions %}\n"
    "{{- '<|im_start|>system\\n' + reasoning_instructions + '<|im_end|>\\n' }}\n"
    "{%- endif %}\n"
    # SYSTEM_ANCHOR opens an if inside the message loop; the fixture closes both
    # so the result is a template jinja2 can actually compile and render.
    "{%- for message in messages %}\n"
    + pt.SYSTEM_ANCHOR + "\n"
    "    {%- endif %}\n"
    "{%- endfor %}\n"
)


def fixture(tmp, sha="52d1adc5f38aa5ebf099c29ed7025ba34cfbb854",
            repo="RadixArk/Qwen3.8-27B-NVFP4"):
    d = os.path.join(tmp, "hub", "models--" + repo.replace("/", "--"), "snapshots", sha)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "chat_template.jinja"), "w") as f:
        f.write(STOCK)
    return tmp


def patched(default_on=True):
    tmp = tempfile.mkdtemp()
    cache = fixture(tmp)
    out = os.path.join(tmp, "out.jinja")
    env = dict(os.environ)
    if not default_on:
        env["LEAN_DEFAULT"] = "0"
    r = subprocess.run([sys.executable, os.path.join(REPO, "patch-template.py"), cache, out],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    return open(out).read(), r.stdout


class TheTemplate(unittest.TestCase):
    def test_the_level_and_the_prompt_land_in_the_template(self):
        t, _ = patched()
        self.assertIn("resolved_reasoning_effort == 'lean'", t)
        self.assertIn(pt.LEAN_INSTRUCTIONS, t)

    def test_lean_becomes_the_default(self):
        t, out = patched()
        self.assertIn("reasoning_effort|default('lean')", t)
        self.assertIn("default reasoning effort: lean", out)

    def test_LEAN_DEFAULT_0_installs_the_level_without_taking_the_default(self):
        t, out = patched(default_on=False)
        self.assertIn("resolved_reasoning_effort == 'lean'", t)
        self.assertIn("reasoning_effort|default('xhigh')", t)
        self.assertIn("default reasoning effort: xhigh", out)

    def test_qwen_three_levels_come_out_byte_identical(self):
        t, _ = patched()
        for frag in ("Reasoning effort is set to xhigh. Please think carefully through the task, "
                     "validate key assumptions, consider plausible alternatives, and prioritize "
                     "correctness, consistency, and clarity in the final answer.",
                     "Reasoning effort is set to low. Keep your thinking brief and focused, moving "
                     "directly to the conclusion without unnecessary elaboration."):
            self.assertIn(frag, t)

    def test_the_refusal_message_names_what_is_supported(self):
        t, _ = patched()
        self.assertIn("lean (default), xhigh, medium, and low", t)
        self.assertNotIn("Supported types are xhigh (default), medium, and low", t)


class TheRender(unittest.TestCase):
    """What the model actually receives, per level."""
    @classmethod
    def setUpClass(cls):
        try:
            from jinja2 import Environment, BaseLoader          # noqa: F401
        except ImportError:
            raise unittest.SkipTest("jinja2 not installed")
        cls.tpl, _ = patched()

    def render(self, **kw):
        from jinja2 import Environment, BaseLoader
        from jinja2.exceptions import TemplateError
        env = Environment(loader=BaseLoader())
        env.globals["raise_exception"] = lambda m: (_ for _ in ()).throw(TemplateError(m))
        return env.from_string(self.tpl).render(
            messages=[{"role": "system", "content": "OPERATOR_SYSTEM"}], **kw)

    def test_lean_injects_the_prompt_and_keeps_the_operator_system(self):
        out = self.render(reasoning_effort="lean")
        self.assertIn("Answer immediately, with no reasoning", out)
        self.assertIn("OPERATOR_SYSTEM", out)
        self.assertIn("\n\n", out)                  # the paragraph break survives the escape

    def test_no_effort_at_all_renders_exactly_like_lean(self):
        self.assertEqual(self.render(), self.render(reasoning_effort="lean"))

    def test_medium_still_injects_nothing(self):
        out = self.render(reasoning_effort="medium")
        self.assertNotIn("Answer immediately", out)
        self.assertNotIn("Reasoning effort is set to", out)
        self.assertIn("OPERATOR_SYSTEM", out)

    def test_an_unknown_level_is_still_refused(self):
        with self.assertRaises(Exception):
            self.render(reasoning_effort="banana")


class TheOpencodeConfig(unittest.TestCase):
    """The level has to reach the file opencode reads, not just the one we write."""
    def cfg(self, variants):
        d = {"provider": {"qwen38": {"models": {"qwen3.8-27b": {
            "limit": {"context": 194048, "input": 194048, "output": 64000}}}}}}
        if variants is not None:
            d["provider"]["qwen38"]["models"]["qwen3.8-27b"]["variants"] = variants
        p = tempfile.mktemp(suffix=".json")
        open(p, "w").write(json.dumps(d, indent=2))
        return p

    def add(self, path):
        return subprocess.run([sys.executable, os.path.join(REPO, "oc-merge-limits.py"), path,
                               "qwen38", "qwen3.8-27b", "--add-variant", "lean"],
                              capture_output=True, text=True, timeout=60)

    def test_added_to_a_config_that_already_has_variants(self):
        p = self.cfg({"low": {"chat_template_kwargs": {"reasoning_effort": "low"}}})
        self.assertEqual(self.add(p).returncode, 0)
        v = json.load(open(p))["provider"]["qwen38"]["models"]["qwen3.8-27b"]["variants"]
        self.assertEqual(v["lean"], {"chat_template_kwargs": {"reasoning_effort": "lean"}})
        self.assertIn("low", v)                      # the operator's own entry survives

    def test_added_to_a_config_that_has_none(self):
        p = self.cfg(None)
        self.assertEqual(self.add(p).returncode, 0)
        m = json.load(open(p))["provider"]["qwen38"]["models"]["qwen3.8-27b"]
        self.assertEqual(m["variants"]["lean"]["chat_template_kwargs"]["reasoning_effort"], "lean")
        self.assertEqual(m["limit"]["context"], 194048)   # limits untouched

    def test_idempotent(self):
        p = self.cfg(None)
        self.add(p)
        r = self.add(p)
        self.assertEqual(r.returncode, 0)
        self.assertIn("unchanged", r.stdout)

    def test_a_model_that_is_not_there_is_reported_not_invented(self):
        p = self.cfg(None)
        r = subprocess.run([sys.executable, os.path.join(REPO, "oc-merge-limits.py"), p,
                            "nope", "nope", "--add-variant", "lean"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 3)


class TheProxyRelay(unittest.TestCase):
    """SGLang refuses any effort outside its own enum, before the template runs."""
    @classmethod
    def setUpClass(cls):
        cls.kp = _load("kp", "keepalive-proxy.py")

    def route(self, body, path="/v1/chat/completions"):
        return self.kp.route_reasoning_effort(body, path)

    def test_a_level_sglang_refuses_is_moved_into_chat_template_kwargs(self):
        out, moved = self.route(b'{"reasoning_effort":"lean"}')
        self.assertEqual(moved, "lean")
        self.assertEqual(json.loads(out),
                         {"chat_template_kwargs": {"reasoning_effort": "lean"}})

    def test_every_level_sglang_accepts_is_left_exactly_where_it_was(self):
        for lvl in self.kp.SGLANG_EFFORTS:
            body = json.dumps({"reasoning_effort": lvl}).encode()
            out, moved = self.route(body)
            self.assertIsNone(moved, lvl)
            self.assertEqual(out, body, lvl)

    def test_a_client_that_already_used_the_open_door_is_not_second_guessed(self):
        body = b'{"reasoning_effort":"lean","chat_template_kwargs":{"reasoning_effort":"low"}}'
        out, moved = self.route(body)
        self.assertIsNone(moved)
        self.assertEqual(out, body)

    def test_other_routes_and_malformed_bodies_pass_through(self):
        for body, path in ((b'{"reasoning_effort":"lean"}', "/v1/messages"),
                           (b'not json at all', "/v1/chat/completions"),
                           (b'{"reasoning_effort":123}', "/v1/chat/completions"),
                           (b'[]', "/v1/chat/completions"),
                           (b'', "/v1/chat/completions")):
            out, moved = self.route(body, path)
            self.assertIsNone(moved, (body, path))
            self.assertEqual(out, body, (body, path))

    def test_other_keys_in_the_body_survive_the_move(self):
        out, _ = self.route(b'{"model":"m","reasoning_effort":"lean","max_tokens":7}')
        d = json.loads(out)
        self.assertEqual(d["model"], "m")
        self.assertEqual(d["max_tokens"], 7)
        self.assertNotIn("reasoning_effort", d)


class TheOneCopyOfThePrompt(unittest.TestCase):
    def test_the_installer_offers_the_level_it_patched(self):
        s = open(os.path.join(REPO, "install.sh")).read()
        self.assertIn('for lvl in ("lean", "low", "medium", "xhigh")', s)
        self.assertIn("--add-variant lean", s)

    def test_the_prompt_text_lives_in_exactly_one_place(self):
        """The words are a measured artifact; two copies would drift."""
        text = pt.LEAN_INSTRUCTIONS
        for name in ("install.sh", "keepalive-proxy.py", "run.sh", "switch-model.sh"):
            self.assertNotIn(text[:60], open(os.path.join(REPO, name)).read(), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
