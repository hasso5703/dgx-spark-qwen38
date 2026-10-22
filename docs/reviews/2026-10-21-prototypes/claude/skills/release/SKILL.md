---
name: release
description: Cut a release of dgx-spark-qwen38 (CHANGELOG commit, tag, push). Only when explicitly asked to release.
disable-model-invocation: true
argument-hint: "[version, e.g. v1.38.0]"
---

A release is one commit that bumps CHANGELOG.md, tagged. Eight tags in 36 hours (v1.37.0 on
2026-10-20 at 12:16 to v1.37.7 on 2026-10-22 at 00:35, Paris time) each paid a full CI run and an
entry, and two of the seven patches (v1.37.2, v1.37.3) shipped what a review found after the release
before them, where the reviews of v1.37.5 and v1.37.7 ran before their tags and cost no release. So
the review comes before the tag, on everything since the last one.

1. `git describe --tags --abbrev=0` gives the last tag; `git log <last-tag>..HEAD --oneline` the
   commits this release carries. Nothing uncommitted (`git status --porcelain`).
2. The `repo-reviewer` subagent on `git diff <last-tag>..HEAD`. Every blocking finding is fixed, with
   its test, before step 3; a non-blocking one goes in the CHANGELOG entry as a known edge.
3. The CHANGELOG entry, in the house shape: `## vX.Y.Z (date): claim`, what changed, **What an update
   does** (what restarts and what keeps running), **Tests** (what fails on the release before). A patch
   when nothing a user runs changes behaviour, a minor when a lane, a target or a default does.
   Commit it: `CHANGELOG vX.Y.Z: claim`.
4. `./ci-local.sh`, whole, on that commit: the last line must read `0 echecs, 0 sautes`. It writes
   `.cache/ci-pass` with the tree's hash.
5. `git tag vX.Y.Z` and `git push origin main --tags` (the guard-release hook compares the stamp of
   step 4 with the tree being tagged: any commit after step 4 means step 4 again).
6. After the push, the GitHub run of ci.yml: a red run is a release to fix now, not a follow-up.
