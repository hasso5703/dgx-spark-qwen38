# Changelog

## v1.6.2 (2026-09-03): the flash pool is the same on every boot

- **The flash lane's KV pool is the same on every boot.** SGLang sizes the pool from
  the host's `MemAvailable` at the instant it profiles (it uses `psutil` on an
  integrated GPU, not `cudaMemGetInfo`), so anything holding memory right then shrinks
  the pool for the life of the boot. Two boots of the identical launcher and image
  drew **189,056 and 249,408 tokens** on 2026-09-03; the bigger draw left 17.2 GiB of
  idle host headroom against 23.7, and a 120k prompt spends about 9 GiB of it, so the
  "better" draw was the one nearer the livelock edge. The launcher now pins
  `--max-total-tokens 190000` (SGLang serves it as 189,952 after page alignment), the
  envelope every needle and canary result in this repo was measured in, and it waits up
  to 180 s for `MemAvailable` to clear 96 GiB before starting (measured: the memory of a
  stopping lane is back within one second of the container exit, so the wait costs
  nothing on a normal switch and only bites when something else is busy). SGLang treats
  the pin as a ceiling, so a boot that still profiles less serves less: the cockpit now
  says so in its event feed, with both numbers. Verified on the reference box after the
  change: pool 189,952, idle 23.7 GiB, needle 2/2 at 120k with a 14.9 GiB floor,
  canaries 4/4, and the KDA kernel is the only QSA kernel that compiled in the container.
- **The cockpit actually records the pool each boot wins.** v1.6 announced that; it
  recorded one boot in five. The recording read the `engine_info` collector's cache at
  the moment the lane turned ready, and at that moment the cache holds either the
  boot's connection errors (nothing recorded) or the previous engine's facts (the wrong
  pool recorded). It reads the engine directly now.
- **A flag named in a comment is not a flag.** The recipe parser scanned the whole
  launcher text, comments included, so a comment reading "--max-total-tokens below
  pins the ceiling" became `max_total_tokens = "below"` and failed the built-in recipe
  as invalid. Full-line comments are dropped before scanning; regression test added.
- **Proxy v6.12: the corruption tripwire trips at 128 marker characters, not 48.** A
  run of 48 exclamation marks is a plausible banner line in a code block; a run of 128
  is not something a model writes. Real corruption runs to `max_tokens`, so the later
  trip costs about two seconds. The OpenAI dialect also gets `data: [DONE]` after the
  error event, so strict clients see a terminated stream rather than a truncated one.
- The v1.6 GitHub release notes were cut at 9,000 characters mid-sentence and the v1.6.1
  notes began with a fragment of the heading line; both are republished whole.

## v1.6.1 (2026-09-03): the dashboard can switch lanes again

- **The cockpit can switch lanes again.** `switch-model.sh` passed bare unit names to
  sudo (`systemctl disable qwen38-sglang`), while the cockpit's sudoers allowlist pins
  exact argv (`/usr/bin/systemctl disable qwen38-sglang.service`). To sudo those are two
  different commands. It had always been wrong and never showed, because a leftover
  blanket `NOPASSWD: /usr/bin/systemctl` on the reference box matched anything; the day
  that file was tightened, every switch from the dashboard died on `sudo: a terminal is
  required to read the password`, half-applied, with the target lane enabled and the old
  one still enabled too. Unit names are fully qualified now, and a CI step cross-checks
  every privileged call in `switch-model.sh` against
  `dashboard/sudoers-cockpit.template`, so the next mismatch fails a build instead of a
  switch. The gate was verified against the broken version: it names the four missing
  entries.

- **A switch says when the serving image is behind the repo.** `switch-model.sh` changes
  the checkpoint, never the serving image: only `install.sh` builds that. So a box that
  pulled a repo whose image pin had moved would keep serving the old image without a
  word, and the image is where the kernel fixes live (v1.6 moved the flash lane to the
  merged sm_121 kernel). The flash branch now compares the installed launcher's image
  with the repo's `FLASH_SERVE_IMAGE` and prints what to run if they differ. Silent when
  they match.

## v1.6 (2026-09-03): the cockpit ships, Qwen's own FP8 joins the 27B lane, and the flash lane gets the merged kernel

- **Spark Cockpit**, the local web dashboard, is now part of the repo instead of
  living on a branch the CHANGELOG kept pointing at. Opt-in and never run by
  `install.sh`: `dashboard/install-dashboard.sh`, bound to `127.0.0.1:30090`,
  login is the API key. It reports lane state from a real generation canary
  rather than `/health` (a wedged SGLang answers `/health` fine), keeps a host
  `MemAvailable` floor and an NVRM allocation-refusal counter, runs one
  supervised action at a time (unit start/stop/restart, lane switch, flush,
  abort, smoke) with every argv audited, and shows which pinned checkpoints are
  actually on disk. Full section in the README.
- **`conc-check.py`: does this lane still answer correctly when requests share the
  engine?** sglang#36548 reports DFlash2 corrupting state under concurrency, and a DGX
  Spark measurement on this repo's own cookbook-cell issue (sglang#35860) puts numbers on
  it: with the packed-FP4-head NVFP4 target at concurrency 8, **111 of 304 greedy answers
  were wrong**, against 0 of 100 served one at a time, 1 of 304 with the dense BF16-head
  export, and 0 of 304 with speculation off. The default target of this repo is a
  packed-FP4-head checkpoint served with `--max-running-requests 8`, so this ships as a
  probe you can run in two minutes rather than as a claim. On the reference box, the FP8
  abliterated target with DFlash2 x8 measured **60/60 serial and 304/304 at concurrency
  8**. The packed-FP4-head target that produced the 111/304 upstream measures
  **60/60 serial and 304/304 concurrent here too**, and **303/304 with 3,000 tokens of
  context unique to each request** (the one miss was verbose, not wrong, and no answer
  ever carried another request's content). This repo's configuration does not reproduce
  the failure on the same hardware and the same checkpoint. What differs is the rest of
  the recipe: a pinned DFlash2 image and drafter revision, an fp8 KV cache, mem fraction
  0.70 and a 8192 chunked prefill, against the cookbook cell's plain 0.80 / 2048. A
  negative result is not a proof of absence, so the probe ships with a `--pad` mode and
  a cross-contamination detector rather than a claim.
- **The cockpit stopped crying drift on a lane that has none.** The 27B lane has two
  unit templates, native and 1M, and `install.sh` remembers which one a box runs. The
  recipe generator always derived from the native one, so a 1M installation reported
  its own three markers (`--context-length 1010000`, `--mem-fraction-static 0.70`,
  `SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN=1`) as drift, permanently, with a warning
  badge on the rail to match. Recipes are now derived from the mode the box actually
  runs, read off the installed unit. On the reference box the served lane went from
  three differences to none, and the recipes that are not installed still differ where
  they truly do, by model and KV dtype. Seven tests, including the exact false alarm.
- **The flash lane serves the merged SM121 kernel now** (`qwen38-flash:v1.6.0-kda`).
  sglang#36845 was merged on 2026-08-30 as a different implementation than the
  2026-08-28 Triton revision this repo vendored: a KDA kernel package under
  `sglang/kernels/kda_kernels/`. No stable tag carries it (v0.5.18 predates the merge
  and the stable `qwen38flashnext` image is still the 2026-08-26 digest), so the repo
  vendors it the way it vendored the last one: verbatim from hashd1ve's pinned
  `4f425ca5`, sha256-pinned, with the 2026-08-28 Triton kernel kept as the fallback the
  route calls outside the KDA contract. The image build now imports both routes for
  real, so a missing symbol fails the build instead of surfacing as a decode crash nine
  minutes into a boot. Validated on the reference box: **needle retrieval 11/11 exact at
  120k prompt tokens** (nine on the build under test, two more on the image that ships
  after a comments-only edit; fresh passphrase each, host memory floor 14.6 GiB),
  **quality canaries 4/4 on both builds**, decode 38.8 / 36.7 / 26.5 tok/s on code, math
  and prose, and no run of token id 0 anywhere in the campaign.
- **Do not send a prompt bigger than the pool to the engine port.** Establishing the
  above cost one wedged engine, and the mechanism is worth writing down. This lane's KV
  pool is 189,056 tokens; a 190k prompt sent direct to `:30000`, past the proxy's
  oversize guard, is queued and never admitted (`#queue-req: 1, #running-req: 0`), and
  from then on the engine answers `/get_load` and `/health` while generating nothing for
  anyone. `POST /abort_request` replies `not found in rid_to_state`, which is
  sglang#36333, whose fix (#36418) is still not merged. Only a restart clears it. The
  proxy refused that prompt correctly; the direct port has no guard, and that is now
  stated where the ceiling is documented.
- **The proxy refuses to relay a corrupted answer** (v6.11). A decode path that loses
  its state on this hardware keeps generating: it emits runs of token id 0, which is
  `!` in the Qwen tokenizer, so the client reads a wall of exclamation marks and has no
  way to tell it from an answer. This is the visible face of sglang#36537 / #36558 /
  #36806 / #36845, and on the flash lane it is why that lane is still beta. The proxy
  now counts marker characters across the stream and, past `CORRUPTION_RUN` in a row
  (48 by default, `0` disables), aborts the generation upstream and sends an explicit
  `corrupted_output` error in the client's own dialect. It reads only the delta text it
  already relays, never tool-call arguments; prose with `!!!` in it is untouched.
  Covered by nine unit tests plus an end-to-end test whose fake engine streams 4,000
  exclamation marks and must be cut short and aborted.
- **A design pass over every cockpit surface**, done by reading the rendered pages
  rather than the stylesheet. What it changed:
  - **The top bar stops breaking.** A long lane name (`27B FP8 uncensored`) pushed
    the action bar onto a second row inside a header with a fixed 58 px height, so
    the second row was drawn above the top of the window. The bar now keeps one row
    and shrinks in a stated order (the lane pill, then the target selector, then the
    lane button), the action groups lost their boxes, the lane button names the unit
    it stops instead of the whole checkpoint, and `--top` follows the measured header
    height so a bar that does wrap takes the rail and the job strip with it.
  - **Tracked-out capitals are gone.** Every panel title, table header and micro-label
    was `text-transform: uppercase` with heavy letter-spacing. They are now sentence
    case at reading weight, separated by a hairline instead of by shouting.
  - **The interface says things in words.** The event feed printed
    `unit started ({'verb': 'start', 'unit': 'qwen38-sglang.service'})` and the job
    strip printed `unit verb=start unit=...`; both now say `start qwen38-sglang`, from
    one vocabulary shared by the server and the page. State transitions use a real
    arrow, so `->` stops splitting into `-` and `>` across a line break.
  - **Empty is designed.** Every table that can legitimately be empty says what would
    fill it instead of showing a bare header row, the served-engine panel says once
    why it has no facts instead of repeating the same sentence down ten rows, and a
    sparkline with nothing in its window says so.
  - **Layout that fills the page.** The request feed and its pool, and the event list
    and the job history, sit side by side; the Models and Setup tabs pair their panels
    two per row; the safety belts explain themselves across the full width with prose
    capped at a readable measure. Values that were sentences (the refresh periods, the
    opencode fit, the repo head, the cockpit mode) are short in the column and complete
    in the tooltip.
  - **Colour carries meaning again.** Six buttons in six colours became one primary
    (switch), the destructive ones in red, the rest neutral. Gauge tracks are visible
    on a dark card at zero. Sparklines are drawn at device resolution.
  - The click-storm test (`dashboard/tests/monkey-check.mjs`) still passes 41/41,
    including no horizontal scrolling at 390 px.
- **The cockpit's bind address is configurable.** It was hardcoded to
  `127.0.0.1` in the unit template, so the dashboard was only ever reachable
  from a browser running on the box itself, which is not where a headless GB10
  keeps its browser. `dashboard/install-dashboard.sh` now takes `DASH_BIND`
  (default unchanged, `127.0.0.1`): `DASH_BIND=0.0.0.0` for every interface,
  `DASH_BIND=$(tailscale ip -4)` for the tailnet only. The health probe dials a
  real address when the bind is a wildcard, the installer warns when a non
  loopback bind has no API key to gate it, and the cockpit prints the exposure
  in its own startup line. Access stays plain HTTP behind the API key, so this
  is for a tailnet, not the open internet. Two things had to be fixed for a
  re-run to actually change anything: `enable --now` leaves an already running
  unit alone, so the installer now follows it with `try-restart` (a bind change
  used to report success while the old process kept the old socket), and the
  unit sets `PYTHONUNBUFFERED=1`, without which Python block buffered stdout
  into the journal pipe and no cockpit startup line was ever readable there.
- **Cockpit self-restart is off by default.** `COCKPIT_AUTOHEAL` defaulted to
  `1`, so an install would have restarted a wedged engine on its own. For a repo
  with a section on how this box freezes that is the wrong default; the unit now
  ships `COCKPIT_AUTOHEAL=0` and the README says how to arm it.
- **`./uninstall.sh` now removes the cockpit's privileged surface.** It knew
  nothing about `qwen38-dashboard.service`, `/etc/sudoers.d/qwen38-cockpit` or
  `/usr/local/bin/qwen38-pyspy-scheduler`, so a full uninstall left a NOPASSWD
  sudoers file behind while `sudoers-cockpit.template` claimed it was removed.
  All three are inventoried by `--list` and deleted by the uninstall.
- **`./uninstall.sh --list` sees the FP8 checkpoints.** The two new repos were
  missing from `HF_REPOS`, so about 60 GB of weights was invisible to the
  inventory that claims to show everything this repo left on the box.
- CI covers the dashboard: `dashboard/*.py` and `dashboard/tests/*.py` join the
  ruff and py_compile lists, the three dashboard shell scripts join `bash -n`
  and shellcheck, and the two cockpit suites run as their own step. Before this,
  the repo's largest component was reached by exactly one gate, the typography
  check, and only because that one walks `git ls-files`.
- **The FP8 targets are served with an fp8 KV cache, as they were measured.**
  The reference box has served `--kv-cache-dtype fp8_e4m3` since 2026-08-31 and
  every FP8 pool figure in this repo was measured with it, but the flag was in
  nobody's template: an `install.sh` from this repo produced a bf16 KV cache and
  about half the pool (measured, same 1m unit: 771,139 with, 382,706 without),
  which the 1m limits above would then have overflowed on the first long
  session. The NVFP4 checkpoints carry KV scales in their own quant config and
  are untouched. CI renders both templates for both cases and fails if the FP8
  path loses the flag or the NVFP4 path gains one.
- **`./run.sh` accepts the FP8 targets.** `install.sh --no-service` installed
  them happily and `run.sh` then died with "MODEL_CHOICE must be stock,
  uncensored or flash", because its own target map and its pin list both
  predated them. The documented no-service path works for all four 27B targets.
- **`needle.sh` no longer reports a lane's own prompt limit as corruption.** It
  probes through the keepalive proxy by default, so the oversize guard and
  `PROMPT_CEILING_TOKENS` apply to it, and its default depths run to 140,000
  while the flash lane's ceiling is 128,000. A refused prompt came back as
  `ERROR`, counted against the retrieval ratio and exited 1, which reads as the
  long-context corruption this probe exists to detect. Refusals are now labelled
  `REFUSED`, excluded from the ratio, explained (lower `--depths`, or probe the
  engine port directly with `PORT=30000`), and an all-refused run exits 2
  because nothing was measured rather than 0 because nothing failed.
- **`oc-merge-limits.py` could report a write it had not made.** It patched the
  limit block with three regexes; a block missing `output` or `input` matched
  nothing for those keys, so the file kept its old values while the tool printed
  the new ones and exited 0, leaving opencode on its own default cap. Missing
  keys are now inserted, and every run re-reads the file afterwards and restores
  the backup rather than report a write that did not land. The inline CI fixture
  became `tests/test_oc_merge_limits.py`, covering four incomplete block shapes,
  an empty one, preservation of unknown keys and JSONC comments, the no-op exit
  code, and the verifier itself.
- **58 cockpit tests were never running.** `dashboard/tests/test_lifecycle.py`
  declares 58 tests across 14 classes and had no `__main__` block, so the CI
  step that invoked it as a script executed nothing and exited 0. The repo's
  largest suite, covering the lifecycle state machine, wedge detection and the
  autoheal decision, was green because it was inert. All 58 pass now that they
  run. CI compares the number of tests unittest reports against the number the
  files declare, both in aggregate and per file when each is invoked directly,
  so a missing entry point or an uncollected class fails instead of passing
  silently.
- **The cockpit records the KV pool each boot wins, per target.** The pool is a
  lottery and it depends on the checkpoint, which is why this repo carries two
  disagreeing 1m measurement campaigns: nobody was recording it. Every
  transition into ready now appends the served pool under its own target key,
  and the lane panel reports min, max, last and spread once two boots exist.
- **A 27B install stops downloading 6 GB it never serves.** The DSpark drafter
  was fetched by every 27B install since v1.2 replaced it with DFlash2, and
  `run.sh` refused to start when it was missing, but no serving path had
  referenced it for six releases. It is no longer downloaded or required; the
  pin stays so the cockpit's registry still recognises a copy on disk, and
  `git checkout v1.1 && ./install.sh` fetches it through that release's own
  installer. A 27B target now needs about 84 GB free instead of 90, and CI
  fails if the set of downloaded checkpoints and the set something actually
  serves drift apart again.
- **The keepalive proxy stops enforcing a dead engine's KV pool.** `pool_tokens()`
  cached the pool for 600 s and dropped it nowhere, so for ten minutes after an
  engine restart the oversize guard sized prompts against the previous engine.
  The pool is a boot lottery (863,398 / 893,479 / 913,334 measured for one
  checkpoint) and changes outright between lanes (about 863k on 27B, 184k on
  flash), so a prompt accepted against a stale larger pool would be relayed to a
  smaller one and wedge the scheduler, which is the failure the guard exists to
  prevent. The cache is now dropped whenever the engine proves unreachable and
  whenever a refresh read fails, with a regression test that goes red if the
  invalidation is removed.
- **An upgrade on an FP8 box no longer demotes it to a custom model.** install.sh
  maps the installed `--model-path` back to a MODEL_CHOICE so a re-run keeps your
  target; that map knew stock and uncensored only, so either FP8 checkpoint fell
  through to the custom branch, dropping the pinned revision and offering only
  stock or uncensored as a way out. Reproduced against the reference box's own
  unit before and after.
- **The FP8 targets ship 1m opencode limits that fit their pool.** The 1m limits
  were static across targets: 680,000 compaction plus 200,000 output is an
  880,000 worst case, against a measured FP8 pool of 771,139. The FP8 pair now
  gets 480000/160000, what `oc-fit-limits.py` derived from the live FP8 engine.
- **The NVFP4 1m pool is documented honestly.** Two campaigns on the reference
  box disagree and their ranges do not overlap (README: 917K-1019K over five
  v1.3-era boots; `oc-fit-limits.py`: 863,398, 893,479 and 913,334 over three
  later ones). The README now states both, names 863K as the floor to plan
  against, and says plainly that the static 1m worst case sits above it, so a
  1m user is told to run `oc-fit-limits.py` after boot rather than discovering
  it mid-session. The default itself is unchanged pending a fresh campaign.
- **The pool cost of FP8 was an extrapolation presented as a measurement.**
  install.sh reasoned 10 GB of extra weights times 20,000 tokens per GB and
  published "roughly 200,000 fewer"; the same 1m unit measures 863,398 on NVFP4
  and 771,139 on FP8, so about 92,000. Corrected in install.sh, README and here.
- **The CI pin contract matched its own name, not the script.** switch-model.sh
  reads 16 pins; the check counted a 12-name pattern, leaving the four FP8 names
  guarded by nothing. It now mirrors the script's `PINS` grep name by name,
  verified with a negative test, and switch-model.sh refuses a target that
  resolves to an empty repo or revision.
- **`oc-fit-limits.py` has tests, and two defects fewer.** It read the lane
  ceiling with a plain `startswith()`, but systemd prefixes only the first
  variable with `Environment=`, so a template reordering would have made the
  ceiling read as absent and handed the flash lane 27B-sized limits. Kilo
  rounding also bottomed out at `0/0` on an implausibly small pool, and writing
  `"context": 0` would break opencode; it now refuses instead.
- Two new targets on the 27B lane, switchable like the others and served by the
  same unit: `fp8` (`Qwen/Qwen3.8-27B-FP8`, Qwen's own release) and
  `uncensored-fp8` (`edp1096/Huihui-Qwen3.8-27B-abliterated-FP8`, huihui-ai's
  abliteration in the same format). `MODEL_CHOICE=fp8 ./install.sh` on a fresh
  box, `./switch-model.sh fp8` on an existing one. No flag changes: SGLang reads
  the quantization scheme from the checkpoint's own config.
- What FP8 costs, measured here: the weights are 30.9 GB against 21 GB for
  NVFP4, which SGLang takes out of the KV pool (measured on the same 1m unit:
  863,398 tokens on NVFP4, 771,139 on FP8, so about 92,000 fewer), and decode is
  bandwidth bound on GB10 so it is slower too, 108 tok/s aggregate at 8 streams
  against 135-148 for the NVFP4 default. What it buys is the quantization
  question off the table.
- Checked before pinning the abliterated FP8: architecture
  `Qwen3_5ForConditionalGeneration`, vision tower present, not gated, 30.9 GB,
  and the weights compared against Qwen's official FP8 file by file, 66 names in
  common and not one shared hash, so the abliteration is real rather than a
  rename. `lm_head` is left out of the quantization, which is what keeps an FP8
  checkpoint loadable by SGLang (the unsloth and RedHat FP8 builds quantize it
  and their own cards say SGLang cannot load them).
- `oc-fit-limits.py`: the generated opencode config asks for no more than the
  served engine can actually hold, and the switch names the checkpoint behind
  the opencode entry it writes.
- 27B overlay: vendors the upstream mrope fix for the fused Qwen3.5 rope kernel.
- Two gaps stated in the README rather than papered over. The FP8 pair does not
  yet carry a full `./bench-matrix.sh` run the way the other three targets do
  (108 tok/s is a single aggregate probe), and **the memory ceiling of FP8
  combined with `CONTEXT_MODE=1m` is not measured**: the "27B lane measured flat
  at 100K, 200K and 300K" result was measured on NVFP4, and FP8 puts about 10 GB
  more in residence. Run the FP8 pair at native context until that curve exists,
  or watch host `MemAvailable` if you run it at 1M.

## v1.5.9 (2026-08-30): opencode is a choice, and every choice is written down

- `./install.sh --no-opencode` (one-liner: `| bash -s -- --no-opencode`): API only. No
  generated config, no `oc` launcher, and `switch-model.sh` leaves your opencode default
  model alone. The choice persists across re-runs and upgrades through a marker file
  (`~/.config/qwen38/opencode.off`), like the installed model, context mode, port and cache;
  `--with-opencode` turns it back on. Your own `~/.config/opencode/opencode.json` is never
  rewritten in either mode. `./uninstall.sh --list` shows the marker.
- switch-model.sh now updates the default model in the config opencode actually reads
  (`~/.config/opencode/opencode.json`) and not only in the generated artifact: on the
  reference box, opencode kept opening on the previous lane after a switch.
- The installer no longer shrinks the 27B opencode limits of a box whose installed unit
  serves more than 262,144 tokens when re-run in native mode (a native re-run had turned
  a 1M box's 700000/200000 into 194048/64000, hence a "21K tokens = 11 percent" gauge).
- The `oc` launcher respects an inherited `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX` instead
  of hardcoding the cap of the last install (a flash install had capped a 1M box at 32000).
- keepalive proxy: every request leaves exactly one end line in the journal, including a
  client that vanishes while its body is still being read (a request had shown as "in
  flight" forever).
- README: a "Your choices, and how they combine" table under Quickstart, with the one-liner
  and clone forms of every sensible combination.

## v1.5.8 (2026-08-30): an engine that is down is reported as down

- keepalive proxy v6.9: when the engine does not answer (stopped, crashed, restarting, still
  loading), every path answers `503 engine_unavailable` with `Retry-After: 30` and a message
  that says the request was NOT refused for its size and can be retried unchanged once
  `/health` answers 200. Before, a body over 200 KB met the oversize guard first, `/tokenize`
  failed because the engine was gone, and the size fallback refused it as `400 context_too_long`
  ("at least ~408841 tokens by size" for a 68,626-token request, live on 2026-08-30 at 01:15
  while the flash engine was restarting). Unknown request shapes still refuse on size, and the
  message now says that the engine itself is up.
- The field case behind it: the flash engine (SGLang dev build `d91c3682b`) hit `NVRM: Xid 31`
  (GPU MMU fault, `FAULT_PTE` read) and `CUDA error: an illegal memory access was encountered`
  in the QSA indexer prefill path (`qsa_indexer.py forward_cuda -> get_prefill_mqa_inputs`),
  one second after a 68k-token request had finished, inside the one-token generation that
  `GET /health` performs in this build when the engine is idle. First Xid in 60 days of kernel
  logs, 30 GiB of host memory free, no `NV_ERR_NO_MEMORY`. systemd restarted the unit
  (`Restart=always`, 15 s) and it served again 8 min 39 s later with the PLE table reused.
- Tests: `tests/test_proxy_guard.py` now covers connection refused, `5xx` and `4xx` from
  `/tokenize`, and runs the real proxy process in front of a fake engine that knows its pool
  but is still loading: a 1 MB body gets `503 engine_unavailable`, never `400 context_too_long`.

## v1.5.7 (2026-08-29): the ceiling follows the lane

- `switch-model.sh` now sets the keepalive proxy's one-prompt ceiling for the target
  lane (flash 128,000 tokens by default, the 27B lane none), restarts the proxy and
  says so. In v1.5.6 only `install.sh` wrote it, so a lane switch left the proxy
  with the previous lane's ceiling: a 27B lane capped at 128K, or a flash lane
  without one. Tested both ways on the reference box (unit and running process
  checked, no engine restart needed).
- `needle.sh --mem`: samples host MemAvailable every 0.5 s during each prompt and
  reports the floor per trial plus a summary, so anyone can measure the memory
  ceiling of their own box the way BENCHMARKS did. No sudo.
- Docs: the 27B lane measured flat at 100K, 200K and 300K on the 1M unit (the
  prefill growth is flash-specific); a 40-minute soak under the 128K ceiling
  (24 prompts, exact, footprint plateaus); ATTRIBUTION names the exact revision
  of the vendored sm121 kernel.

## v1.5.6 (2026-08-29): the oversize guard counts instead of guessing

- keepalive proxy v6.8: the v6.7 guard refused every body whose size divided by
  2.5 chars per token exceeded the KV pool. English prose runs 3.4 to 4.6 chars
  per token, so a 140k-token prompt (479 KB) was refused as "192k tokens" while
  the pool served it (soak of 29/08: 60k, 100k and 120k prompts looped without a
  flush, 140k got a 400 every time). Now the size only nominates a body; the
  engine's own `/tokenize` (present in both images this repo ships; 644 ms for
  474k chars, measured) gives the exact prompt length, chat template applied,
  Anthropic-shaped bodies converted. Refusal when the count exceeds 92 percent
  of the pool (`OVERSIZE_MARGIN_FRAC=0.08`, the same ceiling the README states:
  165K of 178,560); the size-based refusal remains the fallback when the engine
  cannot count. Image, audio and document parts (OpenAI parts or Anthropic
  blocks) are counted as a fixed budget of 4096 tokens each
  (`TOKENS_PER_MEDIA`), never as their base64 text. 8 offline tests (fake
  `/tokenize`) run in CI. Live on the reference box after deployment: the 140k
  needle passes through the proxy (140,111 tokens counted, 92 s), a 1.24 MB body
  is refused in 0.6 s as 230,533 prompt tokens against 169,633 usable, a 300 KB
  Anthropic-shaped body is served, a 600 KB image body is counted as 4,153
  tokens and reaches the engine.
- Measured ceiling for one prompt (direct, 29/08, pool 184,384): 166k, 171k and
  177k tokens (90, 93 and 96 percent of the pool) all served with exact needle
  retrieval in 112 to 123 s. The proxy's 8 percent margin is room for the answer,
  not a hang boundary.
- The KV pool is not the ceiling, memory is. Measured on the reference box at
  fraction 0.81 (fresh boot, 24 GiB available idle): the prefill of a long prompt
  grows the engine's footprint by about 0.27 GiB per 1k tokens beyond ~90k
  (120k prompt: +7.4 GiB; 135k: +11.4; 150k: +15.3; 177k: +22.8, MemAvailable
  down to 0.8 GiB and 15 `NVRM: NV_ERR_NO_MEMORY` kernel lines, the livelock
  edge). The process RSS does not move (unified CUDA allocations). Two effects,
  not one: GPU driver allocation refusals, which torch recovers from by freeing
  its cache and retrying, showed up between 125k and 150k depending on the run
  (0 at 135k in one run, 11 at 125k in another: they follow the page cache state,
  not a clean threshold, and every prompt still answered correctly); and the host
  memory floor, the hard limit, which the curve puts near 170k. The proxy
  therefore enforces an absolute per-lane ceiling for one prompt,
  `PROMPT_CEILING_TOKENS`, set by install.sh in the keepalive unit: 128,000 on
  the flash lane, which keeps about 14 GiB available at the prompt's peak on a
  128 GB box (override with `PROMPT_CEILING_TOKENS=`), none on the 27B lane (not
  measured, then measured the same evening: flat, +3 GiB from 100k to 300k on the
  1M unit, so its limit stays the pool). Deployed on the reference box through
  `install.sh`: 140k and 130k prompts refused with the engine's count, 125k served
  in 81 s. A smaller prefill chunk (512) was tested and
  rejected: 30 percent slower cold prefill and a worse floor at 150k (6.7 GiB,
  13 driver refusals). The v1.5.4 note "host headroom unchanged at 23 GB" was an
  idle measurement and is corrected in the README.
- CHANGELOG cites the upstream PR behind `SGLANG_OPT_MAMBA_SKIP_DECODE_LOCK`
  (sgl-project/sglang#32228, merged 2026-07-29, off by default).

## v1.5.5 (2026-08-29): clean stops, A/B verdict on DSpark v2

- The three unit templates declare `SuccessExitStatus=137 143`: docker ends the
  foreground engine with SIGKILL or SIGTERM on a plain `systemctl stop`, which
  systemd reported as `failed` after every intentional stop.
- BENCHMARKS: DFlash2 vs the new DSpark v2 drafter on this box, same battery,
  both native at 262144 on the SGLang image this repo ships. A tie on the median
  (29.4 to 30.4 vs 29.5) with DSpark v2 behind on the cells that are stable
  across runs (reasoning FR, prose), so DFlash2 stays the 27B drafter.
- `needle.sh` exits 2 with a clear message when the calibration request fails.

## v1.5.4 (2026-08-29): the proxy stops two ways of wedging the flash scheduler

Root cause work on the four hangs of 2026-08-29 (gdb and py-spy on the spinning
scheduler, a controlled campaign of launcher variants): the scheduler of this
build busy-loops forever, with `/health` still answering, when a queued request
can never be admitted. Two triggers reproduced: a prompt longer than the KV pool,
and a large prompt that needs a mamba state slot only an eviction can free (the
default cache is 9 to 15 slots depending on the memory left at boot, 5 per running
request; 9-slot boots hang on the second large prompt, 15-slot boots served six
in a row). No launcher flag fixes it without amputating the KV pool (explicit
sizes, memory ratio and max-running-requests were all measured), so the fix
lives in the keepalive proxy (v6.7), which every service install already fronts:

- a request whose most optimistic token estimate exceeds the engine's KV pool is
  refused with a clear 400 (`context_too_long`) instead of wedging the engine;
- when a client disappears mid-stream, the proxy now posts `/abort_request` for
  that request id (closing the socket alone left orphan generations decoding to
  their max_tokens; measured 29/08).

The launcher also sets `SGLANG_OPT_MAMBA_SKIP_DECODE_LOCK=1`, an option of this
build that stops locking a request's cached mamba state during decode (the
request already works on its own copy-on-write slot; upstream PR
sgl-project/sglang#32228, merged 2026-07-29, off by default). Measured: with the lock,
9-slot boots hang on the second large prompt; without it, four and then five
consecutive large prompts were served with the evictions the lock used to
block. The state cache becomes 12 slots of 4 per request, and with
`--mem-fraction-static 0.81` (host headroom measured unchanged at 23 GB) the KV
pool grows to 178560 tokens, from 159,552.

`needle.sh` exits cleanly when the engine is unreachable. The cockpit (then a
branch, shipped in v1.6) adds the generation probe, the wedged state with
optional autoheal and a cache flush when the engine idles with a mostly-held pool. Upstream report: sglang #30314 (comment).

## v1.5.3 (2026-08-29): PLE table reused across boots

Every flash boot rewrote the 48 GB PLE table through a random-access mapping
(issue #7 by nullburn: ~42 GB written per boot, NVMe saturated by 4 KB faults,
59-minute boots once the page cache is cold; confirmed on the reference box).
The table is a deterministic function of the checkpoint, so it now carries a
completion marker (served revision, geometry, dtype) written after msync; a
boot whose marker matches maps the file and skips the copies, and a real
(re)build runs with sequential readahead before switching to MADV_RANDOM for
serving. The launcher tags the table with the served revision
(SGLANG_QWEN4_PLE_TAG) and wipes the marker together with the table after an
interrupted boot; a tag that is not a commit sha (for example a branch name
passed through MODEL_REV=main) disables reuse, so a moving upstream can never be
served from a stale table; `switch-model.sh` rewrites the tag with the revision.
Serving image tag: qwen38-flash:v1.5.3.

Measured on the reference box: rebuild boot about 12 minutes (55 GB written,
sequential readahead), reuse boot 500 s with 352 MB of block writes for the
whole boot and the table untouched, exact needle retrieval at 60k and 120k real
prompt tokens on the reuse boot.

Known and under investigation (unchanged by this release): with the default
mamba state cache of this build (9 to 15 slots depending on the memory left at
boot, 5 per running request), a large prompt that needs a slot only an eviction
can free, or a prompt longer than the KV pool, makes the scheduler busy-loop
forever instead of refusing the request (`/health` keeps answering; gdb and
py-spy show the event loop spinning in recv_requests with the request queued).
Four occurrences on 2026-08-29. Mitigations shipped so far live in the cockpit
(then a branch, shipped in v1.6: generation probe, wedged state, optional
autoheal, cache flush when idle); a proxy-level refusal of oversize prompts and
an explicit cache size follow.

## v1.5.2 (2026-08-29): flash lane correctness hotfix

**Fixes a silent long-context corruption in the v1.5 flash lane.** The v1.5
overlay widened the QSA trtllm sparse-decode gate to sm_121; on GB10 that
routes decode to FlashInfer's XQA kernel, which emits runs of token id 0 deep
in long contexts (hashd1ve, 2026-08-29: 1 of 4 requests at 120k tokens, 4 of
4 at 210k). The gate is back to upstream and sm_121 decode uses the packed
Triton varlen kernel of sglang#36845 (vendored verbatim, sha256-pinned,
attribution in flash-sglang/ATTRIBUTION.md). Serving image tag: qwen38-flash:v1.5.2.

Validation on the reference box: exact needle retrieval 12/12 at 40k, 67k and
80k real prompt tokens (the depths the probe reached), on top of the kernel
author's 4/4 at 120k/190k/210k. Not yet validated here beyond that: prompts of
~120k tokens wedged this box's scheduler twice on 2026-08-29 (prefill stalls,
`/health` keeps answering, no output), a pre-existing behavior unrelated to the
kernel route and under investigation (mamba state cache of 9 slots against one
checkpoint per 1024-token prefill chunk is the leading hypothesis). Until that
lands, treat ~100k tokens as the practical ceiling of one prompt on this lane.

Also in this release, after a real scheduler wedge on this box (two ~109k-token
requests admitted into a 159k-token KV pool, scheduler spinning at 100 % CPU
with `/health` still answering):
- flash lane: `--max-running-requests 1` (one giant context at a time; a second
  request queues instead of fighting for the pool). Small concurrent requests
  were never the use case of a single-user box.
  The boot log shows the flash lane never actually served two requests at once:
  the mamba state cache is sized to 9 slots by default and each running request
  needs 5 (`max_running_requests is capped to 1 by the mamba state cache`), so
  one active request plus four cached prompts fill it and the next prompt forces
  an eviction, which is where both hangs of 2026-08-29 happened. v1.5.3 sizes
  that cache explicitly.
- opencode config for the flash lane: context 110000 (was 226000, which let a
  conversation outgrow the pool before compaction). The converging install now
  merges the lane's limits into an EXISTING ~/.config/opencode/opencode.json too
  (targeted edit, dated backup, comments and other providers untouched), instead
  of only printing a merge reminder.
- `uninstall.sh --list` knows qwen38-flash:v1.5 as a superseded image.

## v1.5.1 (2026-08-28)

Flash lane robustness hotfix: poisoned PLE table self-healing.

- A flash boot interrupted while the 48 GB PLE mmap backing file is being
  written (power cut, manual stop, kill) left a stale table that wedged every
  later boot in a silent scheduler spin (100% of one core, empty journal, no
  IO). Root-caused and reproduced live; regenerating the table fixed it in
  one boot. The launcher now sets a `.loading` marker at start and a detached
  waiter removes it once `/health` answers; finding the marker at launch
  means the previous boot never got there, so the table is wiped and rebuilt
  automatically (one ~11 min boot instead of a wedged lane).
- No flag, image, or checkpoint changes; 27B lanes untouched.

## v1.5 (2026-08-28)

The flash lane moves to SGLang: working prefix caching, tool-loop fix, vision on.

- **Flash target now serves on SGLang** (`lmsysorg/sglang:qwen38flashnext`,
  digest-pinned), same engine family as the 27B pair. The reason: **prefix
  caching works there** and vLLM's is blocked by a GB10 GDN bug (tracked
  upstream as vllm#54173, filed with this box's exact environment). Measured on
  the reference box: a 30K-token conversation re-served in 0.5 s instead of
  18.4 s (x36); a fresh question on a known 30K prefix in ~3 s (x5.8); decode
  34-42 tok/s (vLLM lane: 31); vision validated including with large prompts;
  canaries 4/4; needle at 100K passing.
- **`flash-sglang/` vendored overlay** (MIT, by hashd1ve, over Apache-2.0
  SGLang sources): patch 1 mmaps the 51B PLE table from NVMe
  (`SGLANG_QWEN4_PLE_MMAP_DIR`, ~48 GB backing file written once at first
  boot, `PLE_DIR` env, default `~/flashnext-ple`); patch 2 fixes the QSA
  resolvers on sm_121 (+32% decode). Patch 2 is the same fix as upstream
  PR #36556, which independent reports confirm also fixes the token-ID-0
  tool-call loop (#36537); this repo carries **both** of that PR's resolver
  edits (the hashd1ve tree had one) after verifying the FA4 dispatcher module
  exists in the pinned image. Provenance is proven at vendor time: each file
  diffs against the module extracted from the pinned image in exactly the
  patched region.
- **The lane authenticates** (`--api-key`, like everything else here); early
  public recipes for this model ran open.
- The v1.4 vLLM lane is retired (git history keeps it: `git checkout v1.4`).
  Upgrades from v1.4 keep port/cache/model choices and regenerate the launch
  script on the new engine. The flash lane now also serves the Anthropic
  protocol, like the 27B lane.
- **27B: known upstream reports, not reproduced here.** sglang#36548 (DFlash2
  can attach a message to the wrong context under concurrent load) and
  sglang#35150 (speculative verify diverges from plain decode in the SSM
  transition) affect DFlash2 builds newer than this repo's pins. Reproduction
  on this repo's exact pinned build: 100 greedy ordering prompts, serial AND
  at concurrency 8, zero wrong answers. The pins stay; documented options if
  you serve many concurrent correctness-critical streams: the
  `RadixArk/Qwen3.8-27B-NVFP4-BF16-LMHead` target (`009632f`), DSpark v2, or
  `--max-running-requests 1`.
- Known upstream behavior documented and reproduced on this lane
  (sglang#35537): with chunked prefill, a long-decoding request can starve new
  requests until it completes. Single-agent use is unaffected.
- **`./uninstall.sh --list`**: read-only inventory of every artifact any
  version of this repo (v1.0 through v1.5) may have left on the box: units,
  drop-ins, unit backups, config, the oc launcher, local AND base docker
  images (matched by tag or digest: a digest pull leaves no tag), the five
  checkpoints, the PLE backing file, each with its size. The uninstall itself
  now prints reclaim commands only for what is actually present, and the
  installer points out superseded engine images after an upgrade (e.g. the
  ~40 GB of v1.4 vLLM images) without ever deleting data on its own.
- CI: flash guards moved to the SGLang launcher (radix cache, NEXTN, split
  attention backends, api-key, PLE mmap dir, no `--language-only`, revision
  lock), vendored-file gate greps, generator executed on all three shapes.

## v1.4 (2026-08-27)

Qwen3.8-Flash-Next 176B as a third switchable target, on one box.

- **New target `MODEL_CHOICE=flash`**: Qwen3.8-Flash-Next (176B hybrid MoE, 6B
  active, QSA sparse attention, multimodal) in RadixArk NVFP4, served by vLLM
  with the model's own MTP speculative head. Full native 262,144 context on a
  single GB10. Measured on the reference box: decode ~31 tok/s
  (code/reasoning), prefill ~2,280 tok/s at 60K and ~2,100 at 189K, quality
  canaries 4/4, needle passing at 190K+ depth.
- **`flash/` vendored overlay** (Apache-2.0, by blazux): the checkpoint's 51B
  N-gram (PLE) table is mmap-served from NVMe through the page cache instead of
  living in the unified pool; that single change is what makes the model fit.
  Two files, sha256-pinned, and the bit-exactness test of the gather runs
  inside the freshly built image at every install (the build refuses to tag on
  failure).
- **`qwen38-flash.service`**: same hardening as the 27B unit (docker caps
  110g, Restart=always, ExecStartPre rm, API key, serve-time --revision lock,
  HF_HUB_OFFLINE boot) plus the GB10-specific serving flags (PIECEWISE CUDA
  graphs with the PLE op split out, prefix caching off on sm_121, FlashInfer
  autotune off), each now guarded by CI.
- **Cross-engine switching**: `./switch-model.sh flash` / `stock` /
  `uncensored`. Both units publish the same port and are never enabled
  together; the switch flips which unit starts at boot, re-verifies the
  checkpoint, regenerates the target template and re-points the opencode
  default model. The installer converges on whichever target is installed,
  including flash, and never resets a served choice.
- **opencode config generation reworked**: one provider per installed engine
  (`qwen38`, `flashnext`), each with `low`/`medium`/`xhigh` reasoning-effort
  variants (xhigh added for the 27B too), written by json.dump instead of a
  heredoc; the default model follows the installed target. Flash limits:
  226,000 context / 32,000 output inside the 262,144 window.
- **Patched chat template for flash** (`chat-template-flashnext.jinja`): the
  upstream Flash-Next template ships the same two agent-hostile behaviors as
  the 27B one (500 on reasoning_effort "max"/"high", 500 on mid-conversation
  system messages); the same two surgical fixes apply, verified by rendering
  all six effort levels.
- CI: flash unit render guards (the GB10-critical flags can never silently
  regress), flash overlay manifest check, opencode generator executed on all
  three install shapes, pin contracts extended (12 for switch-model.sh).
- Uninstall now removes both engines and lists the flash reclaim paths.

## v1.3 (2026-08-27)

Release validated end to end on the reference box before tagging: a converging
upgrade over the live install (only the Description and the new `--revision`
changed in the unit, everything else preserved), stock and uncensored switches
with reboots proving the revision lock (the pinned sha served while the cache
held a newer upstream revision AND a `refs/main` pointing at it), `./run.sh`
foreground with a clean stop, a from-scratch cache simulating a fresh machine
(which caught the Xet stall and the anonymous throttling fixed below),
`./bench.sh` at 50.7 tok/s greedy median (reference: ~50), and a real opencode
session writing files through the `oc` launcher and the keepalive proxy
(which caught the `--yolo` flag-position fix below).

- The 1M context mode: `CONTEXT_MODE=1m ./install.sh` installs the preset that
  serves the reference box daily since 2026-08-22, as one converging command.
  It patches YaRN static scaling (factor 4.0) into both cached `config.json`
  files via the new `patch-yarn.py` (target AND DFlash2 draft, originals
  backed up as `config.json.pre-yarn`; the shared script also replaces
  `switch-model.sh`'s inline patcher and handles the draft's root-level
  config shape), renders a dedicated 1m unit (`--context-length 1010000`,
  `--mem-fraction-static 0.70`, `SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN=1`,
  `HF_HUB_OFFLINE=1` to shield the patched configs from Hub re-resolution,
  `Restart=always` because a Triton crash measured on 2026-08-22 exited 0 and
  `on-failure` never relaunched it), and installs the vendored keepalive proxy
  (`keepalive-proxy.py` v6.6 + `qwen38-keepalive.service` on `PORT+1`): SGLang
  buffers tool-call arguments (127 s of measured silence on a 400-line write)
  and agent CLIs abort silent streams, so the proxy injects the official
  Anthropic ping event / an authentic empty OpenAI chunk every 10 s at SSE
  event boundaries only, closes the upstream when the client leaves, and
  reports an explicit error after 3600 s of upstream silence. `./run.sh`
  refuses 1m (the proxy is a service); `uninstall.sh` removes the proxy
  service too. Native stays the default and is byte-identical to v1.2.7
  behavior.
- Converging upgrades: re-running `install.sh` (or the `get.sh` one-liner) now
  reads the installed unit and keeps the operator's choices instead of
  silently resetting them to the defaults: the target model (stock/uncensored,
  or a custom `--model-path`, kept verbatim with its download and template
  steps skipped), the context mode (native/1m, plus the proxy port), the
  port, and the HF cache location. An explicit env var (`MODEL_CHOICE=`,
  `CONTEXT_MODE=`, `PORT=`, `PROXY_PORT=`, `HF_CACHE=`) still wins. The
  previous unit is backed up to
  `~/.config/qwen38/qwen38-sglang.service.bak-preupdate` before being
  rewritten, so hand-tuned flags stay recoverable. The `oc` launcher passes
  `--yolo` after the user's arguments, not before: opencode's parser rejects
  global flags placed before a subcommand (`oc run ...` printed the help
  instead of running; caught by the release campaign's live opencode test).
- The repo's client story moves from Claude Code to opencode. `install.sh` now
  writes a complete provider config to `~/.config/qwen38/opencode.json`
  (limits sized per context mode so no request can ever 400, reasoning-effort
  variants, vision declared, key referenced via `{file:...}`), installs an
  `oc` launcher to `~/.local/bin/oc` that lifts opencode's hidden 32000
  max_tokens cap to the declared output limit (never clobbering a foreign
  `oc` binary), and no longer writes `claude-code.env` (an existing copy
  keeps working but is unmaintained). The
  Claude Code warmup is removed: `--with-claude-warmup` is now a no-op with a
  notice, `warmup-claude-code.sh` leaves the repo, and an installed
  `warmup.conf` drop-in from an earlier version is cleaned up on upgrade
  (other drop-ins are untouched). The template patches are unchanged: they
  were always server-side and client-agnostic.
- New `extras/` directory, two field-tested opt-ins from the reference box:
  `extras/opencode/auto-continue.js` (opencode plugin that resumes a session
  after a transient technical error or a stuck compaction; never after a
  deliberate abort or an auth problem; stops after 25 relaunches without
  progress) and `extras/cake-ingress/` (ingress anti-bufferbloat: CAKE shapes
  received traffic just under the measured downlink so SSH and the API stay
  at milliseconds while a model download saturates the link; interface
  auto-detected, BANDWIDTH deliberately required and validated, full
  measurement sweep in the script header, `setup.sh --uninstall` restores
  stock networking). The README's Operations section also documents
  `/abort_request` for killing abandoned generations on direct connections.
- The keepalive proxy ships with every service install, not only 1m: SGLang
  buffers tool-call arguments at any context length (127 s of measured
  silence on one 400-line write) and agent CLIs abort silent streams, so the
  proxy is the difference between a finished write and a client retry loop.
  The generated opencode config points at the proxy port on service installs
  and at the server directly with --no-service (where ./run.sh has no proxy).
  The oc launcher resolves opencode from PATH with a fallback to
  ~/.opencode/bin/opencode.
- The native unit moves to `Restart=always` (matching the 1m unit and the
  reference box): a Triton compile crash measured on 2026-08-22 terminated
  with `SystemExit: 0`, a clean exit in systemd's eyes, so `Restart=on-failure`
  never relaunched it. Documentation sweep: real disk numbers everywhere
  (~90 GB fresh, +22 GB with both targets cached), the `HF_HUB_OFFLINE`
  guidance rewritten from measurements, and a note that `claude-code.env` is
  never overwritten again.
- Serve-time revision lock: the units and `run.sh` now pass the pinned
  `--revision` to the server for the target model (the draft already had its
  own pinned revision flag). Until now the pin only governed the download:
  at boot the server could still resolve the repo's "main" and pick up an
  upstream push (RadixArk has already published two newer stock revisions).
  With the sha passed to the server, upstream changes cannot affect what is
  served, online or offline; `switch-model.sh` rewrites the revision together
  with the model path, and a kept custom model reuses its unit's existing
  revision or none. Combined with `HF_HUB_OFFLINE=1` in the 1m unit, the
  running configuration is exactly the repo's, whatever happens upstream.
- Fresh-machine hardening and small fixes, ahead of the release stress test.
  Downloads: a fresh-cache pull stalled forever at 3.3 GB during the release
  campaign; root-caused live to the hub library's Xet transfer backend (an
  established socket moving zero bytes, 0-8 MB/s when moving at all, while
  the classic CDN path measured 89 MB/s on the same box in the same second).
  The download containers now set `HF_HUB_DISABLE_XET=1`, plus
  `HF_HUB_DOWNLOAD_TIMEOUT=30` and a 5-attempt resume loop as a belt for any
  other silent stall, and `HF_TOKEN` is passed through for authenticated
  rate limits (a token already in `$HF_CACHE/token` keeps working through
  the mount). Also,
  pinned-sha downloads now also write the cache's `refs/main` when absent
  (`huggingface_hub` only writes refs for named revisions, so on a fresh
  machine the 1m unit's `HF_HUB_OFFLINE=1` boot would fail to resolve "main";
  never overwritten if present). `switch-model.sh` also regenerates the
  patched chat template from the target's own snapshot on every switch
  (byte-identical between the two known targets today, verified; the belt
  keeps the served template following the served model if one ever diverges).
  `run.sh` error messages now echo the fix-it command with the active
  `MODEL_CHOICE` prefix, so following the advice prepares the configuration
  that failed, not the default one. `uninstall.sh`'s reclaim list gains the
  uncensored checkpoint (~22 GB). The `oc` launcher passes `--yolo` (the
  reference box's way; documented, removable in the launcher file).
- Model switch option: `MODEL_CHOICE=stock|uncensored` in `install.sh` and
  `run.sh`, plus `./switch-model.sh` for surgical live installs (downloads the
  checkpoint, patches 1M YaRN config if the unit uses it, rewrites only the
  `--model-path` line, daemon-reload; no service restart by the script itself).
  Uncensored target = huihui-ai abliteration re-quantized with the identical
  RadixArk modelopt NVFP4 recipe
  (`edp1096/Huihui-RadixArk-Qwen3.8-27B-abliterated-NVFP4` @ `21565d3`);
  the DFlash2 drafter stays unchanged in both modes. `patch-template.py` now
  takes the repo as an optional fourth argument. `switch-model.sh` rewrites the
  unit via a temp file + `sudo install -m 644` (no `sudo sed`) and skips the
  download/YaRN steps idempotently when already done. See README, "Stock ↔
  Uncensored target model".

## v1.2.7 (2026-08-21)

`run.sh` now passes `--sleep-on-idle` too, matching the systemd unit which got the flag in
v1.2.6. Contributed by struxoje in [#4](https://github.com/hasso5703/dgx-spark-qwen38/pull/4),
opened before v1.2.6 was even tagged: the community caught the run.sh path while the unit
path was being A/B tested. Same behavior as measured in the v1.2.6 notes (scheduler idle
CPU 101 % -> 1.7 %, wake-up TTFT unchanged).

## v1.2.6 (2026-08-21)

The systemd unit now passes `--sleep-on-idle`. Without it, SGLang's scheduler busy-spins a
full CPU core whenever the queue is empty, which two users measured as +10-12 W at the wall
(alef204 and emX0r on the forum thread, root-caused by emX0r in MiaAI-Lab issue #4; the flag
exists in the pinned image, so no rebuild). A/B on the reference box before adopting:
scheduler CPU at idle 101 % -> 1.7 %, module power 12.1 -> 10.5 W (median over 60 s), wake-up
TTFT unchanged after 60 s and 300 s of idle (0.234-0.240 s -> 0.234-0.239 s), decode
throughput in family (41.5 tok/s code, 52.8 math, answers correct). Existing installs:
re-run the one-liner or `./install.sh --no-start`, then `sudo systemctl restart qwen38-sglang`.

## v1.2.5 (2026-08-21)

Claude Code env pair rebalanced after a real 64K truncation report and a request-capture
study on Claude Code 2.1.238. `CLAUDE_CODE_MAX_OUTPUT_TOKENS` goes 64000 -> 128000, which
is the CLI's hard ceiling for a third-party model id (any higher value, e.g. 258048, is
silently capped back to 128000). `CLAUDE_CODE_MAX_CONTEXT_TOKENS` goes 258048 -> 130048,
because the pair must satisfy CONTEXT + OUTPUT <= 258048: the server rejects any request
where input + max_tokens exceeds 262144 (a 400, no clamping), Claude Code never shrinks
max_tokens to fit, and its auto-compaction reserves at most 20000 output tokens. The
previous 64000/258048 pair had a latent dead zone (every request past ~198K input tokens
got a 400 before auto-compaction fired at ~225K); no field report yet, fixed preemptively.
Users who prefer longer context over very long single answers can set 64000/194048, as
documented in the README. Existing installs: re-run the one-liner (or `./install.sh
--no-start`) to regenerate `claude-code.env`; the serving image and unit are untouched.

Second fix from the lifecycle audit: the step-3 "container sees the GPU" line always printed
blank, because the image's entrypoint banner starts with an empty line and the check displayed
the first line of output. The GPU line comes after the banner. The check now extracts the
actual `GPU 0: ...` line, refuses to continue if no GPU line appears even when the command
exits 0, and runs one container instead of two. Verified on the reference box, including with
the GPU busy serving.

## v1.2.3 (2026-08-20)

One fix from the lifecycle audit: the systemd unit file was installed with mode 600 (a
`mktemp` + `sudo cp` interaction), so the service ran fine but `systemctl cat` and any
non-root inspection of the unit failed with permission denied. `install.sh` now writes it
with `install -m 644`, the standard mode for unit files. Re-running the one-liner (or
`./install.sh --no-start`) fixes the mode in place without restarting the service.

## v1.2.2 (2026-08-20)

Typography pass, no functional change. All prose dashes were removed from every document,
script message and comment (house style). One comment line inside the vendored overlay
(`dflash2/sglang/srt/models/dflash.py`) was reworded, so the manifest is regenerated and the
serving image tag becomes `qwen38-dflash2:v1.2.2` (rebuilt automatically by `./install.sh`,
about a minute, offline). Runtime behavior, flags, pins and performance are identical to
v1.2.1. Also validated in this release cycle, on the reference box: the full lifecycle
`get.sh` one-liner (fresh install, upgrade over a modified clone, uninstall/reinstall),
`./run.sh` end to end, and the manifest tamper guard (a modified overlay file refuses to
build).

## v1.2.1 (2026-08-20)

Correctness alignment of the vendored DFlash2 overlay with upstream, plus the quality study
that the community's reports triggered.

- **Quantized-head logits: crop → contiguous mask.** Upstream merged the official quantized
  lm_head support for the DFlash2 selector hours after this repo's v1.2 vendoring
  ([sgl-project/sglang#35496](https://github.com/sgl-project/sglang/pull/35496)): slicing the
  padded local vocab produces a non-contiguous view that flashinfer's radix top-k rejects or
  can misread; the fix keeps the logits contiguous and masks the padded tail to -inf. Both
  overlay call sites now follow that pattern. The serving image tag becomes
  `qwen38-dflash2:v1.2.1` (rebuilt automatically by `./install.sh`).
- **The quality question, measured.** Forum users reported a 2-6 point tool-eval drop and
  anecdotal hallucinations vs DSpark. We measured instead of guessing, on a deterministic
  server (reproducible to the point): tool-eval 93/93 (DSpark) vs 91/91 (DFlash2), a stable
  3-scenario delta; then GSM8K 200: **exact parity, 188/200 both**; IFEval 200: split within
  noise (prompt-level favors DSpark with 15 excluded timeouts muddying its denominator,
  instruction-level favors DFlash2). A token-identity test at temperature 0 against the pure
  autoregressive model shows **both** drafters diverge from it (10/10 prompts each, equally
  early): speculative decoding is lossless in exact arithmetic, not in floating point, and
  near-tie argmax flips cascade. The 2-3 tool-eval points are those flips landing, not a
  quality regression. Full methodology and numbers in BENCHMARKS.md, "The losslessness study".
  DFlash2 stays the default.

## v1.2 (2026-08-20)

**DFlash2 becomes the default.** The service now serves the z-lab DFlash2 drafter instead of
DSpark. Measured on the reference box against the v1.1 config, same battery, same night,
deterministic mode, thinking on: every single-stream cell improves except math (parity), with
free prose FR 20.2 vs 14.0, reasoning FR 43.5 vs 30.5, code DE 39.4 vs 25.4; aggregate
throughput 135-148 tok/s at concurrency 8 (vs 100-104) and 258 tok/s at c32
(`--max-running-requests 32`). Quality canaries pass; speculative decoding remains lossless by
construction. Validated end to end including a full machine reboot.

No official SGLang image contains DFLASH2 yet (merged upstream 2026-08-19), so `install.sh`
now builds the serving image locally: the same pinned base digest plus five sha256-verified
overlay files vendored in `dflash2/` (Apache-2.0 from sgl-project PR #35371, plus MiaAI-Lab's
MIT-licensed quantized-lm_head fix; full provenance in `dflash2/ATTRIBUTION.md`, K choice per
r0b0tlab's block sweep). The build is offline and takes about a minute. The day an official
image ships DFLASH2, the repo repins to it and `dflash2/` is retired.

Upgrade: `git pull && ./install.sh` (key and template kept; ~4 GB one-time draft download;
first boot recaptures CUDA graphs). To stay on the DSpark config instead:
`git checkout v1.1 && ./install.sh`.

Also: the DSpark draft pin moves to RadixArk's 2026-08-16 revision (identical weights and
config; the commit only fixed the transformers reference code).

## v1.1 (2026-08-20)

Two flag changes, both measured overnight on the reference box (same battery, same night,
multiple boots per verdict). Upgrade: `git pull && ./install.sh` (your key and template are
kept, the unit is rewritten, the service restarts).

- **`--disable-flashinfer-autotune` (new)**: FlashInfer re-runs its kernel autotune at every
  boot and the result varies: the identical config measured anywhere between 92 and 111 tok/s
  aggregate at concurrency 8 depending on the boot, and up to ±15 % on verify-heavy single-stream
  cells (code, math). Disabling the autotune makes boots deterministic (single-stream cells
  reproduce to the decimal across boots, c8 within ±1.6 %) and cuts about 2 minutes of boot
  time, at a cost of roughly 2 % of the lottery's average. Every published GB10 comparison that
  did not control for this contains boot noise; see BENCHMARKS.md, "The boot lottery".
- **`--cuda-graph-max-bs 4` → `8`**: with `--max-running-requests 8`, decode batches of 5 to 8
  requests were falling outside the captured CUDA graphs and running eager. Capturing up to
  batch size 8 is worth +6.5 % aggregate throughput at concurrency 8 (measured deterministic,
  reproduced across boots) for about 0.4 GB of extra capture memory and a slightly longer first
  boot.

Net effect on the reference box: concurrency-8 aggregate goes from a 92-111 lottery to a stable
100-104 tok/s, and benchmarks against this config become reproducible.

Also in this release: `bench.sh` counts vLLM ≥ 0.27's renamed `reasoning` stream field and warns
above the physical ceiling (issue #2), BENCHMARKS.md gains third-party sections (vLLM DSpark
battery, long-prefix ladder) and the renamed-field trap.

Coming next: a DFlash2-based configuration (z-lab drafter) currently measures +40 % aggregate at
c8 and wins every single-stream cell on this box; it ships as the default once an official SGLang
release image contains DFLASH2 (merged upstream 2026-08-19). Watch this repo.

## v1.0 (2026-08-15)

Initial pinned release: NVFP4 + DSpark on SGLang, one-command install, systemd unit, benchmark
battery (`bench.sh`, `bench-matrix.sh`), BENCHMARKS.md with full methodology.
