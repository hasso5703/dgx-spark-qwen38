# Mirroring the pins

The pins are the contract: a revision or digest that upstream deletes turns
every future install into a failure on the machines that do not hold the
bytes yet (the pin-watch workflow sounds the alarm daily). A mirror is the
extinguisher. This file is the runbook; `./mirror-pins.sh --dry-run` prints
the plan it executes.

## The license check comes first, and a script does not get to answer it

Redistributing a checkpoint is permitted or it is not, and "it is on public
Hugging Face" is not a license. Before a repo earns a row in
`mirror-pins.sh`'s model list, a human reads its license (and its
provenance note when the repo is derived, which abliterations and quants
are), and the conclusion goes into the table below with the date it was
checked. An unchecked repo stays unmirrored; the mirror is not a backup of
whatever happens to exist.

| pin | source | license conclusion (date checked) |
|---|---|---|
| stock | RadixArk/Qwen3.8-27B-NVFP4 | not yet checked: derive from Qwen license, verify redistribution terms |
| unc | edp1096/Huihui-...-abliterated-NVFP4 | not yet checked: derive of a derive, both layers must pass |
| fp8 | Qwen/Qwen3.8-27B-FP8 | not yet checked |
| uncfp8 | edp1096/Huihui-...-FP8 | not yet checked |
| flash | RadixArk/Qwen3.8-Flash-Next-NVFP4 | not yet checked |
| flash-nvda | nvidia/Qwen3.8-Flash-Next-NVFP4 | not yet checked |
| flash-unc | dealignai/...-ABLITERATED-NVFP4 | not yet checked: verify both layers |
| draft | RadixArk/Qwen3.8-27B-DSpark | not yet checked |
| draft2 | maurienne-ai/Qwen3.8-27B-DFlash2-NVFP4-RTNcal | not yet checked: verify both layers |
| images (4) | lmsysorg/sglang@sha256:... | pull-by-digest, retag, push: the Apache-2.0 SGLang images carry their license inside; keep it there |

No row is a conclusion until this table says one, with a date.

## Where the mirror lives

Decided once, recorded here (it belongs to the project, not to one
account's home directory): an org on Hugging Face, controlled by more than
one person where possible, and one registry namespace (`MIRROR_REGISTRY`,
GHCR works) for the images. Until the org exists, `mirror-pins.sh` runs
plan-only and nothing in this repo pretends otherwise.

## How, once the decision exists

```bash
python3 -m venv .venv-mirror && .venv-mirror/bin/pip install huggingface_hub  # the tool the script drives
export HF_TOKEN=hf_...            # a write token scoped to the mirror org
export HF_MIRROR_ORG=dgx-spark-qwen38-mirror
./mirror-pins.sh                    # models: resumable, skips revisions already mirrored
./mirror-pins.sh --images           # images: docker pull by digest, retag, push
```

Images pulled **by digest** cannot silently drift: the digest is the
identity, and what lands on the mirror has the same digest by construction
(verify once per image with `docker inspect --format '{{index .RepoDigests 0}}'`).
Model revisions are re-uploaded at the same revision id; the script treats
"the mirror already answers that revision's `config.json`" as done, which
makes the whole run resumable across interruptions and re-runs.

Cost honesty: the model pins are ~150 GB of downloads and the same again
in uploads, from whatever machine runs this, once per re-pin and once to
seed. A cheap cloud box with fast egress does the seeding in hours instead
of days, and the dry-run plan above is what you hand it.

## After the first seed

- The re-pin ritual (CHANGELOG history) gains one step: mirror first, then
  point `install.sh` at the surviving revision; the mirror never replaces
  a pin, it only makes a re-pin optional when upstream dies.
- `check-pins.sh` grows an optional `--mirrors` mode: ask the mirror what
  it holds, and the pin-watch issue body says which side failed.
- The ROADMAP line moves from "in progress" to shipped when this table has
  real license conclusions and one real mirrored byte.
