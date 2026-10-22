---
name: qualify-lane
description: Measure a lane or a target on the box with the repo's protocol, unattended, into one JSON report the docs are written from. Use after a lane or target is installed and before any number about it is written in a doc, a commit or the CHANGELOG.
argument-hint: "[target] [--quick]"
---

The protocol is CONTRIBUTING.md's ("discard the first batch, repeat three times, compare medians"),
run by a script so that a boot of 5 to 17 minutes is waited for by a process and not by a session.

## Run

    tools/qualify-lane.py <target> --out bench-reports/<target>-$(date +%Y%m%d-%H%M).json

The script (to write once, stdlib only, beside affected-tests.py) does, in this order, and records
each step's command, start, end, exit status and parsed numbers in the report:

1. The box before: `free -b`, MemAvailable, the lane's unit file sha256, the image digest, the
   checkpoint revision, `git rev-parse HEAD`, the cockpit's version.
2. Switch and boot: `./switch-model.sh <target>` then the stop and start it prints; wait for the
   lane's readiness the way the cockpit does (a real generation, not /health), record the boot time
   and the KV pool the engine reports, and MemAvailable at its lowest during the boot.
3. Warm-up: one discarded `./bench.sh` pass.
4. Three repeats of `./bench.sh` and of `./bench-matrix.sh` (its JSON kept), medians computed.
5. Correctness: `./tools-check.py --min 14`, `./needle.sh --depths "60000 120000"`,
   `python3 conc-check.py`, `./bench-agent.py --turns 6 --prefix-tokens 30000`.
6. Quality, unless `--quick`: GSM8K (1,314) and HumanEval (164) through evals/ (evals/README.md
   gives the docker commands), MMLU 500 when the lane is a Qwen one.
7. The box after: MemAvailable, the engine's log tail, whether any kernel refusal appeared
   (`journalctl -k` through the cockpit's allowlisted read).

A step that fails is recorded and the run goes on to the next; the report says which numbers exist.

## Then

- Every number written in docs/<lane>-lane.md, README.md, BENCHMARKS.md or the CHANGELOG cites the
  report's file name and its date.
- Two reports of the same target compare field by field (`tools/qualify-lane.py --diff a.json b.json`):
  a regression past the noise of the three repeats is a finding before a release, not after.
