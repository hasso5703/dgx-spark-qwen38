# Roadmap

Where this is going, written so that a contributor does not have to ask, and
so that "on the roadmap" means a checkbox with a definition of done, not a
mood. Statuses: shipped, in progress, planned, considered (considered items
have a reason they are not yet planned, stated here or in an issue).

## Shipped, current: v1.14.0

Two lanes (27B NVFP4/FP8 with DFlash2 drafting; 176B flash-next with NEXTN
and the PLE table on NVMe), seven switchable targets, the cockpit, the
keepalive proxy with its byte guards and the corruption tripwire, 1M and
native context presets, four reasoning-effort levels with `lean` as the
default, opencode out of the box, and the frozen benchmark battery (v1) that
keeps every number in this repo comparable across years. **Both lanes now
serve an official upstream image and this repo builds none by default**, which
is the end of a long habit of carrying vendored engine files. The flash lane's
old overlay (`OVERLAY_FLASH=1`) was retired in v1.18.7: the launcher of v1.8 on
did not fit its image and nothing had booted the pair since, so its rollback is
the release that shipped it whole, v1.7.2.

## Shipped this cycle: v1.13.0 and v1.14.0

`lean`, a fourth reasoning-effort level measured on 7,008 runs and made the
default: 0.71x the thinking tokens of `medium` with no detectable quality
change, and 0.047x of the shipped `xhigh` on the underspecified requests that
make this model spiral.

Then the 27B lane moved to the official `v0.5.19` release and its eight-file
overlay was deleted. Both reasons it had stayed behind were retired by
measurement rather than by restating them: the overlay carried nothing
upstream lacks (diffed function by function), and the sm_121 objection turned
out not to depart the two images at all, since the one this lane served
shipped the same sm90 and sm100 kernels, byte for byte. The memory fraction
moved to 0.76 because the official image claims a smaller static budget for
the same number.

## Shipped, v1.11

Proxy v6.16 (optional TLS, optional per-client identity), `--enable-metrics`
on every lane (a re-run and a restart carry it onto existing boxes), the
governance layer (SECURITY, CONTRIBUTING, ARCHITECTURE, ROADMAP, the
pin-watch workflow, the issue templates) and the v6.16 client docs. The
metrics flag is live on a box once it has been re-installed and restarted;
the cockpit drift panel says so, per lane, until it is.

## In progress
- **Mirrors of every pin** (planned next): the checkpoints, the draft and
  the base images mirrored into an org this project controls, and
  `MIRROR.md` as the runbook, so a deleted upstream revision is an
  inconvenience instead of the end of an installation path. The pin-watch
  workflow is the alarm; this is the extinguisher. Needs infrastructure
  decisions (which org, where the images live, what it costs) and they are
  not made on vibes: the first mirror is the one this repo would use on a
  fresh install.

## Planned

- **Beyond one box, in that order: more GB10 machines, then more hardware,
  then more models.** What this repo is, today, is a production serving stack
  for one specific machine, and every number in it says "reference box" for
  that reason. The order matters because each step buys the next one: a
  GB10 matrix with community rows proves the recipes survive other people's
  OEMs and kernels; hardware beyond GB10 is only honest once the recipes are
  parameterised by what the card can do, not by what this one does (the
  sm_121 story in v1.14 is the shape of that work: an objection that turned
  out to be about neither image); and a second model family earns its lane on
  the frozen battery, not on a README claim. None of this is a rewrite, it is
  the same discipline applied to a wider set of pins, and nothing ships as
  "supported" before a measured row exists for it.

- **A supported-GB10 matrix beyond the reference box**: every performance
  claim carries "reference box", and the box-report protocol plus
  `bench-matrix.sh` battery v1 exist to widen that with community numbers
  that name their conditions. Planned shape: a machine table in
  BENCHMARKS.md with OEM, OS and battery JSON per row, one row per
  verifiable report.
- **Podman and quadlet path**: the units are plain systemd; a quadlet
  render of the same pinned commands (same digests, same flags, CI-rendered
  and byte-checked like the units today) so containers-first fleets get the
  same pins without a Docker install. Considered first on a box that is not
  the reference one, because it must not be able to drift from the Docker
  path.
- **Per-client limits on the identity layer**: the v6.16 key map names who;
  the honest next step is per-label ceilings and a journal-derived quota,
  designed as an issue with measured motivation, not bolted on.
- **A cockpit panel that reads the Prometheus endpoint** now that it exists
  on every lane: the panel's own canary stays the source of truth for
  "is this lane alive", the metrics join as history, not as a second
  opinion about life.

## Considered (and why not yet)

- **Multi-engine routing (vLLM lanes)**: third-party vLLM numbers on this
  hardware are honest and public (issue #2, BENCHMARKS.md), and they tie or
  lose to the SGLang config here on the workloads this repo serves; adding
  an engine multiplies every gate and every pin, and the burden of proof is
  a measured win on the battery, not a second flavor of parity.
- **A GUI for anything the cockpit does not already do**: the cockpit is
  what an operator needs (state, switch, logs, bundles); new surfaces have
  to earn their maintenance cost the way features do.
- **Hosted or telemetry components**: none, by design (SECURITY.md); the
  telemetry question is answered by the Prometheus endpoint staying local.
- **GGUF fine-tune serving**: `extras/gguf/README.md` carries the
  measured llama.cpp numbers and the sharp edges; a full lane would compete
  with SGLang on the battery before earning a lane, per issue #12.

## Maintenance promises, in the open

- Pins are contracts; `./check-pins.sh` is the daily alarm; a dead pin is
  fixed by a measured re-pin, with the story in the CHANGELOG.
- A contributor's measured number in a box-report issue is a contribution
  to BENCHMARKS.md as soon as its conditions are pinned, credited as such.
- Breaking changes to the serving contract (ports, ceiling chain, the
  closed outcome vocabulary, the recipes format) are issues first, PRs
  second, releases third; the CI gates are the tiebreaker, not taste.
