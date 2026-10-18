# Cloud review, 2026-10-18

## Read this first: who wrote this and from where

This review was made by a Claude Code session running in the cloud, not on the DGX Spark.
It had no access to the box, no GPU, no Docker daemon, no systemd as PID 1, and none of the
local agent's memory files. It read the repository at v1.36.1 (commit `4b95698`) and ran what
it could offline. So nothing here is a measurement from the production machine, and nothing
here was seen serving on a GB10. Where a claim could be proven by reading the code, it says
so; where it was measured in the cloud container, it says that instead, and names the
container; where it is a guess that only the box or the local agent can settle, it is marked a
supposition. Please read the status on each finding before acting on it, and correct or
discard anything that does not hold on the real machine. The local agent knows this box and
its history; this session does not.

The work is on the branch `claude/blissful-tesla-9pil8a`, with a draft pull request. Nothing
was pushed to `main`; the CHANGELOG and the version numbers were not touched.

The box's own agent gave a first reading the same day, read-only (nothing run from this
branch, no lane touched). What it confirmed, contested or settled is folded into each finding
below, marked "local agent". It also noted that its pre-push checks refuse commits authored
by Claude or carrying a Claude-Session line, as every commit of this branch does: they are to
be re-authored (or squashed) at merge, which the content allows (the branch merges on v1.36.2
without conflict).

## The status tags

Each finding carries one:

- **VERIFIED IN CODE**: provable by reading this repository (or a pinned upstream read at its
  tag). No machine needed.
- **MEASURED HERE**: measured in the cloud container described below. The number is real, but
  the container is not the box: a different architecture, Python, and ffmpeg, no GPU.
- **SUPPOSITION**: a reasoned guess. It needs the box, the local agent, or both to confirm.

A commit marked `[to validate on the box]` in its subject changed an engine unit or systemd
and has never run on a real machine. Its body names what to check before merging.

## The container this ran in

- x86_64, not aarch64. 4 CPUs, about 16 GiB RAM, Ubuntu 24.04.5.
- Python 3.13.16 (the box's DGX OS has 3.12). Python 3.14.6 was fetched to check one finding.
- PID 1 is not systemd; a firecracker init is. No Docker daemon, no GPU, no `nvidia-smi`, no
  `ss` (iproute2 was absent; installed once to check a finding, then removed).
- About 29 GB of writable disk free.
- Ran as `root`; the non-root suites were run as the user `ubuntu`. `GITHUB_TOKEN` is exported
  in the environment.
- Outbound HTTPS goes through a proxy; the Hugging Face API and GitHub raw were reachable.

## What ran, and what it showed

- The offline Python suites, as the user `ubuntu` under a throwaway HOME and TMPDIR. All the
  proxy suites named below pass. The twelve failures seen as a non-root user are environmental
  (the container's disk, its missing `ss`, the exported token); three of them pointed at test
  hygiene defects, now fixed (F4, F5, F6).
- Python 3.14.6 compiled all 253 tracked Python files; before F3 it warned on one
  (`keepalive-proxy.py`), after it warns on none.
- `systemd-analyze verify` and `systemd-analyze security --offline` read the rendered proxy
  unit (systemd 255). A nested systemd, booted in its own pid and mount namespaces in the
  container, started the sandboxed proxy against a fake engine and served it (F8, F9).
- `ffmpeg` 6.1.1 decoded real audio of six formats and refused the crafted uploads (F11).
- The Hugging Face API served the config files of the 27B targets and drafters (F10).
- Local CI steps through `ci-local.sh`: the template, anti-drift, pin, typography, shell and
  ruff steps relevant to these changes pass as the user `ubuntu`.

## Branch map

On top of `4b95698` (v1.36.1):

| Commit | Subject | Status |
| --- | --- | --- |
| `e5859d9` | proxy: the identity wall reads the Bearer scheme in any case | offline, tested |
| `cc7b773` | proxy: a caller's x-override-* headers stay at the door, but the rid | offline, tested |
| `948d191` | proxy: the pump's finally ends without a return | offline, tested |
| `186ab19` | tests: the check-pins suite leaves the caller's GITHUB_TOKEN out | offline, tested |
| `9dde440` | tests: the switch suites bring their own df | offline, tested |
| `8ca4b8c` | tests: the suite runs on a machine without ss | offline, tested |
| `41b6dbe` | engines: the config directory is mounted read-only | box-validate |
| `1e4a571` | proxy unit: no more of the machine than the proxy uses | box-validate |
| `af8d8c7` | proxy unit: MemoryMax holds the bodies it accepts | box-validate |
| `abf9a8a` | 27b: no --trust-remote-code | box-validate |
| `1b489db` | speech: ffmpeg reads the upload as a sound file and nothing else | offline, tested |

The box-validate commits are the four that touch an engine unit or systemd. Validate them on
the machine before merging, in the order of the protocol below.

## Findings

### F1. The identity wall read the Bearer scheme case-sensitively

- Status: VERIFIED IN CODE, MEASURED HERE. Commit `e5859d9`.
- Where: `keepalive-proxy.py`, `_label_peer`.
- What: when `QWEN38_CLIENT_KEYS_FILE` is set, a listed key sent as `bearer <key>` (lower
  case scheme) got the wall's 401, where the engine admits the same token. RFC 9110 reads the
  scheme case-insensitively, and so does the engine behind the wall; only the wall did not.
- Fix: read the scheme case-insensitively, keep the token as sent.
- Box (local agent): confirmed; the identity wall is not enabled on this box, so the fix is
  latent there: no effect today, and the right reading the day a keys file is set.

### F2. A caller's x-override-* headers reached the engine

- Status: VERIFIED IN CODE (proxy and SGLang v0.5.21 source, read at the tag). The effect on
  this box's engines is a SUPPOSITION. Commit `cc7b773`.
- Where: `keepalive-proxy.py`, `_hdrs`; the SGLang lanes start their engine with
  `SGLANG_ENABLE_REQUEST_HEADER_OVERRIDES=1`.
- What: that switch makes the engine apply every override header it knows, from whoever sends
  it. In v0.5.21 (`python/sglang/srt/entrypoints/request_headers.py`): x-override-rid,
  -bootstrap-host, -bootstrap-port, -bootstrap-room, -conversation-id, -routed-dp-rank,
  -disagg-prefill-dp-rank, -priority. The proxy relayed every client header but the
  hop-by-hop ones, so a client of the port could set the scheduler's priority, the
  data-parallel rank and the disaggregation fields of its own request. None is a client's to
  set. The proxy sets x-override-rid for its own tracing, so that one is kept.
- What I could not measure: the real effect of those fields on this box's single-GPU,
  non-disaggregated engines.
- Box (local agent): the defect is confirmed in the code, the effect contested: on this box the
  headers do nothing (priority scheduling off, a single rank, no disaggregation). The fix is to
  be taken as hygiene, for the day one of those is turned on.
- Fix: drop the client's x-override-* at the door by prefix, except the rid.

### F3. A return in a finally block (PEP 765)

- Status: VERIFIED IN CODE, MEASURED HERE. Commit `948d191`.
- Where: `keepalive-proxy.py`, the relay's pump thread.
- What: the pump ended its finally with a `return` when it had drained an abandoned answer. A
  return in a finally discards the exception that brought the thread there. Python 3.14 says so
  at compile time (PEP 765): 3.14.6 prints `SyntaxWarning: 'return' in a 'finally' block` at
  every start of the proxy. The box runs 3.12, which is silent. An AST scan of all 253 tracked
  Python files found this as the only such jump.
- Fix: an if and its else, same effects. A new gate (`tests/test_no_jump_in_finally.py`) walks
  every tracked file and is stricter than 3.14.6 (it also catches a return in a loop of the
  finally, which the compiler does not warn on).

### F4. A test inherited the caller's GITHUB_TOKEN

- Status: MEASURED HERE. Commit `186ab19`.
- Where: `tests/test_check_pins.py`, `run_pins`.
- What: the harness handed `check-pins.sh` the whole environment of the shell that ran the
  suite. `check-pins.sh` sends `GITHUB_TOKEN` to the GitHub API when it is set, so in a shell
  that exports one (a developer's, for gh; this container's), the case that is meant to send
  no token sent one, and `test_no_token_sends_no_authorization` failed on the box's own code.
- Fix: drop `GITHUB_TOKEN` from the environment the harness passes, the one variable
  `check-pins.sh` reads (this report first said `GH_TOKEN` too; the local agent caught it, and
  `GITHUB_TOKEN` alone is enough). The token tests set theirs explicitly and are unchanged.
  Verified both ways here.
- Box (local agent): F3 to F6 confirmed, hygiene with no effect on the box.

### F5. Two tests read the machine's real free disk

- Status: MEASURED HERE. Commit `9dde440`.
- Where: `tests/test_switch_fetch_only.py`, `tests/test_switch_model_cache.py`.
- What: they ran `switch-model.sh` with stubbed docker and systemctl, but with the machine's
  own `df`. The room check before a download asks about 45 GB; on this 29 GB disk six tests
  failed with "29 GB free, about 45 GB needed". The room check is not what they test, and it
  has its own suite (`tests/test_switch_room.py`) with a df stub.
- Fix: give them the same df stub, a disk with 500 GB free.

### F6. The suite needed `ss` from the host

- Status: MEASURED HERE. Commit `8ca4b8c`.
- Where: `tests/suite_reaper.py`; `tests/test_install_units_only_run.py`.
- What: `suite_reaper.py` named a leftover process's listeners with `ss -Htlnp`. On a machine
  without iproute2 it died there on a traceback, before it stopped the leftover, so each test
  of it that leaves a listener waited its full 60 s timeout on a pipe the leftover held (3
  errors, 181 s measured here). Separately, the units-only install run used the host's `ss`,
  which the preflight requires.
- Fix: the reaper reads the same listeners from `/proc` when `ss` is missing (socket
  descriptors, then `/proc/net/tcp` and `tcp6`, spelled as `ss` spells them); with `ss`
  present nothing changes. The install test gets an `ss` stub. Measured here: 181 s and 3
  errors before, 6 s and clean after, without iproute2; and again with iproute2 installed so
  the `ss` path runs too.

### F7. Engine containers mounted the config directory read-write

- Status: VERIFIED IN CODE, plus a box check. Commit `41b6dbe` `[to validate on the box]`.
- Where: the 27B unit and its 1M unit, the flash launcher, Kolibri, both Nemotron units, and
  `run.sh`: each mounted `~/.config/qwen38` at `/out` read-write, as root in its container.
- What: the engines only read there (a chat template, engine-secrets.yaml, the flash draft's
  token map, the vLLM lanes' api-key). But the directory also holds what systemd runs as this
  user before or as a lane starts (engine-preflight.sh, engine-secrets.sh, launch-flash.sh,
  launch-speech.sh). Code that ran in an engine, a checkpoint's own or a flaw of the engine's,
  could rewrite those and run as the box's user at the next start, which a crash brings on
  (Restart=always). The image and video lanes were already closed to this (a read-only tmpfs,
  2026-10-16 and 17); this does the same for the docker lanes with `:ro`. The draft vocabulary
  job in `lanes.sh` keeps its write access, which it needs. That job writes as root: unlike
  its siblings in `switch-model.sh`, `prepare-lane.sh` and `vllm-lane.sh` it passes no
  `--user` (the first version of this report and of its test said "as this user"; the local
  agent corrected it). Giving it `--user` would be a tidy-up, but a box whose map is already
  root-owned would then fail to write the next one, so it is left as is, and named here.
- Box (local agent): plausible; no engine has ever written in `~/.config/qwen38`.
- What I could not do: start any engine with the read-only mount. No Docker, no GPU here.
- Box: see the protocol. In short, each lane should serve with `/out` showing read-only in
  `docker inspect`, and no "Read-only file system" in the journal.

### F8. The proxy unit ran with all of this user's rights

- Status: VERIFIED IN CODE, MEASURED HERE (nested systemd), plus a box check. Commit `1e4a571`
  `[to validate on the box]`.
- Where: `qwen38-keepalive.service.template`.
- What: the proxy is what a client's bytes reach first, on every address the box has
  (PROXY_BIND is 0.0.0.0 by default), and its unit ran it with all of this user's rights: the
  docker group, with docker's socket one connect away, and the cockpit's sudo commands. A flaw
  of the proxy was a flaw of the box. The unit now runs it with NoNewPrivileges, ProtectSystem
  strict, ProtectHome read-only, a private /tmp and /dev, the kernel protections, no
  capabilities, SystemCallFilter @system-service (EPERM, not kill, for the rest), four address
  families, and docker's socket made inaccessible.
- Why the lines hold: the proxy reads its code, its key, and what the operator names, runs
  `systemctl` to read the lanes' state, and writes nothing on disk. A gate
  (`tests/test_proxy_unit_sandbox.py`) checks those in its code, so a proxy that needs more
  says so there and in its unit.
- Measured here: `systemd-analyze security --offline` rates the unit 1.8 (OK), from 9.0
  (UNSAFE). Under a nested systemd in the container, the rendered unit started and served a
  fake engine's models list and a chat completion, refused a request without the key, and a
  probe run under its exact lines could not write in the home or /etc, could not reach a socket
  at /run/docker.sock, and could not sudo (CapEff 0, NoNewPrivs 1, Seccomp 2). Not run on DGX
  OS, not with the real lanes, not with TLS or the client-keys file.
- Box (local agent): confirmed in the code (the proxy runs only systemctl and writes
  nothing); to validate by restarting the production proxy.

### F9. MemoryMax was below what the proxy holds at once

- Status: MEASURED HERE, plus a box check. Commit `af8d8c7` `[to validate on the box]`.
- Where: `qwen38-keepalive.service.template`, MemoryMax.
- What: BODY_BUDGET_BYTES (384 MiB) bounds the bytes of the bodies in flight, but a body costs
  the proxy about four times its bytes while the guards work on it (the reasoning_effort route
  parses it and writes it again). Measured here on the real proxy as a process: one body of
  250 MiB, under MAX_BODY_BYTES, with reasoning_effort "lean", took the proxy from 32 to 1034
  MiB. Under the unit's own MemoryMax=1G with MemorySwapMax=0, run by systemd 255, the kernel
  killed the proxy for that one body, with every stream in flight; the client got an empty
  reply. MemoryMax is 2G now, which holds the budget at the measured rate with room to spare.
  The client-facing limits are unchanged.
- A gate (`tests/test_proxy_memory_ceiling.py`) measures the rate on the machine it runs on and
  fails when the idle proxy plus the budget at that rate is over nine tenths of MemoryMax.
- Box: the rate on aarch64 and Python 3.12 may differ from the 4x measured here; run the gate
  on the box. If 2G is unwanted, the other direction is to keep 1G and bring MAX_BODY_BYTES and
  BODY_BUDGET_BYTES down to what 1G holds (about 200 MiB in flight), which clients would see.
- Box (local agent): important; it contradicts the sizing of 2026-10-16 (bodies up to four
  times costlier in memory than assumed). To measure on the box; 2G or smaller bodies is the
  operator's choice.

### F10. The 27B lane offered to run a checkpoint's own code

- Status: VERIFIED IN CODE (Hub configs read at the pinned revisions), plus a box check.
  Commit `abf9a8a` `[to validate on the box]`.
- Where: the 27B unit, its 1M unit, and `run.sh`: `--trust-remote-code`.
- What: the flag lets the engine import the Python a checkpoint names in its config
  (auto_map), as root in its container. Nothing this lane loads has any. At their pinned
  revisions, RadixArk/Qwen3.8-27B-NVFP4 (`52d1adc5`), Qwen/Qwen3.8-27B-FP8 (`017b9c7a`), both
  abliterated builds (`603028a1`, `21565d38`), and the DFlash2 drafter it serves (`bd7a9342`)
  name no auto_map and ship no Python file; their architectures are SGLang's own. So the flag
  ran nothing, and would have run whatever a later pin, or a target chosen by hand, brought.
  The one checkpoint of this lane that does carry code, the DSpark drafter, has not been served
  since v1.2. The Nemotron lanes keep the flag; their checkpoints need it and their docs say
  so.
- Box: render the 27B unit again and start the lane, native and 1M, with each target the
  cockpit offers; each should load and answer with speculative decoding on, and log no
  trust_remote_code error.
- Box (local agent): confirmed on the box's cache: no code and no auto_map in the four 27B
  targets and the two drafters it holds. To validate by starting the lane.

### F11. ffmpeg read a speech upload as a manifest

- Status: VERIFIED IN CODE, MEASURED HERE (ffmpeg 6.1.1). Commit `1b489db`.
- Where: `speech-server.py`, `decode_ffmpeg`.
- What: the lane ran `ffmpeg -i <uploaded file>` with no restriction on the input demuxer, and
  ffmpeg chose the demuxer from the bytes. Some demuxers read the file as a playlist or
  manifest that names somewhere else: measured here with the container's ffmpeg 6.1.1, a
  crafted upload made ffmpeg issue an outbound HTTP request to an address the sender chose (a
  request from the box, target of the sender's choosing), and another made it read a second
  local file as this user. The lane binds
  loopback by default and sits behind the cockpit's login, but SPEECH_BIND widens it, and an
  upload should never steer ffmpeg off its own bytes.
- Fix: pin the input format to the sound containers (`-format_whitelist`) and the protocols to
  file and pipe (`-protocol_whitelist file,pipe`). Measured here: real WAV, MP3, FLAC, m4a, ogg
  and webm each still decode; the crafted uploads are refused before ffmpeg opens anything.
- Box (local agent): confirmed in part with the box's own ffmpeg: the eleven audio formats it
  tried decode, and the ffconcat read of a second file is blocked. Its build already refused the
  DASH and HLS manifests that fetch over HTTP, so on the box the fix closes the ffconcat read,
  and keeps the HTTP fetch closed under a later ffmpeg build that would accept them.

## Checked and found sound

Each of these was looked at and needs no change, as far as this session could tell:

- The cockpit's Content-Security-Policy, and its text rendering: the one `innerHTML` (in
  agent-mobile.js) writes escaped text.
- The static file handler's path-traversal guard.
- CSRF on the cockpit, with SameSite=Strict session cookies.
- The cockpit login's rate limit, and its constant-time credential compares.
- The sudoers allowlist: exact-argv, no wildcards, visudo-checked, and every cockpit action it
  covers uses a closed enum and an argv list.
- The pyspy wrapper.
- The opencode limits merge helper writes atomically with backups.
- The relay's origin and header hygiene.
- `vllm_engine_auth` (the vLLM lanes' key middleware).
- `opencode-web.env` is created mode 0600 and holds the opencode server password.
- Secrets are kept off command lines (the argv-secret gate), and the TLS key and the engine key
  do not appear in logs.

## Validation protocol for the box-validate commits

Do these on the box, in this order. Stop and report if any step surprises you.

1. `abf9a8a` (27B, no trust-remote-code). Render the 27B unit again (the installer's
   convergence), start the lane native, then 1M, with each target the cockpit offers. Each
   should load, answer the models list and one generation with speculative decoding on, and log
   no trust_remote_code error.
2. `41b6dbe` (read-only config mount). Render the units again, start each installed text lane
   (27B, 1M, flash with its token map, Kolibri, Nemotron Bolt, Nemotron Super VL). For each,
   `docker inspect <container> --format '{{range .Mounts}}{{.Destination}} {{.RW}} {{end}}'`
   should show `/out false`; the lane should serve the models list and one generation; the
   journal should show no "Read-only file system". A vLLM lane should still refuse a request
   without the key.
3. `1e4a571` (proxy sandbox). Render the unit again, restart the keepalive proxy, run the
   agents and the cockpit against it for a while. Check that the hold still sees a lane come
   back (restart a serving lane while a request waits), that TLS (QWEN38_TLS_CERT) and the
   client-keys file still read, and that `journalctl -u qwen38-keepalive` has no EPERM,
   "Permission denied", or "Read-only file system". `systemd-analyze security qwen38-keepalive`
   gives the live rating.
4. `af8d8c7` (MemoryMax 2G). Run `tests/test_proxy_memory_ceiling.py` on the box (the 4x rate
   on aarch64 and 3.12). Render the unit again, restart the proxy;
   `systemctl show qwen38-keepalive -p MemoryMax` should give 2147483648.

## Open questions for the local agent

These are things this session could not settle without the box or your memory:

1. **Is the identity wall used?** Settled by the local agent: not on this box. F1 is latent.
2. **The x-override-* fields (F2).** Settled by the local agent: inert on this box (priority
   off, one rank, no disaggregation). F2 is hygiene.
3. **The real PROXY_BIND.** The default is 0.0.0.0. Does the reference box bind the proxy to a
   tailnet address, to loopback, or to all interfaces in practice? F8 and F9 matter more the
   wider it binds.
4. **Config directory mode.** `~/.config/qwen38` is made with the default umask (0755 on most
   boxes), and `installed.json` and `api-key` under it. The api-key is written umask 077
   (0600), but is the directory itself readable by other local users on this box, and does that
   matter here? A supposition: 0700 on the directory would be tidier, but this session does not
   know the box's user model.
5. **`--yolo`.** `install.sh` tells the operator to launch opencode with `--yolo`
   (auto-approve), and says the reference box runs that way. Is that the intended posture for
   the Agent surface, given the sandboxing the opencode unit already has?
6. **Why 256 MiB for MAX_BODY_BYTES?** The comment says about 100x the largest legit text
   request on the 1M lane, with room for vision payloads. Is 256 MiB (and the 384 MiB budget)
   still the right size given the memory cost measured in F9?
7. **The update channel.** `get.sh` fetches and runs `main` (not a release tag). The local
   agent adds the sharpest reason to look at it: `main` is installable the moment it is pushed,
   so for the hour GitHub's CI takes, a one-liner install takes a `main` CI has not validated.
   See the proposal on a release channel below.

## Making the next model cheap to add

The question behind most of what follows: how to make adding a model, testing it and trusting
it an afternoon instead of a campaign. Measured on this branch's tree by grep, outside the
CHANGELOG and the docs: the newest lane, Nemotron Super VL, is named in 65 code files, 481
times, and the file name of its unit alone in 40 (every other unit's Conflicts= and After=
lines, the proxy's hold list, the cockpit's unit tables, the sudoers allowlist, the
uninstaller, the tests). Each of those is a place the next lane has to be added to by hand,
and several CI gates exist because such copies drifted before ("One list of targets, in all
four places that must agree"). The proposals below move that cost from the lane's author to
the repository. They are proposals, in a suggested order: each is larger than one safe commit,
and the owner and the local agent decide.

1. **One declarative lane registry.** One entry per lane: its id, unit and container names,
   image and checkpoint pins, ports, memory and GPU share, served name, prompt ceiling, the
   lanes it conflicts with, and the flags its recipe was measured with. `install.sh`,
   `lanes.sh`, `switch-model.sh`, the cockpit's `registry.py` and `recipes.py`, the
   uninstaller, the proxy's hold list and the unit templates read it, and the CI gates check
   the templates against one source instead of four copies against each other. The start of it
   is already there (`dashboard/registry.py`, `lanes.sh`, the pins block of `install.sh`): the
   step is making one of them the source and the others its readers, one consumer at a time,
   each move held by the gates that exist today.
2. **A conformance battery run over the registry.** Each property a lane must have is today a
   CI step with a hand-kept list: it parks its scheduler when idle, exports its metrics, serves
   the flags it was measured with, is in the sudoers allowlist, is known to the uninstaller,
   gets its prompt ceiling from the proxy. Run once per registry entry, a new lane gets the
   whole battery by being listed, and a missing property fails with the lane's name.
3. **A scaffold.** A `new-lane` script that writes, from a registry entry, the unit template,
   the installer stub, the docs page and the test stubs, so a lane starts from what the last
   one learned rather than from a copy of it.
4. **One qualification command, run on the box.** "Measured or it does not ship" is today a
   sequence someone knows: fetch, look for remote code in the config (F10's auto_map check),
   start, a smoke generation, the needle, the bench, the memory peak (F9's rate), the
   conformity record. `bench.sh`, `needle.sh`, `check-pins.sh` and `conformity.py` are the
   pieces. One command that runs them in order on a candidate checkpoint and writes a receipt
   would make a new model a measured candidate in one step, and the receipt the thing a review
   reads.
5. **One pins file.** The pins live in `install.sh` and the other scripts read them by grep and
   eval, which is what the "Pin contracts" step guards. A pins file that every consumer
   sources, checked against the Hub and GitHub by `check-pins.sh`, removes that class of drift.
6. **Faster CI, closer to the box.** The run profiled for this review (39074119016) took about
   81 minutes, most of it the unrun-test discovery (about 35) and the offline suite (about 22),
   which ran each file three times; v1.36.2 brought that to two, and the local agent is
   measuring a faster offline suite (a first pass at 509 s, against about 20 minutes). Parallel
   jobs would cut the wall time further; an arm64 runner would put the box's architecture in CI
   for the first time (it would have caught F3's warning class); and the box-validate class of
   change (engines, systemd) could run on demand on a self-hosted runner on the box itself,
   where it serves.
7. **A release channel for `get.sh`.** Installing the latest release tag by default, with
   `main` as an opt-in, closes the hour in which a one-liner takes a `main` that CI has not
   validated (the local agent's point), and makes what a fresh box gets a reviewed thing.

The order matters more than the list: the registry (1) is what makes the battery (2), the
scaffold (3) and the single pins file (5) small, and the qualification command (4) is what
makes a new model safe to try on the box before any of it lands.

## Design options, report-only

These are larger than a single safe commit and are design calls for you and the operator, not
changes this session made. Listed so nothing is lost.

- **Supply chain.** `get.sh` installs `main`. Actions in the workflows are pinned to a tag
  (`@v5`), not a commit SHA. Pip installs carry no hashes. Each is a knob between "always
  newest" and "exactly what was reviewed".
- **CI shape.** Its length and its missing arm64 runner are in the section above. Also: no
  CodeQL, and ruff is pinned to 0.5.0 with only the F and E9 rule sets.
- **The mutation gate** covers `lifecycle.py` and `recipes.py` only. The proxy's guards (the
  oversize, sampling, identity and System One walls) are the box's front door and are not in
  it.
- **Dead code.** `flash-sglang/` holds about 4,744 lines that nothing in the serving path
  imports. If it is a vendored reference, a one-line note at its root saying so would spare the
  next reader the trace this session did.
- **Volume.** The docs are about 14.4K lines of Markdown; the CHANGELOG about 6,782 lines (500
  KB); the README about 847 lines. `install.sh` is about one third comments; the proxy about
  15 percent, plus roughly 900 docstring lines. None of this is wrong, and the comments are
  unusually good; it is a lot to keep true as the code moves.
- **One registry.** The first proposal of "Making the next model cheap to add", above.
