# webui: everything the repo does, from a browser (SPEC, branch webui, NOT for push)

Beta badge top-left. House palette (own colors, no NVIDIA green clone). No em dashes.
Design goals: NASA-grade reliability, zero external CDN, realtime without freezes,
single box today but multi-node-ready interfaces (host abstraction in the API layer).

## A -> Z capability map of the repo (everything must be clickable)
1. TARGETS: stock / uncensored / flash. See current, switch (switch-model.sh),
   see pinned revisions, see what is downloaded (which snapshots, sizes).
2. SERVICES: qwen38-sglang, qwen38-flash, qwen38-keepalive: state (enabled/active),
   start/stop/restart, boot-time countdown with live journal tail during boots.
3. CONTEXT MODE (27B): native / 1m, shown; change = guided reinstall command.
4. INSTALL/UPGRADE: run install.sh converge with live log streaming; show pins
   (image digests, model revs) vs upstream (drift detection read-only).
5. UNINSTALL/INVENTORY: uninstall.sh --list rendered as a table (sizes, versions),
   reclaim buttons generating the exact commands (copy, or execute after confirm).
6. BENCH: bench.sh / bench-matrix.sh runs with progress + historical results
   (bench-matrix-*.json parsing + charts).
7. SERVER TELEMETRY: SGLang /metrics (when exposed): running/queued requests,
   token throughput, prefill/decode split, cache hit rate, acceptance length,
   TTFT/TPOT histograms; per-request live feed via keepalive-proxy journal.
8. MACHINE TELEMETRY: GPU (power draw, compute apps: NEVER utilization.gpu alone,
   known stale-read trap), unified memory (MemAvailable! livelock guard),
   CPU, disk (HF cache, PLE file, docker), temps; 1 Hz SSE stream.
9. PROXY: keepalive status, its journal (one line per request: bytes, outcome),
   zombie-abort events.
10. CLIENTS: opencode config preview, one-click "open opencode here" (oc) via a
    terminal handoff (documented command + optional ttyd integration later),
    API key reveal/copy, endpoints cheat-sheet (OpenAI + Anthropic).
11. TEMPLATES/PATCHES: which chat template is served, patch status (effort map,
    system-reminder), YaRN patch state for 1m.
12. LOGS: journalctl streams per unit with filters; docker ps of our containers.
13. HEALTH ACTIONS: smoke test button (canary prompt via proxy), needle quick test,
    template validation, auth check (expects 401 without key).
14. SAFETY RAILS: every mutating action = confirm modal with the exact shell
    command shown; dry-run first where possible; rate-limited; audit log file.

## Architecture (decided after research)
- Backend: single-file Python 3.12 stdlib server (http.server ThreadingHTTPServer),
  SAME zero-dependency ethos as keepalive-proxy.py (psutil optional enhancer).
  SSE for realtime (one-way push), plain POST for actions. No websockets needed.
- Auth: Bearer with the existing ~/.config/qwen38/api-key (or its own key file),
  cookie session for the browser after a login page; 127.0.0.1 bind by default,
  optional LAN flag with mandatory auth.
- Privileges: the app runs as the operator user; systemd control via a narrow
  sudoers drop-in (NOPASSWD for exactly: systemctl start/stop/restart/enable/
  disable of the three qwen38-* units), installed by webui/install-webui.sh
  after explicit consent; everything else needs no root.
- Frontend: one HTML file + one JS + one CSS, hand-rolled, no CDN; canvas-based
  sparkline charts; dark-first house palette; SSE reconnect logic; virtualized
  log views (no unbounded DOM growth).
- Unit: qwen38-webui.service (Restart=always), port 30080 default.
- Multi-node future: every backend route takes an implicit host=local today;
  data layer isolated in a HostAdapter class (local shell now, ssh later).

## Phases
P1 skeleton: server+auth+SSE bus+unit+house CSS shell. P2 telemetry (machine+
services). P3 actions (switch/restart/install streams). P4 server metrics +
request feed. P5 bench+inventory+clients. P6 polish, hardening pass, docs,
screenshots. Each phase ends with tests (curl-level + browser smoke via chrome).
