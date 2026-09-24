#!/usr/bin/env python3
"""FORCE_UPDATE=1 keeps what it promises to keep, off main too.

Off main, get.sh switched to main before it stashed the tracked changes: a change that
conflicted with main ended the run (git's rc 128), and a commit made on a detached HEAD
was left on no branch (found in review, 2026-09-24). Its advice to discard changes also
included `git clean -fd`, which deletes the untracked files it keeps on purpose. This runs
the real get.sh against throwaway git repos, with a throwaway HOME and git limited to
file:// so nothing can reach GitHub, and a stub install.sh."""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
STUB = '#!/usr/bin/env bash\necho "INSTALL_RAN"\n'


def git(*a, cwd, check=True):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=check,
                          env={**os.environ, "GIT_ALLOW_PROTOCOL": "file"})


class Fixture(unittest.TestCase):
    def setUp(self):
        self.base = pathlib.Path(tempfile.mkdtemp(prefix="get-force-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        origin = self.base / "hasso5703" / "dgx-spark-qwen38.git"
        origin.mkdir(parents=True)
        git("init", "-q", "--bare", "-b", "main", ".", cwd=origin)
        up = self.base / "up"
        git("clone", "-q", str(origin), str(up), cwd=self.base)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            git("config", k, v, cwd=up)
        (up / "install.sh").write_text(STUB)
        (up / "install.sh").chmod(0o755)
        (up / "file.txt").write_text("a\n")
        git("add", "-A", cwd=up); git("commit", "-qm", "v1", cwd=up); git("push", "-q", "origin", "main", cwd=up)
        self.work = self.base / "work"
        git("clone", "-q", str(origin), str(self.work), cwd=self.base)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            git("config", k, v, cwd=self.work)
        (up / "file.txt").write_text("b\n")                 # origin moves on
        git("commit", "-qam", "v2", cwd=up); git("push", "-q", "origin", "main", cwd=up)
        self.v2 = git("rev-parse", "HEAD", cwd=up).stdout.strip()

    def run_get(self):
        home = self.base / "home"
        home.mkdir(exist_ok=True)
        return subprocess.run(["bash", str(REPO / "get.sh")], capture_output=True, text=True, timeout=60,
                              cwd=str(self.base),
                              env={"PATH": os.environ["PATH"], "HOME": str(home), "DIR": str(self.work),
                                   "FORCE_UPDATE": "1", "GIT_ALLOW_PROTOCOL": "file"})

    def at_origin(self):
        return git("rev-parse", "HEAD", cwd=self.work).stdout.strip() == self.v2


class OffMain(Fixture):
    def test_a_side_branch_with_a_conflicting_change(self):
        git("checkout", "-qb", "feature", cwd=self.work)
        (self.work / "file.txt").write_text("feat\n")
        git("commit", "-qam", "feature work", cwd=self.work)
        (self.work / "file.txt").write_text("mine, not committed\n")
        r = self.run_get()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("INSTALL_RAN", r.stdout)
        self.assertTrue(self.at_origin())
        self.assertIn("get.sh auto-stash", git("stash", "list", cwd=self.work).stdout)
        self.assertEqual(git("show", "feature:file.txt", cwd=self.work).stdout, "feat\n", "the branch was lost")

    def test_a_commit_on_a_detached_head_is_kept_on_a_branch(self):
        git("checkout", "-q", "--detach", cwd=self.work)
        (self.work / "notes.txt").write_text("x\n")
        git("add", "notes.txt", cwd=self.work); git("commit", "-qm", "local on detached", cwd=self.work)
        local = git("rev-parse", "HEAD", cwd=self.work).stdout.strip()
        r = self.run_get()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.at_origin())
        holders = git("branch", "--contains", local, cwd=self.work).stdout
        self.assertTrue(holders.strip(), "the detached commit is on no branch")


class TheAdviceKeepsUntrackedFiles(unittest.TestCase):
    def test_no_git_clean_in_the_advice(self):
        text = (REPO / "get.sh").read_text()
        self.assertNotIn("git -C $DIR clean", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
