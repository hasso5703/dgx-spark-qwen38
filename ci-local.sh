#!/usr/bin/env bash
# Run this repo's CI shell steps locally, exactly as GitHub does (bash -e), before
# pushing. Steps that need tools missing on this box are reported as SKIPPED.
# Usage: ./ci-local.sh [step-name-substring]
set -u
FILTER="${1:-}"
python3 - "$FILTER" <<'PY'
import subprocess, sys, yaml, shutil, os
flt = sys.argv[1]
d = yaml.safe_load(open(".github/workflows/ci.yml"))
ok = fail = skip = 0
for job in d["jobs"].values():
    for st in job.get("steps", []):
        run = st.get("run")
        if not run:
            continue
        name = st.get("name") or run.splitlines()[0][:60]
        if flt and flt not in name:
            continue
        need = [t for t in ("shellcheck", "docker", "gh") if t in run and not shutil.which(t)]
        if "pip install" in run: need.append("pip (runner-only)")
        if need:
            print(f"  SKIP {name} (missing: {', '.join(need)})"); skip += 1; continue
        r = subprocess.run(["bash", "-e", "-c", run], capture_output=True, text=True, cwd=os.getcwd())
        if r.returncode == 0:
            print(f"  ok   {name}"); ok += 1
        else:
            print(f"  FAIL {name} (rc={r.returncode})"); print("       " + (r.stdout + r.stderr).strip().replace("\n", "\n       ")[-600:]); fail += 1
print(f"\nCI local: {ok} ok, {fail} echecs, {skip} sautes")
sys.exit(1 if fail else 0)
PY
