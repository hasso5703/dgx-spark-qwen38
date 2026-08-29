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

## Absorbed from the parallel session's webui/SPEC.md (now merged here)

Ideas adopted on top of the plan above; `webui/` is removed, `dashboard/` is
the single home:
- Every mutating action shows THE EXACT shell command in its confirm modal,
  with a dry-run first wherever the underlying script offers one.
- Append-only audit log file of every action (who, when, argv, outcome).
- During a lane boot, the lane card streams the unit journal live with a
  boot-time counter (boots are 7-15 min; silence is the enemy).
- Pins vs upstream drift detection stays READ-ONLY (show, never auto-bump).
- Log views are virtualized (bounded DOM), streams are bounded server-side.
- Health actions: one-click smoke (canary via proxy), quick needle test,
  template validation, auth check expecting 401.
- HostAdapter naming for the future multi-node layer (matches the collector
  node_id groundwork).


## Lifecycle engine (etape 1, 2026-08-29)

One pure module (lifecycle.py) turns raw facts into a single explicit state
per engine: stopped, failed, starting, loading-weights, loading-draft,
allocating-kv, capturing-graphs, warming-up, ready, degraded, stopping.
Facts in: systemd ActiveState/SubState + ActiveEnterTimestampMonotonic,
container presence, the container log tail (markers are SGLang's own lines),
/health, and the unit journal (PLE rebuild flag). Boot durations are learned
per unit (rebuild-aware buckets, median of the last 12) and drive the ETA;
a boot is only recorded if the cockpit witnessed the activation change,
so a cockpit restart facing a warm engine never records its uptime.

Action gates live server-side in the same module: starting one engine while
the other occupies the pool is refused with the reason (409), switch and
update wait for transitional states to settle, and mid-boot flash stops are
allowed with a truthful warning (next boot rebuilds the PLE table). The UI
renders the same rules: disabled buttons print their reason, the confirm
modal carries the warnings, and the per-stage progress bar animates from
stage pills plus elapsed-vs-learned-ETA.

## Research notes for the modernization pass (29/08)

Sources read: Grafana dashboard best practices (official docs), dark-first
dashboard guidance (2026), SSE architecture references.

Principles adopted for the cockpit:
- A dashboard answers ONE question first; general to specific, top-left is the
  answer (engine state and the pool), details below. One row per service,
  ordered by data flow: client, proxy, engine, machine.
- Color has meaning and never carries it alone: every state chip pairs a color
  with a word; thresholds drive color; icons or position cues for CVD users.
- Dark is designed, not inverted: WCAG AA contrast, thin bright lines with
  translucent fills for charts, borders and shadows redesigned for dark.
- Refresh proportional to change rate (1 s vitals, 2 s lifecycle, 5 s units,
  30 s engine info, 90 s probes, 300 s registry): already the tier model.
- Skeleton and empty states for every panel; both themes shipped from day one.
- Normalize axes (percent of pool, not raw tokens) where comparison matters.
- Version the layout as code (this repo), never browser-only edits.
- Alerts lead browsing: the event feed and wedged/degraded chips are the entry
  points, panels are the drill-down.


## Identity decision (29/08, modernization pass)

Grounded in the object: the DGX Spark is a graphite chassis with champagne-gold
anodizing and NVIDIA green status light. Tokens: graphite paper and cards, warm
white text, spark gold as the single accent, NVIDIA green for good, lane colors
kept (violet flash, cyan 27B) because they already carry meaning. Light theme
is warm paper, not inverted dark.

Signature: the reservoir. One full-width strip showing tokens held in the KV
pool against the pool's real capacity, the tick where a single prompt tops out,
and, hatched in red, the part of the model window that never fits at these
memory settings. It is the picture of every incident of the day (scheduler
wedge on two giant contexts, a user's OOM at 230K, opencode limits). The card
cascade animation was removed: one accessory less; motion is kept only where it
carries information (boot shimmer, level transition, live pulse).

## Etape 4 (design): recipes, moteurs et versions pilotes depuis le cockpit

Objectif de Hasan: choisir un moteur, sa version, un modele et sa revision, un drafter et ses
parametres, depuis l'interface, avec les memes garanties que le repo (tout epingle, tout
verifie, rien de destructif sans confirmation), et voir en direct ce qui est installe et ce
qui est disponible.

Modele de donnees (une recipe = un fichier YAML, validee par schema, jamais d'argv libre):

    id: flash                      # identifiant stable, enum ferme cote actions
    lane: flash                    # 27b | flash (decide l'unite systemd et le launcher)
    engine:
      family: sglang               # sglang | vllm | llamacpp (extensible, un adaptateur par famille)
      image: lmsysorg/sglang@sha256:...   # digest epingle (jamais un tag mouvant)
      overlay: flash-sglang        # dossier vendore verifie par MANIFEST.sha256, ou null
    model: {repo: RadixArk/Qwen3.8-Flash-Next-NVFP4, revision: 7b71922...}
    drafter: {algorithm: NEXTN, repo: null, revision: null, params: {steps: 3, draft_tokens: 4}}
    serve:                         # uniquement des cles connues, mappees vers des flags par l'adaptateur
      context_length: 262144
      mem_fraction: 0.81
      max_running_requests: 1
      chunked_prefill: 1024
      attention: {prefill: triton, decode: trtllm_mha}
      env: {SGLANG_OPT_MAMBA_SKIP_DECODE_LOCK: "1"}
    validation:                    # ce qui prouve qu'une recipe sert correctement
      needle_depths: [60000, 120000]
      canaries: 4
      bench: bench-matrix

Les trois cibles actuelles (stock, uncensored, flash) deviennent trois recipes generees
depuis les pins d'install.sh (source de verite inchangee); une recipe custom est un
fichier de plus dans ~/.config/qwen38/recipes/.

Operations (toutes via le registre d'actions du cockpit, enums fermes, audit, une a la fois):
- list: recipes integrees + custom, avec pour chacune: image presente ? revision en cache ?
  overlay verifie ? (le registre local repond deja a ces trois questions)
- validate: schema, digest/revision resolus, adaptateur connu, cles serve autorisees
- render (dry-run): unite + launcher rendus a partir des templates du repo, diff contre
  l'installe, affiche AVANT toute application
- apply: sauvegarde de l'unite/launcher installes, rendu, daemon-reload, restart, attente
  health + canaris + needle de la recipe; ECHEC => restauration automatique de la
  sauvegarde (le meme contrat que l'auto-update atomique)
- available: veille upstream etendue aux images (tags/digests recents des familles connues)
  et aux revisions HF des modeles/drafters, avec « nouveau depuis » et un bouton
  « creer une recipe a partir de cette version » qui pre-remplit le YAML

Ce qui n'est PAS fait tant que le contrat n'est pas ecrit: l'adaptateur vllm/llamacpp
(la famille sglang couvre les deux lanes du repo), et l'application d'une recipe custom
sans validation needle (interdit par design).

### Etape 4, avancement 29/08 (soir)

Fait: `recipes.py` (pur, 11 tests sur les vrais fichiers du repo), `/api/recipes`
(lecture seule, cache 60 s), panneau Recipes (recette integree ou custom, image,
modele et revision, drafter, cles de service, presence sur la box, derive contre
l'unite ou le launcher installes), `tests/headless-check.mjs` (chromium headless
pilote par CDP, cookie de session injecte, 0 exception console). Verifie live:
launcher flash = 0 derive, unite 27B = variante 1M (contexte, fraction, env).

Suivant, dans l'ordre: (1) `validate` + `render` (dry-run) comme actions du
registre, diff affiche avant toute application ; (2) `apply` avec sauvegarde,
attente health + canaris + needle de la recette, restauration automatique en cas
d'echec ; (3) veille upstream etendue aux images et aux drafters ; (4) creation
d'une recette custom depuis l'interface (formulaire a enums fermes, ecrit le JSON,
jamais d'argv libre).

### Deux directions pour la refonte visuelle (a discuter avec Hasan)

`dashboard/mockups/design-directions.html` (statique, aucun script) montre, avec les vraies
formes de donnees du 29/08 : A « Instrument » (console telemetrie, monospace partout,
echelle graduee en hero, lanes en tableau de bord, etapes de boot en segments) et B
« Ledger » (carnet de mesures, papier chaud, serif pour les chiffres seulement, regles
d'encre au lieu de cartes, boot en chronologie). Les deux abandonnent la grille de
cartes actuelle pour un seul instrument fort et un systeme typographique strict. Capture :
`~/flashnext-work/design-directions.png`. La direction choisie sera appliquee a l'app
entiere en une passe, tokens d'abord, puis composants.
