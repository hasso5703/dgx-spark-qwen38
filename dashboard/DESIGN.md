# Spark Cockpit: design foundations (branch `webapp`, NOT released)

One web app that does everything this repo does from the shell, by clicks, with
everything visible in real time. Ships later as `qwen38-dashboard.service`.
BETA badge top-left until Hasan says otherwise.

## Non-negotiables (from the owner)

- Nothing ever breaks: the app must never be able to wedge the box, the serving
  stack, or itself. Every action that mutates state gets confirmation +
  guardrails; every failure is isolated and shown, the rest keeps working.
- No em/en dashes anywhere (house rule, CI-guarded already).
- Beautiful, professional, own palette; UX readable at a glance.
- DGX Spark only for now, but abstractions ready for multi-node later.
- Atomic self-update with automatic rollback.

## Stack decision

**Backend: single Python 3 process, stdlib only (http.server ThreadingHTTPServer).**
Rationale, challenged against FastAPI/uvicorn:
- Zero supply chain: stock DGX OS python3 runs it, no venv, no pip, no wheels
  to pin, nothing to break on update. That IS the reliability requirement.
- Our scale is one box, a handful of viewers: threads + SSE are ample.
- SSE proxy pitfalls (buffering middleboxes) do not apply: same-host or LAN,
  direct connection, no CDN. Long-poll fallback still provided.
- FastAPI would be nicer to write, worse to own: 30+ transitive deps on a box
  that must never break. Rejected.

**Frontend: one static HTML + vanilla JS + hand CSS, no CDN, no framework.**
Same supply-chain logic. Canvas-drawn sparklines (no chart lib).

**Transport: SSE (`/api/stream`) for push; every panel also fetchable by plain
GET for the fallback path. Actions are POST with JSON, CSRF-protected.**

## Security model (NASA mode)

- Binds 127.0.0.1 by default; LAN exposure is an explicit config flag.
- Session auth: the app reuses the repo's api-key file as its bearer secret
  (cookie session after a login page; the key never appears in URLs).
- CSRF token on every mutating POST; same-origin checked.
- The backend never interpolates client input into shell commands: every
  action maps to a fixed allowlisted argv template; parameters are validated
  against closed enums (model in {stock,uncensored,flash}, unit in the fixed
  set, etc.). No free-form command execution, ever.
- Privileged actions (systemctl start/stop/enable, unit writes) go through a
  dedicated sudoers drop-in installed by the app installer:
  `hasan ALL=(root) NOPASSWD: /usr/bin/systemctl start qwen38-sglang.service, ...`
  listing EXACT argv lines only. The app runs unprivileged.
- Read paths are allowlisted absolute prefixes (repo dir, config dir, HF cache
  metadata); no path traversal possible (resolved + prefix-checked).
- Rate limiting on actions; idempotency keys on switches.

## Data sources (verified live on the box, 2026-08-28)

| panel | source | notes |
|---|---|---|
| GPU power/temp | `nvidia-smi --query-gpu=power.draw,temperature.gpu` | memory.used/total are [N/A] on GB10; utilization.gpu can freeze stale (documented trap) |
| GPU processes | `nvidia-smi --query-compute-apps=pid,used_memory` | works |
| Unified memory | `/proc/meminfo` (MemTotal/MemAvailable) | the real GB10 gauge |
| CPU | `/proc/stat` deltas, per-core | |
| Disk | `df -B1` on $HOME + docker root + statvfs; NVMe IO from `/proc/diskstats` | |
| Serving engine | SGLang `GET /get_server_info` (100+ fields: model, revision, quant, memfrac, mrr, ctx), `GET /get_load`, `GET /health` | Bearer key from config; works on both lanes |
| Live decode telemetry | `docker logs --since` parsing of scheduler lines (`#running-req`, `#full token`, `accept len`, tok/s) | robust regex, degrade gracefully |
| Requests in flight | keepalive proxy log lines (journald via `docker`/file) + `/get_load` | proxy already logs one line per request with outcome |
| systemd | `systemctl show -p ...` (read, no sudo) / actions via sudoers allowlist | |
| Containers | `docker ps/inspect/stats --no-stream` | user is in docker group |
| Repo state | `git -C repo` describe/status/log, pins parsed from install.sh | |
| Inventory | `./uninstall.sh --list` (read-only by design) | |

## Update engine (atomic, self-rolling-back)

- Releases = git tags on the repo. The app runs from `releases/<sha>/` with a
  `current` symlink; `previous` kept.
- Update flow: fetch, checkout new worktree, run its self-checks, atomically
  swap `current`, systemd restart of the dashboard only, health-check within
  N seconds by the NEW process writing a beacon; the OLD version's watchdog
  (systemd `ExecStartPre` guard + `previous` symlink) restores on failure.
- The dashboard updating the SERVING stack = running the repo's own
  `get.sh`/`install.sh` in a supervised job with live log streaming; those
  scripts already converge and never reset choices (validated through v1.5).
- Never two mutating jobs at once (single job-lock with owner and TTL).

## Extensibility groundwork (do not implement now)

- Every data collector implements one interface: `collect() -> dict` with a
  `node_id` field; the store is keyed by node. Today node_id is always
  `local`. Remote nodes later = same schema over HTTP.
- Actions declare `{id, title, argv_template, params_schema, danger_level}` in
  a registry; the UI renders from the registry. New engines/models = registry
  entries, not new UI code.

## Palette (own identity, light+dark)

- Ink `#0b1020` / Paper `#f6f7fb` bases; accent `#5b8cff` (actions),
  `#22c58b` (healthy), `#f2a93b` (warnings), `#e5484d` (danger),
  `#8b5cf6` (flash lane), `#0ea5e9` (27B lane). Lane colors used consistently
  everywhere a lane appears. Typography: system stack, tabular numerals for
  metrics. Motion: 150-250 ms ease-out, no gratuitous animation.

## Phases

1. Foundations: this doc, CAPABILITIES.md, skeleton server + unit + palette tokens.
2. Read-only cockpit: machine + engine + systemd + repo panels, SSE live.
3. Actions: switches, service control, proxy, jobs with streamed logs.
4. Update engine + inventory + opencode shortcut + polish.
5. Hardening pass: failure injection, load, security review, docs.
Each phase ends with a browser-validated visual check and a written test log.
