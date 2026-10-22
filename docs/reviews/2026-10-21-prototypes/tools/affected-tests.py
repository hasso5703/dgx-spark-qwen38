#!/usr/bin/env python3
"""The test files a change can reach, from a map of what each one reads: built by tracing it.

  tools/affected-tests.py map [-j N]           run every test file alone under strace -f and write
                                               .cache/test-map.json: for each file, the repo paths it and
                                               every process it started opened, stat-ed, listed or ran,
                                               and how long it took
  tools/affected-tests.py select [REV]         the test files the worktree's changes can reach, counted from
                                               the map's own commit (or from REV when it is older), with
                                               the time they took
  tools/affected-tests.py run [REV] [-j N] [--suite-wide]
                                               run them as the CI's offline step does (N at a time, the
                                               files that take a port number out of a closed socket alone
                                               after the others) and check declared == ran for each

A test file is selected when it changed, when a file it read changed, or when an entry came or went
in a directory it listed (a new file, or the new directory that holds it). Anything the map cannot
vouch for selects every file: no map, a map built on a commit that is not an ancestor of HEAD, a
change to the CI workflow, to this tool or to the test interpreter finder. A file that failed while
the map was built is always run.

`run` leaves out tests/test_suite_isolation.py, which re-runs every module of tests/ and so reads
what all of them read: it checks the suite, not a change, and the full CI runs it (--suite-wide runs
it here too). `map` does not trace it, for the same reason: any change reaches it.

The map is a cache: rebuild it when it is old (`map` takes about as long as one run of the suite,
more under strace). It covers the unittest files (tests/test_*.py, dashboard/tests/test_*.py); the
browser checks, the fuzz and property suites and the mutation score stay in the full CI. Stdlib
only; strace is needed by `map` alone.
"""
import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

_here = pathlib.Path(__file__).resolve().parent
_top = subprocess.run(["git", "-C", str(_here), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
REPO = pathlib.Path(_top.stdout.strip()) if _top.returncode == 0 else _here.parent
MAP = REPO / ".cache" / "test-map.json"
# What the map cannot vouch for: a change here selects everything.
EVERYTHING = {".github/workflows/ci.yml", "tests/testpy.sh", "ci-local.sh",
              str(pathlib.Path(__file__).resolve().relative_to(REPO)) if pathlib.Path(__file__).resolve().is_relative_to(REPO)
              else "tools/affected-tests.py"}
IGNORED_PARTS = {".git", "__pycache__", ".venv-test", ".venv", ".hypothesis", ".cache", "node_modules"}
# strace -y prints a descriptor as 3</path>; -f -o prefixes each line with the pid
LINE = re.compile(r"^(?:\[pid\s+(\d+)\]\s+|(\d+)\s+)?(\w+)\((.*)$")
RESUMED = re.compile(r"^(?:\[pid\s+(\d+)\]\s+|(\d+)\s+)?<\.\.\. (\w+) resumed>(.*)$")
QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
FD_PATH = re.compile(r"^(\d+)<([^>]*)>")
RET = re.compile(r"\)\s+=\s+(-?\d+)")
# A test that asks git for the tracked files (git ls-files) depends on the set of files, which git
# reads from its index and not from a directory listing: it is selected by any file added or removed.
GIT_INDEX = "<the set of tracked files>"
# Selected by nearly any change, since it re-runs the whole tests/ suite: `run` leaves it to the full CI.
SUITE_WIDE = {"tests/test_suite_isolation.py"}


def test_files():
    return sorted(str(p.relative_to(REPO)) for p in [*REPO.glob("tests/test_*.py"), *REPO.glob("dashboard/tests/test_*.py")])


def runs_alone(rel):
    """The CI's rule: a file that takes a port number out of a socket it closes runs alone."""
    return "getsockname()[1]" in (REPO / rel).read_text()


def command(rel):
    """How the CI's offline step runs a file: from dashboard/ for the cockpit's tests."""
    if rel.startswith("dashboard/"):
        return ["python3", "tests/" + rel.rsplit("/", 1)[1]], REPO / "dashboard"
    return ["python3", rel], REPO


def in_repo(path):
    """The repo-relative form of an absolute path ("" for the root itself), or None for a path
    outside it or in a directory of tools (.git, caches, venvs)."""
    try:
        rel = pathlib.PurePosixPath(os.path.normpath(path)).relative_to(REPO)
    except ValueError:
        return None
    if IGNORED_PARTS & set(rel.parts):
        return None
    return str(rel) if rel.parts else ""


def parse_trace(lines, root_pid_cwd):
    """(paths read or probed, directories listed) from the lines of an strace -f -y log, following
    each process's working directory through chdir and fork, so a relative path lands where it is.
    Read line by line: a file that starts thousands of shells writes a log of hundreds of MB."""
    cwd, reads, lists, pending = {}, set(), set(), {}
    default = str(root_pid_cwd)
    for raw in lines:
        raw = raw.rstrip("\n")
        r = RESUMED.match(raw)
        if r:
            pid = r.group(1) or r.group(2) or "root"
            name, first = pending.pop(pid, (r.group(3), ""))
            rest = first + r.group(4)
        else:
            m = LINE.match(raw)
            if not m:
                continue
            pid = m.group(1) or m.group(2) or "root"
            name, rest = m.group(3), m.group(4)
            if rest.endswith("<unfinished ...>"):
                pending[pid] = (name, rest[:-len("<unfinished ...>")])
                continue
        here = cwd.get(pid, default)
        ret = RET.search(rest)
        if name in ("clone", "clone3", "fork", "vfork") and ret and int(ret.group(1)) > 0:
            cwd[ret.group(1)] = here
            continue
        args = QUOTED.findall(rest)
        if name == "chdir" and args and ret and ret.group(1) == "0":
            cwd[pid] = os.path.join(here, args[0])
            continue
        if name == "fchdir":
            fd = FD_PATH.match(rest)
            if fd and ret and ret.group(1) == "0":
                cwd[pid] = fd.group(2)
            continue
        if not args:
            continue
        base = here
        fd = FD_PATH.match(rest)
        if fd:                                      # openat(3</dir>, "name", ...), statx(3</dir>, ...)
            base = fd.group(2)
        path = args[0] if args[0].startswith("/") else os.path.join(base, args[0])
        if os.path.normpath(path) == str(REPO / ".git" / "index"):
            lists.add(GIT_INDEX)
            continue
        rel = in_repo(path)
        if rel is None:
            continue
        if "O_DIRECTORY" in rest:
            lists.add(rel)
        elif rel:
            reads.add(rel)
    return reads, lists


def trace_one(rel, strace):
    argv, cwd = command(rel)
    with tempfile.TemporaryDirectory(prefix="affected-") as d:
        log = os.path.join(d, "trace")
        env = {**os.environ, "HOME": os.path.join(d, "home"), "TMPDIR": d}
        os.makedirs(env["HOME"])
        t0 = time.time()
        # --seccomp-bpf: the kernel stops the tracee on the traced calls only (1.6 times faster here)
        r = subprocess.run([strace, "-f", "--seccomp-bpf", "-qq", "-y", "-e", "trace=%file,%process", "-e", "signal=none",
                            "-o", log, *argv], cwd=cwd, env=env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        secs = round(time.time() - t0, 1)
        with open(log, encoding="utf-8", errors="replace") as f:
            reads, lists = parse_trace(f, cwd)
    reads.discard(rel)
    return rel, {"secs": secs, "ok": r.returncode == 0, "reads": sorted(reads), "lists": sorted(lists)}


def longest_first(entries):
    """A sort key: the files that took longest when the map was last built first, as the CI's
    offline step starts its longest files first, so that they do not end the run alone; with no
    map yet, the ones that step names as its longest (its `first=` line)."""
    if entries:
        return lambda f: (-(entries.get(f, {}).get("secs") or 0), f)
    ci = REPO / ".github" / "workflows" / "ci.yml"
    m = re.search(r'^\s*first="([^"]+)"', ci.read_text() if ci.exists() else "", re.M)
    first = m.group(1).split() if m else []
    return lambda f: (first.index(f) if f in first else len(first), f)


def build_map(jobs):
    strace = shutil.which("strace")
    if not strace:
        sys.exit("map needs strace (sudo apt install strace); select and run do not")
    files = test_files()
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    took = longest_first(json.loads(MAP.read_text())["tests"] if MAP.exists() else {})
    together = sorted((f for f in files if not runs_alone(f) and f not in SUITE_WIDE), key=took)
    alone = [f for f in files if runs_alone(f) and f not in SUITE_WIDE]
    # what it reads is what every other file reads: not traced, and any change selects it
    out = {f: {"secs": None, "ok": True, "reads": [], "lists": [], "suite_wide": True} for f in files if f in SUITE_WIDE}
    with ThreadPoolExecutor(jobs) as pool:
        for rel, entry in pool.map(lambda f: trace_one(f, strace), together):
            out[rel] = entry
            print(f"  {entry['secs']:7.1f}s {'ok  ' if entry['ok'] else 'FAIL'} {rel}", flush=True)
    for rel in alone:
        rel, entry = trace_one(rel, strace)
        out[rel] = entry
        print(f"  {entry['secs']:7.1f}s {'ok  ' if entry['ok'] else 'FAIL'} {rel} (alone)", flush=True)
    MAP.parent.mkdir(exist_ok=True)
    MAP.write_text(json.dumps({"version": 1, "commit": head, "tests": out}, indent=1, sort_keys=True))
    failed = [r for r, e in out.items() if not e["ok"]]
    print(f"map of {len(out)} test files written to {MAP.relative_to(REPO)}"
          + (f"; {len(failed)} failed while traced: {', '.join(failed)}" if failed else ""))


def changes(rev):
    """(changed paths, added or removed paths) between REV and the worktree, untracked files included."""
    git = ["git", "-C", str(REPO)]
    diff = subprocess.run(git + ["diff", "--name-status", "--no-renames", rev], capture_output=True, text=True, check=True).stdout
    changed, moved = set(), set()
    for line in diff.splitlines():
        status, _, path = line.partition("\t")
        changed.add(path)
        if status[:1] in ("A", "D"):
            moved.add(path)
    untracked = subprocess.run(git + ["ls-files", "--others", "--exclude-standard"], capture_output=True, text=True).stdout.splitlines()
    untracked = [u for u in untracked if not IGNORED_PARTS.intersection(pathlib.PurePosixPath(u).parts)]   # this map, caches
    changed.update(untracked)
    moved.update(untracked)
    return changed, moved


def tree_dirs(rev):
    """Every directory git tracks at REV."""
    return set(subprocess.run(["git", "-C", str(REPO), "ls-tree", "-r", "-d", "--name-only", rev],
                              capture_output=True, text=True, check=True).stdout.splitlines())


def parent(path):
    return str(pathlib.PurePosixPath(path).parent) if "/" in path else ""


def listings_changed(moved, before, there_now):
    """The directories whose listing gains or loses an entry: the parent of each added or removed
    path, and, when that parent came or went with it, its parent in turn, up to one that is there
    both before (in BEFORE) and after (THERE_NOW(d)); the root always is."""
    out = set()
    for p in moved:
        d = parent(p)
        while True:
            out.add(d)
            if d == "" or (d in before and there_now(d)):
                break
            d = parent(d)
    return out


def pick(entries, files, changed, moved, dirs):
    """{test file: why} for the files a change reaches, given the map's ENTRIES."""
    picked = {}
    for f in files:
        entry = entries.get(f)
        if entry is None:
            picked[f] = "not in the map (a new test file)"
        elif entry.get("suite_wide"):
            if changed:
                picked[f] = "re-runs every module of tests/, so reads what any of them reads"
        elif not entry["ok"]:
            picked[f] = "failed while the map was built, so what it reads may be missing"
        elif f in changed:
            picked[f] = "the test file itself changed"
        else:
            hit = sorted(changed.intersection(entry["reads"]))
            if hit:
                picked[f] = "reads " + ", ".join(hit[:3]) + (" ..." if len(hit) > 3 else "")
            elif moved and GIT_INDEX in entry["lists"]:
                picked[f] = "asks git for the tracked files, and a file was added or removed"
            else:
                listed = sorted(dirs.intersection(entry["lists"]))
                if listed:
                    picked[f] = "lists " + (listed[0] or "the repo root") + ", where an entry came or went"
    return picked


def select(rev):
    """(the test files to run, why each one, the seconds they took when the map was built)."""
    files = test_files()
    if not MAP.exists():
        return files, {f: "no map yet" for f in files}, None
    m = json.loads(MAP.read_text())
    anc = subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", m["commit"], "HEAD"])
    if anc.returncode != 0:
        return files, {f: "the map was built on a commit that is not an ancestor of HEAD" for f in files}, m
    # Counted from the map's commit: a change committed since then is a change the map never saw.
    # A REV older than the map widens the count; a newer one would narrow it, and is not taken.
    base = m["commit"]
    if rev and subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", rev, base]).returncode == 0:
        base = rev
    changed, moved = changes(base)
    if changed & EVERYTHING:
        why = f"{sorted(changed & EVERYTHING)[0]} changed"
        return files, {f: why for f in files}, m
    dirs = listings_changed(moved, tree_dirs(base), lambda d: (REPO / d).is_dir())
    picked = pick(m["tests"], files, changed, moved, dirs)
    return sorted(picked), picked, m


def run_one(rel):
    argv, cwd = command(rel)
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="affected-run-") as d:
        os.makedirs(os.path.join(d, "home"))
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           env={**os.environ, "HOME": os.path.join(d, "home"), "TMPDIR": d})
    out = r.stdout + r.stderr
    declared = sum("def test_" in line for line in (REPO / rel).read_text().splitlines())   # grep -c, as the CI
    ran = re.findall(r"^Ran (\d+)", out, re.M)
    problem = None
    if r.returncode != 0:
        problem = "failed:\n" + "\n".join(out.splitlines()[-40:])
    elif declared and (not ran or int(ran[-1]) != declared):
        problem = f"ran {ran[-1] if ran else 0} of its {declared} declared tests"
    return rel, round(time.time() - t0, 1), problem


def run(rev, jobs, suite_wide):
    files, why, m = select(rev)
    if not suite_wide:
        for f in sorted(SUITE_WIDE.intersection(files)):
            print(f"left to the full CI: {f} (it re-runs every module of tests/; --suite-wide runs it here)")
        files = [f for f in files if f not in SUITE_WIDE]
    if not files:
        print("no test file reads what changed")
        return 0
    together = sorted((f for f in files if not runs_alone(f)), key=longest_first((m or {}).get("tests", {})))
    alone = [f for f in files if runs_alone(f)]
    t0 = time.time()
    results = []
    with ThreadPoolExecutor(jobs) as pool:
        results += list(pool.map(run_one, together))
    results += [run_one(f) for f in alone]
    bad = [(f, p) for f, _, p in results if p]
    for f, p in bad:
        print(f"FAIL {f}: {p}")
    print(f"{len(results) - len(bad)} of {len(results)} test files passed in {time.time() - t0:.0f} s"
          f" ({len(test_files())} in the suite)")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("map")
    p.add_argument("-j", type=int, default=min(4, os.cpu_count() or 1))
    p = sub.add_parser("select")
    p.add_argument("rev", nargs="?")
    p = sub.add_parser("run")
    p.add_argument("rev", nargs="?")
    p.add_argument("-j", type=int, default=min(4, os.cpu_count() or 1))
    p.add_argument("--suite-wide", action="store_true", help="run tests/test_suite_isolation.py too")
    a = ap.parse_args()
    if a.cmd == "map":
        build_map(a.j)
    elif a.cmd == "select":
        files, why, m = select(a.rev)
        secs = sum((m or {}).get("tests", {}).get(f, {}).get("secs") or 0 for f in files)
        for f in files:
            print(f"{f}  ({why[f]})")
        print(f"{len(files)} of {len(test_files())} test files, about {secs:.0f} s of work when the map was built")
    else:
        sys.exit(run(a.rev, a.j, a.suite_wide))


if __name__ == "__main__":
    main()
