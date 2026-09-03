# Everything the repo can do (A to Z) and where it lands in the UI

Source of truth audited from v1.5 (434f522). Each capability maps to a panel
or an action in the registry. Nothing shells out free-form; every action is a
fixed argv template.

## Install and upgrade
| capability | shell today | UI |
|---|---|---|
| First install / upgrade (converging) | `get.sh` one-liner / `install.sh` | Setup tab prints the exact terminal command: the installer needs an interactive sudo that a service cannot give, and a half-applied install is the one failure the cockpit must never cause |
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
| opencode | `~/.config/qwen38/opencode.off` marker, `~/.config/opencode/opencode.json`, `~/.local/bin/oc` | State-aware panel: on/off (the installer's --no-opencode choice), default model and whether it follows the served lane, per-lane limits, launcher and its output cap; off hides the `oc` instruction and shows the re-enable command |
| Chat templates | patch-template.py outputs | Template panel: which file, patched markers present, regenerate job |
| Repo state | git status/log/tags | Repo panel: version, latest upstream tag, CHANGELOG view, update available badge |

## Agent tab (v1.7.0)
| capability | shell today | UI |
|---|---|---|
| opencode from the laptop | `oc` in a terminal on the box | Agent tab: opencode's web interface framed from the relay, behind the cockpit login (no second login, no Basic prompt) |
| Server state | `systemctl status opencode-web`, `journalctl -u opencode-web` | chip (ready, starting, stopped, relay waiting), served version, installed binary if newer, Logs tab source |
| Restart after `opencode upgrade` | `sudo systemctl restart opencode-web` | Restart server button (exact-argv sudoers lines, confirmed like every action) |
| Reach it on its own | none | Open in a tab (same relay, same session) |
| Autonomy (no approval clicks) | `oc` runs with `--yolo` | `AGENT_AUTO=1` at install (unit sets `OPENCODE_PERMISSION`); the tab shows the mode the running server applies |

## Explicitly out of scope now (groundwork only)
Multi-node, multi-GPU, clusters: the collector/action registries carry
`node_id` and capability flags from day one, UI stays single-node.

## Recipes (step 4, since 29/08 evening)
| capability | shell today | UI |
|---|---|---|
| What each lane serves, as data | pins in `install.sh` + unit/launcher templates | Recipes panel: built-in recipes derived from the repo, custom JSON recipes from `~/.config/qwen38/recipes/` |
| Is it on the box | `docker images`, HF cache scan | presence chips (image, model revision, drafter revision) |
| Does the installed lane match | read the unit or launcher by hand | drift list per recipe (image, model, revision, drafter, serving keys, env) |
| Validate a custom recipe | none | schema + closed enums + ranges, errors shown inline (planned: as an action with dry-run render) |
| Apply a recipe | `switch-model.sh` (built-in targets only) | planned: apply with backup, health + canaries + needle, automatic restore on failure |
