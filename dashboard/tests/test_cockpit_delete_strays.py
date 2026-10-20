#!/usr/bin/env python3
"""The models view's delete: the stray revisions of the caches the lanes read, scanned again by
the job, each with only the blobs it alone links to; what a ref or an installed unit names is
kept, and said. The action takes no parameter: the page sends no name and no path, so every
action's parameters stay a closed set. The cockpit runs live here (not dry run), on a Hugging
Face cache of the test's own."""
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
REPO = HERE.parents[2]
sys.path.insert(0, str(DASH))
sys.path.insert(0, str(HERE.parent))
from test_registry import fake_repo  # noqa: E402

ENV_BEFORE: dict = {}
DSPARK = "RadixArk/Qwen3.8-27B-DSpark"


def setUpModule():
    # the snapshot is taken during the module's first test, not at import: pytest 9 keeps
    # a note about the running test in os.environ and pops it when the last test ends; a
    # teardown restoring an import-time snapshot takes that note with it, and the runner
    # then cannot end the last test (KeyError in teardown, found 2026-10-19)
    ENV_BEFORE.update(os.environ)


def tearDownModule():
    os.environ.clear()
    os.environ.update(ENV_BEFORE)


class Delete(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-delete-"))
        (cls.tmp / "config").mkdir()
        (cls.tmp / "config" / "api-key").write_text("k\n")
        closed = "http://127.0.0.1:1"
        os.environ.update(HOME=str(cls.tmp), COCKPIT_DRY_RUN="0", COCKPIT_CONFIG_DIR=str(cls.tmp / "config"),
                          COCKPIT_REPO_DIR=str(REPO), COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0",
                          COCKPIT_AUTOHEAL="0", COCKPIT_ENGINE=closed, COCKPIT_PROXY=closed, COCKPIT_IMAGE=closed)
        spec = importlib.util.spec_from_file_location("cockpit_delete_under_test", DASH / "cockpit.py")
        cls.cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cp)
        cls.cp.run = lambda argv, timeout=5.0, merge_err=False: ""    # no docker, no systemctl
        cls.unit = cls.tmp / "qwen38-kolibri.service"
        # every lane's unit, the Nemotron lane's too: the box's own, once installed, named its real
        # cache and the scan read it (found on the reference box, 2026-10-16)
        for name in ("KOLIBRI_UNIT_PATH", "NEMOTRON_UNIT_PATH", "NEMOTRON_SUPER_UNIT_PATH", "IMAGE_UNIT_PATH", "VIDEO_UNIT_PATH"):
            setattr(cls.cp, name, cls.unit)
        cls.cp.UNIT_PATHS = {k: cls.unit for k in cls.cp.UNIT_PATHS}
        cls.pinned = cls.cp.registry_snapshot(max_age=0.0)["pins"]["DRAFT_REV"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.hub = self.tmp / ".cache" / "huggingface" / "hub"
        shutil.rmtree(self.hub, ignore_errors=True)
        self.hub.mkdir(parents=True)
        self.unit.write_text("ExecStart=/usr/bin/docker run --rm img\n")
        self.stray = "b" * 40
        self.repo = fake_repo(self.hub, {self.pinned: {"config.json": b"same", "w.safetensors": b"x" * 10},
                                         self.stray: {"config.json": b"same", "w.safetensors": b"y" * 20}},
                              shared={"config.json"}, name="models--RadixArk--Qwen3.8-27B-DSpark")
        self.cache = str(self.tmp / ".cache" / "huggingface")

    def act(self, params=None):
        code, body = self.cp.start_action("delete_strays", params or {})
        if code == 202:
            job = self.cp.JOBS[body["job"]]
            for _ in range(200):
                if job.status != "running":
                    break
                time.sleep(0.05)
            body["status"], body["lines"], body["result"] = job.status, job.lines, job.result
        return code, body

    def test_a_stray_revision_goes_with_only_its_own_blobs(self):
        code, body = self.act()
        self.assertEqual(code, 202, body)
        self.assertEqual(body["status"], "done", body)
        self.assertEqual((body["result"].get("deleted"), body["result"].get("bytes")), (1, 20), body)
        self.assertFalse((self.repo / "snapshots" / self.stray).exists())
        self.assertEqual((self.repo / "snapshots" / self.pinned / "w.safetensors").read_bytes(), b"x" * 10)
        self.assertEqual((self.repo / "snapshots" / self.pinned / "config.json").read_bytes(), b"same")
        self.assertEqual(sorted(b.name for b in (self.repo / "blobs").iterdir()),
                         sorted(["config.json", f"{self.pinned[:4]}-w.safetensors"]))

    def test_what_a_ref_or_a_unit_names_is_kept_and_said(self):
        (self.repo / "refs").mkdir()
        (self.repo / "refs" / "main").write_text(self.stray)
        code, body = self.act()
        self.assertEqual((code, body["result"].get("deleted")), (202, 0), body)
        self.assertTrue(any("kept" in ln and "refs/main" in ln for ln in body["lines"]), body["lines"])
        (self.repo / "refs" / "main").unlink()
        # a custom target serves a revision no pin names: --revision in its unit
        self.unit.write_text(f"ExecStart=/usr/bin/docker run --rm img --revision {self.stray}\n")
        code, body = self.act()
        self.assertEqual(body["result"].get("deleted"), 0, body)
        self.assertTrue(any("kept" in ln and "unit" in ln for ln in body["lines"]), body["lines"])
        self.assertTrue((self.repo / "snapshots" / self.stray).is_dir())

    def test_what_the_client_sends_changes_nothing(self):
        # no parameter is declared, so none is passed on: a name or a path sent anyway is not read
        code, body = self.act({"revision": f"{self.cache}|{DSPARK}|{self.pinned}", "path": str(self.repo)})
        self.assertEqual(code, 202, body)
        self.assertEqual(body["result"].get("deleted"), 1, body)
        self.assertTrue((self.repo / "snapshots" / self.pinned).is_dir())

    def test_nothing_stray_is_a_job_that_says_so(self):
        shutil.rmtree(self.repo / "snapshots" / self.stray)
        code, body = self.act()
        self.assertEqual((code, body["result"].get("deleted")), (202, 0), body)
        self.assertIn("no stray revision to delete", body["lines"][-1])

    def test_the_scan_says_what_a_delete_frees_or_why_it_is_kept(self):
        def stray():
            reg = self.cp.registry_snapshot(max_age=0.0)
            m = next(m for m in reg["models"] if m["repo_id"] == DSPARK)
            return {r["rev"]: r for r in m["revisions"]}
        revs = stray()
        self.assertEqual(revs[self.stray]["delete"], {"bytes": 20})
        self.assertNotIn("delete", revs[self.pinned])          # a pinned revision is never offered
        (self.repo / "refs").mkdir()
        (self.repo / "refs" / "main").write_text(self.stray)
        self.assertIn("refs/main", stray()[self.stray]["delete"]["refused"])
        (self.repo / "refs" / "main").unlink()
        self.unit.write_text(f"ExecStart=/usr/bin/docker run --rm img --revision {self.stray}\n")
        self.assertIn("unit", stray()[self.stray]["delete"]["refused"])


if __name__ == "__main__":
    unittest.main()
