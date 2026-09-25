# Mirroring the pins

The pins are the contract: a revision or digest that upstream deletes turns
every future install into a failure on the machines that do not hold the
bytes yet (the pin-watch workflow sounds the alarm daily). A mirror is the
extinguisher. This file is the runbook; `./mirror-pins.sh` with no argument
prints the plan it executes, and reads the license table below to decide what
it may copy.

## The license check comes first, and a script does not get to answer it

Redistributing a checkpoint is permitted or it is not, and "it is on public
Hugging Face" is not a license. Before a repo earns a row in
`mirror-pins.sh`'s model list, a human reads its license (and its
provenance note when the repo is derived, which abliterations and quants
are), and the conclusion goes into the table below with the date it was
checked, as `(YYYY-MM-DD)`. The script mirrors a checkpoint only when its row
here says something other than "not yet checked" and carries that date: an
unchecked repo stays unmirrored; the mirror is not a backup of whatever
happens to exist.

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
| qwen-image | Qwen/Qwen-Image-2.1 | not yet checked |
| images (2) | lmsysorg/sglang@sha256:... | copied whole by digest: the Apache-2.0 SGLang images carry their license inside; keep it there |

No row is a conclusion until this table says one, with a date.

`check-pins.sh` also watches three pins this script does not copy, since they live on
GitHub and PyPI rather than on Hugging Face or a registry: the image lane's SGLang commit
and its `sglang` wheel, and opencode's release asset with its digest.

## Where the mirror lives

Decided once, recorded here (it belongs to the project, not to one
account's home directory): an org on Hugging Face, controlled by more than
one person where possible, and one registry namespace (`MIRROR_REGISTRY`,
GHCR works) for the images. Until the org exists, `mirror-pins.sh` runs
plan-only and nothing in this repo pretends otherwise.

## How, once the decision exists

```bash
python3 -m venv .venv-mirror && .venv-mirror/bin/pip install huggingface_hub  # the script uses this venv when it exists
export HF_TOKEN=hf_...            # a write token scoped to the mirror org
export HF_MIRROR_ORG=dgx-spark-qwen38-mirror
./mirror-pins.sh --models           # checkpoints: resumable, skips pins already tagged on the mirror
docker login registry.example.com   # the registry MIRROR_REGISTRY names
export MIRROR_REGISTRY=registry.example.com/dgx-spark-qwen38
./mirror-pins.sh --images           # images: copied whole by digest, then asked for by digest
```

Each checkpoint gets a repo of its own on the mirror, named after its owner and
its name (`RadixArk__Qwen3.8-Flash-Next-NVFP4`: two owners publish a checkpoint
of that name), and the pinned revision becomes the tag `upstream-<revision>`.
A commit id cannot be carried over, since the mirror's commit is a new one: the
tag is what names the pin there: a re-pin to the mirror sets that pin's
`_REPO` line in install.sh to the mirror repo and its `_REV` line to the tag. After the upload the
script compares the tag with the upstream revision file for file (name, size,
content hash) and fails on any difference; a pin already tagged is compared
and skipped, which makes the run resumable.

An image pinned by digest is a multi-platform index (the two pinned here
each list amd64 and arm64 manifests). A `docker pull` and `docker push` carry
only the platform of the machine that ran them, under that platform's digest,
so an install pinned to the index digest would not find it on the mirror.
The script copies the index whole with `docker buildx imagetools create`, then
asks the mirror for the pinned digest and fails when it does not answer it.

Cost honesty: the ten checkpoint pins are 546 GB at their pinned revisions
(measured 2026-09-24: 106 GB for the four 27B checkpoints, 403 GB for the
three flash ones, 4 GB for the two drafters, 33 GB for Qwen-Image), downloaded and uploaded again
from whatever machine runs this, once to seed and once per re-pin. A cheap
cloud box with fast egress does the seeding in hours instead of days, and the
plan above is what you hand it.

## After the first seed

- The re-pin ritual (CHANGELOG history) gains one step: mirror first, then
  point `install.sh` at the surviving revision; the mirror never replaces
  a pin, it only makes a re-pin optional when upstream dies.
- `check-pins.sh` grows an optional `--mirrors` mode: ask the mirror what
  it holds, and the pin-watch issue body says which side failed.
- The ROADMAP line moves from "in progress" to shipped when this table has
  real license conclusions and one real mirrored byte.
