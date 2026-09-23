# opencode integration

What the installer writes for [opencode](https://opencode.ai), why each piece is there, and how to opt out. Moved out of the README in v1.14.2.

The installer writes a complete, ready-to-use [opencode](https://opencode.ai) config at `~/.config/qwen38/opencode.json` (the API key is referenced via `{file:...}`, no secret inside). It contains one provider per installed engine (`qwen38` for the 27B pair, `flashnext` for flash), each with `low` / `medium` / `xhigh` reasoning-effort variants (no variant = the template's own default, xhigh), and its default model follows the installed target (`./switch-model.sh` re-points it on every switch):

A box with no opencode config of its own gets this one as `~/.config/opencode/opencode.json`
(since v1.18.4: before, it was printed as a `cp` command, and a first install left opencode,
`oc` and the Agent tab with no provider for the box's own model, "Provider not found: qwen38").
A config you already have is kept: the installer merges the served lane's limits into it,
and adds the provider of a lane installed since (the 27B first, the flash lane later) when
it already has one of this repo's providers. When it has none of them, the installer says
so and leaves it alone: merge the `qwen38` (and/or `flashnext`) block from
`~/.config/qwen38/opencode.json` into it yourself.

```bash
oc        # opencode on this box's model, with the output cap lifted (see below)
```

`oc` and the cockpit's Agent tab also load that generated config through
`OPENCODE_CONFIG`, which opencode reads over your global one: their default is the model
this box serves even when your own config names another, and opencode can never fall back
to its free hosted model there. That fallback is what a fresh box got before v1.18.4 (with
no provider for the box, opencode answered with `big-pickle`, a cloud model). Plain
`opencode` reads only your own config.

## Which opencode: one pinned version, on every box

Four things this repo does were read out of one opencode binary and checked against its
behaviour: the compaction threshold `oc-fit-limits.py` sizes the limits for, the hidden
32,000-token output cap the `oc` launcher lifts, the overflow phrases the proxy answers
with so that a refusal makes opencode compact instead of failing, and `--yolo` /
`OPENCODE_PERMISSION` for the Agent tab. So `install.sh` pins the opencode it runs:
**1.18.32** (`OPENCODE_VERSION`, with the sha256 GitHub publishes for
`opencode-linux-arm64.tar.gz` in `OPENCODE_SHA256`). It was checked against the 1.18.27
the repo was measured on, on 2026-09-23: the same 21 overflow phrases and 3 exclusions
verbatim, `max_tokens` 182,000 with the launcher's variable and 32,000 without it on
both (recorded on a fake endpoint), the same compaction and permission code once the
bundler's chunk names are set aside, `serve --hostname` and the hidden `--yolo` on both.

What a run does with the opencode it finds:

| found | what `install.sh` does |
|---|---|
| none | downloads the pinned release, checks its sha256, installs it where opencode's own installer does (`~/.opencode/bin`) and adds that to `PATH` in `~/.bashrc` |
| the pinned version | nothing |
| an older one in `~/.opencode/bin` | replaces it the same way, checked |
| an older one elsewhere (npm, brew, bun) | `opencode upgrade <pinned>`, opencode's own upgrader, which knows how it was installed |
| a newer one | keeps it and says so: going back a version can leave sessions a newer opencode wrote unreadable |

Left alone, opencode installs its own patch releases (read out of its binary: only
`"autoupdate": false`, `"notify"` or a minor/major release stop it), which is how two boxes
installed a week apart end up on two versions. With the pin, the generated config and
yours get `"autoupdate": "notify"`: opencode still announces a release, and does not
install it itself. A stricter `false` you set is kept. The Agent tab says when the
opencode it serves is not the pinned one.

`OPENCODE_PIN=0 ./install.sh` keeps whatever opencode you have and leaves `autoupdate`
alone. `OPENCODE_VERSION=<x> OPENCODE_SHA256=<its digest>` pins another release (a
version without its checksum is refused: it would not be a pin). A download that does
not match its sha256 is not installed, and nothing about opencode fails the rest of the
install.

## Opting out

Do not want any of it? `./install.sh --no-opencode` (one-liner: `| bash -s -- --no-opencode`) installs the API only: no generated config, no `oc` launcher, and `switch-model.sh` never touches your opencode default model. The choice is remembered by later runs (marker `~/.config/qwen38/opencode.off`); `./install.sh --with-opencode` turns it back on. Your own `~/.config/opencode/opencode.json` is never rewritten in either mode: when the integration is on, the installer only merges into it the served lane's limits, its compaction block, the provider of a lane installed since and, with the version pinned, `"autoupdate": "notify"`, each by a targeted edit that keeps your comments and your other providers, with a dated backup first.

What the shipped config gets right for you:

1. **The hidden 32K output cap**: opencode sends `max_tokens = min(limit.output, OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX or 32000)`. Without that env var, a long thinking phase hits 32000 tokens, the turn ends silently (`finish_reason: length`, no text, no tool call) and you have to re-prompt. The installer ships an **`oc` launcher** (`~/.local/bin/oc`, skipped if an unrelated `oc` binary exists) that exports the right value and execs `opencode --yolo`: launch with `oc` instead of `opencode` and the cap sits above every output limit this repo declares, on every target and in either context mode. It is deliberately a ceiling rather than the installed target's own number, because `limit.output` is what follows a model switch: a ceiling copied from the installed target survives the switch and cuts the next lane's turn in the same silence (`./oc-limits.sh --max-out` is where both installers read it). Note that `--yolo` auto-approves every tool action (how the reference box runs); remove it from the launcher file if you prefer per-action prompts.
2. **Limits that can never 400**: the server rejects any request where `input + max_tokens` exceeds the window (no clamping), so the config ships `context/input 194048, output 64000` in native mode (258048 worst case, a 4096 margin under 262144, whether the 32K cap is lifted or not) and `700000/200000` in 1m mode as a starting pair, which the installer then fits to the pool the boot actually got (`oc-fit-limits.py`, after step 9): the pool changes from boot to boot (832,993 to 922,094 tokens over sixteen 27B boots on the reference box), and that pair does not fit the smaller ones. Since v1.18.2 the cockpit does the same fit by itself after a switch and a Start.
3. **Reasoning-effort variants**: the generated config declares `medium` and `low` variants (ctrl+t in the TUI); the default is the model's `xhigh`. This works because the patched template accepts and maps effort tiers (`max`/`high` → `xhigh`, `minimal` → `low`, [contributed by helge](https://forums.developer.nvidia.com/t/380257/10)); any client sending an unmapped tier would get a 500 on the stock template.
4. **Mid-conversation system messages**: some agent clients inject system messages after turn 1; the stock template raises `System message must be at the beginning`. Patched to render them as `<system-reminder>` blocks.
5. **Vision declared**: `attachment` + `modalities` are set, so image attachments and on-disk image reads work end to end (the model is natively multimodal).

On service installs the generated config points at the **keepalive proxy port** (`PORT+1`), not the server directly, and that is deliberate: SGLang buffers tool-call arguments while they stream (127 s of measured silence on one 400-line file write, at native context), and opencode drops a stream after roughly 140-180 s without a real chunk. The proxy (`qwen38-keepalive.service`, vendored `keepalive-proxy.py`) fills those silences with protocol-correct keepalives, at SSE event boundaries only, and makes sure a client that gives up does not leave a generation running (v6.14: it names every request with `x-override-rid` so it can abort one that has not produced anything yet, aborts before closing the socket, and drains the answer where the engine offers no rid to abort with). With `./install.sh --no-service` there is no proxy: the config then points at the server directly, and huge single-file writes may abort. One more caveat, measured: SGLang's `--api-key` only accepts `Authorization: Bearer`, **not** `x-api-key`.

[Back to the README](../README.md)
