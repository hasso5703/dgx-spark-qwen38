#!/usr/bin/env python3
"""The installer refuses root, and says which mistake was made.

On 2026-09-13 the reference box was installed with
`curl .../get.sh | sudo bash`. Nothing failed: git cloned into /root, install.sh
wrote /root/.config/qwen38 with a brand new API key, rendered the units as
User=root pointing at that key, started the engine, ran its smoke test and
reported success. The box then answered 401 to every client on it, because they
all read ~/.config/qwen38/api-key, and finding out why took a read of the sudo
audit log. A silent success into the wrong home is the worst failure shape this
installer has, so both entry points refuse root before writing anything.

The refusal has two shapes because the mistakes are different. With SUDO_USER
set there is always a right answer (drop the sudo) and no override: the message
names the login and the path whose key would stop working. A genuine root login
is merely unusual, so it refuses by default and ALLOW_ROOT=1 gets through.

Root is faked by stubbing `id` on PATH rather than by being root: a test suite
must not need root to prove what happens under it, and a PATH that can lie
about `id` could edit the script it is lying to.
"""
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
ONELINER = "https://raw.githubusercontent.com/hasso5703/dgx-spark-qwen38/main/get.sh"


def fake_root_bin():
    """A PATH entry whose `id` claims uid 0, and nothing else."""
    d = tempfile.mkdtemp(prefix="fake-root-bin-")
    idsh = os.path.join(d, "id")
    with open(idsh, "w") as f:
        f.write("#!/bin/sh\n"
                "case \"$*\" in\n"
                "  -u) echo 0 ;;\n"
                "  -un) echo root ;;\n"
                "  -g) echo 0 ;;\n"
                "  -gn) echo root ;;\n"
                "  *) exec /usr/bin/id \"$@\" ;;\n"
                "esac\n")
    os.chmod(idsh, os.stat(idsh).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return d


def run(script, *, sudo_user=None, allow_root=None, args=(), **extra):
    """Run script with a faked root identity, in a throwaway HOME."""
    env = {"PATH": fake_root_bin() + ":/usr/local/bin:/usr/bin:/bin",
           "HOME": tempfile.mkdtemp(prefix="root-refusal-home-")}
    if sudo_user is not None:
        env["SUDO_USER"] = sudo_user
    if allow_root is not None:
        env["ALLOW_ROOT"] = allow_root
    env.update(extra)
    r = subprocess.run(["bash", str(REPO / script), *args],
                       capture_output=True, text=True, env=env, cwd=str(REPO), timeout=60)
    return r.returncode, r.stdout + r.stderr, env["HOME"]


class InstallRefusesSudo(unittest.TestCase):
    def test_sudo_in_front_is_refused_by_name(self):
        rc, out, _ = run("install.sh", sudo_user="alice")
        self.assertEqual(rc, 1)
        self.assertIn("do not put sudo in front of this installer", out)

    def test_the_message_names_the_login_and_the_key_that_would_break(self):
        rc, out, _ = run("install.sh", sudo_user="alice")
        self.assertIn("alice", out)
        self.assertIn("/.config/qwen38/api-key", out)
        self.assertIn("401", out)

    def test_the_message_gives_the_command_that_works(self):
        _, out, _ = run("install.sh", sudo_user="alice")
        self.assertIn("./install.sh", out)
        self.assertIn(ONELINER, out)

    def test_allow_root_does_not_excuse_sudo(self):
        # ALLOW_ROOT is for a box with no other user. Under sudo there IS
        # another user, and their home is the one the units must point at.
        rc, out, _ = run("install.sh", sudo_user="alice", allow_root="1")
        self.assertEqual(rc, 1)
        self.assertIn("do not put sudo in front of this installer", out)

    def test_nothing_is_written_before_the_refusal(self):
        _, _, home = run("install.sh", sudo_user="alice")
        self.assertEqual(os.listdir(home), [],
                         "the refusal must land before the first write")


class InstallRefusesPlainRoot(unittest.TestCase):
    def test_plain_root_is_refused_with_the_way_out(self):
        rc, out, _ = run("install.sh")
        self.assertEqual(rc, 1)
        self.assertIn("not as root", out)
        self.assertIn("ALLOW_ROOT=1", out)

    def test_allow_root_gets_through_the_wall(self):
        # Proven without installing anything: a deliberately invalid PORT dies
        # at the next refusal down, which can only be reached past the wall.
        rc, out, _ = run("install.sh", allow_root="1", PORT="abc")
        self.assertEqual(rc, 1)
        self.assertIn("PORT must be a number", out)
        self.assertNotIn("not as root", out)


class GetShRefusesRootBeforeTheClone(unittest.TestCase):
    def test_piping_into_sudo_bash_is_refused(self):
        rc, out, _ = run("get.sh", sudo_user="alice")
        self.assertEqual(rc, 1)
        self.assertIn('do not pipe this into "sudo bash"', out)
        self.assertIn("alice", out)

    def test_plain_root_is_refused_with_the_way_out(self):
        rc, out, _ = run("get.sh")
        self.assertEqual(rc, 1)
        self.assertIn("not as root", out)
        self.assertIn("ALLOW_ROOT=1", out)

    def test_the_refusal_lands_before_any_clone(self):
        _, out, home = run("get.sh", sudo_user="alice")
        self.assertNotIn("Cloning", out)
        self.assertEqual(os.listdir(home), [])

    def test_allow_root_gets_through_the_wall(self):
        # DIR points at a path that is not a clone, so the run stops at the
        # origin check: past the wall, still no network and nothing written.
        rc, out, _ = run("get.sh", allow_root="1", DIR="/nonexistent/not-a-clone")
        self.assertEqual(rc, 1)
        self.assertIn("not a clone of this repo", out)
        self.assertNotIn("not as root", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
