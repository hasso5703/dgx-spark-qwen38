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
    """Literal NAME=value pins, unwrapping ${NAME:-default} indirection.

    Only the first assignment at column 0 of each name is the pin: an indented one is a
    later override (`DRAFT2_REPO="$CUR_DRAFT"`) or a line of the help text
    (`IMAGE=lmsysorg/sglang:v0.5.19`), and taking the last one of any kind replaced the
    served drafter and the 27B digest, which were then never checked (found in review,
    2026-09-24)."""
    out = {}
    for line in open(REPO / path):
        m = re.match(rf"^({pattern})=(.*)$", line.rstrip())
        if not m or m.group(1) in out:
            continue
        name, val = m.group(1), m.group(2).split()[0].strip("\"'")
        d = re.match(r"\$\{[^:}]+:-(.+)\}$", val)
        out[name] = d.group(1).strip("\"'") if d else val
    return out


def _between(text, start, end):
    i = text.index(start)
    return text[i:text.index(end, i + len(start))]


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
        # What the removal itself names, as whole words: the sudo path (units there) stops
        # and deletes the units and every container, the other one has run.sh's to remove.
        # Presence anywhere in the file proved nothing, since a container's name is a
        # prefix of its unit's (found in review, 2026-09-24).
        priv = _between(ui, "  if sudo -v; then\n", "\n  else\n")
        cls.disabled = {f"{u}.service" for u in re.findall(
            r"for u in (.*?); do\n\s*sudo systemctl disable --now \"\$u\.service\"", priv)[0].split()}
        cls.unit_files = set(re.findall(r"^\s*sudo rm -f (.*)$", priv, re.M)[0].replace(
            '"$SYSTEMD_DIR/', " ").replace('"', " ").split())
        cls.removed_priv = set(re.findall(r"^\s*docker rm -f (.*?) 2>/dev/null", priv, re.M)[0].split())
        unpriv = _between(ui, "\nelse\n", "\nfi\n")
        cls.removed_unpriv = set(re.findall(r"^\s*docker rm -f (.*?) 2>/dev/null", unpriv, re.M)[0].split())

    def test_every_pinned_checkpoint_is_in_the_weights_inventory(self):
        missing = [f"{k}={v}" for k, v in self.install.items()
                   if k.endswith("_REPO") and "/" in v and v not in self.hf_repos]
        self.assertEqual(missing, [])

    def test_the_parser_reads_the_pins_not_their_overrides(self):
        # the pins the two above rely on, read from the lines that set them (a repo id and
        # two digests, not "$CUR_DRAFT" and the help text's moving tag)
        self.assertRegex(self.install["DRAFT2_REPO"], r"^[\w.-]+/[\w.-]+$")
        self.assertRegex(self.install["IMAGE"], r"^lmsysorg/sglang@sha256:[0-9a-f]{64}$")
        self.assertRegex(self.install["FLASH_IMAGE"], r"^lmsysorg/sglang@sha256:[0-9a-f]{64}$")

    def test_the_image_lanes_checkpoint_is_in_the_weights_inventory(self):
        # install-image.sh pins it, not install.sh, and it was the one missing: 31 GB
        # that uninstall.sh neither listed nor offered to reclaim (2026-09-23).
        m = re.search(r'\bMODEL="\$\{MODEL:-([^}]+)\}"', open(REPO / "install-image.sh").read())
        self.assertIsNotNone(m)
        self.assertIn(m.group(1), self.hf_repos)

    def test_every_pinned_image_is_inventoried_by_repo_or_digest(self):
        # By whole entry: a local repo is listed with every tag it has, a pulled image by
        # its exact reference. A substring test let any lmsysorg/sglang digest through,
        # since "lmsysorg/sglang" is part of every base image listed.
        missing = []
        for k, v in self.install.items():
            if "IMAGE" not in k or v.startswith("$") or ("/" not in v and ":" not in v):
                continue
            repo = v.partition("@")[0].split(":")[0]
            if repo not in self.local_images and v not in self.base_images:
                missing.append(f"{k}={v}")
        self.assertEqual(missing, [])

    def test_every_unit_any_installer_writes_is_listed_and_removed(self):
        written = set()
        for f in ("install.sh", "dashboard/install-dashboard.sh",
                  "dashboard/install-agent.sh", "switch-model.sh"):
            written |= _literals(f, r"qwen38-[a-z-]+\.service|opencode-web\.service")
        # by the variable that names /etc/systemd/system since v1.18.7 (a test runs the
        # script against a sandbox)
        self.assertIn("SYSTEMD_DIR=/etc/systemd/system\n", self.ui_text)
        for u in sorted(written):
            with self.subTest(unit=u):
                self.assertIn(u, self.units, f"{u} written but not inventoried")
                self.assertIn(u, self.disabled, f"{u} is not stopped and disabled")
                self.assertIn(u, self.unit_files, f"{u} is not deleted")

    def test_privileged_surfaces_are_listed_and_removed(self):
        for path in ("/etc/sudoers.d/qwen38-cockpit",
                     "/usr/local/bin/qwen38-pyspy-scheduler"):
            with self.subTest(path=path):
                self.assertIn(path, self.ui_text)

    def test_every_named_container_is_removed(self):
        # the lanes' containers are named in their unit templates and the flash launcher,
        # run.sh's in run.sh
        names = {}
        for f in ["install.sh", "run.sh"] + sorted(p.name for p in REPO.glob("*.template")):
            for n in re.findall(r"docker run [^\n]*?--name ([a-z0-9-]+)", (REPO / f).read_text()):
                names.setdefault(n, f)
        self.assertTrue({"qwen38-sglang", "qwen38-flash", "qwen38-sglang-run"} <= set(names), names)
        for n in sorted(names):
            with self.subTest(container=n):
                self.assertIn(n, self.removed_priv, f"{n} ({names[n]}) is left by the sudo path")
        # a box with no unit to stop still has run.sh's foreground engine to remove
        self.assertIn("qwen38-sglang-run", self.removed_unpriv)

    def test_ple_backing_store_is_inventoried(self):
        self.assertIn("flashnext-ple", self.ui_text)

    def test_operator_owned_units_stay_nobody_business(self):
        self.assertNotIn("llamacpp", self.ui_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
