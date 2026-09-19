# Architecture

A map of the repo for someone who intends to change it. The CHANGELOG is the
memory (every bug, measured, with the gate that now holds it); TESTING.md is
the testing philosophy; SECURITY.md is the trust model. This file is the
layout: what each part is for, where state lives, and which invariants the
CI keeps true.

## The box, end to end

```
  agent client (opencode, Claude Code, any OpenAI/Anthropic client)
      |  :30001
      v
  keepalive-proxy.py ── feeds ──> cockpit request feed / zombie guard
      |  127.0.0.1 :30000
      v
  SGLang engine (docker, exactly one lane enabled at a time)
      ├── 27B lane: qwen38-sglang(.service)      NVFP4/FP8 + DFlash2 draft
      └── flash lane: qwen38-flash(.service)      176B NVFP4 + NEXTN, PLE table on NVMe
  HF cache (weights, pinned revisions)    ~/flashnext-ple (176B N-gram table)

  cockpit :30090 ── sudo exact argv ──> systemd (start/stop/switch/flush)
      └── relay :30091 ──> opencode serve 127.0.0.1:4096 (Agent tab)
```

Everything the stack writes to the machine lives in one config dir,
`~/.config/qwen38` (called `CONFIG_DIR` in every script): the API key (0600,
also the cockpit login), the patched chat templates, the flash launcher, the
reduced draft vocabulary, the deployed copy of the proxy, the opencode
config artifact, the `opencode.off` and `cockpit.off` markers, the cockpit state files
(`cockpit-secret` 0600, `cockpit-events.jsonl`, `cockpit-history.json`,
`wedge-*.txt` dumps), and unit backups (`*.bak-preupdate`). Outside it:
systemd units in `/etc/systemd/system`, one sudoers file in
`/etc/sudoers.d`, the pyspy forensics wrapper in `/usr/local/bin`, the `oc`
launcher in `~/.local/bin`, and the two weight trees (HF cache, PLE dir).
`./uninstall.sh --list` is the authoritative inventory of all of it.

## The parts, and the one job of each

| file | the job | notes worth reading |
|---|---|---|
| `install.sh` | converging installer: refuses root before it writes, reads the installed unit so a re-run keeps your target, port, cache and context mode, renders every artifact from the pins, installs the cockpit as step 10/10 and ends on its URL | the convergence block and `resolve_flash_*_args` (a flag computed before convergence caused the worst bug in v1.8.0); pins at the top are a contract, not decoration |
| `keepalive-proxy.py` | the only component that reads bytes it does not control | keepalive at SSE event boundaries, zombie abort ordering (`x-override-rid`, abort *before* close, drain otherwise), oversize guard that counts via `/tokenize` and prices images from their headers, corruption tripwire; the `v6.x` header is its real version, read live by the cockpit |
| `run.sh` / `switch-model.sh` / `uninstall.sh` / `get.sh` | foreground serving, surgical live switch, read-only-first removal, one-liner bootstrap | the switch never restarts a service by itself and never touches images; it stages unit files at fixed paths because the cockpit's sudoers pins that argv exactly |
| `oc-limits.sh` | the one table of opencode limits, three callers | the comment block next to each number *is* the measurement record; the ceiling/threshold/two-thousand-step invariant is CI-held |
| `dashboard/` | the cockpit: state from a real generation canary, not `/health` (a wedged SGLang answers `/health` fine) | `lifecycle.py` is the pure state machine (100% branch coverage, mutation-scored), `recipes.py` derives "what the repo says this lane runs" from the repo's own text, `registry.py` knows the checkpoints, `agent_relay.py` is the browser boundary; every privileged call is exact argv through sudo, every action audited |
| `flash-sglang/` | vendored engine overlay, sha256-manifested, provenance in its ATTRIBUTION.md | the flash lane's rollback path since v1.8 (its content is upstream in the served image); the manifest is regenerated with intent, a tampered file refuses to build. The 27B lane had one too until v1.14, retired once measurement showed upstream carried all of it |
| `bench.sh`, `bench-matrix.sh`, `needle.sh`, `bench-agent.py`, `conc-check.py`, `tools-check.py` | the instruments: single-lane probe, frozen comparable battery, long-context retrieval, the agent-loop shape, the concurrency question, and whether a client can execute what the model emits | the battery is versioned and frozen so numbers stay comparable across years; `bench.sh` asks the engine which model it serves before printing a reference |
| `patch-yarn.py`, `patch-template.py`, `build-token-map.py`, `oc-*.py`, `oc-*.sh` | the config surgery, all idempotent, all reversible | patch-yarn has `--restore`/`--check` because a native install coming home from 1m would otherwise crash at load |
| `check-pins.sh` | supply-chain availability, over the network, on demand | deliberately not in CI: a green build must not depend on Hugging Face being up |
| `evals/` | the two quality drivers, MMLU and HumanEval, because the image's own invocations do not run and fail by printing an empty score | the failure modes and the exact docker commands are in its README; gated against a stub sglang so the contracts that make them work cannot rot silently |
| `docs/` | one subject per file, everything the README is an entry point to: the 1M mode, the flash lane, the cockpit tour, operations, opencode, clients, the memory trap, upstream standing | the README's index table is the map; a section that outgrows the README moves here and leaves a summary and a link behind |
| `extras/` | opt-ins that earned their keep on the reference box | CAKE anti-bufferbloat for downloads, opencode auto-continue plugin |

## Invariants the CI holds (each one exists because it once broke)

1. The five independent spellings of the target list (selector options,
   action enum, `install.sh` case, `switch-model.sh` guard, cockpit
   `BUILTIN_IDS`) must agree; a failing run names the odd one out.
2. The pin blocks: each consumer's own grep must select, by name, every pin
   it uses; adding a pin to `install.sh` without naming it breaks the build
   on purpose.
3. The sudoers allowlist carries no wildcard, matches every privileged call
   `switch-model.sh` makes, and stages paths cross-checked character for
   character against their callers.
4. The opencode limits chain: compaction threshold + one worst step <= the
   proxy ceiling, table and ceiling from `oc-limits.sh` only, no caller may
   own a copy of a number.
5. Every serving lane carries `--sleep-on-idle`; speculative tiers run
   3/1/4 steps; the FP8 path keeps its KV flag and the NVFP4 path does not
   gain one; flash recipes keep the radix cache and the api-key.
6. The proxy's outcome vocabulary is a closed set: adding an outcome fails
   CI until it names what it means.
7. Python is stdlib only, everywhere, forever; `node --check` holds the one
   JS plugin; no em or en dashes anywhere tracked.
8. Tests are counted: what `unittest` reports versus what the files declare,
   per file and in aggregate, because a suite that ran nothing once passed.
9. The offline suite cannot touch the box: witness HOME hashed either side,
   listening sockets compared, argv recorded by every stub.
10. Coverage floors only rise, set from the most constrained runner; the
    pure-logic modules are at 100% branch coverage and the mutation score is
    the real quality gate.
11. No test file in `tests/` is left unrun: the unittest modules are
    discovered, the script-style ones must each be named by a step, and
    declared must equal ran both in aggregate and per file. Four tracked
    files had fallen off the hand-kept list, and one suite declared two
    tests and ran zero on every runner (a `SkipTest` out of `setUpClass`
    reports one skip with `testsRun` 0; skip with a class decorator).
12. Neither entry point installs as root. A `sudo bash` run succeeds into
    `/root` with a new API key and leaves every client on 401 against a
    healthy engine, which is the one failure shape this stack has that
    produces no error anywhere.

## How a change moves through the repo

`install.sh` renders (never edits) the units, the launcher, the configs and
the templates from the pins; `switch-model.sh` rewrites exactly the lines it
owns and defers the rest to the next `install.sh`; the cockpit *derives*
what the repo says a lane should run from `install.sh` and the unit
templates, so adding a flag means updating the recipe builders and their
tests in the same commit (a recipe built from an unrendered placeholder is
refused by `builtin()` on purpose). The three files that must agree on any
target change (`oc-limits.sh`, the `install.sh` case, the cockpit selector)
are checked against each other by CI, so the honest workflow is: change
them, run `./ci-local.sh`, read what it names.

## Known open edges (stated, not hidden)

- No image this repo can serve ships an sm_121 build of `sgl_kernel`: both the
  official release and the locally built image this lane used until v1.14 carry
  sm90 and sm100 only, and GB10 loads the sm100 cubin. It measures fine, it is
  simply not a tuned path.
- The 176B boot rewrites its 47.7 GiB N-gram table every start (~10 min);
  the cost, the reason, and the upstream shape of a fix are in the flash
  launcher's comments.
- FP8 combined with the 1M context is not memory-measured; the README says
  to run at native until the curve exists.
- TLS, per-client identity and multi-tenant use do not exist; SECURITY.md
  names them as the design's edges, and anything toward them starts as an
  issue with a measured motivation.
