---
name: new-lane
description: Add a model to the box, as a new engine lane or as a new target of an existing lane. Use when asked to integrate, add, serve or package a new model (a Hugging Face repo, a model card, an NVIDIA or vendor recipe), before writing any installer, unit or test for it.
argument-hint: "[hf-repo or model card URL]"
---

Adding a lane touched 72 to 98 files in each of the last four (Kolibri, Bolt, Super VL, FP4-Local).
Most of that is lists and copies; this skill keeps the work to what is specific to the model.

## 1. The facts, before any file (write them in the issue or the PR body)

From the model card and the checkpoint itself, not from memory:
- repo and revision (40 hex), license, size on disk of every file the engine reads
- engine and image: the card's own command for a DGX Spark, the image by digest, any wheel by sha256
- architecture string in config.json, and whether the pinned engine image registers it
- context window, KV cache dtype the checkpoint declares, drafter (repo, revision, method)
- served model name, chat template, reasoning parser, tool parser, modalities
- whether it needs `--trust-remote-code` (then read the remote code at that revision, and say so)

Decide: a new target of an existing lane (same engine, same unit) or a new lane (its own unit).

## 2. Scaffold (no measurement yet)

- A target of an existing lane: its pins in that lane's installer, its arm in switch-model.sh, its
  limits in oc-limits.sh, its picker label in oc-point-default.py.
- A new vLLM lane: copy the smallest vLLM installer (install-nemotron-super.sh) and its unit template,
  keep every shared step in vllm-lane.sh, and add the lane to lanes.toml.
- Then `python3 tools/lanegen.py render && python3 tools/lanegen.py check`: render writes the
  Conflicts= and After= of every unit; check names every list that still disagrees. Fix each one it
  names; never add a new hand-kept list of the lanes.

## 3. Tests, from real output

- Fixtures from the box, trimmed, not invented: the boot log, `/v1/models`, `/metrics`
  (dashboard/tests/fixtures/), each with the date and the command that printed it.
- A test per behaviour the lane adds, run through the scripts' functions or to a wall
  (tests/installer_wall.py), never by slicing a script on a string.
- `python3 tools/affected-tests.py run` until green.

## 4. Measure

Run `/qualify-lane <target>`: it boots the lane, discards a warm-up, repeats three times, runs the
evals and the checks, and writes one JSON report. Every number in the docs comes from that report.

## 5. Docs and release

- docs/<lane>-lane.md with the sections every lane page has: which build and why, the serving
  command, measured, clients, switching to it and back.
- README: its section, its row in the index table, its disk line; BENCHMARKS.md: its headline row.
- CHANGELOG: what it is, what an update does, the tests (each failing on the release before).
- `./ci-local.sh` whole, then the `repo-reviewer` subagent on the whole diff, then `/release`.
