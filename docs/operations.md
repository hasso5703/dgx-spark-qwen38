# Operations, extras and upgrades

Day-to-day commands, the opt-in extras, and what an upgrade from an earlier version does. Moved out of the README in v1.14.2.

## Day to day

The command list is in the README, under "Operations". What follows is what does not fit there.

**Killing an abandoned generation.** If a client dies mid-generation the server keeps
decoding for nothing (symptom: power draw and GPU busy with no active session). Behind
the keepalive proxy this heals itself: the proxy aborts the upstream the moment the
client disconnects. For direct connections (`./run.sh`, curl, custom clients), abort
everything in flight with:

```bash
curl -X POST -H "Authorization: Bearer $(cat ~/.config/qwen38/api-key)" \
  -H 'Content-Type: application/json' -d '{"abort_all": true}' http://127.0.0.1:30000/abort_request
```

The server keeps running; use it only when you know the in-flight work is abandoned,
because it aborts EVERY request currently decoding, yours included.

`HF_HUB_OFFLINE=1` is fine **once every pinned checkpoint is cached**: the 1m unit sets it on
purpose (it protects the YaRN-patched configs from any Hub re-resolution) and the reference
box serves that way across reboots; the LongCat metadata probe
(`srt/utils/hf_transformers/config.py`) reads from the cache, verified in the pinned image.
Do **not** set it on a first install or over an incomplete cache: the probe's harmless online
miss then becomes a hard `LocalEntryNotFoundError` at startup
([reported by helge](https://forums.developer.nvidia.com/t/380257/10)). With the pinned
revisions cached, that metadata probe is the only network call.

Notes: the server's own `watchdog_timeout=300` is a *hang* detector (kills a genuinely stuck forward so systemd restarts it); it does not limit generation length. Two concurrent generations share the memory bus (~half speed each): the GB10 is a batch-1-per-moment machine.

**Idle power**: without `--sleep-on-idle`, SGLang's scheduler busy-spins a full CPU core while doing nothing (reported as +10-12 W at the wall by [alef204 and emX0r](https://forums.developer.nvidia.com/t/380257/56), diagnosed in [MiaAI-Lab issue #4](https://github.com/MiaAI-Lab/Qwen3.8-27B-SGLang-DGX-Spark/issues/4)). Every serving lane ships the flag, and a CI gate requires it: the 27B units and `run.sh` since v1.2.6, the flash launcher since v1.8.5, where the lane was measured holding a core at 101 % for 12 h 21 min of idle because the launcher had been written without it. A/B on the reference box: scheduler CPU 101 % -> 1.7 % at idle, module power 12.1 -> 10.5 W, and wake-up TTFT unchanged (0.234-0.240 s before, 0.234-0.239 s after, measured after 60 s and 300 s of idle), throughput in family (41.5 tok/s code, 52.8 math).

**Metrics**: every text lane passes `--enable-metrics` (the image lane exports
none), so Prometheus scrapes `/metrics` (request rates, KV usage, acceptance
lengths under speculative decoding, queue depth). Since v1.17 the engine port is
on loopback, so a scraper on another machine reads `http://<box>:30001/metrics`,
which the proxy relays like every route; one on the box can read
`http://127.0.0.1:30000/metrics` directly. The endpoint needs no key on either
port (with the proxy's per-client identity file set, it needs a listed key like
every route but `/health`), the same trust model the proxy port's whole surface
already assumes (trusted network: loopback, tailnet; see SECURITY.md), and it
arrives at your next `./install.sh` re-run and engine start.

## Extras (opt-in)

Three field-tested pieces from the reference box, deliberately not part of the default
install because they touch things beyond the serving stack:

**`extras/opencode/auto-continue.js`**: an opencode plugin that automatically resumes a
session interrupted by a transient technical error (tool-call delta without id, timeout,
network reset) or left stuck right after a context compaction, so a one-off incident no
longer freezes an overnight run. It never resumes after a deliberate abort, a permission
prompt, or an auth/quota problem, and stops after 25 relaunches without progress. Install:

```bash
mkdir -p ~/.config/opencode/plugins && cp extras/opencode/auto-continue.js ~/.config/opencode/plugins/
```

Plugins load when opencode starts (a running session never picks it up). Log at
`~/.config/qwen38/auto-continue.log`; tune with `AC_THROTTLE_MS`, `AC_IDLE_DELAY_MS`,
`AC_MAX_CONSECUTIVE`, `AC_LOG`.

**`extras/gguf/`**: a note, not a lane. What llama.cpp measured on this box against the
SGLang path (25.6 tok/s on code against 32-40, prose 17.7-18.2 against 17-22, prefill
about a third), the four traps that make a GGUF benchmark on GB10 lie to you, and what
evidence would make a GGUF target worth adding. Written for
[issue #12](https://github.com/hasso5703/dgx-spark-qwen38/issues/12).

**`extras/cake-ingress/`**: ingress anti-bufferbloat. While a model download saturates
your link, the queue builds up inside the ISP box and everything else drowns (measured on
the reference box: 1 ms ping became a 4797 ms average and the tunnel in front of the API
answered 502). The fix shapes RECEIVED traffic just under your real link capacity with
CAKE, so the queue forms on the Spark where it is scheduled fairly; SSH and the API stay
at a few milliseconds while the download still runs at ~97 % speed. You must pass your
own measured downlink (never the NIC speed; the interface is auto-detected):

```bash
BANDWIDTH=950Mbit  extras/cake-ingress/setup.sh    # 1 Gb/s link (the reference box)
BANDWIDTH=475Mbit  extras/cake-ingress/setup.sh    # 500 Mb/s link
BANDWIDTH=2350Mbit extras/cake-ingress/setup.sh    # 2.5 Gb/s link
extras/cake-ingress/setup.sh --uninstall           # back to stock networking
```

Boot-persistent (`cake-ingress.service`). Verify with a `ping 1.1.1.1` kept running
during a big download. The full bandwidth sweep and the reasoning are in the script's
header; setting BANDWIDTH too high is the one mistake that silently does nothing.

## Upgrading from an earlier version

```bash
cd dgx-spark-qwen38 && git pull && ./install.sh
```

Your choices survive the upgrade: the API key, the patched template, your own systemd drop-ins
under `/etc/systemd/system/qwen38-sglang.service.d/`, the opencode on/off choice (v1.5.9), and (since v1.3) the installed target
model, port and HF cache location, which are read from the installed unit (v1.4: units; a box
serving the flash target keeps it, exactly like a 27B choice). The unit itself is
rewritten on the repo's current flags (the previous one is backed up to
`~/.config/qwen38/<unit>.bak-preupdate`) and the service restarts on the new
config. v1.3 -> v1.4 changes nothing by itself for a 27B box: the flash stack is only
downloaded and installed when you ask for it (`MODEL_CHOICE=flash`), and the regenerated
`opencode.json` gains an `xhigh` reasoning-effort variant. v1.4 -> v1.5 moves the flash
lane from vLLM to SGLang (working prefix caching; the upgrade keeps your port, cache and
model choices and regenerates the launch script on the new engine; the first boot writes
the 48 GB PLE backing file). **v1.7 -> v1.8 moves the flash lane onto the official image
and off this repo's overlay**: the upgrade pulls one 15 GB image, builds nothing, deletes
the previous N-gram table file so the next boot writes a fresh one (~12 min), and serves 4
concurrent requests instead of 1. It also raises that lane's one-prompt ceiling from 128,000
to 200,000 tokens and its opencode limits with it, so an agent client will start sending
longer conversations: that is measured, not assumed (needle 3/3 at 120K and 1/1 at 200K, host
memory floor 12.6 GiB). The rollback was `OVERLAY_FLASH=1 ./install.sh` until v1.18.7, which
retired it (the launcher of v1.8 does not fit the v1.7 image); the v1.7 path shipped whole in
v1.7.2. **27B boxes were untouched by
v1.8**, on purpose at the time; v1.14 moved that lane to the official image too, after
measuring both reasons it had stayed behind. Upgrading from v1.2.x also removes the deprecated Claude Code warmup drop-in if you had
installed it, and no longer writes `claude-code.env`: an existing copy keeps working and will
never be overwritten again (earlier versions regenerated it on every install, losing any
customization), but it is unmaintained; the supported client config is `opencode.json`. v1.1 → v1.2 downloads the ~4 GB
DFlash2 draft (up to v1.13 it also built a local serving image; v1.14 serves the official
one instead). Going back to the locally built image of v1.13 and earlier means going back to v1.13:
`SERVE_IMAGE=qwen38-dflash2:v1.2.3 ./install.sh` serves that tag if it is still on the box
(`docker images qwen38-dflash2`), but this version deletes the files that build it, so
`git checkout v1.13.0 && ./install.sh` is the path that works whether or not the image
survived. Nothing measured on this hardware argues for going back. To return to the DSpark config: `git checkout v1.1 && ./install.sh`.
Change history: [CHANGELOG.md](../CHANGELOG.md).

[Back to the README](../README.md)
