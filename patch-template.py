#!/usr/bin/env python3
"""Patch RadixArk/Qwen3.8-27B-NVFP4's chat template for agentic clients (Claude Code).

Two surgical fixes, no behavior change otherwise:
  1. reasoning_effort 'max'/'high' -> 'xhigh' (clients like Claude Code send "max";
     the stock template only accepts xhigh/medium/low and raises a 500 otherwise).
  2. Mid-conversation system messages -> rendered as <system-reminder> blocks
     (Claude Code injects system-reminders after turn 1; the stock template raises
     'System message must be at the beginning').

The 'minimal' -> 'low' mapping (an OpenAI effort tier) was contributed
by forum user helge: https://forums.developer.nvidia.com/t/380257/10

Usage: patch-template.py <hf_cache_dir> <output_path>
Idempotent: succeeds if the patches are already applied.
"""
import glob
import sys

EFFORT_ANCHOR = (
    "    {%- set resolved_reasoning_effort = reasoning_effort|default('xhigh') %}\n"
    "    {%- if resolved_reasoning_effort not in ('xhigh', 'medium', 'low') %}"
)
EFFORT_PATCHED = (
    "    {%- set resolved_reasoning_effort = reasoning_effort|default('xhigh') %}\n"
    "    {%- if resolved_reasoning_effort in ('max', 'high') %}\n"
    "        {%- set resolved_reasoning_effort = 'xhigh' %}\n"
    "    {%- elif resolved_reasoning_effort == 'minimal' %}\n"
    "        {%- set resolved_reasoning_effort = 'low' %}\n"
    "    {%- endif %}\n"
    "    {%- if resolved_reasoning_effort not in ('xhigh', 'medium', 'low') %}"
)
SYSTEM_ANCHOR = (
    "    {%- if message.role == \"system\" %}\n"
    "        {%- if not loop.first %}\n"
    "            {{- raise_exception('System message must be at the beginning.') }}\n"
    "        {%- endif %}"
)
SYSTEM_PATCHED = (
    "    {%- if message.role == \"system\" %}\n"
    "        {%- if not loop.first %}\n"
    "            {{- '<|im_start|>user\\n<system-reminder>\\n' + content + '\\n</system-reminder><|im_end|>' + '\\n' }}\n"
    "        {%- endif %}"
)


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    hf_cache, out_path = sys.argv[1], sys.argv[2]
    hits = sorted(glob.glob(
        f"{hf_cache}/hub/models--RadixArk--Qwen3.8-27B-NVFP4/snapshots/*/chat_template.jinja"
    ))
    if not hits:
        sys.exit(f"chat_template.jinja not found under {hf_cache} — run the checkpoint download first")
    tpl = open(hits[-1]).read()

    for name, anchor, patched, marker in (
        ("reasoning_effort", EFFORT_ANCHOR, EFFORT_PATCHED, "'minimal'"),
        ("system-reminder", SYSTEM_ANCHOR, SYSTEM_PATCHED, "<system-reminder>"),
    ):
        if anchor in tpl:
            tpl = tpl.replace(anchor, patched, 1)
            print(f"patch '{name}': applied")
        elif marker in tpl:
            print(f"patch '{name}': already present upstream — nothing to do")
        else:
            sys.exit(
                f"patch '{name}': anchor not found and fix not present — the upstream "
                "template changed. Please open an issue with the template revision."
            )

    with open(out_path, "w") as f:
        f.write(tpl)
    print(f"patched template written to {out_path}")


if __name__ == "__main__":
    main()
