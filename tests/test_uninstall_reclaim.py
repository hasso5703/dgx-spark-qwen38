#!/usr/bin/env python3
"""The reclaim commands uninstall.sh prints must work when pasted.

Engine containers of past versions ran as root on the mounted HF cache and left root-owned
.no_exist and refs entries in three checkpoints of the reference box (2026-08-28 to
2026-09-12): the printed `rm -rf` stopped on "Permission denied" there. A directory the user
cannot empty gets `sudo rm -rf`. These run the uninstaller's own function, as written."""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]


def rm_cmd(path) -> str:
    text = (REPO / "uninstall.sh").read_text()
    start = text.index("rm_cmd() {")
    fn = text[start:text.index("\n}\n", start) + 3]
    r = subprocess.run(["bash", "-c", fn + f'rm_cmd "{path}"'], capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


class TheReclaimCommandsWorkWhenPasted(unittest.TestCase):
    def test_a_directory_the_user_owns_is_a_plain_rm(self):
        t = pathlib.Path(tempfile.mkdtemp(prefix="reclaim-"))
        (t / "snapshots" / "abc").mkdir(parents=True)
        (t / "snapshots" / "abc" / "config.json").write_text("{}")
        self.assertEqual(rm_cmd(t), "rm -rf")

    def test_a_directory_the_user_cannot_empty_needs_sudo(self):
        t = pathlib.Path(tempfile.mkdtemp(prefix="reclaim-"))
        locked = t / ".no_exist" / "abc"
        locked.mkdir(parents=True)
        (locked / "generation_config.json").write_text("")
        locked.chmod(0o555)
        try:
            self.assertEqual(rm_cmd(t), "sudo rm -rf")
        finally:
            locked.chmod(0o755)

    def test_a_root_owned_file_in_a_users_directory_is_still_a_plain_rm(self):
        # The PLE table is root's (the flash container writes it), in a directory that is
        # the user's: unlinking it needs the directory, not the file.
        t = pathlib.Path(tempfile.mkdtemp(prefix="reclaim-"))
        f = t / "ple_table.bin"
        f.write_text("x")
        f.chmod(0o444)
        self.assertEqual(rm_cmd(t), "rm -rf")


if __name__ == "__main__":
    unittest.main(verbosity=2)
