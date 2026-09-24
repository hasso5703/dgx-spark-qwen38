#!/usr/bin/env python3
"""The opencode config is edited the way opencode reads it, and only where it has to be.

opencode parses its config with jsonc-parser and allowTrailingComma (read in the 1.18.32
binary): // and /* */ comments anywhere outside a string, and a comma before a closing
brace or bracket. oc-merge-limits.py understood whole-line // comments only, so any other
form made every edit refuse behind install.sh's `|| true`, and oc-point-default.py
rewrote the whole file with json.dump at every install and switch: non-ASCII came back as
escapes, a comment made it exit 3, a default model pointed at another provider was
replaced, and there was no backup. Backups named to the second kept one copy of five
edits made within that second, and not the original (found in review, 2026-09-24)."""
import importlib.util
import json
import os
import pathlib
import resource
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
MERGE = REPO / "oc-merge-limits.py"
POINT = REPO / "oc-point-default.py"
KEY = "/home/x/.config/qwen38/api-key"


def load_module():
    spec = importlib.util.spec_from_file_location("ocm_under_test", MERGE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def run(script, *args):
    r = subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True,
                       timeout=60)
    return r.returncode, r.stdout + r.stderr


def jsonc(text):
    """What opencode reads, by an independent route: strip what jsonc-parser allows."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            i = text.find("\n", i) if "\n" in text[i:] else n
        elif text.startswith("/*", i):
            i = text.index("*/", i) + 2
        else:
            out.append(c)
            i += 1
    s = "".join(out)
    import re
    while True:
        t = re.sub(r",(\s*[}\]])", r"\1", s)
        if t == s:
            return json.loads(s)
        s = t


# opencode accepts all of this: a comment at the end of a line, a block comment, a string
# holding // and /*, a comma before each closing brace, and a comment line between the
# last provider and the one before it.
CONFIG = """{
  "$schema": "https://opencode.ai/config.json", // opencode's schema
  /* the agents, in French */
  "agent": {"fr": {"prompt": "Réponds en français, voir http://x/*y*/ // pas un commentaire"},},
  "provider": {
    "anthropic": {"options": {"apiKey": "{env:ANTHROPIC_API_KEY}"},},
    // this box
    "qwen38": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {"baseURL": "http://127.0.0.1:30001/v1", "apiKey": "{file:%s}"}, // the key file
      "models": {"qwen3.8-27b": {"name": "old", "limit": {"context": 1, "input": 1, "output": 1,},},},
    },
  },
  "model": "qwen38/qwen3.8-27b",
}
""" % KEY

COMMENTS = ("// opencode's schema", "/* the agents, in French */", "// this box", "// the key file")


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="oc-jsonc-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = self.dir / "opencode.json"

    def write(self, text, newline="\n"):
        self.path.write_bytes(text.replace("\n", newline).encode())
        return self.path

    def text(self):
        return self.path.read_bytes().decode()

    def assertCommentsKept(self):
        for c in COMMENTS:
            self.assertIn(c, self.text())
        self.assertIn("Réponds en français", self.text(), "non-ASCII came back escaped")

    def backups(self):
        return sorted(p for p in self.dir.iterdir() if ".bak-" in p.name)


class TheMergesReadWhatOpencodeReads(Case):
    """O4: every form jsonc-parser accepts, in each edit."""

    def test_the_limits(self):
        self.write(CONFIG)
        rc, out = run(MERGE, self.path, "qwen38", "qwen3.8-27b", 173000, 64000)
        self.assertEqual(rc, 0, out)
        self.assertEqual(jsonc(self.text())["provider"]["qwen38"]["models"]["qwen3.8-27b"]["limit"],
                         {"context": 173000, "input": 173000, "output": 64000})
        self.assertCommentsKept()

    def test_the_compaction_autoupdate_and_variant(self):
        self.write(CONFIG)
        for args in (("--compaction", 170000), ("--autoupdate", "notify"),
                     ("qwen38", "qwen3.8-27b", "--add-variant", "lean")):
            rc, out = run(MERGE, self.path, *args)
            self.assertEqual(rc, 0, f"{args}: {out}")
        doc = jsonc(self.text())
        self.assertEqual(doc["compaction"], {"preserve_recent_tokens": 170000, "prune": True})
        self.assertEqual(doc["autoupdate"], "notify")
        self.assertEqual(doc["provider"]["qwen38"]["models"]["qwen3.8-27b"]["variants"]["lean"],
                         {"chat_template_kwargs": {"reasoning_effort": "lean"}})
        self.assertCommentsKept()

    def test_a_provider_added(self):
        self.write(CONFIG)
        gen = self.dir / "generated.json"
        flash = {"options": {"apiKey": "{file:%s}" % KEY}, "models": {"qwen3.8-flash-next": {}}}
        gen.write_text(json.dumps({"provider": {"flashnext": flash}}))
        before = jsonc(self.text())
        rc, out = run(MERGE, self.path, "--add-providers", gen)
        self.assertEqual(rc, 0, out)
        after = jsonc(self.text())
        self.assertEqual(after["provider"].pop("flashnext"), flash)
        self.assertEqual(after, before)
        self.assertCommentsKept()

    def test_the_providers_removed_at_uninstall(self):
        """O5 too: the box's provider is the last one, under a comment line; left there,
        it reads a key file uninstall.sh deletes, and opencode no longer starts."""
        self.write(CONFIG)
        want = jsonc(self.text())
        del want["provider"]["qwen38"], want["model"]
        rc, out = run(MERGE, self.path, "--remove-providers", KEY)
        self.assertEqual(rc, 0, out)
        self.assertEqual(jsonc(self.text()), want)
        self.assertIn("Réponds en français", self.text())

    def test_the_last_provider_under_a_whole_line_comment(self):
        """O5 in plain JSON plus one // line, which the old reader did understand."""
        self.write('{\n  "provider": {\n    "anthropic": {"options": {}},\n    // this box\n'
                   '    "qwen38": {"options": {"apiKey": "{file:%s}"}}\n  },\n'
                   '  "model": "anthropic/claude-sonnet-5"\n}\n' % KEY)
        rc, out = run(MERGE, self.path, "--remove-providers", KEY)
        self.assertEqual(rc, 0, out)
        self.assertEqual(jsonc(self.text()), {"provider": {"anthropic": {"options": {}}},
                                              "model": "anthropic/claude-sonnet-5"})

    def test_an_unterminated_block_comment_is_refused_untouched(self):
        body = '{"provider": {"qwen38": {"models": {"qwen3.8-27b": {"limit": {}}}}} /* open'
        self.write(body)
        rc, out = run(MERGE, self.path, "qwen38", "qwen3.8-27b", 5, 5)
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.text(), body)
        self.assertEqual(self.backups(), [])


class TheEditLandsWhereTheKeyIs(Case):
    """O7 and O8: the member edited is the one at the path, not the first text match."""

    def test_a_commented_block_and_a_nested_key_first(self):
        self.write('{\n  // "compaction": {"preserve_recent_tokens": 1},\n'
                   '  "agent": {"a": {"compaction": {"preserve_recent_tokens": 2}}},\n'
                   '  /* "provider": {"qwen38": {"models": {"qwen3.8-27b": {"limit": {"context": 9}}}}}, */\n'
                   '  "compaction": {"preserve_recent_tokens": 3, "prune": false},\n'
                   '  "provider": {"qwen38": {"models": {"qwen3.8-27b": {"limit": {"context": 4}}}}}\n}\n')
        self.assertEqual(run(MERGE, self.path, "--compaction", 50000)[0], 0)
        rc, out = run(MERGE, self.path, "qwen38", "qwen3.8-27b", 7, 8)
        self.assertEqual(rc, 0, out)
        doc = jsonc(self.text())
        self.assertEqual(doc["agent"]["a"]["compaction"], {"preserve_recent_tokens": 2})
        self.assertEqual(doc["compaction"], {"preserve_recent_tokens": 50000, "prune": True})
        self.assertEqual(doc["provider"]["qwen38"]["models"]["qwen3.8-27b"]["limit"],
                         {"context": 7, "input": 7, "output": 8})
        self.assertIn('// "compaction": {"preserve_recent_tokens": 1}', self.text())

    def test_an_empty_variants_object_gets_the_level(self):
        self.write('{"provider": {"qwen38": {"models": {"qwen3.8-27b": {"variants": {}}}}}}\n')
        rc, out = run(MERGE, self.path, "qwen38", "qwen3.8-27b", "--add-variant", "lean")
        self.assertEqual(rc, 0, out)
        self.assertIn("lean", jsonc(self.text())["provider"]["qwen38"]["models"]["qwen3.8-27b"]["variants"])


class TheFileIsKept(Case):
    """O6 and O9, and a write that cannot cut the file."""

    def test_five_edits_in_one_second_keep_five_backups(self):
        m = load_module()
        self.write(CONFIG)
        original = self.text()
        with mock.patch.object(m.time, "strftime", return_value="20260924-120000"):
            for argv in ((self.path, "--autoupdate", "notify"),
                         (self.path, "qwen38", "qwen3.8-27b", "173000", "64000"),
                         (self.path, "--compaction", "170000"),
                         (self.path, "qwen38", "qwen3.8-27b", "--add-variant", "lean")):
                self.assertEqual(m.main(["oc-merge-limits.py", *map(str, argv)]), 0)
        names = [p.name for p in self.backups()]
        self.assertEqual(len(names), 4, names)
        self.assertEqual(self.backups()[0].read_bytes().decode(), original,
                         "the first backup is not the original")

    def test_crlf_stays_crlf(self):
        self.write(CONFIG, newline="\r\n")
        rc, out = run(MERGE, self.path, "qwen38", "qwen3.8-27b", "--add-variant", "lean")
        self.assertEqual(rc, 0, out)
        raw = self.path.read_bytes()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), "a line ending was changed")

    def test_a_symlink_stays_a_symlink(self):
        """A guard on the one-step write, which must not replace a link by a file."""
        real = self.dir / "dotfiles.json"
        real.write_text('{"provider": {"qwen38": {"models": {"qwen3.8-27b": {"limit": {}}}}}}\n')
        os.chmod(real, 0o640)
        self.path.symlink_to(real)
        rc, out = run(MERGE, self.path, "qwen38", "qwen3.8-27b", 173000, 64000)
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(jsonc(real.read_text())["provider"]["qwen38"]["models"]["qwen3.8-27b"]["limit"]["output"],
                         64000)
        self.assertEqual(os.stat(real).st_mode & 0o777, 0o640)

    def test_a_write_cut_short_leaves_the_file_whole(self):
        """A full disk mid-write: the file was truncated, then written in place. The process's
        file size limit sits between the original and the edit, so the backup lands and the
        edit does not, in either implementation."""
        body = ('{\n  "provider": {"qwen38": {"models": {"qwen3.8-27b": '
                '{"limit": {"context": 1, "input": 1, "output": 1}}}}}\n}\n')
        self.write(body)
        limit = len(body.encode()) + 4

        def small_disk():
            resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
        r = subprocess.run([sys.executable, str(MERGE), str(self.path), "qwen38", "qwen3.8-27b", "173000",
                            "64000"], capture_output=True, text=True, timeout=60, preexec_fn=small_disk)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.text(), body, "the config was left cut")
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.endswith(".tmp")], [])
        self.assertNotIn("Traceback", r.stderr)


class TheDefaultModelFollowsOnlyWhatIsTheBoxs(Case):
    """O3: oc-point-default.py, run on the user's config at every install and switch."""

    def point(self, lane="27b", choice="stock", window="262144"):
        return run(POINT, self.path, lane, choice, window)

    def test_a_config_with_comments_follows_the_lane(self):
        self.write(CONFIG.replace('"model": "qwen38/qwen3.8-27b"', '"model": "flashnext/qwen3.8-flash-next"')
                   .replace('"qwen38": {\n      "npm"', '"flashnext": {"models": {"qwen3.8-flash-next": {}}},\n'
                            '    "qwen38": {\n      "npm"'))
        rc, out = self.point()
        self.assertEqual(rc, 0, out)
        doc = jsonc(self.text())
        self.assertEqual(doc["model"], "qwen38/qwen3.8-27b")
        self.assertEqual(doc["provider"]["qwen38"]["models"]["qwen3.8-27b"]["name"],
                         "Qwen3.8-27B NVFP4 + DFlash2 (local, 262K)")
        self.assertCommentsKept()
        self.assertEqual(len(self.backups()), 1, "no backup before the edit")

    def test_only_the_three_members_change(self):
        self.write(CONFIG)
        before = jsonc(self.text())
        rc, out = self.point(window="1010000")
        self.assertEqual(rc, 0, out)
        after = jsonc(self.text())
        self.assertEqual(after.pop("small_model"), "qwen38/qwen3.8-27b")
        after["provider"]["qwen38"]["models"]["qwen3.8-27b"]["name"] = "old"
        self.assertEqual(after, before)
        self.assertCommentsKept()

    def test_a_default_pointed_elsewhere_is_kept(self):
        body = CONFIG.replace('"model": "qwen38/qwen3.8-27b"', '"model": "anthropic/claude-sonnet-5"')
        self.write(body)
        rc, out = self.point()
        self.assertEqual(rc, 0, out)
        doc = jsonc(self.text())
        self.assertEqual(doc["model"], "anthropic/claude-sonnet-5")
        self.assertNotIn("small_model", doc, "an unset small_model under a kept default was set")
        self.assertIn("left as it is", out)

    def test_a_small_model_pointed_elsewhere_is_kept(self):
        self.write(CONFIG.replace('"model": "qwen38/qwen3.8-27b",',
                                  '"model": "flashnext/qwen3.8-flash-next", "small_model": "anthropic/claude-haiku",'))
        rc, out = self.point()
        self.assertEqual(rc, 0, out)
        doc = jsonc(self.text())
        self.assertEqual((doc["model"], doc["small_model"]), ("qwen38/qwen3.8-27b", "anthropic/claude-haiku"))

    def test_nothing_to_change_writes_nothing(self):
        self.write(CONFIG)
        self.assertEqual(self.point()[0], 0)
        once, n = self.text(), len(self.backups())
        rc, out = self.point()
        self.assertEqual(rc, 0, out)
        self.assertIn("unchanged", out)
        self.assertEqual((self.text(), len(self.backups())), (once, n))


class TheGeneratedConfigIsWrittenOnce(unittest.TestCase):
    """install.sh's generator gives the served entry the name oc-point-default.py applies
    right after it, so a run that changes nothing writes the file neither time: it used to
    write a generic name that the next call renamed, twice at every run."""

    def test_generator_then_point_default_is_a_fixed_point(self):
        install = (REPO / "install.sh").read_text()
        self.assertIn('OC_SERVED_NAME="$(python3 "$REPO_DIR/oc-point-default.py" --label "$MODEL_CHOICE" '
                      '"$OC_WINDOW")"', install)
        self.assertIn('OC_SERVED_NAME="$OC_SERVED_NAME"', install)
        start = install.index("OC_CONFIG_DIR=\"$CONFIG_DIR\" python3 - <<'PYEOF'")
        body = install[install.index("\n", start) + 1:install.index("\nPYEOF\n", start)]
        d = pathlib.Path(tempfile.mkdtemp(prefix="oc-gen-"))
        self.addCleanup(shutil.rmtree, d, True)
        name = subprocess.run([sys.executable, str(POINT), "--label", "stock", "1010000"],
                              capture_output=True, text=True, check=True).stdout.strip()
        env = dict(os.environ, OC_LANE="27b", OC_27B="1", OC_FLASH="1", OC_PORT="30001", OC_CTX="700000",
                   OC_OUT="200000", OC_LABEL="local, 1M", OC_CONTEXT_MODE="1m", OC_27B_CTX="700000",
                   OC_27B_OUT="200000", OC_KEEP="170000", OC_PIN="1", OC_CONFIG_DIR=str(d),
                   OC_SERVED_NAME=name)
        for turn in (1, 2):
            r = subprocess.run([sys.executable, "-c", body], capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            if turn == 2:
                self.assertIn("kept", r.stdout, "the second run rewrote the generated config")
            rc, out = run(POINT, d / "opencode.json", "27b", "stock", "1010000")
            self.assertEqual(rc, 0, out)
            self.assertIn("unchanged", out, f"run {turn}: the generator's name was not the served one")
        self.assertEqual([p.name for p in d.iterdir() if ".bak-" in p.name], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
