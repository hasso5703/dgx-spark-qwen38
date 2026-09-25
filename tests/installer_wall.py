"""Installer runs that end where the test says they end, on whatever machine runs the suite.

Six test files run install.sh or get.sh whole and count on one refusal to stop the run early.
Past it, step 1 asks nvidia-smi and docker about the machine and sudo for a ticket, step 2
pulls, step 6 writes the checkpoint configs in the HF cache, and get.sh fetches and resets
the checkout it is started from. Nothing checked that the refusal held: a refusal that moved
down would have run the rest for real on the box the suite ran on, get.sh was started from
the real checkout, and the tests read the box's own units in /etc (found in review,
2026-09-24).

walled() is a copy of the script that ends at a wall: a line that prints WALL and exits
before the part that acts on the machine, so a refusal that does not fire shows up as the
wall and nothing past it can run. The copy reads its units from a directory the test owns,
never /etc/systemd/system. fence() is a PATH entry that answers for the commands that act on
the box: each one records its call and fails, except `systemctl is-enabled`, which answers
from the units the test says are enabled.
"""
import atexit
import pathlib
import shutil
import stat
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
WALL = "WALL: the run went past the refusal under test"
UNITS = "/etc/systemd/system/"
REAL_UNITS = "the box's own units"

# Where each script's run stops. install.sh: the first line of step 1, which is the first
# thing that probes the machine; or, for the one test that means to run step 1, the line
# that ends it. get.sh: the first line after its root refusal.
BEFORE_STEP_1 = 'step "1/10 Preflight checks"\n'
AFTER_STEP_1 = 'echo "OK (aarch64, ${TOTAL_GB} GB RAM, ${FREE_DISK_GB} GB free)"\n'
GET_PAST_ROOT = '  REPO_URL="https://github.com/hasso5703/dgx-spark-qwen38"\n'

# Every directory this makes lives under one root, removed when the process ends: the suite
# runs on the box it tests, and /tmp there held 7,200 directories other tests left behind.
_ROOT = []


def _tmp(prefix):
    if not _ROOT:
        _ROOT.append(tempfile.mkdtemp(prefix="installer-wall-"))
        atexit.register(shutil.rmtree, _ROOT[0], True)
    return pathlib.Path(tempfile.mkdtemp(prefix=prefix, dir=_ROOT[0]))


# Every command the installers use to act on the machine or reach the network.
FENCED = ("docker", "sudo", "nvidia-smi", "systemctl", "journalctl", "curl", "wget", "git",
          "pip", "pip3", "hf", "huggingface-cli", "tailscale", "opencode", "loginctl")


def walled(script="install.sh", at=None, after=False, units=None):
    """Path of a copy of `script` (a repo file) that exits at the wall.

    at: the line the wall goes before (or after, with after=True); the default for
    install.sh is the first line of step 1, for get.sh the line after the root refusal.
    units: the directory standing in for /etc/systemd/system (a fresh empty one by
    default); every mention of that path in the copy is pointed at it. The box's own units
    are read only with units=REAL_UNITS, by the one test that is about the box it runs on."""
    text = (REPO / script).read_text()
    at = at or (BEFORE_STEP_1 if script == "install.sh" else GET_PAST_ROOT)
    if text.count(at) != 1:
        raise AssertionError(f"{script} no longer has the line the wall goes at: {at!r}")
    wall = f'echo "{WALL}"; exit 97\n'
    text = text.replace(at, at + wall if after else wall + at, 1)
    if units != REAL_UNITS:
        units = pathlib.Path(units or _tmp("units-"))
        text = text.replace(UNITS, f"{units}/")
        if "/etc/systemd" in text[:text.index(wall)]:
            raise AssertionError(f"{script} reads units under a path this copy does not redirect")
    d = _tmp("walled-")
    copy = d / pathlib.Path(script).name
    copy.write_text(text)
    copy.chmod(0o755)
    return str(copy)


def units_dir(units=None):
    """A directory standing in for /etc/systemd/system, holding `units` ({name: text})."""
    d = _tmp("units-")
    for name, text in (units or {}).items():
        (d / name).write_text(text)
    return str(d)


def fence(enabled=()):
    """(PATH entry, record file). Put the entry first on PATH: each command in FENCED
    appends its argv to the record and exits 97; `systemctl is-enabled [--quiet] UNIT`
    answers 0 for the units in `enabled`, 1 for the others, and records nothing."""
    d = _tmp("fence-")
    record = d / "calls"
    record.touch()
    for name in FENCED:
        body = f'#!/bin/sh\necho "{name} $*" >> "{record}"\nexit 97\n'
        if name == "systemctl":
            cases = "".join(f'    *" {u}") exit 0 ;;\n' for u in enabled)
            body = ('#!/bin/sh\ncase "$1" in\n  is-enabled)\n    case " $*" in\n' + cases +
                    '    esac\n    exit 1 ;;\nesac\n' + body.split("\n", 1)[1])
        (d / name).write_text(body)
        (d / name).chmod((d / name).stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(d), record


def fenced_env(home=None, enabled=(), first=None, **extra):
    """(env, record): a minimal environment whose PATH starts with the fence (after
    `first`, a directory of the test's own stubs, when given) and a throwaway HOME."""
    entry, record = fence(enabled)
    path = ":".join(p for p in (first, entry, "/usr/local/bin:/usr/bin:/bin") if p)
    env = {"PATH": path, "HOME": str(home or _tmp("home-"))}
    env.update({k: str(v) for k, v in extra.items()})
    return env, record


def reached(record):
    """What the run asked the fenced commands for, one line per call."""
    return pathlib.Path(record).read_text().splitlines()


def cwd():
    """A directory that is not a clone of this repo: get.sh uses the checkout it is started
    from, and the real one is the last place a test should start it."""
    return str(_tmp("cwd-"))

