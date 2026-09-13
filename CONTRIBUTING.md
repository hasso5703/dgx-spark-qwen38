# Contributing

This repo is a serving stack that has to keep working on other people's
machines, and a measurement record that has to stay comparable across years.
Both halves are CI-enforced, so contributing here is mostly about keeping the
gates true. Everything below is what a maintainer will check, and what
breaks a build if it is missing.

## Ground rules, short version

- **Measured or it does not ship.** A flag, a ceiling, a limit table entry, or
  a performance claim needs a run on hardware, and the numbers go in the
  commit message and (if they change a headline) in BENCHMARKS.md with their
  protocol. A single post-boot run is not a measurement: discard the first
  batch, repeat three times, compare medians (the boot lottery is real and
  killed, but box load is not).
- **Quality is lossless by construction or it is opt-in.** Anything that can
  change what the model says ships off by default; anything that only moves
  bytes per step is fair game if acceptance and the needle probe hold.
- **Nothing unpinned reaches a user.** Checkpoints pin a revision at download
  and the same `--revision` at serve; images pin digests; vendored overlay
  files pin sha256 in their manifest. `./check-pins.sh` asks the network
  whether the pins still resolve; it is deliberately not in CI (a green
  build must not depend on Hugging Face being up).
- **A test names the bug it prevents**, in the words of what went wrong. The
  CHANGELOG carries the story, the test carries the guard.

## Setting up

The runtime is stdlib only and stays that way. Three measuring gates need
pip packages, once:

```bash
python3 -m venv .venv-test && .venv-test/bin/pip install coverage hypothesis
```

Run the gates the way GitHub runs them:

```bash
./ci-local.sh                # everything, about twelve minutes
./ci-local.sh Coverage       # one gate by name fragment
```

Three quarters of the full run is the mutation gate re-running a suite once
per injected fault; run the fragment you touched while working, and the
whole thing before pushing.

## What CI holds, so you know where you will be caught

- `bash -n` and `shellcheck -S warning` on every shell script; `node --check`
  on the opencode plugin; `ruff --select F,E9` and `py_compile` on every
  Python file (new files under `tests/` are picked up automatically).
- Every `tests/test_*.py` must run standalone (`__main__` block) and CI
  counts what ran against what the files declare: a suite with tests that
  never ran once exited 0, so silence is not a pass.
- The offline suite touches nothing: it runs under a witness `HOME`, hashed
  before and after, and no listening socket may survive it.
- Drift gates that exist because each of these drifted once: the five
  independent spellings of the target list must agree (a failing run names
  the odd one out), the sudoers allowlist must match the privileged calls
  `switch-model.sh` actually makes and carry no wildcard, the pin blocks in
  `run.sh` and `switch-model.sh` must still select every name they use, the
  opencode limits table must satisfy the ceiling arithmetic, every serving
  lane must carry `--sleep-on-idle`, and the proxy's outcome vocabulary is
  closed (adding an outcome fails CI until someone says what it means).
- House typography: no em or en dashes anywhere tracked. Use a comma, or
  restructure the sentence.
- Coverage floors only ever rise, and they are set from the most constrained
  environment (a GitHub runner has no docker, no systemctl, no GPU).

## Adding a target model, the honest checklist

Adding a model touches more places than one, which is why every one of them
is CI-checked: the case in `install.sh` and its pins, `switch-model.sh`'s
guard and picker label, `run.sh`'s map if it is servable without systemd,
`oc-limits.sh`'s limits table with a stated reason for every number, the
cockpit registry, and `BENCHMARKS.md` with the boot-protocol numbers.
The fastest way to learn the full list is to add the model and read what CI
fails on; every gate names its own odd one out. A target that changes
quality (quantization, abliteration) needs the reasoning written down the
way the existing abliterated entries do: chosen on verified shard
equivalence, not on popularity.

## Commit style

`area: claim`, subject under ~70 columns, body as a short story: what was
wrong, how it was measured, what the fix is, what now holds the line.
The last line is the receipt (a test file, a CI gate, a live measurement).
Releases are one commit that bumps `CHANGELOG.md` and the tags are cut from
those commits; keep your PRs out of that business, the maintainer writes the
release notes from your commit messages, which is why they carry the story.

## If you are not sure

Open an issue first (feature request, or a box report with your
`bench-matrix-<label>.json` attached). A PR that surprises a maintainer is
a PR that needs a second opinion from you, and both of you would rather
have had the conversation first.
