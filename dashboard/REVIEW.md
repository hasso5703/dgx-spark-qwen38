# Spark Cockpit: state of the branch for review (BETA)

Branch `webapp`, never pushed. Everything below was built, run and validated
live on the reference box on 2026-08-28, with screenshots taken through a
CDP-driven Chromium at every step.

## Run it

```bash
python3 dashboard/cockpit.py            # dev: http://127.0.0.1:30090
bash dashboard/install-dashboard.sh     # or as a systemd service (sudo asked once)
```

Login = the server API key (`~/.config/qwen38/api-key`). The sudoers allowlist
(nine exact systemctl lines, visudo-checked before install) is only needed for
the unit start/stop buttons; everything else runs unprivileged.

## What works today (all validated on screen)

- Live panels over SSE (1 s) with polling fallback and per-panel failure
  isolation: unified memory (the real GB10 gauge), CPU, GPU power/temp/
  processes (with the documented GB10 quirks), serving engine identity
  (model, revision, quant, context, speculative, backends, radix cache),
  live requests (running/waiting/tokens/accept length), services with
  start/stop buttons, containers, repo state.
- Engine chip states: healthy / booting / down. "Booting" is inferred from
  health-down plus a container burning CPU: a journald-silent weight load is
  not an outage (field lesson learned the hard way).
- Actions with NASA rails: fixed-argv registry, closed-enum validation,
  CSRF, single-job lock, append-only audit log, confirm modal showing the
  exact command, live job log streaming. Security paths tested: 403 without
  CSRF, 400 out-of-enum, isolated 502 while the engine boots.
- Inventory panel: the uninstall --list scan rendered (units, backups,
  config, legacy files, images by tag AND digest, checkpoints with sizes,
  the PLE backing file).
- Live logs panel: container logs and keepalive journal tail.
- opencode card with the one-command handoff.
- Login page, house palette light/dark, BETA badge, canvas sparklines,
  zero external dependencies anywhere (stdlib backend, vanilla frontend).

## Deliberately not done yet (needs your call or the next iteration)

- The atomic self-update engine for the app itself (release dirs + rollback):
  designed in DESIGN.md, waiting until the branch has a remote to update from.
- Structured per-request feed (parsing proxy lines into a table): the raw
  journal view covers it today.
- Bench runner and canary/needle buttons in the UI (backend patterns exist).
- LAN exposure flag + HTTPS story (localhost-only today by design).

## Field notes from building it

The cockpit caught real things while being built: a broken containers
collector (docker stats with an absent name), the display:grid vs hidden
modal bug, the engine restart of 17:34 whose clean-stop origin the tripwire
now watches for, and the boot-is-not-a-freeze lesson now encoded in the UI.
