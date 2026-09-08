"""Offline tests for the cockpit's own staleness detection.

A running cockpit imports this repo's python once, at start, and serves its html
from disk on every request. So a repo updated underneath a running process shows
NEW controls backed by OLD logic. That happened on 2026-09-08: the switch
selector offered two flash targets the action layer still refused, which is a
button that errors when pressed. install.sh now restarts the unit, but a plain
`git pull` does not, so the process compares its sources against disk and says
so. These tests pin that it does, and that it stays quiet when nothing moved.

cockpit.py is loaded as a module here: importing it must not bind a port or
start a thread, which is itself worth pinning.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
sys.path.insert(0, str(DASH))


def load_cockpit(from_dir: Path):
    """Import cockpit.py from a directory as a fresh module object."""
    spec = importlib.util.spec_from_file_location(
        f"cockpit_under_test_{from_dir.name}", from_dir / "cockpit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Fingerprint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ck = load_cockpit(DASH)

    def test_importing_the_cockpit_binds_nothing(self):
        # If importing it started the server, every test here would hold :30090.
        self.assertFalse(hasattr(self.ck, "_SERVER_STARTED_AT_IMPORT"))
        self.assertTrue(callable(self.ck.code_fingerprint))

    def test_it_watches_the_files_its_behaviour_comes_from(self):
        fp = self.ck.code_fingerprint()
        for name in ("cockpit.py", "recipes.py", "static/index.html", "static/app.js"):
            self.assertIn(name, fp, name)
            self.assertNotEqual(fp[name], "missing", name)
        # every watched name exists in the shipped tree, or the check is noise
        for name in self.ck.CODE_FILES:
            self.assertTrue((DASH / name).exists(), name)

    def test_quiet_when_nothing_moved(self):
        self.assertEqual(self.ck.code_is_stale(), [])

    def test_it_notices_a_changed_file(self):
        # A copy of the tree, so the real one is never touched.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "dashboard"
            shutil.copytree(DASH, root, ignore=shutil.ignore_patterns(
                "__pycache__", ".ruff_cache", "tests", "mockups"))
            (root / "tests").mkdir(exist_ok=True)
            ck = load_cockpit(root)
            self.assertEqual(ck.code_is_stale(), [])
            target = root / "static" / "app.js"
            target.write_text(target.read_text() + "\n// touched by a test\n")
            os.utime(target, (0, 0))          # a different mtime AND size
            self.assertEqual(ck.code_is_stale(), ["static/app.js"])
            # two files, both reported, sorted
            other = root / "recipes.py"
            other.write_text(other.read_text() + "\n# touched\n")
            self.assertEqual(ck.code_is_stale(), ["recipes.py", "static/app.js"])

    def test_a_deleted_file_is_reported_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "dashboard"
            shutil.copytree(DASH, root, ignore=shutil.ignore_patterns(
                "__pycache__", ".ruff_cache", "tests", "mockups"))
            ck = load_cockpit(root)
            (root / "registry.py").unlink()
            self.assertIn("registry.py", ck.code_is_stale())


if __name__ == "__main__":
    unittest.main(verbosity=2)
