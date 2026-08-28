# Everything the repo can do (A to Z) and where it lands in the UI

Source of truth audited from v1.5 (434f522). Each capability maps to a panel
or an action in the registry. Nothing shells out free-form; every action is a
fixed argv template.

## Install and upgrade
| capability | shell today | UI |
|---|---|---|
| First install / upgrade (converging) | `get.sh` one-liner / `install.sh` | Jobs page: "Update serving stack" (runs get.sh, streamed log, converge-safe) |
| Choose target model | `MODEL_CHOICE=stock/uncensored/flash` | Switch page selector |
| 1M context mode (27B) | `CONTEXT_MODE=1m` | Switch page toggle (27B only, greyed on flash with the reason) |
| Custom port / HF cache / PLE dir | `PORT= HF_CACHE= PLE_DIR=` | Settings (read + edit with validation, applied on next update job) |
| No-service foreground run | `install.sh --no-service && run.sh` | Documented only (interactive terminal concept), not a UI job |

## Serving control
| capability | shell today | UI |
|---|---|---|
| Which lane serves | `systemctl start/stop qwen38-sglang / qwen38-flash` | Big lane cards with Start/Stop (mutually exclusive, confirmations) |
| Boot enablement | `systemctl enable/disable` | Toggle per unit (exactly one serving unit enabled, enforced) |
| Keepalive proxy | `systemctl ... qwen38-keepalive` | Proxy card: state, upstream, restart |
| Switch target model | `./switch-model.sh stock/uncensored/flash` | Switch page: pick target, shows exactly what will change (dry-run diff), then queued+restart button |
| Model revisions served | `--revision` in unit/launcher | Shown on lane cards (pin sha, upstream main sha, drift indicator) |
| Kill a stuck generation | proxy auto-abort / restart service | "Abort all" (flush_cache + restart) with guardrails |

## Observability
| capability | shell today | UI |
|---|---|---|
| Server state | `systemctl status`, `journalctl`, `docker logs` | Live log viewer per unit (follow, filter, download) |
| Engine internals | `/get_server_info`, `/get_load`, `/health` | Engine panel: model, quant, revision, ctx, mrr, load, queue |
| Decode telemetry | docker log scheduler lines | Live charts: running requests, tokens, accept length, tok/s |
| Requests through proxy | keepalive proxy request lines | Requests table: start, bytes, duration, outcome |
| Machine | nvidia-smi (power/temp/procs), /proc, df | Machine panel: unified memory, CPU, disk, GPU power/temp, top GPU procs |
| Benchmarks | `./bench.sh`, `./bench-matrix.sh` | Jobs: run bench, parse and chart results, history kept |
| Quality canaries | (campaign scripts) | Job: 4-canary battery against the live lane |

## Housekeeping
| capability | shell today | UI |
|---|---|---|
| Inventory of everything installed | `./uninstall.sh --list` | Inventory page (sizes, current vs superseded, copy reclaim commands) |
| Reclaim superseded images | printed `docker rmi ...` | One-click per item WITH confirmation + never the current images |
| Uninstall | `./uninstall.sh [--yes]` | Danger zone, double confirmation, keeps data by default |
| API key | `~/.config/qwen38/api-key` | Shown masked, copy button, regenerate (with restart warning) |
| opencode | `oc` launcher, opencode.json | "Open opencode" shortcut: copies the exact command / launches via ttyd-style terminal page if enabled; shows current default model and limits |
| Chat templates | patch-template.py outputs | Template panel: which file, patched markers present, regenerate job |
| Repo state | git status/log/tags | Repo panel: version, latest upstream tag, CHANGELOG view, update available badge |

## Explicitly out of scope now (groundwork only)
Multi-node, multi-GPU, clusters: the collector/action registries carry
`node_id` and capability flags from day one, UI stays single-node.
