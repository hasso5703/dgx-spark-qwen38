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
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "check-pins.sh"
OC_SHA = re.search(r'^OPENCODE_SHA256="\$\{OPENCODE_SHA256:-([0-9a-f]{64})\}"', (REPO / "install.sh").read_text(), re.M).group(1)

# Answers the way huggingface.co, Docker Hub, GitHub and PyPI do. The scenario (JSON in
# FAKE_PINS) maps a URL substring to [http code, x-error-code] for the requests that read a
# code, "no-registry-token" empties the token endpoint's answer, and "bodies" maps a URL
# substring to the body a JSON request gets (by default: the PyPI release asked for, not
# yanked; the opencode release with the digest in FAKE_OC_SHA). Every call is logged with
# its argv and whatever came in on stdin for -K -.
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
    if part not in ("no-registry-token", "bodies") and part in url:
        code, err = answer
if fmt is not None:
    sys.stdout.write(fmt.replace("%{http_code}", str(code)).replace("%header{x-error-code}", err))
    sys.exit(0)
for part, body in (scen.get("bodies") or {}).items():
    if part in url:
        sys.stdout.write(body); sys.exit(0)
if url.startswith("https://pypi.org/pypi/"):
    version = url.rstrip("/").split("/")[-2]
    sys.stdout.write(json.dumps({"info": {"version": version}, "urls": [{"yanked": False}]}))
elif "/releases/tags/" in url:
    sys.stdout.write(json.dumps({"assets": [{"name": "opencode-linux-arm64.tar.gz",
                                              "digest": "sha256:" + os.environ.get("FAKE_OC_SHA", "")}]}))
'''


class PinsBase(unittest.TestCase):
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
                   FAKE_LOG=str(self.log), FAKE_OC_SHA=OC_SHA)
        if token is not None:
            env["HF_TOKEN"] = token
        r = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True,
                           timeout=60)
        return r.returncode, r.stdout + r.stderr

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]


class TheCheckPins(PinsBase):
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
        self.assertEqual(len(hf), 10, hf)          # nine checkpoints and the image lane's
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
        images = out.split("Images", 1)[1].split("\n\n", 1)[0].strip().splitlines()
        self.assertTrue(images, out)
        self.assertTrue(all("FAIL" in ln for ln in images), out)

    def test_a_deleted_image_fails(self):
        rc, out = self.run_pins({"registry-1.docker.io/v2/lmsysorg/sglang/manifests/sha256:d6e7": [404, ""]})
        self.assertEqual(rc, 1, out)
        self.assertIn("HTTP 404", out)


class ThePinsOutsideThePinBlock(PinsBase):
    """opencode's release and digest, and the image lane's checkpoint, source commit and
    wheel, were checked by nothing: a release, commit or file removed upstream went unseen
    by the daily watch (found in review, 2026-09-24)."""

    def line(self, out, label):
        return next(ln for ln in out.splitlines() if f" {label} " in ln)

    def test_they_are_all_checked(self):
        rc, out = self.run_pins()
        self.assertEqual(rc, 0, out)
        for label in ("qwen-image", "sglang-source", "sglang-wheel", "opencode"):
            self.assertIn("ok", self.line(out, label), out)
        self.assertTrue(any("Qwen/Qwen-Image-2.1/raw/790c9263" in c["url"] and c["url"].endswith("/model_index.json")
                            for c in self.calls()), "the image checkpoint is asked for at its pinned revision")

    def test_a_removed_source_commit_fails(self):
        rc, out = self.run_pins({"api.github.com/repos/sgl-project/sglang/commits/": [422, ""]})
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL", self.line(out, "sglang-source"))

    def test_a_wheel_gone_or_yanked_fails(self):
        rc, out = self.run_pins({"bodies": {"pypi.org": '{"message": "Not Found"}'}})
        self.assertIn("FAIL", self.line(out, "sglang-wheel"), out)
        rc, out = self.run_pins({"bodies": {"pypi.org": json.dumps({"info": {"version": "0.5.20"},
                                                                     "urls": [{"yanked": True}]})}})
        self.assertIn("every file yanked", self.line(out, "sglang-wheel"), out)

    def test_an_opencode_asset_whose_bytes_changed_fails(self):
        body = json.dumps({"assets": [{"name": "opencode-linux-arm64.tar.gz", "digest": "sha256:" + "0" * 64}]})
        rc, out = self.run_pins({"bodies": {"/releases/tags/": body}})
        self.assertEqual(rc, 1, out)
        self.assertIn("installs refuse it", self.line(out, "opencode"))

    def test_an_opencode_release_that_is_gone_fails(self):
        rc, out = self.run_pins({"bodies": {"/releases/tags/": '{"message": "Not Found"}'}})
        self.assertIn("no such release or asset", self.line(out, "opencode"), out)


if __name__ == "__main__":
    unittest.main()
