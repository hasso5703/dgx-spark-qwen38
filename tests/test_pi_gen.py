#!/usr/bin/env python3
"""pi-gen.py derives the pi and omp agent configs from the generated opencode.json.

pi and omp read no opencode.json (docs/pi.md): their default model, provider
names and limits are plain values in their own files, so the files this
generator writes are where those facts live. These check the derivation against
the artifact: key form, limits, the pi/omp value parity (one build, two
serializations), the omp thinking ladder taken from the artifact's variants,
the compaction sizing, and the quiet paths (no artifact, foreign providers).
The omp YAML is read back by a mini reader for the emitter's exact subset:
pi-gen.py is stdlib-only, this CI step installs no PyYAML, and neither side
of the parity check may import one. The spelling on disk is verified against
omp itself (docs/pi.md); what these tests pin is the pair's values.
"""
import json
import os
import pathlib
import subprocess
import tempfile
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
GEN = REPO / "pi-gen.py"

ARTIFACT = {
    "provider": {
        "qwen38": {
            "options": {"baseURL": "http://127.0.0.1:30001/v1", "apiKey": "{file:/keys/api-key}"},
            "models": {"qwen3.8-27b": {
                "name": "Qwen3.8-27B (local)",
                "limit": {"context": 173000, "input": 173000, "output": 64000},
                "variants": {"lean": {}, "low": {}, "medium": {}, "xhigh": {}},
                "modalities": {"input": ["text", "image"]}}},
        },
        "someone-elses": {"options": {"baseURL": "https://x.test/v1"}, "models": {"m": {}}},
    },
    "compaction": {"preserve_recent_tokens": 46000},
    "model": "qwen38/qwen3.8-27b",
}


def run(args):
    return subprocess.run([sys.executable, str(GEN)] + args, capture_output=True, text=True)


def yload(text):
    """Read what pi-gen.py's own writer emits: quoted strings, bare true/
    false/int, two-space indents, '- ' items. Nothing else, on purpose: the
    generator promises those shapes and the real omp parses them."""
    rows = [(len(l) - len(l.lstrip(" ")), l.strip()) for l in text.splitlines() if l.strip()]
    i = 0

    def sc(tok):
        if tok.startswith('"'):
            return json.loads(tok)
        if tok in ("true", "false"):
            return tok == "true"
        if tok in ("{}", "[]"):
            return {} if tok == "{}" else []
        return int(tok)

    def block(ind):
        nonlocal i
        if i < len(rows) and rows[i][1].startswith("-"):
            out = []
            while i < len(rows) and rows[i][0] == ind and rows[i][1].startswith("-"):
                body = rows[i][1][1:].strip()
                if body.startswith('"') or ":" not in body:
                    i += 1
                    out.append(sc(body))
                else:                       # '- id: x': a mapping item, first key inlined
                    rows[i] = (ind + 2, body)
                    out.append(block(ind + 2))
            return out
        out = {}
        while i < len(rows) and rows[i][0] == ind and not rows[i][1].startswith("-"):
            key, _, val = rows[i][1].partition(":")
            val = val.strip()
            i += 1
            if val:
                out[key] = sc(val)          # quoted strings keep any ':' inside
            else:
                out[key] = block(ind + 2) if i < len(rows) and rows[i][0] >= ind + 2 else {}
        return out

    return block(0)


class PiGen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.art = os.path.join(self.tmp.name, "opencode.json")
        with open(self.art, "w") as f:
            f.write(json.dumps(ARTIFACT))
        self.out = os.path.join(self.tmp.name, "out")

    def tearDown(self):
        self.tmp.cleanup()

    def gen(self, *extra):
        r = run(["--config", self.art, "--out", self.out] + list(extra))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def read_pi(self):
        with open(os.path.join(self.out, "pi", "models.json")) as f:
            models = json.loads(f.read())
        with open(os.path.join(self.out, "pi", "settings.json")) as f:
            settings = json.loads(f.read())
        return models, settings

    def read_omp(self):
        with open(os.path.join(self.out, "omp", "models.yml")) as f:
            models = yload(f.read())
        with open(os.path.join(self.out, "omp", "config.yml")) as f:
            cfg = yload(f.read())
        return models, cfg

    def test_generation(self):
        self.gen()
        models, settings = self.read_pi()
        prov = models["providers"]["qwen38"]
        self.assertNotIn("someone-elses", models["providers"])
        self.assertEqual(prov["apiKey"], "!cat /keys/api-key")
        entry = prov["models"][0]
        self.assertEqual((entry["contextWindow"], entry["maxTokens"]), (173000, 64000))
        self.assertEqual(entry["input"], ["text", "image"])
        self.assertEqual(settings["defaultProvider"], "qwen38")
        self.assertEqual(settings["defaultModel"], "qwen3.8-27b")
        self.assertEqual(settings["compaction"]["keepRecentTokens"], 46000)
        self.assertEqual(settings["compaction"]["modelOverrides"]["qwen38/qwen3.8-27b"], {"reserveTokens": 64000})
        self.assertEqual(settings["modelThinkingLevels"], {"qwen38/qwen3.8-27b": "off"})
        # its system prompt goes out as "system": as "developer" the served template refused
        # every request (400 "Unexpected message role.", pi 0.99.2, 2026-10-01)
        self.assertEqual(entry["compat"], {"supportsDeveloperRole": False})

    def test_omp_parity_and_thinking(self):
        self.gen()
        models, _ = self.read_pi()
        omp_models, omp_cfg = self.read_omp()
        pi_entry = dict(models["providers"]["qwen38"]["models"][0])
        omp_entry = dict(omp_models["providers"]["qwen38"]["models"][0])
        pi_entry.pop("inputLimits")
        pi_entry.pop("compat")
        thinking = omp_entry.pop("thinking")
        compat = omp_entry.pop("compat")
        self.assertEqual(omp_entry, pi_entry)            # one build, two serializations
        # the template's lean rides on minimal through reasoningEffortMap, and
        # qwenTemplateReasoningEffort is what routes the mapped pick onto
        # chat_template_kwargs.reasoning_effort. Without that flag the picker
        # moved and no effort field was sent (captured against omp 18.4.6,
        # 2026-10-01): it is the one field the ladder needs.
        self.assertEqual(thinking, {"mode": "effort", "efforts": ["minimal", "low", "medium", "xhigh"],
                                    "defaultLevel": "minimal"})
        # and rides chat_template_kwargs alone: omp's default dialect also sent a top-level
        # reasoning_effort, which SGLang refuses as "lean" (a 400 on every request at the
        # default level, omp 18.4.9, 2026-10-01)
        self.assertEqual(compat, {"thinkingFormat": "qwen-chat-template", "qwenTemplateReasoningEffort": True,
                                  "reasoningEffortMap": {"minimal": "lean"}})
        self.assertEqual(omp_cfg["modelRoles"]["default"], "qwen38/qwen3.8-27b")

    def test_idempotent(self):
        self.gen()
        again = self.gen()
        self.assertNotIn("wrote", again.stdout)

    def test_no_artifact_is_quiet_success(self):
        r = run(["--config", os.path.join(self.tmp.name, "none.json"), "--out", self.out])
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.out))

    def test_foreign_artifact_writes_nothing(self):
        art = os.path.join(self.tmp.name, "foreign.json")
        with open(art, "w") as f:
            f.write('{"provider": {"other": {}}, "model": "other/x"}')
        r = run(["--config", art, "--out", self.out])
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("wrote", r.stdout)

    def test_a_model_without_tiers_still_speaks_the_sglang_dialect(self):
        art = json.loads(json.dumps(ARTIFACT))
        del art["provider"]["qwen38"]["models"]["qwen3.8-27b"]["variants"]
        with open(self.art, "w") as f:
            f.write(json.dumps(art))
        self.gen()
        omp_models, _ = self.read_omp()
        entry = omp_models["providers"]["qwen38"]["models"][0]
        self.assertNotIn("thinking", entry)
        self.assertEqual(entry["compat"], {"thinkingFormat": "qwen-chat-template"})

    def test_a_key_path_with_a_space_is_quoted_for_the_shell(self):
        # both agents run the !command in a shell
        art = json.loads(json.dumps(ARTIFACT))
        art["provider"]["qwen38"]["options"]["apiKey"] = "{file:/home/my user/api-key}"
        with open(self.art, "w") as f:
            f.write(json.dumps(art))
        self.gen()
        models, _ = self.read_pi()
        self.assertEqual(models["providers"]["qwen38"]["apiKey"], "!cat '/home/my user/api-key'")
        with open(os.path.join(self.out, "omp", "models.yml")) as f:
            self.assertIn('apiKey: "!cat \'/home/my user/api-key\'"', f.read())

    def test_key_line_is_quoted(self):
        # A plain scalar may not start with '!': unquoted, the !cat key is a
        # YAML tag, not a string, and omp would misread the provider. The
        # reader above would shrug at that; the byte on disk cannot.
        self.gen()
        with open(os.path.join(self.out, "omp", "models.yml")) as f:
            self.assertIn('apiKey: "!cat /keys/api-key"', f.read())


if __name__ == "__main__":
    unittest.main()
