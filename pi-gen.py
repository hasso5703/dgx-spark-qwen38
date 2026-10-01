#!/usr/bin/env python3
"""Generate the pi and omp configs from this repo's generated opencode.json.

Usage: pi-gen.py [--config <opencode.json>] [--out <dir>] [--dry-run]

pi (https://pi.dev) and omp (oh-my-pi, https://omp.sh) read no opencode.json,
so this writes the agents' side of this box's facts as ready files next to it
- never into ~/.pi or ~/.omp: copying them into the agent's own directory
stays the user's one cp:

  <out>/pi/models.json + settings.json    the canonical pair (pi's JSON)
  <out>/omp/models.yml + config.yml       the same values converted (omp YAML)

<config> defaults to ~/.config/qwen38/opencode.json, <out> to ~/.config/qwen38.
All read from the artifact: one provider per installed lane (the {file:...}
key as "!cat <path>": read at request time, no secret copied, rotation-proof),
per model name, modalities, contextWindow, maxTokens and a 1568x1568 /
512 KiB image resize limit; pi's settings.json adds the default model,
modelThinkingLevels "off" for our models (unset sends no effort field, which
is the patched template's lean default), keepRecentTokens from the artifact's
compaction block and per-lane reserveTokens = the lane's output cap. omp
gets the same values re-serialized: the same models array, thinking block
added, inputLimits dropped (its schema has no room for it); default provider
+ model id -> modelRoles.default. The thinking block lists the template's own
tiers (the artifact's variants, lean included, what pi's fixed enum cannot
offer) with defaultLevel lean; compat carries qwenTemplateReasoningEffort
plus a reasoningEffortMap, which route the pick onto
chat_template_kwargs.reasoning_effort, what the patched template reads.

switch-model.sh regenerates these files on every switch (block 4f): re-copy
them after a switch or an oc-fit-limits.py fit. A file whose content would
not change is not touched; --dry-run prints what would change. The YAML comes
from a small stdlib writer: this repo's runtime imports nothing outside the
stdlib, and PyYAML is a measuring-gate package (tests/testpy.sh). Exit 0 when
done or with nothing to generate (no artifact, as on a --no-opencode box),
1 on a failed write, 2 on usage errors, 3 on an unreadable artifact.
"""
import argparse
import json
import os
import sys

OURS = ("qwen38", "flashnext")
RESIZE = {"maxWidth": 1568, "maxHeight": 1568, "maxBytes": 524288, "jpegQuality": 75}


def read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return None


def write(path, text, dry):
    if read_text(path) == text:
        print(f"left as it is: {path}")
        return
    if dry:
        print(f"would write {path}")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        print(f"could not write {path}: {e}")
        sys.exit(1)
    print(f"wrote {path}")


def yd(d):
    """Block-style YAML (mapping insertion order, two-space indents), the shape
    yaml.safe_dump(sort_keys=False) wrote before this repo's stdlib rule met
    this file. The values are only dicts, lists, strings, ints and bools; the
    keys are schema names and the two provider ids, every one plain-safe. All
    strings go through json.dumps, whose output is a valid YAML double-quoted
    scalar too: quoting all of them is how a leading-'!' apiKey (a YAML tag
    indicator when plain) survives without a special case."""
    def sc(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, str):
            return json.dumps(v)
        return str(v)
    def lines(v, ind):
        pad = "  " * ind
        if isinstance(v, dict):
            out = []
            for k, val in v.items():
                if val and isinstance(val, (dict, list)):
                    out.append(f"{pad}{k}:")
                    out += lines(val, ind + 1)
                elif isinstance(val, dict):
                    out.append(f"{pad}{k}: {{}}")
                elif isinstance(val, list):
                    out.append(f"{pad}{k}: []")
                else:
                    out.append(f"{pad}{k}: {sc(val)}")
            return out
        out = []
        for it in v:
            if isinstance(it, (dict, list)) and it:
                inner = lines(it, ind + 1)
                out.append(f"{pad}- {inner[0][len(pad) + 2:]}")
                out += inner[1:]
            elif isinstance(it, dict):
                out.append(f"{pad}- {{}}")
            elif isinstance(it, list):
                out.append(f"{pad}- []")
            else:
                out.append(f"{pad}- {sc(it)}")
        return out
    return "\n".join(lines(d, 0)) + "\n"


def api_key(options):
    v = (options or {}).get("apiKey", "")
    if isinstance(v, str) and v.startswith("{file:") and v.endswith("}"):
        return "!cat " + v[6:-1]
    return v if isinstance(v, str) and v else "qwen38"


def model_meta(mid, m):
    lim = m.get("limit") or {}
    modal = (m.get("modalities") or {}).get("input") or ["text"]
    e = {"id": mid, "reasoning": True, "input": modal,
         "contextWindow": lim.get("context"), "maxTokens": lim.get("output"),
         "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}
    if m.get("name"):
        e["name"] = m["name"]
    if "image" in modal:
        e["inputLimits"] = {"images": {"resize": dict(RESIZE)}}
    return e


def build(artifact):
    """artifact -> (pi pair, {lane: the template's effort tiers}); pi can only
    offer its fixed level enum, so the lean-aware tier list (the artifact's own
    variants: what the patched template natively accepts) goes to omp alone."""
    provs, thinking, overrides, efforts, default = {}, {}, {}, {}, artifact.get("model")
    for prov, pcfg in (artifact.get("provider") or {}).items():
        if prov not in OURS or not isinstance(pcfg, dict):
            continue
        models = pcfg.get("models") or {}
        opts = pcfg.get("options") or {}
        base = opts.get("baseURL")
        if not base or not models:
            continue
        mid = next(iter(models))
        meta = model_meta(mid, models[mid] if isinstance(models[mid], dict) else {})
        provs[prov] = {"baseUrl": base, "api": "openai-completions",
                       "apiKey": api_key(opts), "models": [meta]}
        thinking[f"{prov}/{mid}"] = "off"
        overrides[f"{prov}/{mid}"] = {"reserveTokens": meta["maxTokens"]}
        tiers = list((models[mid] or {}).get("variants") or {})
        if tiers:
            efforts[f"{prov}/{mid}"] = tiers
    models_doc = {"providers": provs}
    settings = {}
    if isinstance(default, str) and "/" in default:
        settings["defaultProvider"], settings["defaultModel"] = default.split("/", 1)
    settings["modelThinkingLevels"] = thinking
    comp = {"enabled": True}
    preserve = (artifact.get("compaction") or {}).get("preserve_recent_tokens")
    if preserve:
        comp["keepRecentTokens"] = preserve
    comp["modelOverrides"] = overrides
    settings["compaction"] = comp
    return models_doc, settings, efforts


# omp's thinking levels are a fixed enum (like pi's); the template's own tiers
# are reached through compat.reasoningEffortMap, which maps the internal level
# onto the provider string, and compat.qwenTemplateReasoningEffort, which
# routes the mapped pick onto chat_template_kwargs.reasoning_effort, what the
# patched template reads. The catalog ships that flag on by default for Qwen
# 3.8+ ids on LM Studio, llama.cpp discovery and vLLM only, and this proxy's
# provider id is none of those: without it the picker moved and no effort
# field was sent (captured against omp 18.4.6, 2026-10-01). So the generator
# sets it beside every thinking block. omp validates models.yml itself: these
# spellings were fixed against its error output, not only the docs.
OMP_LEVEL = {"lean": "minimal", "low": "low", "medium": "medium", "minimal": "minimal",
             "high": "high", "xhigh": "xhigh", "max": "max"}


def omp_thinking(tiers):
    """template tiers -> (thinking block, compat), enum-clean. lean rides on
    minimal, high/max collapse onto xhigh, exactly as the patched template
    maps. compat always carries qwenTemplateReasoningEffort; the map only
    when a tier needs another spelling."""
    levels, rmap = [], {}
    for t in tiers:
        lvl = OMP_LEVEL.get(t)
        if not lvl:
            continue
        if lvl not in levels:
            levels.append(lvl)
        if lvl != t:
            rmap[lvl] = t
    if not levels:
        return None, None
    default = "minimal" if "lean" in tiers and "minimal" in levels else levels[0]
    thinking = {"mode": "effort", "efforts": levels, "defaultLevel": default}
    compat = {"qwenTemplateReasoningEffort": True}
    if rmap:
        compat["reasoningEffortMap"] = rmap
    return thinking, compat


def omp_convert(models_doc, settings, efforts):
    """The pi pair, re-serialized into omp's YAML shapes: same values, no rebuild.
    The thinking block carries omp's enum levels; reasoningEffortMap turns the
    selection onto the template's tiers (minimal -> lean) and, with
    qwenTemplateReasoningEffort set, omp routes it onto
    chat_template_kwargs.reasoning_effort."""
    provs = {}
    for prov, b in models_doc["providers"].items():
        entries = []
        for m in b["models"]:
            # inputLimits is pi's shape; omp's models.yml schema does not carry
            # it, and a schema error makes omp skip the whole file. models is an
            # ARRAY in omp too (verified against omp's own validation error).
            e = {k: v for k, v in m.items() if k != "inputLimits"}
            thinking, compat = omp_thinking(efforts.get(f"{prov}/{m['id']}", []))
            if thinking:
                e["thinking"] = thinking
            if compat:
                e["compat"] = compat
            entries.append(e)
        provs[prov] = {"baseUrl": b["baseUrl"], "api": b["api"], "apiKey": b["apiKey"],
                       "models": entries}
    cfg = {}
    if "defaultProvider" in settings:
        cfg["modelRoles"] = {"default": f"{settings['defaultProvider']}/{settings['defaultModel']}"}
    return {"providers": provs}, cfg


def main():
    ap = argparse.ArgumentParser(description="Generate the pi and omp configs from opencode.json")
    ap.add_argument("--config", default=os.path.join(os.environ.get("HOME", ""), ".config", "qwen38", "opencode.json"))
    ap.add_argument("--out", default=os.path.join(os.environ.get("HOME", ""), ".config", "qwen38"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        text = read_text(a.config)
        if text is None:
            print(f"{a.config} not found (a --no-opencode box?); nothing to generate.")
            return
        artifact = json.loads(text)
        if not isinstance(artifact, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as e:
        print(f"could not read {a.config}: {e}")
        sys.exit(3)
    models_doc, settings, efforts = build(artifact)
    if not models_doc["providers"]:
        print(f"NOTE: {a.config} has none of this repo's providers ({'/'.join(OURS)}): nothing to generate.")
        return
    omp_models, omp_cfg = omp_convert(models_doc, settings, efforts)
    jd = lambda d: json.dumps(d, indent=2) + "\n"
    write(os.path.join(a.out, "pi", "models.json"), jd(models_doc), a.dry_run)
    write(os.path.join(a.out, "pi", "settings.json"), jd(settings), a.dry_run)
    write(os.path.join(a.out, "omp", "models.yml"), yd(omp_models), a.dry_run)
    write(os.path.join(a.out, "omp", "config.yml"), yd(omp_cfg), a.dry_run)


if __name__ == "__main__":
    main()
