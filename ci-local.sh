#!/usr/bin/env bash
# Run this repo's CI shell steps locally, exactly as GitHub does (bash -e), before
# pushing. A step is reported as SKIPPED when it calls a tool missing on this box, or
# when a package its runner-only `pip install` line provides is missing here.
# Usage: ./ci-local.sh [step-name-substring]
# Exit status: 1 when a step failed, 3 when nothing ran (a filter that matched no step,
# or only skipped ones), 0 otherwise.
set -u
FILTER="${1:-}"
python3 - "$FILTER" <<'PY'
import os, re, subprocess, sys, yaml, shutil, tempfile
flt = sys.argv[1]
d = yaml.safe_load(open(".github/workflows/ci.yml"))
ok = fail = skip = 0
# A tool is missing when a step CALLS it, not when its name appears in the step's text.
# The test was a substring one: without gh, 13 of 61 steps were skipped for containing
# "through", "high" or "github", the sudoers gate among them, and docker is named by two
# steps that never run it (found in review, 2026-09-24). Each missing tool gets a stub,
# first on PATH, that records the call and fails the way a missing command does.
stubs = tempfile.mkdtemp(prefix="ci-local-stubs-")
for tool in ("shellcheck", "docker", "gh"):
    if not shutil.which(tool):
        with open(os.path.join(stubs, tool), "w") as f:
            f.write(f'#!/bin/sh\necho {tool} >> "$CI_LOCAL_CALLED"\n'
                    f'echo "ci-local: {tool} is not installed here" >&2\nexit 127\n')
        os.chmod(os.path.join(stubs, tool), 0o755)
PIP = re.compile(r"pip install\s+'([A-Za-z0-9_.-]+)==")


def have(pkg, env):
    """Whether this box has what a runner-only pip line installs, found the way the steps
    find it (tests/testpy.sh, under the step's own HOME)."""
    if pkg == "ruff":
        return shutil.which("ruff") is not None
    return subprocess.run(["./tests/testpy.sh", pkg.replace("-", "_")], env=env,
                          capture_output=True).returncode == 0


try:
    for job in d["jobs"].values():
        for st in job.get("steps", []):
            run = st.get("run")
            if not run:
                continue
            name = st.get("name") or run.splitlines()[0][:60]
            if flt and flt not in name:
                continue
            # Run every step under a throwaway HOME. A GitHub runner has no
            # ~/.config/qwen38, and a test that quietly read the developer's own API
            # key passed here and failed there; local has to be the harder of the two.
            home = tempfile.mkdtemp(prefix="ci-local-home-")
            called = os.path.join(home, ".ci-local-called")
            env = {**os.environ, "HOME": home, "CI_LOCAL_CALLED": called,
                   "PATH": stubs + os.pathsep + os.environ.get("PATH", "")}
            try:
                # The runner installs its pinned packages; here they have to be there
                # already. The line is dropped, and what it installs is looked for: the
                # old test looked for ruff alone, and without ruff it skipped all six
                # pip steps, coverage and hypothesis included. A line ending in
                # `|| echo ...` is one the step runs without, and says so.
                need = []
                for ln in (ln for ln in run.splitlines() if "pip install" in ln):
                    m = PIP.search(ln)
                    if m and "|| echo" not in ln and not have(m.group(1), env):
                        need.append(m.group(1))
                if need:
                    print(f"  SKIP {name} (missing: {', '.join(need)})"); skip += 1; continue
                script = "\n".join(ln for ln in run.splitlines() if "pip install" not in ln)
                r = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True,
                                   cwd=os.getcwd(), env=env)
                tools = sorted(set(open(called).read().split())) if os.path.exists(called) else []
            finally:
                shutil.rmtree(home, ignore_errors=True)
            if tools:
                print(f"  SKIP {name} (missing: {', '.join(tools)})"); skip += 1
            elif r.returncode == 0:
                print(f"  ok   {name}"); ok += 1
            else:
                print(f"  FAIL {name} (rc={r.returncode})"); print("       " + (r.stdout + r.stderr).strip().replace("\n", "\n       ")[-600:]); fail += 1
finally:
    shutil.rmtree(stubs, ignore_errors=True)
print(f"\nCI local: {ok} ok, {fail} echecs, {skip} sautes")
if fail:
    sys.exit(1)
if not ok:
    print("nothing ran" + (f": no step matches '{flt}'" if not skip else ": every step it matched was skipped"))
    sys.exit(3)
PY
