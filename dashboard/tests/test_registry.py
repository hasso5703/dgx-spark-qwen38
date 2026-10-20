"""Offline tests for registry.py and the presence it feeds.

Two bugs shipped here on 2026-09-08, both found by asking the live cockpit what
it thought was on the box:

  * the flash lane moved to a digest-pinned image, and the image scan asked
    docker only for `{{.Repository}}:{{.Tag}}`. An image pulled by digest carries
    no tag, docker hides untagged images from the default listing, and the
    cockpit reported "image missing" about the very image the lane was running.
  * PIN_MODELS is a fifth list of the served checkpoints, and the two new flash
    targets were not in it, so their presence read "unknown" instead of
    present/absent: exactly the answer an operator needs before switching.

Both are pinned here, and PIN_MODELS is checked against install.sh so a seventh
target cannot be added to the installer and forgotten in the registry.
"""
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
REPO = HERE.parents[2]
sys.path.insert(0, str(DASH))
import recipes as rc  # noqa: E402
import registry as rg  # noqa: E402


class DockerImages(unittest.TestCase):
    def test_a_digest_reference_survives_the_parser(self):
        digest = "lmsysorg/sglang@sha256:" + "9" * 64
        rows = rg.parse_docker_images([f"{digest} 30.3GB cdd9649ba1cf"])
        self.assertEqual([r["ref"] for r in rows], [digest])
        self.assertTrue(rows[0]["engine"])

    def test_untagged_and_undigested_rows_are_dropped(self):
        rows = rg.parse_docker_images([
            "<none>:<none> 30.3GB aaaaaaaaaaaa",          # untagged, tag pass
            "lmsysorg/sglang@<none> 30.3GB aaaaaaaaaaaa",  # no digest, digest pass
            "qwen38-flash:v1.6.0-kda 30.3GB bbbbbbbbbbbb",
        ])
        self.assertEqual([r["ref"] for r in rows], ["qwen38-flash:v1.6.0-kda"])

    def test_the_same_image_seen_twice_is_listed_once(self):
        # The caller feeds both passes, and a tagged image appears in both.
        rows = rg.parse_docker_images([
            "qwen38-flash:v1.6.0-kda 30.3GB bbbbbbbbbbbb",
            "qwen38-flash:v1.6.0-kda 30.3GB bbbbbbbbbbbb",
        ])
        self.assertEqual(len(rows), 1)

    def test_short_rows_are_ignored(self):
        self.assertEqual(rg.parse_docker_images(["", "only-one-field", "two fields"]), [])

    def test_the_images_the_vllm_lanes_build_are_ours(self):
        # built on the box, tagged and with no digest: Library did not list them (2026-10-16)
        rows = rg.parse_docker_images([
            "qwen38-nemotron:vllm77154ca6-b12x1.5.1-1ee85ca80e0fc 24.5GB aaaaaaaaaaaa",
            "qwen38-kolibri:vllm0.29.0-aai1.0.0-18372a72249386434 22.1GB bbbbbbbbbbbb",
            "qwen38-nemotron-test:b12x1.5.1 24.5GB cccccccccccc",
        ])
        self.assertEqual([r["engine"] for r in rows], [True, True, False])

    def test_a_foreign_image_is_not_ours(self):
        rows = rg.parse_docker_images(["postgres:16-alpine 288MB cccccccccccc"])
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["engine"])

    def test_the_fp4_local_b12x_image_is_ours_by_digest_and_by_pin_tag(self):
        # the flash lane's FP4-Local config runs it: missing from Library, and its recipe
        # always read "image absent" (found in review, 2026-10-20)
        assigns = rc.parse_assignments((REPO / "install.sh").read_text())
        image = rc.builtin("flash-fp4-local", assigns, rc.load_templates(REPO))["engine"]["image"]
        self.assertTrue(image.startswith("eugr/spark-vllm-b12x@sha256:"))
        rows = rg.parse_docker_images([
            f"{image} 28.1GB aaaaaaaaaaaa",
            "eugr/spark-vllm-b12x:latest 28.1GB aaaaaaaaaaaa",
            "qwen38-pinned:flash-local-0123456789ab 28.1GB aaaaaaaaaaaa",
            "eugr/spark-vllm-b12x-other:latest 1GB bbbbbbbbbbbb",
        ])
        self.assertEqual([r["engine"] for r in rows], [True, True, True, False])
        rec = rc.builtin("flash-fp4-local", assigns, rc.load_templates(REPO))
        reg = {"images": [r for r in rows if r["engine"]], "models": []}
        self.assertTrue(rc.presence(rec, reg)["image"])


class Presence(unittest.TestCase):
    """presence() answers "is what this recipe needs on the box". A digest-pinned
    image must resolve, and a managed repo that is absent must read False (not
    None), because None means "not knowable" and says nothing useful."""

    def registry_with(self, image_refs, repos):
        return {"images": [{"ref": r} for r in image_refs],
                "managed_repos": sorted(set(rg.PIN_MODELS.values())),
                "models": [{"repo_id": repo, "revisions": [{"rev": rev}]}
                           for repo, rev in repos.items()]}

    def test_a_digest_pinned_image_is_found(self):
        assigns = rc.parse_assignments((REPO / "install.sh").read_text())
        rec = rc.builtin("flash", assigns, rc.load_templates(REPO))
        image = rec["engine"]["image"]
        self.assertIn("@sha256:", image, "the flash lane is expected to be digest-pinned")
        reg = self.registry_with([image], {rec["model"]["repo"]: rec["model"]["revision"]})
        self.assertTrue(rc.presence(rec, reg)["image"])
        self.assertTrue(rc.presence(rec, reg)["model"])
        # and the same recipe against a box that only has a tagged image
        reg2 = self.registry_with(["qwen38-flash:v1.6.0-kda"], {})
        self.assertFalse(rc.presence(rec, reg2)["image"])

    def test_every_flash_target_is_knowable(self):
        assigns = rc.parse_assignments((REPO / "install.sh").read_text())
        templates = rc.load_templates(REPO)
        for rid in ("flash", "flash-nvda", "flash-uncensored"):
            rec = rc.builtin(rid, assigns, templates)
            reg = self.registry_with([], {})   # nothing on the box
            got = rc.presence(rec, reg)["model"]
            self.assertIs(got, False,
                          f"{rid}: a pinned checkpoint that is absent must read False, "
                          f"not {got!r} (unknown), or the operator cannot tell whether "
                          f"switching would work")


class PinModelsCoversInstaller(unittest.TestCase):
    def test_every_served_checkpoint_pin_is_in_pin_models(self):
        text = (REPO / "install.sh").read_text()
        # <PREFIX>_REPO="owner/name" paired with <PREFIX>_REV=<40 hex>.
        # Overridable pins use the <PREFIX>_REPO="${<PREFIX>_REPO:-owner/name}"
        # shape instead (the 27B draft since v1.9): read the default either way.
        # Both regexes capture the PREFIX, so compare prefixes, not full names.
        repos = dict(re.findall(r'^([A-Z0-9_]+)_REPO="([^"$][^"]*)"', text, re.M))
        for p, default in re.findall(r'^([A-Z0-9_]+)_REPO="\$\{\1_REPO:-([^}]+)\}"', text, re.M):
            repos.setdefault(p, default)
        rev_prefixes = set(re.findall(r'^([A-Z0-9_]+)_REV=', text, re.M))
        pairs = {f"{p}_REV": repo for p, repo in repos.items() if p in rev_prefixes}
        self.assertGreaterEqual(len(pairs), 7, pairs)
        for var, repo in pairs.items():
            with self.subTest(var):
                self.assertIn(var, rg.PIN_MODELS,
                              f"install.sh pins {var} for {repo}, and registry.py's "
                              f"PIN_MODELS does not know it: that target's presence "
                              f"reads 'unknown' in the cockpit")
                self.assertEqual(rg.PIN_MODELS[var], repo)

    def test_pin_models_names_nothing_the_installer_dropped(self):
        # every lane's installer since the side lanes' pins joined it (2026-10-14)
        text = "\n".join((REPO / f).read_text() for f in rg.PIN_FILES)
        for var, repo in rg.PIN_MODELS.items():
            with self.subTest(var):
                # re.M explicitly: assertRegex anchors at the start of the whole
                # string, which is never what a shell assignment check wants.
                self.assertTrue(re.search(rf"^{var}=", text, re.M),
                                f"{var} is gone from install.sh")
                self.assertTrue(repo in text, f"{repo} is gone from install.sh")


SIDE_INSTALLERS = ("install-image.sh", "install-video.sh", "install-kolibri.sh")


def side_pins(text):
    """{<P>_REV: repo} of a side installer: a 40-hex <P>_REV beside the repo it pins, named
    <P>_REPO or <P> itself (IMAGE_MODEL_PIN beside IMAGE_MODEL_PIN_REV)."""
    repos = dict(re.findall(r'^([A-Z0-9_]+)="([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"', text, re.M))
    out = {}
    for var in re.findall(r'^([A-Z0-9_]+_REV)="[0-9a-f]{40}"', text, re.M):
        p = var[:-len("_REV")]
        repo = repos.get(p + "_REPO") or repos.get(p)
        if repo:
            out[var] = repo
    return out


class TheCachesTheUnitsName(unittest.TestCase):
    def test_a_mount_and_an_hf_home_are_read_once_each(self):
        texts = ["ExecStart=/usr/bin/docker run -v /data/hf:/root/.cache/huggingface -v /x:/y img",
                 "docker run --rm \\\n  -v /data/hf:/root/.cache/huggingface \\\n  img",
                 "Environment=HF_HOME=/data/hf\nEnvironment=HF_HOME=/other/hf",
                 "Environment=PYTHONUNBUFFERED=1", ""]
        self.assertEqual(rg.cache_dirs(texts), ["/data/hf", "/other/hf"])


def fake_repo(root, revs, shared=(), refs=None, name="models--o--m"):
    """A Hugging Face cache repo folder: each revision's snapshot links its files to blobs,
    a file named in shared links to the same blob in every revision."""
    import os
    repo = root / name
    (repo / "blobs").mkdir(parents=True)
    for rev, files in revs.items():
        snap = repo / "snapshots" / rev
        snap.mkdir(parents=True)
        for name, data in files.items():
            blob = repo / "blobs" / (name if name in shared else f"{rev[:4]}-{name}")
            blob.write_bytes(data)
            os.symlink(os.path.relpath(blob, snap), snap / name)
    for name, rev in (refs or {}).items():
        (repo / "refs").mkdir(exist_ok=True)
        (repo / "refs" / name).write_text(rev)
    return repo


class CompleteForWhatTheLaneServes(unittest.TestCase):
    """A snapshot is complete for a lane when every index its allow patterns cover has its
    shards: the reference box's MiniMax-H3 keeps Ref2VA's transformer index with none of its
    shards beside the whole FL2VA partition its lane serves, and the registry read that
    revision incomplete (found building the models view, 2026-10-14). The same rule as
    ckpt-fetch.py's complete(), checked against it."""

    def setUp(self):
        import tempfile, json
        self.json = json
        self.root = Path(tempfile.mkdtemp(prefix="registry-complete-"))
        self.addCleanup(__import__("shutil").rmtree, self.root, True)

    def snap(self, parts):
        """{"FL2VA": True, "Ref2VA": False}: each part an index of two shards, whole or not."""
        s = self.root / ("s" + str(len(list(self.root.iterdir()))))
        for part, whole in parts.items():
            d = s / part / "transformer"
            d.mkdir(parents=True)
            (d / "model.safetensors.index.json").write_text(self.json.dumps(
                {"weight_map": {"a": "1.safetensors", "b": "2.safetensors"}}))
            (d / "1.safetensors").write_text("w")
            if whole:
                (d / "2.safetensors").write_text("w")
        (s / "model_index.json").write_text("{}")
        return s

    def ckpt_fetch(self):
        import importlib.util, types
        saved = sys.modules.get("huggingface_hub")
        fake = types.ModuleType("huggingface_hub")
        fake.constants, fake.snapshot_download = types.SimpleNamespace(), None
        sys.modules["huggingface_hub"] = fake
        try:
            spec = importlib.util.spec_from_file_location("ckpt_fetch_parity", REPO / "ckpt-fetch.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            if saved is None:
                del sys.modules["huggingface_hub"]
            else:
                sys.modules["huggingface_hub"] = saved
        return mod

    def test_only_what_the_patterns_cover_counts(self):
        s = self.snap({"FL2VA": True, "Ref2VA": False})
        self.assertFalse(rg.snapshot_complete(s))
        self.assertTrue(rg.snapshot_complete(s, ["FL2VA/*", "model_index.json"]))
        self.assertFalse(rg.snapshot_complete(s, ["Ref2VA/*", "model_index.json"]))

    def test_the_same_answer_as_ckpt_fetch(self):
        cf = self.ckpt_fetch()
        for parts in ({"FL2VA": True}, {"FL2VA": False}, {"FL2VA": True, "Ref2VA": False}):
            s = self.snap(parts)
            for allow in (None, ["FL2VA/*", "model_index.json"], ["Ref2VA/*"], ["FL2VA/"]):
                with self.subTest(parts=parts, allow=allow):
                    self.assertEqual(rg.snapshot_complete(s, allow), cf.complete(s, allow))

    def test_a_link_counts_once_its_blob_is_there_with_no_index_too(self):
        # a snapshot of single files whose link points at a blob gone read as whole here, while
        # ckpt-fetch.py, which fetches it, read it as missing (found in review, 2026-10-15)
        cf = self.ckpt_fetch()
        d = Path(tempfile.mkdtemp(prefix="reg-link-"))
        (d / "blobs").mkdir()
        s = d / "snapshots" / ("a" * 40)
        s.mkdir(parents=True)
        (s / "model.safetensors").symlink_to(d / "blobs" / "b1")
        self.assertFalse(rg.snapshot_complete(s))
        self.assertEqual(rg.snapshot_complete(s), cf.complete(s))
        (d / "blobs" / "b1").write_text("w")
        self.assertTrue(rg.snapshot_complete(s))
        self.assertEqual(rg.snapshot_complete(s), cf.complete(s))

    def test_the_video_lane_patterns_are_the_switchs_own(self):
        sw = (REPO / "switch-model.sh").read_text()
        self.assertEqual(rg.video_allow(sw, "fl2va")[0], "FL2VA/*")
        self.assertEqual(rg.video_allow(sw, "ref2va")[0], "Ref2VA/*")
        self.assertIn("model_index.json", rg.video_allow(sw, "fl2va"))


class WhatARevisionTakes(unittest.TestCase):
    """Library's Size said 268.8 GiB for the reference box's MiniMax-H3, whose one revision
    takes 134.1 on disk (2026-10-15): its snapshot links 192 files to 80 blobs (its partitions
    share their weights), and each file was counted. A revision counts each of its blobs once;
    two revisions sharing a blob each count it, and the repo's disk once."""

    def setUp(self):
        import tempfile
        self.root = Path(tempfile.mkdtemp(prefix="registry-size-"))
        self.addCleanup(__import__("shutil").rmtree, self.root, True)

    def test_two_files_on_one_blob_take_it_once(self):
        import os
        repo = fake_repo(self.root, {"a" * 40: {"part1.safetensors": b"x" * 100, "config.json": b"{}"}})
        snap = repo / "snapshots" / ("a" * 40)
        os.symlink(os.readlink(snap / "part1.safetensors"), snap / "part2.safetensors")   # a second file, the same blob
        (m,) = rg.scan_hf_cache(self.root)
        self.assertEqual(m["revisions"][0]["bytes"], 102)
        self.assertEqual(m["disk_bytes"], 102)

    def test_a_blob_two_revisions_share_counts_in_each(self):
        fake_repo(self.root, {"a" * 40: {"config.json": b"same", "w.safetensors": b"x" * 10},
                              "b" * 40: {"config.json": b"same", "w.safetensors": b"y" * 20}}, shared={"config.json"})
        (m,) = rg.scan_hf_cache(self.root)
        self.assertEqual([r["bytes"] for r in m["revisions"]], [14, 24])
        self.assertEqual(m["disk_bytes"], 34)


class WhatADeleteRemoves(unittest.TestCase):
    """A stray revision is deleted with the blobs only it links to: a blob another revision
    shares stays, and so does a revision a ref names, which a load by name reads."""

    def setUp(self):
        import tempfile
        self.root = Path(tempfile.mkdtemp(prefix="registry-delete-"))
        self.addCleanup(__import__("shutil").rmtree, self.root, True)
        self.a, self.b = "a" * 40, "b" * 40

    def test_only_the_blobs_no_other_revision_shares(self):
        repo = fake_repo(self.root, {self.a: {"config.json": b"same", "w.safetensors": b"x" * 10},
                                     self.b: {"config.json": b"same", "w.safetensors": b"y" * 20}},
                         shared={"config.json"})
        plan = rg.deletion_plan(repo, self.b)
        self.assertNotIn("refused", plan)
        self.assertEqual([Path(b).name for b in plan["blobs"]], ["bbbb-w.safetensors"])
        self.assertEqual(plan["bytes"], 20)

    def test_a_revision_a_ref_names_is_refused(self):
        repo = fake_repo(self.root, {self.a: {"w.safetensors": b"x"}}, refs={"main": self.a})
        self.assertIn("refs/main", rg.deletion_plan(repo, self.a)["refused"])

    def test_a_revision_not_in_the_cache_is_refused(self):
        repo = fake_repo(self.root, {self.a: {"w.safetensors": b"x"}})
        self.assertIn("refused", rg.deletion_plan(repo, self.b))
        self.assertIn("refused", rg.deletion_plan(repo, "../" + self.a))


class PinModelsCoversEveryLane(unittest.TestCase):
    """The registry knew the text lanes' checkpoints only: the image, video and Kolibri
    lanes' weights read as "other models", with no pin and no status, so nothing could say
    which of their revisions a lane serves and which is stray (found building the models
    view, 2026-10-14)."""

    def test_every_side_installer_pin_is_in_pin_models(self):
        seen = 0
        for f in SIDE_INSTALLERS:
            for var, repo in side_pins((REPO / f).read_text()).items():
                seen += 1
                with self.subTest(f=f, var=var):
                    self.assertEqual(rg.PIN_MODELS.get(var), repo)
        self.assertEqual(seen, 6)   # the image and its Turbo, the video and its adapter, Kolibri twice

    def test_the_pins_are_read_where_every_lane_writes_them(self):
        self.assertTrue({"install.sh", "switch-model.sh", *SIDE_INSTALLERS} <= set(rg.PIN_FILES), rg.PIN_FILES)
        for var in rg.PIN_MODELS:
            with self.subTest(var):
                self.assertTrue(any(re.search(rf"^\s*{var}=", (REPO / f).read_text(), re.M) for f in rg.PIN_FILES),
                                f"{var} is pinned in none of {rg.PIN_FILES}")

    def test_a_side_lane_revision_is_pinned_or_stray(self):
        pins = rg.parse_pins((REPO / "install-kolibri.sh").read_text())
        models = rg.classify([{"repo_id": "Aleph-Alpha/Kolibri-1", "disk_bytes": 1,
                               "revisions": [{"rev": pins["KOLIBRI_FP8_REV"]}, {"rev": "f" * 40}]}], pins)
        self.assertTrue(models[0]["managed"])
        self.assertEqual([r["status"] for r in models[0]["revisions"]], ["pinned", "stray"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
