# dgx-spark-qwen38: what every session on this repo must know

A serving stack for one GB10 box (DGX Spark, ASUS Ascent GX10): eight engine lanes that take turns in
one 128 GB unified pool, a speech lane on the CPU beside them, a keepalive proxy on :30001, a cockpit
on :30090. Everything below is a rule CI or a review already holds; the why of each one is in
CONTRIBUTING.md, TESTING.md and ARCHITECTURE.md.

## Never

- Never run `sudo ./install.sh` (or any installer under sudo): `$HOME` becomes `/root`, every client gets 401.
- Never raise `--mem-fraction-static` past the values in docs/gb10-memory.md: a pool at zero freezes the
  box until a power cycle. 0.80 was measured crashing the 27B lane.
- Never write an em dash, an en dash or a minus sign (U+2212) in a tracked file.
- Never add a Python dependency to the runtime: stdlib only, forever. Engine needs live in a lane's venv.
- Never let anything unpinned reach a user: images by digest, checkpoints by revision (at fetch AND at
  serve), wheels by sha256.
- Never put a secret in an argv (docker -e NAME=value, curl -H with the key): pass it by name or file.
- Never write a fixed path in /tmp from a workflow step: `${TMPDIR:-/tmp}`.
- Never edit a rendered unit in /etc: change the template and re-render.
- Never start, stop or restart an engine from switch-model.sh: it prints the commands.

## Always

- Measured or it does not ship: a flag, a ceiling or a claim needs a run on the box; discard the first
  batch, three repeats, medians; the numbers go in the commit body.
- A test names the bug it prevents, in the words of what went wrong; every test file runs standalone
  and CI counts declared against ran.
- Test behaviour, not text: call a function of a sourced library, or run the script to a wall
  (tests/installer_wall.py). Do not add new tests that slice a script by a string anchor.
- One list of the lanes: lanes.toml (tools/lanegen.py check). Never spell the lanes again by hand.
- Shell under `set -euo pipefail`: a `VAR="$(... grep ...)"` must end in `|| true`.
- Commit style: `area: claim`, a body that tells what was wrong, how it was measured, what holds it now;
  the last line is the receipt (a test, a gate, a measurement).

## The verification ladder (use the cheapest rung that can fail)

0. Edit time, automatic: `.claude/hooks/fast-checks.sh` runs the static gates on the file you wrote.
1. Before a commit: `tools/affected-tests.py run` (the test files that read what changed, minutes;
   tests/test_suite_isolation.py, which re-runs the whole suite, is left to rung 2).
   Rebuild its map (`tools/affected-tests.py map`) after a full CI that passed, or when `select`
   starts picking most of the suite: it counts every change since the map's own commit.
2. Before a push to main, a tag or a release: `./ci-local.sh`, whole, last line `0 echecs, 0 sautes`
   (`.claude/hooks/guard-release.sh` refuses the push or the tag otherwise).
3. Before a minor release: the on-box proofs (each lane built and proved: `./install.sh --all-weights`
   on a box that can afford it, or each lane's `--with-<lane>`), and a first install from a clean account.

## Where things are

- Pins: the top of install.sh, and each side lane's installer (install-*.sh); switch-model.sh reads them.
- Lanes: lanes.toml; vLLM lanes share vllm-lane.sh; all lanes share lanes.sh.
- The proxy's real version: the `v6.x` header of keepalive-proxy.py.
- The story of every change: CHANGELOG.md and `git log`. Code comments say why the code is as it is
  NOW; the history of how it got there belongs in the commit and the CHANGELOG, not in the comment.

## Workflows (skills)

- Adding a lane or a target: `/new-lane`. Measuring a lane: `/qualify-lane`. Releasing: `/release`.
- Reviewing before a release: the `repo-reviewer` subagent, on the whole diff since the last tag.
