# Everything the repo can do (A to Z) and where it lands in the UI

Re-audited against the code at v1.18.7 (2026-09-24). The first version of this
page, written at v1.5 (434f522), listed the UI planned then, and a dozen of its
rows never shipped: a capability with no place in the cockpit now says so.
Nothing shells out free-form; every action is a fixed argv template.

## Install and upgrade
| capability | shell today | UI |
|---|---|---|
| First install / upgrade (converging) | `get.sh` one-liner / `install.sh` | Setup tab prints the exact terminal command: the installer needs an interactive sudo that a service cannot give, and a half-applied install is the one failure the cockpit must never cause |
| Choose target model | `MODEL_CHOICE=stock/uncensored/fp8/uncensored-fp8/flash/flash-uncensored/flash-nvda`, and the image lane with `--with-image` | Target selector and Switch in the top bar: the seven text targets and Qwen-Image 2.1, each one `switch-model.sh <target>` job |
| 1M context mode (27B) | `CONTEXT_MODE=1m` (the default since v1.12.1) or `native` | None: an install choice. The lane card shows the window the engine serves |
| Custom port / HF cache / PLE dir | `PORT= HF_CACHE= PLE_DIR=` | None: install-time choices, kept by every re-run |
| No-service foreground run | `install.sh --no-service && run.sh` | Documented only (interactive terminal concept), not a UI job |

## Serving control
| capability | shell today | UI |
|---|---|---|
| Which lane serves | `systemctl start/stop qwen38-sglang / qwen38-flash / qwen38-image` | Start/Stop of the lane in the top bar, and on each engine's card in the Engines tab; the server refuses a second engine while one runs, and every action is confirmed first |
| Boot enablement | `systemctl enable/disable` | No toggle: a switch enables the target's unit and disables the others. Each card says whether its unit starts at boot |
| Keepalive proxy | `systemctl ... qwen38-keepalive` | Its row in the Engines tab: state, whether it starts at boot, the running version and whether it is the repo's copy, Start/Stop |
| Switch target model | `./switch-model.sh <target>` | Switch: the confirmation shows the exact command, the job's live output shows what it changes. It writes the target's unit or launcher and never starts, stops or restarts an engine: Start is its own click |
| Model revisions served | `--revision` in unit/launcher | Revision on the lane card; the Models tab's Recipes compare it with the pin, and its Upstream watch compares each pin with Hugging Face's `main` |
| Kill a stuck generation | proxy auto-abort / restart service | "Abort all": `POST /abort_request` with `abort_all` to the engine, confirmed first |

## Observability
| capability | shell today | UI |
|---|---|---|
| Server state | `systemctl status`, `journalctl`, `docker logs` | Logs tab: the last 120 lines of an engine container's log or of a unit's journal, re-read every 3 s with follow ticked (no filter, no download) |
| Engine internals | `/server_info`, `/v1/loads`, `/health` | Engines tab (model, revision, quantization, window, KV pool, speculative, attention, radix cache, engine version, image) and Overview's Right now (running, waiting, tokens in KV, accept length) |
| Decode telemetry | docker log scheduler lines | Requests tab, Pool and decode: KV pool held, KV usage from the engine log, Mamba state slots, accept length |
| Requests through proxy | keepalive proxy request lines | Requests table: start, client, path, bytes, duration, outcome |
| Machine | nvidia-smi (power/temp/procs), /proc, df | Machine tab: unified memory, page cache and swap, free disk at home and under Docker, GPU power, temperature and processes, CPU load, the safety belts |
| Benchmarks | `./bench.sh`, `./bench-matrix.sh` | None: terminal tools (BENCHMARKS.md) |
| Quality canaries | (campaign scripts) | No battery. The cockpit runs one real generation of its own when the engine has been quiet a minute (Generation probe), and Smoke sends one through the proxy on demand |

## Housekeeping
| capability | shell today | UI |
|---|---|---|
| Inventory of everything installed | `./uninstall.sh --list` | Models tab, Inventory: the same rows, read-only |
| Reclaim superseded images | the `docker rmi` lines `install.sh` and `./uninstall.sh` print | None: never run from the page |
| Uninstall | `./uninstall.sh [--yes]` | None: a terminal job |
| API key | `~/.config/qwen38/api-key` | It is the login. No panel shows or regenerates it; the diagnostics bundle masks it |
| opencode | `~/.config/qwen38/opencode.off` marker, `~/.config/opencode/opencode.json`, `~/.local/bin/oc` | State-aware panel in Setup: on/off (the installer's --no-opencode choice), default model, per-lane limits, launcher, whether the limits fit the served pool, and Fit the limits to this engine |
| Chat templates | patch-template.py outputs | None: `install.sh` and `switch-model.sh` write them |
| Repo state | git status/log/tags | Setup tab, Repo: version, branch, head, working tree, the running proxy's version, and whether a newer release is out (a banner too) |

## Agent tab (v1.7.0)
| capability | shell today | UI |
|---|---|---|
| opencode from the laptop | `oc` in a terminal on the box | Agent tab: opencode's web interface framed from the relay, behind the cockpit login (no second login, no Basic prompt) |
| Server state | `systemctl status opencode-web`, `journalctl -u opencode-web` | chip (ready, starting, stopped, relay waiting), served version, installed binary if newer, Logs tab source |
| Restart after `opencode upgrade` | `sudo systemctl restart opencode-web` | Restart server button (exact-argv sudoers lines, confirmed like every action) |
| Reach it on its own | none | Open in a tab (same relay, same session); Fullscreen inside the cockpit (corner button or Escape to come back, remembered) |
| Autonomy (no approval clicks) | `oc` runs with `--yolo` | `AGENT_AUTO=1` at install (unit sets `OPENCODE_PERMISSION`); the tab shows the mode the running server applies |

## Explicitly out of scope now (groundwork only)
Multi-node, multi-GPU, clusters: the collector/action registries carry
`node_id` and capability flags from day one, UI stays single-node.

## Recipes (step 4, since 29/08 evening)
| capability | shell today | UI |
|---|---|---|
| What each lane serves, as data | pins in `install.sh` + unit/launcher templates | Recipes panel: built-in recipes derived from the repo, custom JSON recipes from `~/.config/qwen38/recipes/` |
| Is it on the box | `docker images`, HF cache scan | presence per recipe (image, model revision, drafter revision) |
| Does the installed lane match | read the unit or launcher by hand | drift list per recipe (image, model, revision, drafter, serving keys, env) |
| Validate a custom recipe | none | schema + closed enums + ranges, errors shown inline |
| Apply a recipe | `switch-model.sh` (built-in targets only) | None: a custom recipe is read and checked, never applied |
