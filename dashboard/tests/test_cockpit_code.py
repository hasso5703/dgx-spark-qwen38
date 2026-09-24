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
import atexit
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
    """Import cockpit.py from a directory as a fresh module object.

    COCKPIT_CONFIG_DIR is redirected first because importing is not free of side
    effects: _session_secret() persists an HMAC secret so a service restart does
    not log every browser out, and without this it wrote cockpit-secret into the
    developer's own ~/.config/qwen38. The CI step "The offline suite touches
    nothing outside itself" is what found it.
    """
    # not setdefault(): its argument is evaluated anyway, and left a directory per call
    if "COCKPIT_CONFIG_DIR" not in os.environ:
        os.environ["COCKPIT_CONFIG_DIR"] = tempfile.mkdtemp(prefix="cockpit-code-cfg-")
        atexit.register(shutil.rmtree, os.environ["COCKPIT_CONFIG_DIR"], ignore_errors=True)
    os.environ.setdefault("COCKPIT_PORT", "0")
    os.environ.setdefault("COCKPIT_AGENT_PORT", "0")
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
        # If importing it started the server, every test here would hold :30090. Measured
        # around a fresh import: the threads it leaves and the sockets it opens. It used to
        # look for a name nothing defines, so a server started at import passed (found in
        # review, 2026-09-24).
        import threading

        def sockets():
            out = set()
            for fd in os.listdir("/proc/self/fd"):
                try:
                    target = os.readlink(f"/proc/self/fd/{fd}")
                except OSError:               # the listing's own descriptor, closed since
                    continue
                if target.startswith("socket:"):
                    out.add(target)
            return out
        if not os.path.isdir("/proc/self/fd"):
            self.skipTest("needs /proc to see the sockets a process holds")
        threads, socks = set(threading.enumerate()), sockets()
        ck = load_cockpit(DASH)
        new_threads = set(threading.enumerate()) - threads
        new_socks = sockets() - socks
        self.assertEqual(new_threads, set(), "importing cockpit.py started a thread")
        self.assertEqual(new_socks, set(), "importing cockpit.py opened a socket")
        self.assertTrue(callable(ck.code_fingerprint))

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


class TabsAgree(unittest.TestCase):
    """Three lists describe the tabs, and a tab exists only if all three carry it: the
    rail's buttons, the panels they reveal, and the router's own array. Adding a tab to
    two of the three is a rail entry that opens nothing, or a panel nobody can reach.
    The System One, Image and Video tabs were added on 2026-09-21 and this is the gate
    that says the next one is added everywhere."""

    def setUp(self):
        self.html = (DASH / "static/index.html").read_text()
        self.js = (DASH / "static/app.js").read_text()

    def test_the_rail_the_panels_and_the_router_carry_the_same_tabs(self):
        import re
        rail = re.findall(r'class="nav" data-tab="([a-z-]+)"', self.html)
        panels = re.findall(r'class="tab[^"]*" id="tab-([a-z-]+)"', self.html)
        m = re.search(r"const TABS = \[([^\]]+)\]", self.js)
        self.assertTrue(m, "app.js no longer declares a TABS array")
        router = re.findall(r"'([a-z-]+)'", m.group(1))
        self.assertEqual(sorted(rail), sorted(panels),
                         "a rail button opens no panel, or a panel has no button")
        self.assertEqual(sorted(rail), sorted(router),
                         "the router and the rail disagree on which tabs exist")
        self.assertEqual(rail, router, "the rail and the router disagree on tab ORDER")

    def test_every_tab_the_browser_checks_walk_is_a_tab_that_exists(self):
        import re
        rail = re.findall(r'class="nav" data-tab="([a-z-]+)"', self.html)
        for name in ("monkey-check.mjs", "mobile-check.mjs"):
            src = (DASH / "tests" / name).read_text()
            m = re.search(r"const TABS = \[([^\]]+)\]", src)
            self.assertTrue(m, f"{name} no longer declares a TABS array")
            self.assertEqual(sorted(re.findall(r"'([a-z-]+)'", m.group(1))), sorted(rail),
                             f"{name} walks a different set of tabs than the cockpit has")


if __name__ == "__main__":
    unittest.main(verbosity=2)
