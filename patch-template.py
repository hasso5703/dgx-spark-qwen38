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

Usage: patch-template.py <hf_cache_dir> <output_path> [revision]
Idempotent: succeeds if the patches are already applied. When a revision is
given (sha or ref name like 'main'), the template is taken from that exact
snapshot; otherwise the most recently modified snapshot is used.
"""
import glob
import os
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
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    hf_cache, out_path = sys.argv[1], sys.argv[2]
    revision = sys.argv[3] if len(sys.argv) == 4 else None
    repo_dir = f"{hf_cache}/hub/models--RadixArk--Qwen3.8-27B-NVFP4"
    chosen = None
    if revision:
        ref_file = f"{repo_dir}/refs/{revision}"
        if os.path.isfile(ref_file):  # ref name (e.g. 'main') -> resolve to the sha
            revision = open(ref_file).read().strip()
        cand = f"{repo_dir}/snapshots/{revision}/chat_template.jinja"
        if os.path.isfile(cand):
            chosen = cand
        else:
            print(f"note: pinned revision {revision[:12]} has no chat_template.jinja snapshot, "
                  "falling back to the newest one")
    if chosen is None:
        hits = glob.glob(f"{repo_dir}/snapshots/*/chat_template.jinja")
        if not hits:
            sys.exit(f"chat_template.jinja not found under {hf_cache}. Run the checkpoint download first")
        chosen = max(hits, key=os.path.getmtime)
        if len(hits) > 1:
            print(f"note: {len(hits)} snapshots present, using the most recent: {chosen.split('/')[-2][:12]}")
    tpl = open(chosen).read()

    for name, anchor, patched, marker in (
        ("reasoning_effort", EFFORT_ANCHOR, EFFORT_PATCHED, "'minimal'"),
        ("system-reminder", SYSTEM_ANCHOR, SYSTEM_PATCHED, "<system-reminder>"),
    ):
        if anchor in tpl:
            tpl = tpl.replace(anchor, patched, 1)
            print(f"patch '{name}': applied")
        elif marker in tpl:
            print(f"patch '{name}': already present upstream, nothing to do")
        else:
            sys.exit(
                f"patch '{name}': anchor not found and fix not present: the upstream "
                "template changed. Please open an issue with the template revision."
            )

    with open(out_path, "w") as f:
        f.write(tpl)
    print(f"patched template written to {out_path}")


if __name__ == "__main__":
    main()
