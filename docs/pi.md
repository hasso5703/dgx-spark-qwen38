# pi and omp integration

What `pi-gen.py` generates for [pi](https://pi.dev) and [omp](https://omp.sh) (oh-my-pi), the coding agents that read no `opencode.json`. Companion to [docs/opencode.md](opencode.md): same engines, same key, same limits, from one source.

## Generated artifacts, user-copied

pi (`~/.pi/agent`) and omp (`~/.omp/agent`) keep their default model, provider names and limits as plain values in their own files. `pi-gen.py` writes the agents' side of this box's facts as ready files next to `~/.config/qwen38/opencode.json`, the artifact install.sh fits, `./switch-model.sh` re-points and `oc-fit-limits.py` re-fits:

```
~/.config/qwen38/pi/models.json      ~/.config/qwen38/pi/settings.json
~/.config/qwen38/omp/models.yml      ~/.config/qwen38/omp/config.yml
```

Run `./pi-gen.py` once to create them. From then on `./switch-model.sh` (block 4f) and a fit (`oc-fit-limits.py`, the cockpit's button) regenerate them; a box that never ran it gets no such files. `python3 pi-gen.py --dry-run` shows what a regeneration would change.

**The repo never writes into `~/.pi` or `~/.omp`;** installing is your copy. On an agent with no config of its own yet:

```bash
mkdir -p ~/.pi/agent && cp -n ~/.config/qwen38/pi/*.json ~/.pi/agent/
mkdir -p ~/.omp/agent && cp -n ~/.config/qwen38/omp/*.yml ~/.omp/agent/
```

`cp -n` copies nothing over a file that exists: if you already have a `models.json` or `models.yml` with other providers, merge the `qwen38` and `flashnext` providers into it by hand (each agent reads one such file), and likewise the default model and compaction settings. Copy again after a switch or a fit, and the agent carries the new lane.

## One build, two serializations

The pi pair is canonical; the omp pair is converted from it: same models array, thinking block added, pi's `inputLimits` and `compat` dropped (no room for them in omp's schema, and a schema error makes omp disable the whole file), default provider and bare model id into `modelRoles.default`. Same values, different files, built once: they cannot disagree. The artifact's `{file:...}` key becomes both agents' `!cat <path>` form, which they run in a shell at request time: no secret in their files, and a rotated key is read on the next request.

## Reasoning effort, the system prompt, images, compaction

The patched template reads `reasoning_effort`: `lean` is its default, `max` and `high` read as `xhigh`, `minimal` as `low`. SGLang takes the effort from `chat_template_kwargs.reasoning_effort` or from a top-level `reasoning_effort`, and validates the top-level one against `none`, `minimal`, `low`, `medium`, `high`, `xhigh` and `max`: a top-level `lean` is a 400.

- **pi.** `modelThinkingLevels` is `off` for our models: no effort field, so the template's default, `lean` (what "no variant" means for opencode). A level picked in pi goes out as the top-level field: `minimal` reads as `low`, and `high` as `xhigh` (pi sends `high` for `xhigh` and `max` too). Each model also carries `compat.supportsDeveloperRole: false`: pi sends its system prompt as the `developer` role to a reasoning model on a provider it does not know, and the served template refuses that role (every request was a 400 "Unexpected message role." without it).
- **omp.** Its picker goes through the same enum, and `compat.reasoningEffortMap` translates the pick for the provider: the generator gives it `efforts: [minimal, low, medium, xhigh]` with `minimal` mapped to `lean` and the default, the same ladder (`lean/low/medium/xhigh`) the artifact's `variants` declare; `off` lands on `lean`, and a level the list does not hold on the nearest one below it (`high` on `medium`, `max` on `xhigh`). `compat.qwenTemplateReasoningEffort` routes the mapped level onto `chat_template_kwargs.reasoning_effort` (it ships on by default for Qwen 3.8+ ids on LM Studio, llama.cpp discovery and vLLM only, none of them this proxy's provider id: without it the picker moved and nothing was sent), and `compat.thinkingFormat: qwen-chat-template`, omp's dialect for vLLM and SGLang, sends it there alone: omp's default dialect also sent a top-level `lean`, a 400 on every request at the default level.

`images.autoResize` (on) plus a generated 1568×1568 / 512 KiB per-model `resize` cap keep an attachment from blowing the prompt past the lane. Compaction follows the artifact: `keepRecentTokens` = opencode's `preserve_recent_tokens`, and each lane's `reserveTokens` = that lane's output cap.

Verified on 2026-10-01 with pi 0.99.2 (`@earendil-works/pi-coding-agent`) and omp 18.4.9 (`@oh-my-pi/pi-coding-agent`, which needs Bun 1.3.14 or later), through the proxy to the flash lane: a reply at every thinking level of both agents, and a tool call (`read`) in each. Both lanes' images validate the effort the same way.

## What still differs

pi lifts no hidden output cap (none exists) and retries transient errors itself; the [auto-continue plugin](../extras/opencode/auto-continue.js)'s stuck-after-compaction case is still opencode's edge. Nothing pins pi or omp versions (opencode's pin exists because repo behaviour was read out of its binary): a release of either that changes these fields can need a regeneration. omp's roles, fallback chains and path-scoped models stay yours: the artifact sets only the two providers and the `default` role's model. The cockpit's Agent view stays opencode-only.

[Back to the README](../README.md)
