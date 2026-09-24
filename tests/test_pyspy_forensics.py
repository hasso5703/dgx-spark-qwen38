#!/usr/bin/env python3
"""A wedge's forensics save the scheduler's stacks, never the wrapper's complaint.

The py-spy wrapper printed its errors ("py-spy not installed", "no serving container
running") on stdout, and the cockpit took any text back, stderr merged in, for a dump:
the complaint was saved as wedge-*.txt under "scheduler stacks saved" (found in review,
2026-09-24). The wrapper runs here from a copy whose py-spy and docker are stubs."""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
WRAPPER = (REPO / "dashboard" / "pyspy-scheduler.sh").read_text()
COCKPIT = (REPO / "dashboard" / "cockpit.py").read_text()


def run(pyspy_exists):
    d = pathlib.Path(tempfile.mkdtemp(prefix="pyspy-"))
    (d / "bin").mkdir()
    (d / "bin" / "docker").write_text("#!/bin/sh\nexit 1\n")          # no container answers
    (d / "bin" / "docker").chmod(0o755)
    fake = d / "py-spy"
    if pyspy_exists:
        fake.write_text("#!/bin/sh\necho stacks\n")
        fake.chmod(0o755)
    text = WRAPPER.replace("for c in /usr/local/bin/py-spy /usr/bin/py-spy /root/.local/bin/py-spy; do",
                           f"for c in {fake}; do")
    assert text != WRAPPER
    (d / "w.sh").write_text(text)
    r = subprocess.run(["bash", str(d / "w.sh")], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{d}/bin:/usr/bin:/bin"})
    return r.returncode, r.stdout, r.stderr


class TheWrapperKeepsStdoutForTheDump(unittest.TestCase):
    def test_no_py_spy(self):
        rc, out, err = run(False)
        self.assertEqual((rc, out), (3, ""))
        self.assertIn("py-spy not installed", err)

    def test_no_container(self):
        rc, out, err = run(True)
        self.assertEqual((rc, out), (4, ""))
        self.assertIn("no serving container running", err)


class TheCockpitReadsTheStatus(unittest.TestCase):
    def test_a_dump_is_saved_only_on_success(self):
        i = COCKPIT.index('"/usr/local/bin/qwen38-pyspy-scheduler"]')
        block = COCKPIT[i - 300:i + 1400]
        self.assertIn("if r.returncode == 0:\n", block)
        self.assertLess(block.index("if r.returncode == 0:"), block.index("dump = r.stdout"))
        self.assertIn('add_event("forensics", f"no scheduler stacks: {why}")', block)
        self.assertNotIn("merge_err=True", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
