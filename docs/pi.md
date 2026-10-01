# pi and omp integration

What `pi-gen.py` generates for [pi](https://pi.dev) and [omp](https://omp.sh) (oh-my-pi), the coding agents that read no `opencode.json`. Companion to [docs/opencode.md](opencode.md): same engines, same key, same limits, from one source.

## Generated artifacts, user-copied

pi (`~/.pi/agent`) and omp (`~/.omp/agent`) keep their default model, provider names and limits as plain values in their own files. `pi-gen.py` writes the agents' side of this box's facts as ready files next to `~/.config/qwen38/opencode.json` — the artifact install.sh fits, `./switch-model.sh` re-points and `oc-fit-limits.py` re-fits:

```
~/.config/qwen38/pi/models.json      ~/.config/qwen38/pi/settings.json
~/.config/qwen38/omp/models.yml      ~/.config/qwen38/omp/config.yml
```

**The repo never writes into `~/.pi` or `~/.omp`;** installing is your one `cp`:

```bash
mkdir -p ~/.pi/agent && cp ~/.config/qwen38/pi/*.json ~/.pi/agent/
# omp: merge config.yml into yours if you have one; models.yml is ours
mkdir -p ~/.omp/agent && cp ~/.config/qwen38/omp/models.yml ~/.omp/agent/
```

`./switch-model.sh` regenerates these files on every switch (block 4f), so re-copy after a switch or a fit and the agent carries the new lane. `python3 pi-gen.py --dry-run` shows what a regen would change.

## One build, two serializations

The pi pair is canonical; the omp pair is converted from it (same models array, thinking block added, pi's `inputLimits` dropped — no room for it in omp's schema, and a schema error makes omp disable the whole file; default provider + bare model id → `modelRoles.default`): same values, different files, built once — they cannot disagree. The artifact's `{file:...}` key becomes both agents' `!cat <path>` form: read at request time, no secret in their files, rotation-proof (restart the agent after a rotation).

## Reasoning effort, multimodal, compaction

The patched template accepts `lean` and maps `max`/`high` → `xhigh`, `minimal` → `low`. pi can only offer its fixed level enum, so `modelThinkingLevels` is `off` for our models — no effort field sent, which lands on the template's default, `lean` (the same thing "no variant" means for opencode). omp's picker goes through the same enum, and its `compat.reasoningEffortMap` translates the pick to the provider, so the generator gives it `efforts: [minimal, low, medium, xhigh]` with `minimal → lean` mapped and defaulting — the same real ladder (`lean/low/medium/xhigh`) the artifact's `variants` declare, with `lean` behind `minimal`. What the picker picks reaches the engine only through `compat.qwenTemplateReasoningEffort`: it routes the mapped level onto `chat_template_kwargs.reasoning_effort`, what the patched template reads, and it ships on by default for Qwen 3.8+ ids on LM Studio, llama.cpp discovery and vLLM only, none of them this proxy's provider id. So the generator sets the flag beside every thinking block; without it the picker moved and nothing was sent (request capture against the omp binary, 2026-10-01). `images.autoResize` (on) plus a generated 1568×1568 / 512 KiB per-model `resize` cap keep an attachment from blowing the prompt past the lane. Compaction follows the artifact: `keepRecentTokens` = opencode's `preserve_recent_tokens`, and each lane's `reserveTokens` = that lane's output cap.

## What still differs

pi lifts no hidden output cap (none exists) and retries transient errors itself — the [auto-continue plugin](../extras/opencode/auto-continue.js)'s stuck-after-compaction case is still opencode's edge. Nothing pins pi or omp versions (opencode's pin exists because repo behaviour was read out of its binary). omp's roles/fallback chains/path-scoped models stay yours: the artifact sets only the two providers and the `default` role's model. The cockpit's Agent view stays opencode-only.

[Back to the README](../README.md)
