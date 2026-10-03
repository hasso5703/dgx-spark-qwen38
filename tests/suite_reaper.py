#!/usr/bin/env python3
"""Run a command as a child subreaper, and report what it leaves running.

Usage: tests/suite_reaper.py COMMAND [ARG...]

The offline suite is run on the box that serves production, so "it leaves no
listening socket behind" was checked by comparing every listener on the machine
before and after it. On 2026-10-03 that failed twice on the reference box for
ports VS Code opened and closed while the suite ran, the second time while the
suite itself left nothing: a comparison of the whole machine cannot tell the
suite from an editor.

This process makes itself a child subreaper (prctl PR_SET_CHILD_SUBREAPER), so
every process the command starts, and every orphan of those, stays one of its
descendants, whatever session or environment it gave itself. What is still
running once the command is over (after a short grace for processes on their
way out) is therefore the command's own: each such process is reported, with
the sockets it listens on, then stopped.

Exit status: the command's own when it failed, 3 when it left a process
running, 2 on a usage or setup error, 0 otherwise.
"""
import ctypes
import os
import re
import signal
import subprocess
import sys
import time

PR_SET_CHILD_SUBREAPER = 36
# How long a process the command started may take to exit once it is over. Read
# here so the tests can shorten it.
GRACE_S = float(os.environ.get("SUITE_REAPER_GRACE_S", "5"))


def reap():
    """Collect every child that has exited, orphans handed to us included."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def descendants(root):
    """The live processes under root, from /proc (a zombie holds nothing)."""
    parent = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                stat = f.read()
        except OSError:
            continue  # gone between the listing and the read
        # The command name may hold spaces and parentheses: fields start after the last ")".
        fields = stat[stat.rindex(")") + 2:].split()
        if fields[0] != "Z":
            parent[int(name)] = int(fields[1])
    found = []
    for pid in parent:
        up = parent[pid]
        while up in parent and up != root:
            up = parent[up]
        if up == root:
            found.append(pid)
    return sorted(found)


def listening(pids):
    """{pid: [local address, ...]} for the TCP listeners these processes hold."""
    out = subprocess.run(["ss", "-Htlnp"], capture_output=True, text=True).stdout
    held = {}
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        for pid in {int(p) for p in re.findall(r"pid=(\d+)", line)} & set(pids):
            held.setdefault(pid, []).append(cols[3])
    return held


def command_line(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            text = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return "(exited)"
    return text[:160] or "(no command line)"


def stop(pids):
    """SIGTERM, a moment to exit, then SIGKILL for what is left; then collect the dead."""
    for sig, wait in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 1.0)):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + wait
        while pids and time.monotonic() < deadline:
            reap()
            pids = [p for p in pids if p in descendants(os.getpid())]
            if pids:
                time.sleep(0.1)
        if not pids:
            break
    # A process that died after the last reap() is a zombie of ours, which descendants() does
    # not count: left to this process's exit, it outlived the report until init collected it
    # (the test of it failed 5 times in 24 under load, 2026-10-03). Wait for every child.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return  # no child left, dead or alive
        if pid == 0:
            time.sleep(0.05)


def main(argv):
    if not argv:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        print(f"suite_reaper: prctl(PR_SET_CHILD_SUBREAPER) failed, errno {ctypes.get_errno()}",
              file=sys.stderr)
        return 2
    child = os.fork()
    if child == 0:
        try:
            os.execvp(argv[0], argv)
        except OSError as e:
            print(f"suite_reaper: cannot run {argv[0]}: {e}", file=sys.stderr)
        os._exit(127)
    code = None
    while code is None:
        pid, status = os.waitpid(-1, 0)  # orphans that exit meanwhile are collected here too
        if pid == child:
            code = os.waitstatus_to_exitcode(status)
    me = os.getpid()
    deadline = time.monotonic() + GRACE_S
    while True:
        reap()
        left = descendants(me)
        if not left or time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    if left:
        held = listening(left)
        if held:
            print("the offline suite left a listening socket behind:")
            for pid, addrs in sorted(held.items()):
                print(f"  {', '.join(sorted(addrs))} held by pid {pid}: {command_line(pid)}")
        others = [p for p in left if p not in held]
        if others:
            print("the offline suite left a process running:")
            for pid in others:
                print(f"  pid {pid}: {command_line(pid)}")
        stop(left)
        print("(stopped now)")
    if code != 0:
        return code
    return 3 if left else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
