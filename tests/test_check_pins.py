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
SPEECH = (REPO / "install-speech.sh").read_text()


def pin(text, name):
    return re.search(rf'^\s*{name}="([^"]*)"', text, re.M).group(1)


LLAMA_PIN = re.search(r'^LLAMA_CPP_PIN="\$\{SPEECH_LLAMA_CPP_PIN:-([0-9a-f]{40})\}"', SPEECH, re.M).group(1)
# each recognizer size install-speech.sh offers: its GGUF repo, revision, model and projector
ASR = re.findall(r'ASR_REPO="([^"]+)"; ASR_REV="([0-9a-f]{40})"\n\s+ASR_FILE="([^"]+)";[^\n]*\n\s+MMPROJ_FILE="([^"]+)"', SPEECH)
KOKORO_URL = pin(SPEECH, "KOKORO_URL")
KOKORO_REPO = KOKORO_URL.split("https://github.com/", 1)[1].split("/releases/", 1)[0]
KOKORO_FILES = (pin(SPEECH, "KOKORO_MODEL_FILE"), pin(SPEECH, "KOKORO_VOICES_FILE"))
# What the services answer for pins the fake cannot make up, written next to it as
# upstream.json: huggingface.co's /raw/ of an LFS file answers its pointer, which names the
# sha256 the install checks (134 bytes for a recognizer GGUF, read on 2026-10-16); Kokoro's
# release holds its two files, with no digest (GitHub keeps none for assets uploaded before it
# started to, as these were, in 2025).
KOLIBRI = (REPO / "install-kolibri.sh").read_text()
NEMOTRON = (REPO / "install-nemotron.sh").read_text()
# and PyPI publishes the very wheel files the Kolibri and Nemotron builds download, by URL and sha256
UPSTREAM = {
    "lfs": dict(re.findall(r'(?:ASR|MMPROJ)_FILE="([^"]+)"; (?:ASR|MMPROJ)_SHA="([0-9a-f]{64})"', SPEECH)),
    "assets": {KOKORO_REPO: [{"name": f, "digest": None} for f in KOKORO_FILES]},
    "wheels": {"aleph-alpha-inference": {"url": pin(KOLIBRI, "KOLIBRI_PLUGIN_URL"), "sha256": pin(KOLIBRI, "KOLIBRI_PLUGIN_SHA256")},
               "b12x": {"url": pin(NEMOTRON, "NEMOTRON_B12X_URL"), "sha256": pin(NEMOTRON, "NEMOTRON_B12X_SHA256")}},
    # sglang 0.5.21 requires cuda-tile==1.6.0rc5, a stub on PyPI whose wheels pypi.nvidia.com
    # lists in a PEP 503 page, by relative link (both read on 2026-10-16)
    "requires": {"sglang": ["cuda-tile==1.6.0rc5", "tilelang==0.1.12"]},
    "nvidia": {"cuda-tile": ["cuda_tile-1.6.0rc5-cp312-cp312-manylinux2014_x86_64.whl",
                             "cuda_tile-1.6.0rc5-cp312-cp312-manylinux2014_aarch64.whl",
                             "cuda_tile-1.6.0rc5-cp313-cp313-manylinux2014_aarch64.whl"]},
}

# Answers the way huggingface.co, Docker Hub, GitHub and PyPI do. The scenario (JSON in
# FAKE_PINS) maps a URL substring to [http code, x-error-code, x-ratelimit-remaining] (the
# last one optional; "000" is curl's code for no answer), "no-registry-token" empties the
# token endpoint's answer, and "bodies" maps a URL substring to the body the request gets (by
# default: the PyPI release asked for, not yanked; a release's assets from upstream.json, else
# the opencode release with the digest in FAKE_OC_SHA; an LFS pointer for a file upstream.json
# names). Like curl, it writes the body unless -o sends it elsewhere, then what -w asks
# for. Every call is logged with its argv and whatever came in on stdin for -K -.
FAKE_CURL = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
scen = json.loads(os.environ.get("FAKE_PINS", "{}"))
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "upstream.json")) as f:
        up = json.load(f)
except OSError:
    up = {}
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
    package, version = url.rstrip("/").split("/")[-3:-1]
    pinned = up.get("wheels", {}).get(package)
    file = {"yanked": False, **({"url": pinned["url"], "digests": {"sha256": pinned["sha256"]}} if pinned else {})}
    body = json.dumps({"info": {"version": version, "requires_dist": up.get("requires", {}).get(package)}, "urls": [file]})
elif body is None and url.startswith("https://pypi.nvidia.com/"):
    body = "".join('<a href="%s#sha256=%s">%s</a><br/>\n' % (f, "0" * 64, f) for f in up.get("nvidia", {}).get(url.split("/")[3], []))
elif body is None and "/releases/tags/" in url:
    repo = url.split("/repos/", 1)[1].split("/releases/", 1)[0]
    body = json.dumps({"assets": up.get("assets", {}).get(repo) or [{"name": "opencode-linux-arm64.tar.gz",
                                   "digest": "sha256:" + os.environ.get("FAKE_OC_SHA", "")}]})
elif body is None and url.startswith("https://huggingface.co/") and url.rsplit("/", 1)[-1] in up.get("lfs", {}):
    body = "version https://git-lfs.github.com/spec/v1\noid sha256:%s\nsize 804749248\n" % up["lfs"][url.rsplit("/", 1)[-1]]
elif body is None and "/manifests/" in url:
    # one image, as Docker Hub answers for three of the four pins: its platform is in its config
    body = json.dumps({"mediaType": "application/vnd.docker.distribution.manifest.v2+json", "schemaVersion": 2,
                       "config": {"mediaType": "application/vnd.docker.container.image.v1+json", "digest": "sha256:" + "c" * 64},
                       "layers": [{"digest": "sha256:" + "d" * 64}]})
elif body is None and "/blobs/" in url:
    # that config: the platform, and a "config" of its own (the container's), as every one has
    body = json.dumps({"architecture": "arm64", "os": "linux", "config": {"Env": ["PATH=/usr/bin"], "Cmd": ["/bin/bash"]},
                       "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "e" * 64]}})
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
        (self.bin / "upstream.json").write_text(json.dumps(UPSTREAM))
        self.log = self.t / "curl.log"

    def run_pins(self, scenario=None, token=None, *args):
        # neither of the caller's tokens: a GITHUB_TOKEN exported in the shell that runs the
        # suite went on to the script, and the case without a token then sent one (found in
        # a cloud review, 2026-10-18, in a container that exports it)
        env = {k: v for k, v in os.environ.items() if k not in ("HF_TOKEN", "GITHUB_TOKEN")}
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
        self.assertEqual(len(hf), 24, hf)          # ten checkpoints (the flash lane's fourth, FP4-Local, since v1.37), the diffusion lanes' three, the video Turbo adapter, Kolibri's two, each Nemotron lane's two (the model and its drafter) and the speech lane's four GGUF (two recognizers, each a model and its projector)
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
        self.assertEqual(len(gh), 6, gh)          # the two source commits, the opencode release, llama.cpp's commit and Kokoro's release, once per file
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

    def image_line(self, out, label):
        return next(ln for ln in out.splitlines() if f" {label} " in ln)

    def test_an_image_without_linux_arm64_fails(self):
        """The box is aarch64: a digest of no linux/arm64 image pulls, then runs nothing, and the
        watch asked for an HTTP 200 alone (found in the audit of 2026-10-16)."""
        def index(*platforms):   # an index, as Docker Hub answers for lmsysorg/sglang:v0.5.21
            return json.dumps({"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
                {"digest": "sha256:" + str(n) * 64, "platform": {"os": p.split("/")[0], "architecture": p.split("/")[1]}}
                for n, p in enumerate(platforms)]})
        rc, out = self.run_pins({"bodies": {"/manifests/sha256:b125": index("linux/amd64", "unknown/unknown")}})
        self.assertEqual(rc, 1, out)
        line = self.image_line(out, "27b-base")
        self.assertIn("FAIL", line)
        self.assertIn("no linux/arm64 image: an index of linux/amd64", line)
        self.assertIn("ok", self.image_line(out, "flash-base"), out)
        # and an index that names it passes, with no blob to fetch: amd64, its attestation, arm64
        rc, out = self.run_pins({"bodies": {"/manifests/sha256:b125": index("linux/amd64", "unknown/unknown", "linux/arm64")}}, None, "27b-base")
        self.assertEqual(rc, 0, out)
        self.assertIn("(linux/arm64)", self.image_line(out, "27b-base"), out)

    def test_a_single_platform_image_is_read_from_its_config(self):
        # three of the four pins are one image, not an index: its platform is in its config blob,
        # which also holds a "config" key of its own, the container's (the first real run of
        # 2026-10-16 read that as a manifest, and failed all three)
        manifest = json.dumps({"mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                               "config": {"digest": "sha256:" + "c" * 64}, "layers": []})
        blob = "/v2/lmsysorg/sglang/blobs/sha256:" + "c" * 64

        def config(arch):
            return json.dumps({"architecture": arch, "os": "linux", "config": {"Env": ["PATH=/usr/bin"]}, "rootfs": {"type": "layers"}})
        for scen, rc_want, said in (
                ({"bodies": {blob: config("arm64")}}, 0, "ok"),
                ({"bodies": {blob: config("amd64")}}, 1, "no linux/arm64 image: linux/amd64"),
                ({blob: [503, ""]}, 1, "HTTP 503 from the registry for its config, so not checked"),
                ({"bodies": {blob: "<html>error</html>"}}, 1, "not JSON, so not checked")):
            with self.subTest(said=said):
                scen.setdefault("bodies", {})["/manifests/sha256:3e79541d"] = manifest
                rc, out = self.run_pins(scen, None, "flash-base")
                self.assertEqual(rc, rc_want, out)
                self.assertIn(said, self.image_line(out, "flash-base"), out)
                self.assertIn("https://registry-1.docker.io" + blob, [c["url"] for c in self.calls()])

    def test_a_registry_answer_that_is_not_json_is_not_asked(self):
        rc, out = self.run_pins({"bodies": {"/manifests/sha256:b125": "<html>upstream connect error</html>"}}, None, "27b-base")
        self.assertEqual(rc, 1, out)
        self.assertIn("not JSON, so not checked", self.image_line(out, "27b-base"), out)
        self.assertIn("could not be asked about", out)


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

    def test_the_kolibri_lanes_are_checked_too(self):
        # its checkpoints, base image and plugin wheel were checked by nothing: a revision,
        # digest or release removed upstream went unseen (found in review, 2026-10-14)
        rc, out = self.run_pins()
        self.assertEqual(rc, 0, out)
        for label in ("kolibri-fp8", "kolibri-nvfp4", "kolibri-plugin", "kolibri-base"):
            self.assertIn("ok", self.line(out, label), out)
        urls = [c["url"] for c in self.calls()]
        self.assertTrue(any("Aleph-Alpha/Kolibri-1/raw/258cae8ffffc" in u for u in urls), "at its pinned revision")
        self.assertTrue(any("Quantinine/Kolibri-1-NVFP4A16-FP8/raw/4a8406455c47" in u for u in urls))
        self.assertIn("https://pypi.org/pypi/aleph-alpha-inference/1.0.0/json", urls)
        self.assertTrue(any("/v2/vllm/vllm-openai/manifests/sha256:18372a72249386" in u for u in urls))

    def test_the_nemotron_lane_is_checked_too(self):
        # its model, its drafter, the vLLM nightly its build starts from and the b12x wheel
        rc, out = self.run_pins()
        self.assertEqual(rc, 0, out)
        for label in ("nemotron-bolt", "nemotron-draft", "nemotron-b12x", "nemotron-base"):
            self.assertIn("ok", self.line(out, label), out)
        urls = [c["url"] for c in self.calls()]
        self.assertTrue(any("nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB/raw/c6cc2e8ffa35" in u for u in urls), "at its pinned revision")
        self.assertTrue(any("nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB-LiLiCorr/raw/4c6a5613b6d8" in u for u in urls))
        self.assertIn("https://pypi.org/pypi/b12x/1.5.1/json", urls)
        self.assertTrue(any("/v2/vllm/vllm-openai/manifests/sha256:1ee85ca80e0f" in u for u in urls))

    def test_the_nemotron_super_lane_is_checked_too(self):
        # its model and its drafter; its image is the Nemotron lane's, checked with it
        rc, out = self.run_pins()
        self.assertEqual(rc, 0, out)
        for label in ("nemotron-super", "super-draft"):
            self.assertIn("ok", self.line(out, label), out)
        urls = [c["url"] for c in self.calls()]
        self.assertTrue(any("nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4/raw/8a115b9d966a" in u for u in urls), "at its pinned revision")
        self.assertTrue(any("nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4-LiLiCorr/raw/7c7221aeb4cb" in u for u in urls))

    def test_a_removed_nemotron_super_revision_fails(self):
        rc, out = self.run_pins({"nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4/raw/": [404, "RevisionNotFound"]})
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL", self.line(out, "nemotron-super"))

    def test_a_removed_nemotron_revision_fails(self):
        # NVIDIA rewrote both repos' history when it made them public: the revisions measured
        # first were gone the same day, which this check is there to see
        rc, out = self.run_pins({"nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB/raw/": [404, "RevisionNotFound"]})
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL", self.line(out, "nemotron-bolt"))

    def test_a_removed_kolibri_revision_fails(self):
        rc, out = self.run_pins({"Quantinine/Kolibri-1-NVFP4A16-FP8/raw/": [404, "RevisionNotFound"]})
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL", self.line(out, "kolibri-nvfp4"))

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

    def test_a_pinned_wheel_file_gone_or_different_on_pypi_fails(self):
        """The Kolibri and Nemotron builds download one file by URL and check its sha256, and the
        watch asked PyPI for the version alone (found in the audit of 2026-10-16): the pinned file
        deleted while another one of the release stayed went unseen."""
        b12x, plugin = UPSTREAM["wheels"]["b12x"], UPSTREAM["wheels"]["aleph-alpha-inference"]
        sdist_only = json.dumps({"info": {"version": "1.5.1"}, "urls": [
            {"yanked": False, "url": b12x["url"].rsplit("/", 1)[0] + "/b12x-1.5.1.tar.gz", "digests": {"sha256": "d" * 64}}]})
        other_sum = json.dumps({"info": {"version": "1.0.0"}, "urls": [
            {"yanked": False, "url": plugin["url"], "digests": {"sha256": "0" * 64}}]})
        for scen, label, said in (({"bodies": {"pypi.org/pypi/b12x/": sdist_only}}, "nemotron-b12x", "the pinned file is not one PyPI publishes"),
                                  ({"bodies": {"pypi.org/pypi/aleph-alpha-inference/": other_sum}}, "kolibri-plugin",
                                   f"the pinned file has sha256 {'0' * 64} on PyPI: installs refuse it")):
            with self.subTest(label=label):
                rc, out = self.run_pins(scen)
                self.assertEqual(rc, 1, out)
                self.assertIn("FAIL", self.line(out, label), out)
                self.assertIn(said, self.line(out, label), out)
        # and the answer that carries the pinned file and sum passes: what the default fixture answers
        rc, out = self.run_pins(None, None, "nemotron-b12x")
        self.assertEqual(rc, 0, out)
        self.assertIn(b12x["url"], json.dumps(UPSTREAM))

    def test_cuda_tile_is_checked_where_an_install_gets_it(self):
        """sglang==0.5.21 requires cuda-tile==1.6.0rc5, a "wheel-stub" sdist on PyPI that downloads
        the real wheel from pypi.nvidia.com: an rc NVIDIA removes breaks a fresh image or video
        lane, and nothing watched it (found in the audit of 2026-10-16)."""
        rc, out = self.run_pins()
        self.assertEqual(rc, 0, out)
        for label in ("cuda-tile", "cuda-tile-video"):
            self.assertIn("ok", self.line(out, label), out)
        urls = [c["url"] for c in self.calls()]
        wheel = "https://pypi.nvidia.com/cuda-tile/cuda_tile-1.6.0rc5-cp312-cp312-manylinux2014_aarch64.whl"
        for u in ("https://pypi.org/pypi/cuda-tile/1.6.0rc5/json", "https://pypi.nvidia.com/cuda-tile/", wheel):
            self.assertIn(u, urls)
        # the wheel is asked about, never downloaded
        self.assertTrue(all("-I" in c["argv"] for c in self.calls() if c["url"] == wheel))

    def test_cuda_tile_gone_or_out_of_reach_fails(self):
        x86_only = '<a href="cuda_tile-1.6.0rc5-cp312-cp312-manylinux2014_x86_64.whl#sha256=00">x</a>'

        def sglang_requires(*reqs):
            return {"bodies": {"pypi.org/pypi/sglang/": json.dumps({"info": {"version": "0.5.21", "requires_dist": list(reqs)},
                                                                     "urls": [{"yanked": False}]})}}
        for scen, said, dead in (
                ({"bodies": {"pypi.nvidia.com/cuda-tile/": x86_only}}, "pypi.nvidia.com lists no cp312 aarch64 wheel of it", True),
                ({"pypi.nvidia.com/cuda-tile/cuda_tile-": [404, ""]}, "HTTP 404 from pypi.nvidia.com for cuda_tile-1.6.0rc5-cp312", True),
                ({"bodies": {"pypi.org/pypi/cuda-tile/": '{"message": "Not Found"}'}}, "PyPI's stub: not on PyPI", True),
                ({"pypi.nvidia.com": [503, ""]}, "HTTP 503 from pypi.nvidia.com, so not checked", False),
                # the version is the one the pinned sglang requires, so the check follows the pin
                (sglang_requires("cuda-tile==1.7.0"), "cuda-tile==1.7.0 (pypi.nvidia.com lists no cp312 aarch64 wheel", True),
                (sglang_requires("torch==2.13.0"), "requires no cuda-tile", True)):
            with self.subTest(said=said):
                rc, out = self.run_pins(scen, None, "cuda-tile")
                self.assertEqual(rc, 1, out)
                self.assertIn("FAIL", self.line(out, "cuda-tile"), out)
                self.assertIn(said, self.line(out, "cuda-tile"), out)
                self.assertEqual("could not be asked about" in out, not dead, out)

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

    SPEECH_LABELS = ("speech-llama-cpp", "speech-asr-0.6b", "speech-mmproj-0.6b", "speech-asr-1.7b",
                     "speech-mmproj-1.7b", "speech-kokoro", "speech-voices")

    def test_the_speech_lane_is_checked_too(self):
        # llama.cpp's commit, each recognizer's two GGUF and Kokoro's two files were checked by
        # nothing: one removed upstream went unseen until a fresh install failed (found in the
        # audit of 2026-10-16)
        rc, out = self.run_pins()
        self.assertEqual(rc, 0, out)
        for label in self.SPEECH_LABELS:
            self.assertIn("ok", self.line(out, label), out)
        urls = [c["url"] for c in self.calls()]
        self.assertIn(f"https://api.github.com/repos/ggml-org/llama.cpp/commits/{LLAMA_PIN}", urls)
        # every size of the script's case, each file at its revision: a size added is a size checked
        self.assertEqual(len(ASR), SPEECH.count('ASR_REPO="'), ASR)
        self.assertGreaterEqual(len(ASR), 2, ASR)
        for repo, rev, model, mmproj in ASR:
            for f in (model, mmproj):
                self.assertIn(f"https://huggingface.co/{repo}/raw/{rev}/{f}", urls)
        tag = KOKORO_URL.rsplit("/", 1)[1]
        self.assertEqual(urls.count(f"https://api.github.com/repos/{KOKORO_REPO}/releases/tags/{tag}"), 2, urls)

    def test_a_speech_pin_gone_or_changed_upstream_fails(self):
        other = "version https://git-lfs.github.com/spec/v1\noid sha256:" + "0" * 64 + "\nsize 1\n"
        model, voices = KOKORO_FILES
        for scen, label, said in (
                ({"ggml-org/Qwen3-ASR-1.7B-GGUF/raw/": [404, "RevisionNotFound"]}, "speech-asr-1.7b", "HTTP 404"),
                ({"bodies": {"/mmproj-Qwen3-ASR-0.6B-Q8_0.gguf": other}}, "speech-mmproj-0.6b", "installs refuse it"),
                ({"bodies": {"/Qwen3-ASR-0.6B-Q8_0.gguf": "{}"}}, "speech-asr-0.6b", "no LFS pointer"),
                ({"repos/ggml-org/llama.cpp/commits/": [422, ""]}, "speech-llama-cpp", "no such commit"),
                ({"bodies": {"kokoro-onnx/releases/tags/": json.dumps({"assets": [{"name": model, "digest": None}]})}},
                 "speech-voices", "the release has no such asset"),
                # a file uploaded again gets a digest, which then has to be the pinned sum
                ({"bodies": {"kokoro-onnx/releases/tags/": json.dumps({"assets": [{"name": model, "digest": "sha256:" + "0" * 64},
                                                                                  {"name": voices, "digest": None}]})}},
                 "speech-kokoro", "installs refuse it"),
                # Kokoro's leave to have no digest is its own: opencode's release still needs one
                ({"bodies": {"anomalyco/opencode/releases/tags/": json.dumps({"assets": [{"name": "opencode-linux-arm64.tar.gz", "digest": None}]})}},
                 "opencode", "GitHub publishes no digest")):
            with self.subTest(label=label, said=said):
                rc, out = self.run_pins(scen)
                self.assertEqual(rc, 1, out)
                self.assertIn("FAIL", self.line(out, label), out)
                self.assertIn(said, self.line(out, label), out)


class TheEndpointsThatDoNotAnswer(PinsBase):
    """Issue #33: the scheduled run of 2026-10-01 13:08 got 504s from the GitHub API for the
    two source commits and the opencode release, said the release was "no such release or
    asset, or no answer", and filed an issue; every pin resolved on the next run. A pin that
    could not be asked about is still not resolved (exit 1), and is said to be that."""

    def line(self, out, label):
        return next(ln for ln in out.splitlines() if f" {label} " in ln)

    GITHUB = ("sglang-source", "sglang-source-video", "opencode", "speech-llama-cpp", "speech-kokoro", "speech-voices")

    def test_a_github_api_that_answers_504_is_not_a_dead_pin(self):
        rc, out = self.run_pins({"api.github.com": [504, ""], "bodies": {"api.github.com": "<html>504 Gateway Time-out</html>"}})
        self.assertEqual(rc, 1, out)
        for label in self.GITHUB:
            self.assertIn("HTTP 504 from the GitHub API, so not checked", self.line(out, label), out)
            self.assertNotIn("no such", self.line(out, label), out)
        self.assertIn("6 of 41 pins could not be asked about", out)
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
