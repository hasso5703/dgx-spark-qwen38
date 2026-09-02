#!/usr/bin/env python3
"""get.sh's guard logic, offline, against a real git fixture.

get.sh is what every user runs, and nothing exercised its behaviour: shellcheck
and `bash -n` only prove it parses. The fixture builds a bare repo whose path
contains this project's slug (so get.sh's origin check passes), clones it, and
replaces install.sh with a stub that records its arguments, so the whole script
runs to its exec without touching a real install."""
import os
import shutil
import subprocess
import sys
import tempfile

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GET = os.path.join(REPO_DIR, "get.sh")
STUB = '#!/usr/bin/env bash\necho "INSTALL_RAN args=$*"\n'


def git(*a, cwd, check=True):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=check)


def fixture(base):
    """A bare 'origin' whose URL carries the slug, plus a clone one commit behind."""
    origin = os.path.join(base, "hasso5703", "dgx-spark-qwen38.git")
    os.makedirs(origin)
    git("init", "-q", "--bare", "-b", "main", ".", cwd=origin)
    work = os.path.join(base, "work")
    git("clone", "-q", origin, work, cwd=base)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git("config", k, v, cwd=work)
    with open(os.path.join(work, "install.sh"), "w") as f:
        f.write(STUB)
    os.chmod(os.path.join(work, "install.sh"), 0o755)
    git("add", "-A", cwd=work); git("commit", "-qm", "v1", cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    return origin, work


def run_get(work, env_extra=None, args=()):
    env = {**os.environ, "DIR": work, **(env_extra or {})}
    return subprocess.run(["bash", GET, *args], capture_output=True, text=True, env=env)


def main() -> None:
    base = tempfile.mkdtemp()
    try:
        _, work = fixture(base)

        # 1. Clean clone on main: get.sh runs through to the installer.
        r = run_get(work)
        assert r.returncode == 0, f"clean clone refused: {r.stderr}"
        assert "INSTALL_RAN" in r.stdout, r.stdout

        # 2. Arguments reach install.sh unchanged.
        r = run_get(work, args=["--no-service", "--no-start"])
        assert "INSTALL_RAN args=--no-service --no-start" in r.stdout, r.stdout

        # 3. An untracked file must NOT block, must survive, and must be reported.
        #    It cannot make the checkout stale and `reset --hard` leaves it alone;
        #    blocking on it used to fail the install of anyone keeping notes there.
        note = os.path.join(work, "my-notes.txt")
        with open(note, "w") as f:
            f.write("keep me")
        r = run_get(work)
        assert r.returncode == 0, f"an untracked file blocked the update: {r.stderr}"
        assert os.path.exists(note), "the untracked file was removed"
        assert open(note).read() == "keep me", "the untracked file was altered"
        assert "Untracked files kept in place" in r.stdout, r.stdout
        os.remove(note)

        # 4. A modified tracked file DOES block, and says so.
        with open(os.path.join(work, "install.sh"), "a") as f:
            f.write("# local edit\n")
        r = run_get(work)
        assert r.returncode != 0, "a modified tracked file must block"
        assert "modified tracked files" in r.stderr, r.stderr

        # 5. FORCE_UPDATE=1 stashes the tracked change and proceeds.
        r = run_get(work, env_extra={"FORCE_UPDATE": "1"})
        assert r.returncode == 0, f"FORCE_UPDATE did not recover: {r.stderr}"
        assert "INSTALL_RAN" in r.stdout, r.stdout
        assert git("stash", "list", cwd=work).stdout.strip(), "the change was not stashed"

        # 6. A clone that is not this repo is refused, whatever it contains.
        other = os.path.join(base, "other")
        git("clone", "-q", work, other, cwd=base)
        git("remote", "set-url", "origin", "https://github.com/someone/else.git", cwd=other)
        r = run_get(other)
        assert r.returncode != 0 and "not a clone of this repo" in r.stderr, r.stderr

        # 7. A side branch is refused without FORCE_UPDATE, accepted with it.
        git("checkout", "-qb", "side", cwd=work)
        r = run_get(work)
        assert r.returncode != 0 and "not 'main'" in r.stderr, r.stderr
        r = run_get(work, env_extra={"FORCE_UPDATE": "1"})
        assert r.returncode == 0, f"FORCE_UPDATE did not switch back to main: {r.stderr}"

        print("test_get_sh: OK")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
