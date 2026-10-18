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
- Box: whether the identity wall is used at all on this box is an open question (see below). If
  it is not, this is a latent fix; if it is, it decides which clients a case-varying SDK can
  reach.

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
  non-disaggregated engines. It may be nil (no prefill/decode disaggregation here) or it may
  let a client jump the scheduler queue with -priority. The local agent or a test on the box
  can settle it.
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
- Fix: drop `GITHUB_TOKEN` and `GH_TOKEN` from the environment the harness passes. The token
  tests set theirs explicitly and are unchanged. Verified both ways here.

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
  job in `lanes.sh` keeps its write access, which it needs.
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

### F11. ffmpeg read a speech upload as a manifest

- Status: VERIFIED IN CODE, MEASURED HERE (ffmpeg 6.1.1). Commit `1b489db`.
- Where: `speech-server.py`, `decode_ffmpeg`.
- What: the lane ran `ffmpeg -i <uploaded file>` with no restriction on the input demuxer, and
  ffmpeg chose the demuxer from the bytes. Some demuxers read the file as a playlist or
  manifest that names somewhere else: measured here, a crafted upload made ffmpeg issue an
  outbound HTTP request to an address the sender chose (a request from the box, target of the
  sender's choosing), and another made it read a second local file as this user. The lane binds
  loopback by default and sits behind the cockpit's login, but SPEECH_BIND widens it, and an
  upload should never steer ffmpeg off its own bytes.
- Fix: pin the input format to the sound containers (`-format_whitelist`) and the protocols to
  file and pipe (`-protocol_whitelist file,pipe`). Measured here: real WAV, MP3, FLAC, m4a, ogg
  and webm each still decode; the crafted uploads are refused before ffmpeg opens anything.
- Box: a phone m4a and a browser webm or ogg should still transcribe as before (the box's
  ffmpeg is its own build; the two options are long-standing ffmpeg input options).

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

1. **Is the identity wall used?** `QWEN38_CLIENT_KEYS_FILE` is unset by default. If no box
   sets it, F1 is a latent fix and the "single serving key" model is the real one; if boxes do
   set it, F1 matters now. Which is it?
2. **The x-override-* fields (F2).** On a single-GPU, non-disaggregated box, do -priority,
   -routed-dp-rank or the bootstrap fields do anything a client could abuse, or are they inert
   here? A test on the box would settle it.
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
7. **The update channel.** `get.sh` fetches and runs `main` (not a release tag). Intended, or
   should a fresh install pin to the latest release tag by default?

## Design options, report-only

These are larger than a single safe commit and are design calls for you and the operator, not
changes this session made. Listed so nothing is lost.

- **Supply chain.** `get.sh` installs `main`. Actions in the workflows are pinned to a tag
  (`@v5`), not a commit SHA. Pip installs carry no hashes. Each is a knob between "always
  newest" and "exactly what was reviewed".
- **CI shape.** The run is about 81 minutes. The "No test file left unrun" step is about 35
  minutes and the offline suite about 22. There is no arm64 runner (so the box's own
  architecture is never exercised in CI), no CodeQL, and ruff is pinned to 0.5.0 with only the
  F and E9 rule sets. An arm64 job would have caught the PEP 765 warning class and would run
  the engine-unit changes closer to the box.
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
- **One registry.** Lane identities, pins and unit paths are spread across `install.sh`,
  `lanes.sh`, `switch-model.sh`, `dashboard/registry.py` and the templates, held in agreement
  by CI gates. A single declarative source would make the gates smaller, but the move is large
  and risky; noted, not urged.
