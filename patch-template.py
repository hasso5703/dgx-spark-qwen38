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

Usage: patch-template.py <hf_cache_dir> <output_path> [revision] [repo]
Idempotent: succeeds if the patches are already applied. When a revision is
given (sha or ref name like 'main'), the template is taken from that exact
snapshot; otherwise the most recently modified snapshot is used. The optional
repo arg (default: RadixArk/Qwen3.8-27B-NVFP4) selects which cached repo to
read the template from.
"""
import glob
import os
import sys

EFFORT_ANCHOR = (
    "    {%- set resolved_reasoning_effort = reasoning_effort|default('xhigh') %}\n"
    "    {%- if resolved_reasoning_effort not in ('xhigh', 'medium', 'low') %}"
)
EFFORT_PATCHED = (
    "    {%- set resolved_reasoning_effort = reasoning_effort|default('@@DEF@@') %}\n"
    "    {%- if resolved_reasoning_effort in ('max', 'high') %}\n"
    "        {%- set resolved_reasoning_effort = 'xhigh' %}\n"
    "    {%- elif resolved_reasoning_effort == 'minimal' %}\n"
    "        {%- set resolved_reasoning_effort = 'low' %}\n"
    "    {%- endif %}\n"
    "    {%- if resolved_reasoning_effort not in ('xhigh', 'medium', 'low', 'lean') %}"
)

# -- Patch 3: the "lean" effort level, and it becomes the default -------------
# Qwen ships three levels and defaults to the most expensive one. Measured on
# this box (Qwen3.8-27B NVFP4 on SGLang, 7,008 runs; method and tables in
# LEAN.md):
#
#   On 364 public problems (HumanEval 164 + GSM8K 200), xhigh costs 3.19x the
#   thinking tokens of medium [2.73, 3.69] and scores 2.2 points LOWER
#   (McNemar p=0.057). On HumanEval alone it is 4.3 points lower at p=0.039,
#   and five of its ten failures there are truncations: it spent the whole
#   budget thinking and returned nothing.
#
#   On 58 underspecified requests, xhigh spends 2,478 thinking tokens and 237
#   seconds each against 420 and 73 for medium, truncates 10.3% of the time and
#   returns an empty answer 6.9% of the time.
#
# "lean" is 74 words that cost 0.436x the thinking tokens of medium
# [0.40, 0.47] on those 58 requests with no measured quality change, and 0.047x
# of xhigh [0.04, 0.06] with 17.2 points MORE usable answers [+10.9, +24.1].
#
# Qwen's three levels are left byte-identical: ask for medium and you get
# Qwen's medium, so numbers stay comparable with everyone else's. Only the
# DEFAULT moves, which is what every client that sends nothing receives.
# LEAN_DEFAULT=0 keeps Qwen's xhigh default while still offering the level.
LEAN_INSTRUCTIONS = "Answer immediately, with no reasoning, whenever the request asks for something you can simply write down: a rename, a reformat, a definition, a lookup, a single concrete edit. Reason only when producing the answer needs steps you cannot skip.\n\nIf the request is underspecified, pick the most common sensible interpretation, state it in one line, and proceed. If you notice yourself checking something twice, or weighing the same options again, stop there and commit."
LEAN_ANCHOR = "    {%- if resolved_reasoning_effort == 'xhigh' %}\n"
LEAN_PATCHED = (
    "    {%- if resolved_reasoning_effort == 'lean' %}\n"
    "        {%- set reasoning_instructions = '" + LEAN_INSTRUCTIONS + "' %}\n"
    "    {%- elif resolved_reasoning_effort == 'xhigh' %}\n"
)

# Patch 4: the refusal message must name what is actually supported, or a client
# that sends a bad value is told to use a default that is no longer the default.
MSG_ANCHOR = (
    "        {{- raise_exception('Unexpected reasoning effort ' ~ reasoning_effort ~ "
    "'. Supported types are xhigh (default), medium, and low.') }}"
)
MSG_PATCHED = (
    "        {{- raise_exception('Unexpected reasoning effort ' ~ reasoning_effort ~ "
    "'. Supported types are @@LEVELS@@.') }}"
)
# the refusal names the default the template really has (it said "lean (default)" on a box
# installed with LEAN_DEFAULT=0, whose default is xhigh)
MSG_LEVELS = {True: "lean (default), xhigh, medium, and low", False: "lean, xhigh (default), medium, and low"}

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
    if len(sys.argv) not in (3, 4, 5):
        sys.exit(__doc__)
    hf_cache, out_path = sys.argv[1], sys.argv[2]
    revision = sys.argv[3] if len(sys.argv) >= 4 else None
    repo = sys.argv[4] if len(sys.argv) == 5 else "RadixArk/Qwen3.8-27B-NVFP4"
    repo_dir = f"{hf_cache}/hub/models--{repo.replace('/', '--')}"
    chosen = None
    if revision:
        ref_file = f"{repo_dir}/refs/{revision}"
        if os.path.isfile(ref_file):  # ref name (e.g. 'main') -> resolve to the sha
            revision = open(ref_file, encoding="utf-8").read().strip()
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
    tpl = open(chosen, encoding="utf-8").read()

    # LEAN_DEFAULT=0 installs the level without making it the default, for a box
    # that wants Qwen's shipped behaviour until it has run its own numbers. Unset, the
    # template already installed decides: the choice was read from the environment of
    # every run, so a box installed with LEAN_DEFAULT=0 went back to lean at its next
    # ./install.sh or cockpit Switch, neither of which passes it (found in review,
    # 2026-09-24). LEAN.md presents it as a kept choice.
    asked = os.environ.get("LEAN_DEFAULT")
    if asked is not None:
        lean_default = asked != "0"
    else:
        try:
            with open(out_path, encoding="utf-8") as f:
                installed = f.read()
        except OSError:
            installed = ""
        lean_default = "reasoning_effort|default('xhigh')" not in installed
    effort_patched = EFFORT_PATCHED.replace("@@DEF@@", "lean" if lean_default else "xhigh")
    msg_patched = MSG_PATCHED.replace("@@LEVELS@@", MSG_LEVELS[lean_default])
    print(f"default reasoning effort: {'lean' if lean_default else 'xhigh'}"
          + ("" if asked is not None else " (kept from the installed template)" if installed else ""))

    for name, anchor, patched, marker in (
        ("reasoning_effort", EFFORT_ANCHOR, effort_patched, "'minimal'"),
        ("lean", LEAN_ANCHOR, LEAN_PATCHED, "'lean' %}"),
        ("effort-message", MSG_ANCHOR, msg_patched, "Supported types are lean"),
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

    # The engine reads this file when it starts, and install.sh keeps a running engine only
    # when nothing it reads was written after it started: an identical template is left as
    # it is, mtime included, or every run would make the next one restart the engine.
    try:
        with open(out_path, encoding="utf-8") as f:
            if f.read() == tpl:
                print(f"patched template unchanged: {out_path}")
                return
    except OSError:
        pass
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(tpl)
    print(f"patched template written to {out_path}")


if __name__ == "__main__":
    main()
