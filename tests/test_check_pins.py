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
# FAKE_PINS) maps a URL substring to [http code, x-error-code, x-ratelimit-remaining] (the
# last one optional; "000" is curl's code for no answer), "no-registry-token" empties the
# token endpoint's answer, and "bodies" maps a URL substring to the body the request gets (by
# default: the PyPI release asked for, not yanked; the opencode release with the digest in
# FAKE_OC_SHA). Like curl, it writes the body unless -o sends it elsewhere, then what -w asks
# for. Every call is logged with its argv and whatever came in on stdin for -K -.
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
code, err, left = 200, "", "59"
for part, answer in scen.items():
    if part not in ("no-registry-token", "bodies") and part in url:
        code, err = answer[0], answer[1]
        left = answer[2] if len(answer) > 2 else left
body = next((b for part, b in (scen.get("bodies") or {}).items() if part in url), None)
if body is None and url.startswith("https://pypi.org/pypi/"):
    version = url.rstrip("/").split("/")[-2]
    body = json.dumps({"info": {"version": version}, "urls": [{"yanked": False}]})
elif body is None and "/releases/tags/" in url:
    body = json.dumps({"assets": [{"name": "opencode-linux-arm64.tar.gz",
                                   "digest": "sha256:" + os.environ.get("FAKE_OC_SHA", "")}]})
if "-o" not in args:
    sys.stdout.write(body or "")
if fmt is not None:
    sys.stdout.write(fmt.replace("\\n", "\n").replace("%{http_code}", str(code)).replace("%header{x-error-code}", err)
                     .replace("%header{x-ratelimit-remaining}", str(left)))
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
        self.assertEqual(len(hf), 13, hf)          # nine checkpoints, the diffusion lanes' three and the video Turbo adapter
        for c in hf:
            self.assertIn("Authorization: Bearer hf_secretvalue123", c["stdin"], c)
            self.assertFalse(any("hf_secretvalue123" in a for a in c["argv"]), c["argv"])

    def test_github_token_is_sent_and_not_on_the_command_line(self):
        """Issue #34: an anonymous caller gets 60 requests an hour per address, a runner's
        address is shared, and both tries of 2026-10-02 got 403 with the limit spent."""
        env_tok = "ghs_secretvalue456"
        env = {k: v for k, v in os.environ.items() if k not in ("HF_TOKEN", "GITHUB_TOKEN")}
        env.update(PATH=f"{self.bin}:{env['PATH']}", FAKE_PINS="{}", FAKE_LOG=str(self.log), FAKE_OC_SHA=OC_SHA,
                   GITHUB_TOKEN=env_tok)
        r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        gh = [c for c in self.calls() if c["url"].startswith("https://api.github.com/")]
        self.assertEqual(len(gh), 3, gh)          # the two source commits and the opencode release
        for c in gh:
            self.assertIn(f"Authorization: Bearer {env_tok}", c["stdin"], c)
            self.assertFalse(any(env_tok in a for a in c["argv"]), c["argv"])
        others = [c for c in self.calls() if not c["url"].startswith("https://api.github.com/")]
        self.assertFalse(any(env_tok in c["stdin"] + " ".join(c["argv"]) for c in others),
                         "the GitHub token went somewhere else than GitHub")

    def test_the_pin_watch_hands_the_check_its_token(self):
        wf = (REPO / ".github/workflows/pin-watch.yml").read_text()
        step = wf.split("- id: pins", 1)[1].split("run: |", 1)[0]
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", step)

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
        rc, out = self.run_pins({"registry-1.docker.io/v2/lmsysorg/sglang/manifests/sha256:b125": [404, ""]})
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
        for label in ("qwen-image", "qwen-image-turbo", "sglang-source", "sglang-wheel", "opencode"):
            self.assertIn("ok", self.line(out, label), out)
        self.assertTrue(any("Qwen/Qwen-Image-2.1/raw/790c9263" in c["url"] and c["url"].endswith("/model_index.json")
                            for c in self.calls()), "the image checkpoint is asked for at its pinned revision")
        self.assertTrue(any("Qwen/Qwen-Image-2.1-Turbo/raw/d65dbc9a" in c["url"] and c["url"].endswith("/model_index.json")
                            for c in self.calls()), "the Turbo checkpoint is asked for at its pinned revision")

    def test_a_removed_source_commit_fails(self):
        rc, out = self.run_pins({"api.github.com/repos/sgl-project/sglang/commits/": [422, ""]})
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL", self.line(out, "sglang-source"))

    def test_a_wheel_gone_or_yanked_fails(self):
        rc, out = self.run_pins({"bodies": {"pypi.org": '{"message": "Not Found"}'}})
        self.assertIn("FAIL", self.line(out, "sglang-wheel"), out)
        rc, out = self.run_pins({"bodies": {"pypi.org": json.dumps({"info": {"version": "0.5.21"},
                                                                     "urls": [{"yanked": True}]})}})
        self.assertIn("every file yanked", self.line(out, "sglang-wheel"), out)

    def test_an_opencode_asset_whose_bytes_changed_fails(self):
        body = json.dumps({"assets": [{"name": "opencode-linux-arm64.tar.gz", "digest": "sha256:" + "0" * 64}]})
        rc, out = self.run_pins({"bodies": {"/releases/tags/": body}})
        self.assertEqual(rc, 1, out)
        self.assertIn("installs refuse it", self.line(out, "opencode"))

    def test_an_opencode_release_that_is_gone_fails(self):
        rc, out = self.run_pins({"/releases/tags/": [404, ""], "bodies": {"/releases/tags/": '{"message": "Not Found"}'}})
        self.assertEqual(rc, 1, out)
        self.assertIn("HTTP 404 from the GitHub API: no such release", self.line(out, "opencode"), out)
        self.assertIn("A removed upstream revision", out)

    def test_an_opencode_release_without_its_asset_fails(self):
        rc, out = self.run_pins({"bodies": {"/releases/tags/": '{"assets": [{"name": "opencode-darwin-arm64.zip"}]}'}})
        self.assertEqual(rc, 1, out)
        self.assertIn("the release has no such asset", self.line(out, "opencode"), out)


class TheEndpointsThatDoNotAnswer(PinsBase):
    """Issue #33: the scheduled run of 2026-10-01 13:08 got 504s from the GitHub API for the
    two source commits and the opencode release, said the release was "no such release or
    asset, or no answer", and filed an issue; every pin resolved on the next run. A pin that
    could not be asked about is still not resolved (exit 1), and is said to be that."""

    def line(self, out, label):
        return next(ln for ln in out.splitlines() if f" {label} " in ln)

    GITHUB = ("sglang-source", "sglang-source-video", "opencode")

    def test_a_github_api_that_answers_504_is_not_a_dead_pin(self):
        rc, out = self.run_pins({"api.github.com": [504, ""], "bodies": {"api.github.com": "<html>504 Gateway Time-out</html>"}})
        self.assertEqual(rc, 1, out)
        for label in self.GITHUB:
            self.assertIn("HTTP 504 from the GitHub API, so not checked", self.line(out, label), out)
            self.assertNotIn("no such", self.line(out, label), out)
        self.assertIn("3 of 20 pins could not be asked about", out)
        self.assertIn("run it again before re-pinning anything", out)
        self.assertNotIn("A removed upstream revision", out)

    def test_no_answer_at_all_is_said_so(self):
        rc, out = self.run_pins({"api.github.com": ["000", ""]})
        self.assertEqual(rc, 1, out)
        for label in self.GITHUB:
            self.assertIn("no answer from the GitHub API, so not checked", self.line(out, label), out)

    def test_a_rate_limit_is_no_answer_and_a_plain_403_is_one(self):
        rc, out = self.run_pins({"api.github.com/repos/sgl-project": [403, "", "0"]})
        self.assertIn("HTTP 403 from the GitHub API, so not checked", self.line(out, "sglang-source"), out)
        rc, out = self.run_pins({"api.github.com/repos/sgl-project": [403, "", "41"]})
        self.assertIn("HTTP 403 from the GitHub API: no such commit", self.line(out, "sglang-source"), out)
        self.assertIn("A removed upstream revision", out)

    def test_hugging_face_pypi_and_the_registry_out_of_reach_are_not_asked_either(self):
        for scen, label, said in (({"huggingface.co/RadixArk/Qwen3.8-27B-NVFP4/": [503, ""]}, "stock", "HTTP 503 from Hugging Face, so not checked"),
                                  ({"huggingface.co/RadixArk/Qwen3.8-27B-NVFP4/": ["000", ""]}, "stock", "no answer from Hugging Face, so not checked"),
                                  ({"bodies": {"pypi.org": "upstream connect error"}}, "sglang-wheel", "no answer from PyPI, so not checked"),
                                  ({"registry-1.docker.io/v2/lmsysorg/sglang/manifests/sha256:b125": [502, ""]}, "27b-base", "HTTP 502 from the registry, so not checked")):
            with self.subTest(label=label, said=said):
                rc, out = self.run_pins(scen)
                self.assertEqual(rc, 1, out)
                self.assertIn(said, self.line(out, label), out)
                self.assertIn("could not be asked about", out)

    def test_one_dead_pin_among_unanswered_ones_is_still_called_dead(self):
        rc, out = self.run_pins({"api.github.com": [504, ""], "RadixArk/Qwen3.8-27B-NVFP4/": [404, "EntryNotFound"]})
        self.assertEqual(rc, 1, out)
        self.assertIn("Some pins do not resolve.", out)
        self.assertNotIn("could not be asked about", out)


class ThePinWatch(unittest.TestCase):
    """.github/workflows/pin-watch.yml asks a second time, ten minutes later, before it files
    an issue: issue #33 was filed on one answer of an API having a bad minute. Its step runs
    here as written, with a check-pins.sh that fails the first N times and a sleep that only
    records what it was asked."""

    def step(self, fails):
        import textwrap
        wf = (REPO / ".github/workflows/pin-watch.yml").read_text()
        block = wf.split("- id: pins", 1)[1].split("run: |", 1)[1]
        lines = []
        for ln in block.splitlines()[1:]:
            if ln.strip() and not ln.startswith(" " * 10):
                break
            lines.append(ln)
        script = textwrap.dedent("\n".join(lines))
        t = pathlib.Path(tempfile.mkdtemp(prefix="pin-watch-"))
        self.addCleanup(shutil.rmtree, t, ignore_errors=True)
        (t / "bin").mkdir()
        (t / "bin/sleep").write_text('#!/bin/sh\necho "$1" >> "$SLEPT"\n')
        (t / "bin/sleep").chmod(0o755)
        (t / "check-pins.sh").write_text(
            "#!/bin/bash\nn=$(cat count 2>/dev/null || echo 0); echo $((n + 1)) > count\n"
            f'if [ "$n" -lt {fails} ]; then echo "  FAIL  opencode (HTTP 504 from the GitHub API, so not checked)"; exit 1; fi\n'
            'echo "18 pins checked, all resolve."\n')
        (t / "check-pins.sh").chmod(0o755)
        env = dict(os.environ, PATH=f"{t / 'bin'}:{os.environ['PATH']}", GITHUB_OUTPUT=str(t / "out"),
                   SLEPT=str(t / "slept"))
        r = subprocess.run(["bash", "-e", "-c", script], cwd=t, env=env, capture_output=True, text=True, timeout=60)
        out = (t / "out").read_text() if (t / "out").exists() else ""
        slept = (t / "slept").read_text().split() if (t / "slept").exists() else []
        return r, out, slept, (t / "count").read_text().strip()

    def test_a_pin_that_passes_on_the_second_ask_files_nothing(self):
        r, out, slept, asked = self.step(fails=1)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(out.strip(), "rc=0")
        self.assertEqual((slept, asked), (["600"], "2"))

    def test_a_pin_that_fails_twice_is_reported(self):
        r, out, slept, asked = self.step(fails=2)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(out.strip(), "rc=1")
        self.assertEqual((slept, asked), (["600"], "2"))

    def test_a_run_that_passes_asks_once(self):
        r, out, slept, asked = self.step(fails=0)
        self.assertEqual(out.strip(), "rc=0")
        self.assertEqual((slept, asked), ([], "1"))


if __name__ == "__main__":
    unittest.main()
