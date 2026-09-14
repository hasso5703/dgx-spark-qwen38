#!/usr/bin/env python3
"""Everything install.sh (and the dashboard installers) can leave on a box
must be something uninstall.sh --list knows, or it is a residue nobody can
see. The scare that earned this file: qwen38-llamacpp.service sitting in
/etc/systemd/system, which turned out to be the operator's own hand-made
llama.cpp unit (predates the repo's gguf work, created by no repo script),
so the inventory is CORRECT to ignore it, and this test encodes exactly that
distinction: repo-created means listed, operator-owned means nobody's
business. Pure static parsing, runs anywhere, no box needed.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _vars(path, pattern):
    """Literal NAME=value pins, unwrapping ${NAME:-default} indirection."""
    out = {}
    for line in open(REPO / path):
        m = re.match(rf"^\s*({pattern})=(.*)$", line.rstrip())
        if not m:
            continue
        name, val = m.group(1), m.group(2).split()[0].strip("\"'")
        d = re.match(r"\$\{[^:}]+:-(.+)\}$", val)
        out[name] = d.group(1).strip("\"'") if d else val
    return out


def _literals(path, pattern):
    return set(re.findall(pattern, open(REPO / path).read()))


class UninstallKnowsEveryResidue(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install = _vars("install.sh", r"[A-Z0-9_]*_REPO|[A-Z0-9_]*IMAGE[A-Z0-9_]*")
        ui = open(REPO / "uninstall.sh").read()
        cls.units = set(re.findall(r"for u in (.*?);\s*do", ui, re.S)[0].split())
        cls.local_images = set(re.findall(r"LOCAL_IMAGE_REPOS=\"(.*?)\"", ui)[0].split())
        cls.base_images = set(re.findall(r"BASE_IMAGES=\"(.*?)\"", ui)[0].split())
        cls.hf_repos = set(re.findall(r"HF_REPOS=\"(.*?)\"", ui)[0].split())
        cls.ui_text = ui

    def test_every_pinned_checkpoint_is_in_the_weights_inventory(self):
        missing = [f"{k}={v}" for k, v in self.install.items()
                   if k.endswith("_REPO") and "/" in v and v not in self.hf_repos]
        self.assertEqual(missing, [])

    def test_every_pinned_image_is_inventoried_by_repo_or_digest(self):
        known = " ".join(self.local_images) + " " + " ".join(self.base_images)
        missing = []
        for k, v in self.install.items():
            if "IMAGE" not in k or "/" not in v:
                continue
            repo, _, ref = v.partition("@")
            repo = repo.split(":")[0]
            if repo not in known and ref not in known and v not in known:
                missing.append(f"{k}={v}")
        self.assertEqual(missing, [])

    def test_every_unit_any_installer_writes_is_listed_and_removed(self):
        written = set()
        for f in ("install.sh", "dashboard/install-dashboard.sh",
                  "dashboard/install-agent.sh", "switch-model.sh"):
            written |= _literals(f, r"qwen38-[a-z-]+\.service|opencode-web\.service")
        for u in sorted(written):
            with self.subTest(unit=u):
                self.assertIn(u, self.units, f"{u} written but not inventoried")
                self.assertIn(f"/etc/systemd/system/{u}", self.ui_text)

    def test_privileged_surfaces_are_listed_and_removed(self):
        for path in ("/etc/sudoers.d/qwen38-cockpit",
                     "/usr/local/bin/qwen38-pyspy-scheduler"):
            with self.subTest(path=path):
                self.assertIn(path, self.ui_text)

    def test_every_named_container_is_removed(self):
        names = set()
        for f in ("install.sh", "run.sh"):
            names |= set(re.findall(r"docker run .*?--name ([a-z0-9-]+)", open(REPO / f).read()))
        for n in sorted(names):
            with self.subTest(container=n):
                self.assertIn(n, self.ui_text)

    def test_ple_backing_store_is_inventoried(self):
        self.assertIn("flashnext-ple", self.ui_text)

    def test_operator_owned_units_stay_nobody_business(self):
        self.assertNotIn("llamacpp", self.ui_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
