# opencode integration

What the installer writes for [opencode](https://opencode.ai), why each piece is there, and how to opt out. Moved out of the README in v1.14.2.

The installer writes a complete, ready-to-use [opencode](https://opencode.ai) config at `~/.config/qwen38/opencode.json` (the API key is referenced via `{file:...}`, no secret inside). It contains one provider per installed engine (`qwen38` for the 27B pair, `flashnext` for flash), each with `low` / `medium` / `xhigh` reasoning-effort variants (no variant = the template's own default, xhigh), and its default model follows the installed target (`./switch-model.sh` re-points it on every switch):

```bash
# no opencode config yet? use it as-is:
mkdir -p ~/.config/opencode && cp ~/.config/qwen38/opencode.json ~/.config/opencode/opencode.json
# already have one? merge the "qwen38" (and/or "flashnext") provider block into it
opencode
```

Do not want any of it? `./install.sh --no-opencode` (one-liner: `| bash -s -- --no-opencode`) installs the API only: no generated config, no `oc` launcher, and `switch-model.sh` never touches your opencode default model. The choice is remembered by later runs (marker `~/.config/qwen38/opencode.off`); `./install.sh --with-opencode` turns it back on. Your own `~/.config/opencode/opencode.json` is never rewritten in either mode: the installer only merges the served lane's limits into it when the integration is on.

What the shipped config gets right for you:

1. **The hidden 32K output cap**: opencode sends `max_tokens = min(limit.output, OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX or 32000)`. Without that env var, a long thinking phase hits 32000 tokens, the turn ends silently (`finish_reason: length`, no text, no tool call) and you have to re-prompt. The installer ships an **`oc` launcher** (`~/.local/bin/oc`, skipped if an unrelated `oc` binary exists) that exports the right value and execs `opencode --yolo`: launch with `oc` instead of `opencode` and the cap sits above every output limit this repo declares, on every target and in either context mode. It is deliberately a ceiling rather than the installed target's own number, because `limit.output` is what follows a model switch: a ceiling copied from the installed target survives the switch and cuts the next lane's turn in the same silence (`./oc-limits.sh --max-out` is where both installers read it). Note that `--yolo` auto-approves every tool action (how the reference box runs); remove it from the launcher file if you prefer per-action prompts.
2. **Limits that can never 400**: the server rejects any request where `input + max_tokens` exceeds the window (no clamping), so the config ships `context/input 194048, output 64000` in native mode (258048 worst case, a 4096 margin under 262144, whether the 32K cap is lifted or not) and `700000/200000` in 1m mode (worst case 880000, under the worst measured KV pool).
3. **Reasoning-effort variants**: the generated config declares `medium` and `low` variants (ctrl+t in the TUI); the default is the model's `xhigh`. This works because the patched template accepts and maps effort tiers (`max`/`high` → `xhigh`, `minimal` → `low`, [contributed by helge](https://forums.developer.nvidia.com/t/380257/10)); any client sending an unmapped tier would get a 500 on the stock template.
4. **Mid-conversation system messages**: some agent clients inject system messages after turn 1; the stock template raises `System message must be at the beginning`. Patched to render them as `<system-reminder>` blocks.
5. **Vision declared**: `attachment` + `modalities` are set, so image attachments and on-disk image reads work end to end (the model is natively multimodal).

On service installs the generated config points at the **keepalive proxy port** (`PORT+1`), not the server directly, and that is deliberate: SGLang buffers tool-call arguments while they stream (127 s of measured silence on one 400-line file write, at native context), and opencode drops a stream after roughly 140-180 s without a real chunk. The proxy (`qwen38-keepalive.service`, vendored `keepalive-proxy.py`) fills those silences with protocol-correct keepalives, at SSE event boundaries only, and makes sure a client that gives up does not leave a generation running (v6.14: it names every request with `x-override-rid` so it can abort one that has not produced anything yet, aborts before closing the socket, and drains the answer where the engine offers no rid to abort with). With `./install.sh --no-service` there is no proxy: the config then points at the server directly, and huge single-file writes may abort. One more caveat, measured: SGLang's `--api-key` only accepts `Authorization: Bearer`, **not** `x-api-key`.

[Back to the README](../README.md)
