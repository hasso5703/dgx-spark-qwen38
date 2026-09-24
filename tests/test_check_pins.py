#!/usr/bin/env python3
"""check-pins.sh tells a dead pin from a gated one, and never counts an unasked pin as resolved.

Hugging Face answers an anonymous request with 401 both for a gated repo and for a repo
that does not exist or is private (checked against huggingface.co on 2026-09-24: a gated
repo carries `x-error-code: GatedRepo`, a missing one only "Invalid username or password."),
and the script filed every 401 or 403 under "gated", which it never counted as a failure:
a deleted pinned repo kept the daily pin watch green. Its advice, "set HF_TOKEN", could not
help either, since its curl sent no token. And an image whose registry token could not be
fetched was printed "skip" and still counted in "N pins checked, all resolve", exit 0.
These run the script as written against a fake curl that answers like the real services.
"""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "check-pins.sh"

# Answers the way huggingface.co and Docker Hub do. The scenario (JSON in FAKE_PINS) maps a
# URL substring to [http code, x-error-code] for checkpoint and manifest requests, and
# "no-registry-token" empties the token endpoint's answer. Every call is logged with its
# argv and whatever came in on stdin for -K -.
FAKE_CURL = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
scen = json.loads(os.environ.get("FAKE_PINS", "{}"))
url = next((a for a in args if a.startswith("http")), "")
fmt = args[args.index("-w") + 1] if "-w" in args else None
stdin = ""
if "-K" in args and args[args.index("-K") + 1] == "-":
    stdin = sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"argv": args, "stdin": stdin, "url": url}) + "\n")
if url.startswith("https://auth.docker.io/"):
    if not scen.get("no-registry-token"):
        sys.stdout.write('{"token":"regtoken","expires_in":300}')
    sys.exit(0)
code, err = 200, ""
for part, answer in scen.items():
    if part != "no-registry-token" and part in url:
        code, err = answer
if fmt is not None:
    sys.stdout.write(fmt.replace("%{http_code}", str(code)).replace("%header{x-error-code}", err))
'''


class TheCheckPins(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="check-pins-"))
        self.addCleanup(shutil.rmtree, self.t, ignore_errors=True)
        self.bin = self.t / "fakebin"
        self.bin.mkdir()
        (self.bin / "curl").write_text(FAKE_CURL)
        (self.bin / "curl").chmod(0o755)
        self.log = self.t / "curl.log"

    def run_pins(self, scenario=None, token=None, *args):
        env = {k: v for k, v in os.environ.items() if k != "HF_TOKEN"}
        env.update(PATH=f"{self.bin}:{env['PATH']}", FAKE_PINS=json.dumps(scenario or {}),
                   FAKE_LOG=str(self.log))
        if token is not None:
            env["HF_TOKEN"] = token
        r = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True,
                           timeout=60)
        return r.returncode, r.stdout + r.stderr

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_every_pin_answered_resolves(self):
        rc, out = self.run_pins()
        self.assertEqual(rc, 0, out)
        self.assertIn("all resolve", out)
        self.assertNotIn("FAIL", out)

    def test_a_deleted_repo_is_a_failure_not_gated(self):
        # what huggingface.co answers an anonymous caller for a repo that is gone
        rc, out = self.run_pins({"RadixArk/Qwen3.8-27B-NVFP4/": [401, ""]})
        self.assertEqual(rc, 1, out)
        line = next(ln for ln in out.splitlines() if "RadixArk/Qwen3.8-27B-NVFP4 " in ln)
        self.assertIn("FAIL", line)
        self.assertNotIn("all resolve", out)

    def test_a_gated_repo_is_named_and_fails_the_fresh_install_check(self):
        rc, out = self.run_pins({"RadixArk/Qwen3.8-27B-NVFP4/": [401, "GatedRepo"]})
        self.assertEqual(rc, 1, out)
        line = next(ln for ln in out.splitlines() if "RadixArk/Qwen3.8-27B-NVFP4 " in ln)
        self.assertIn("FAIL", line)
        self.assertIn("gated", line)

    def test_a_removed_revision_still_fails(self):
        rc, out = self.run_pins({"RadixArk/Qwen3.8-27B-NVFP4/": [404, "EntryNotFound"]})
        self.assertEqual(rc, 1, out)
        self.assertIn("HTTP 404", out)

    def test_hf_token_is_sent_and_not_on_the_command_line(self):
        rc, out = self.run_pins(None, "hf_secretvalue123")
        self.assertEqual(rc, 0, out)
        hf = [c for c in self.calls() if c["url"].startswith("https://huggingface.co/")]
        self.assertEqual(len(hf), 9, hf)
        for c in hf:
            self.assertIn("Authorization: Bearer hf_secretvalue123", c["stdin"], c)
            self.assertFalse(any("hf_secretvalue123" in a for a in c["argv"]), c["argv"])

    def test_no_token_sends_no_authorization(self):
        self.run_pins()
        for c in self.calls():
            self.assertNotIn("Authorization", c["stdin"] + " ".join(c["argv"]).replace(
                "Authorization: Bearer regtoken", ""), c)

    def test_an_image_that_could_not_be_asked_about_is_not_resolved(self):
        rc, out = self.run_pins({"no-registry-token": True}, None, "base")
        self.assertEqual(rc, 1, out)
        self.assertNotIn("all resolve", out)
        self.assertEqual(sum("FAIL" in ln for ln in out.splitlines()), 3, out)

    def test_a_deleted_image_fails(self):
        rc, out = self.run_pins({"registry-1.docker.io/v2/lmsysorg/sglang/manifests/sha256:d6e7": [404, ""]})
        self.assertEqual(rc, 1, out)
        self.assertIn("HTTP 404", out)


if __name__ == "__main__":
    unittest.main()
