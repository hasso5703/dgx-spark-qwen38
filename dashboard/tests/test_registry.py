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

    def test_a_foreign_image_is_not_ours(self):
        rows = rg.parse_docker_images(["postgres:16-alpine 288MB cccccccccccc"])
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["engine"])


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
        # <PREFIX>_REPO="owner/name" paired with <PREFIX>_REV=<40 hex>
        # both regexes capture the PREFIX, so compare prefixes, not full names
        repos = dict(re.findall(r'^([A-Z0-9_]+)_REPO="([^"]+)"', text, re.M))
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
        text = (REPO / "install.sh").read_text()
        for var, repo in rg.PIN_MODELS.items():
            with self.subTest(var):
                # re.M explicitly: assertRegex anchors at the start of the whole
                # string, which is never what a shell assignment check wants.
                self.assertTrue(re.search(rf"^{var}=", text, re.M),
                                f"{var} is gone from install.sh")
                self.assertTrue(repo in text, f"{repo} is gone from install.sh")


if __name__ == "__main__":
    unittest.main(verbosity=2)
