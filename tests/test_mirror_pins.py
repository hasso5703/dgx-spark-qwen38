#!/usr/bin/env python3
"""mirror-pins.sh copies the pins it may copy, where it says, as they are.

Found in review, 2026-09-24, and each checked here against fakes of docker and of
huggingface_hub that keep the real signatures:
- the images went to the wrong place: `dst="${dst#*/}"` took the mirror registry's host off
  again, so MIRROR_REGISTRY=registry.example.com retagged lmsysorg/sglang locally and pushed
  it to Docker Hub; and a pull and a push copy one platform of the pinned index, under
  another digest, so no install pinned by digest could have found it on the mirror;
- only one image of three was listed (STOCK_IMAGE is no variable of install.sh), and six
  checkpoints of nine, none of them license-checked although the script says a checkpoint
  earns its row only after that check;
- two owners' checkpoints of one name got one mirror name;
- the model path could not run: `upload_folder` takes keyword arguments only, a commit id
  cannot be chosen on the mirror, the runbook's venv was never used, and any mode that was
  not exactly --dry-run, a typo included, executed.
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

# docker: logs every call; `buildx imagetools inspect` of a mirror ref answers only once a
# `buildx imagetools create` has put that digest there (or FAKE_MIRROR_LOSES says it never
# will); pull, tag and push log and succeed.
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
log, state = os.environ["FAKE_LOG"], os.environ["FAKE_STATE"]
with open(log, "a") as f:
    f.write(json.dumps({"tool": "docker", "argv": args}) + "\n")
held = set(json.load(open(state))) if os.path.exists(state) else set()
if args[:2] == ["buildx", "version"]:
    sys.exit(0)
if args[:3] == ["buildx", "imagetools", "create"]:
    dst, src = args[args.index("--tag") + 1], args[-1]
    if not os.environ.get("FAKE_MIRROR_LOSES"):
        held.add(dst.rsplit(":", 1)[0] + "@" + src.split("@", 1)[1])
    json.dump(sorted(held), open(state, "w"))
    sys.exit(0)
if args[:3] == ["buildx", "imagetools", "inspect"]:
    sys.exit(0 if args[3] in held else 1)
sys.exit(0)
'''

# huggingface_hub: the calls mirror-pins.sh makes, with the library's own signatures
# (1.14: upload_folder and create_repo keyword-only, create_tag keyword after repo_id).
FAKE_HUB = r'''import json, os, types
LOG = os.environ["FAKE_LOG"]
FILES = [("config.json", 10, None, "b1"), ("model.safetensors", 999, "abc", "b2")]


def _log(call, **kw):
    with open(LOG, "a") as f:
        f.write(json.dumps({"tool": "hub", "call": call, **kw}) + "\n")


def snapshot_download(repo_id, *, revision=None, **kw):
    _log("snapshot_download", repo_id=repo_id, revision=revision)
    return "/tmp/fake-snapshot"


class HfApi:
    def model_info(self, repo_id, *, revision=None, files_metadata=False, **kw):
        _log("model_info", repo_id=repo_id, revision=revision)
        sib = [types.SimpleNamespace(rfilename=n, size=s, blob_id=b,
                                     lfs=types.SimpleNamespace(sha256=h) if h else None) for n, s, h, b in FILES]
        return types.SimpleNamespace(siblings=sib)

    def repo_exists(self, repo_id, **kw):
        return False

    def list_repo_refs(self, repo_id, **kw):
        return types.SimpleNamespace(tags=[])

    def create_repo(self, repo_id, *, exist_ok=False, **kw):
        _log("create_repo", repo_id=repo_id)

    def upload_folder(self, *, repo_id, folder_path, commit_message=None, revision=None, **kw):
        _log("upload_folder", repo_id=repo_id, folder_path=str(folder_path), revision=revision)
        return types.SimpleNamespace(oid="f" * 40)

    def create_tag(self, repo_id, *, tag, revision=None, tag_message=None, **kw):
        _log("create_tag", repo_id=repo_id, tag=tag, revision=revision)
'''


class TheMirrorPins(unittest.TestCase):
    def setUp(self):
        self.t = pathlib.Path(tempfile.mkdtemp(prefix="mirror-pins-"))
        self.addCleanup(shutil.rmtree, self.t, ignore_errors=True)
        self.bin = self.t / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "docker").chmod(0o755)
        hub = self.t / "py" / "huggingface_hub"
        hub.mkdir(parents=True)
        (hub / "__init__.py").write_text(FAKE_HUB)
        self.log = self.t / "calls.log"
        self.mirror_md = self.t / "MIRROR.md"
        self.mirror_md.write_text((REPO / "MIRROR.md").read_text())

    def conclude(self, pin, text="redistributable under Apache-2.0, notice kept (2026-09-24)"):
        md = self.mirror_md.read_text()
        md, n = re.subn(rf"^\| {re.escape(pin)} \|([^|]*)\|[^|]*\|$", rf"| {pin} |\1| {text} |", md, flags=re.M)
        self.assertEqual(n, 1, f"no row for {pin} in MIRROR.md")
        self.mirror_md.write_text(md)

    def run_mirror(self, *args, **env_extra):
        env = {k: v for k, v in os.environ.items() if k not in ("HF_TOKEN", "HF_MIRROR_ORG", "MIRROR_REGISTRY")}
        env.update(PATH=f"{self.bin}:{env['PATH']}", PYTHONPATH=str(self.t / "py"), FAKE_LOG=str(self.log),
                   FAKE_STATE=str(self.t / "state.json"), MIRROR_MD=str(self.mirror_md), MIRROR_PYTHON="python3")
        env.update(env_extra)
        r = subprocess.run(["bash", str(REPO / "mirror-pins.sh"), *args], env=env, capture_output=True,
                           text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def calls(self, tool=None):
        if not self.log.exists():
            return []
        rows = [json.loads(ln) for ln in self.log.read_text().splitlines()]
        return [r for r in rows if tool is None or r["tool"] == tool]

    def test_a_mode_it_does_not_know_runs_nothing(self):
        rc, out = self.run_mirror("--dryrun", HF_MIRROR_ORG="org", HF_TOKEN="hf_x", MIRROR_REGISTRY="reg.example.com")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.calls(), [])

    def test_the_plan_lists_every_pin_under_a_name_of_its_own(self):
        rc, out = self.run_mirror()
        self.assertEqual(rc, 0, out)
        models = re.findall(r"^\s+mirror: (\S+) @ upstream-", out, re.M)
        self.assertEqual(len(models), 9, out)
        self.assertEqual(len(set(models)), 9, models)
        images = re.findall(r"^\s+mirror: (\S+@sha256:[0-9a-f]{64})", out, re.M)
        install = (REPO / "install.sh").read_text()
        pinned = re.findall(r'^(?:IMAGE|FLASH_IMAGE)="(?:\$\{\w+:-)?[^"@]+@(sha256:[0-9a-f]{64})', install, re.M)
        self.assertEqual(sorted(i.split("@")[1] for i in images), sorted(pinned), out)
        self.assertEqual(self.calls(), [], "the plan wrote or called something")

    def test_a_checkpoint_without_a_license_conclusion_is_not_mirrored(self):
        rc, out = self.run_mirror("--models", HF_MIRROR_ORG="org", HF_TOKEN="hf_x")
        self.assertEqual(rc, 1, out)
        self.assertEqual([c for c in self.calls("hub") if c["call"] != "model_info"], [], out)

    def test_a_concluded_checkpoint_is_mirrored_tagged_and_compared(self):
        self.conclude("stock")
        rc, out = self.run_mirror("--models", HF_MIRROR_ORG="org", HF_TOKEN="hf_x")
        self.assertEqual(rc, 1, out)             # the eight others are not concluded
        hub = self.calls("hub")
        rev = re.search(r'^STOCK_REV="([0-9a-f]{40})"', (REPO / "install.sh").read_text(), re.M).group(1)
        self.assertIn({"tool": "hub", "call": "snapshot_download", "repo_id": "RadixArk/Qwen3.8-27B-NVFP4",
                       "revision": rev}, hub)
        up = [c for c in hub if c["call"] == "upload_folder"]
        self.assertEqual([c["repo_id"] for c in up], ["org/RadixArk__Qwen3.8-27B-NVFP4"], out)
        self.assertIn({"tool": "hub", "call": "create_tag", "repo_id": "org/RadixArk__Qwen3.8-27B-NVFP4",
                       "tag": f"upstream-{rev}", "revision": "f" * 40}, hub)
        self.assertIn("done: 2 files", out)

    def test_images_are_copied_whole_to_the_mirror_and_checked_there(self):
        rc, out = self.run_mirror("--images", MIRROR_REGISTRY="registry.example.com/team")
        self.assertEqual(rc, 0, out)
        writes = [c["argv"] for c in self.calls("docker") if c["argv"][:1] in (["tag"], ["push"])
                  or c["argv"][:3] == ["buildx", "imagetools", "create"]]
        self.assertTrue(writes, out)
        for argv in writes:
            dst = argv[argv.index("--tag") + 1] if "--tag" in argv else argv[-1]
            self.assertTrue(dst.startswith("registry.example.com/team/"), argv)
        self.assertFalse([c for c in self.calls("docker") if c["argv"][:1] in (["pull"], ["push"])],
                         "a pull and a push carry one platform of the index, under another digest")

    def test_a_copy_the_mirror_does_not_answer_by_digest_fails(self):
        rc, out = self.run_mirror("--images", MIRROR_REGISTRY="registry.example.com", FAKE_MIRROR_LOSES="1")
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL", out)


if __name__ == "__main__":
    unittest.main()
