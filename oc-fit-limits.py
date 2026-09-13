#!/usr/bin/env python3
"""Fit the opencode limits to the engine that is actually serving.

Usage: oc-fit-limits.py [--dry-run] [--engine http://127.0.0.1:30000]

opencode's `limit.context` is what it grows a conversation up to before it
compacts, and `limit.output` is what it asks the engine to generate. Both are
static numbers in a config file, while the KV pool they must fit into is
decided at boot and changes with the checkpoint: 863,398 tokens for the 27B
NVFP4 build, 771,139 for Qwen's FP8 one, 382,706 for that same FP8 build before
the KV cache was pinned to fp8. Declaring more than the pool can serve does not
fail early: the session grows until the keepalive proxy refuses a prompt it
cannot serve, mid-conversation (field case 2026-08-30).

So this reads the pool from the live engine and writes limits that fit, using
the same usable share the proxy's own guard applies, minus a margin for the
boot lottery (the pool moves by a few percent from one boot to the next).

It edits only the `limit` object of the served lane's model, in both the
generated artifact and the config opencode really reads, through
oc-merge-limits.py, and does nothing at all when the integration is off.
"""
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", Path.home() / ".config/qwen38"))
REPO_DIR = Path(__file__).resolve().parent
# Same knob and default as the proxy's oversize guard, so one prompt that opencode
# is willing to build is one prompt the proxy is willing to relay.
USABLE = 1.0 - float(os.environ.get("OVERSIZE_MARGIN_FRAC", "0.08"))
# The pool is not the same on every boot (measured: 863,398 then 893,479 then
# 913,334 for the same checkpoint). Leave room so today's limits still fit
# tomorrow's boot.
BOOT_MARGIN = float(os.environ.get("OC_BOOT_MARGIN", "0.10"))
# Output is a slice of the budget, capped: past this, a runaway generation costs
# more pool than any real answer needs.
OUTPUT_SHARE = 0.25
OUTPUT_CAP = 200_000
# When a lane sets a prompt ceiling, the opencode context must sit under it, and
# by how much is a derivation, not a guess.
#
# opencode compacts when the LAST response's reported usage reaches
# limit.input - min(COMPACTION_RESERVE, its output cap), so its threshold is
# context - COMPACTION_RESERVE (opencode 1.18.27, SessionCompaction). It decides
# between turns; the next turn then appends its tool results and sends. So what
# must fit under the ceiling is threshold + one worst step:
#
#     context - COMPACTION_RESERVE + WORST_STEP <= ceiling
#     context <= ceiling - (WORST_STEP - COMPACTION_RESERVE)
#
# WORST_STEP is measured, not assumed: 43,863 tokens, the largest single-step
# prompt growth over 2,156 flash-lane steps in opencode's own session store
# (2026-09; p99 is 18,512). The derived minimum is 23,863; it is rounded up to
# the next 5,000 so this tool and the oc-limits.sh table land on the same number
# (200,000 - 25,000 = 175,000) instead of one grid step apart.
#
# Context == ceiling means the proxy refuses before compaction fires, which is
# the exact failure this tool exists to prevent.
COMPACTION_RESERVE = int(os.environ.get("OC_COMPACTION_RESERVE") or "20_000")
WORST_STEP = int(os.environ.get("OC_WORST_STEP") or "43_863")
_MARGIN_FLOOR = max(0, WORST_STEP - COMPACTION_RESERVE)
CEILING_MARGIN = int(os.environ.get("OC_CEILING_MARGIN") or
                     -(-_MARGIN_FLOOR // 5000) * 5000)
LANE_MODEL = {"qwen3.8-27b": "qwen38", "qwen3.8-flash-next": "flashnext"}


def engine_info(base: str) -> dict:
    key = (CONFIG_DIR / "api-key").read_text().strip()
    req = urllib.request.Request(base + "/get_server_info",
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def fit(pool: int, ceiling: int = 0) -> tuple[int, int]:
    """(context, output) that one request can always hold in this pool."""
    budget = int(pool * USABLE * (1.0 - BOOT_MARGIN))
    output = min(OUTPUT_CAP, int(budget * OUTPUT_SHARE))
    context = budget - output
    if ceiling > 0:                      # a per-lane prompt ceiling caps the context too
        context = min(context, max(0, ceiling - CEILING_MARGIN))
    return (context // 1000) * 1000, (output // 1000) * 1000


def ceiling_from_env(text: str) -> int:
    """PROMPT_CEILING_TOKENS out of `systemctl show -p Environment` output.

    systemd prints every variable on one line behind a single `Environment=`
    prefix, so the FIRST variable carries that prefix and the rest do not. The
    naive startswith() missed the ceiling whenever it happened to be listed
    first, which is one template reordering away and fails silently: a flash
    lane would then get 27B-sized opencode limits and refuse a prompt
    mid-conversation, the exact failure this tool exists to prevent.
    """
    for part in text.split():
        part = part[len("Environment="):] if part.startswith("Environment=") else part
        if part.startswith("PROMPT_CEILING_TOKENS="):
            try:
                return int(part.split("=", 1)[1] or 0)
            except ValueError:
                return 0
    return 0


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    base = "http://127.0.0.1:30000"
    if "--engine" in argv:
        base = argv[argv.index("--engine") + 1]
    if (CONFIG_DIR / "opencode.off").exists():
        print("opencode integration is off on this box (./install.sh --no-opencode): nothing to fit")
        return 0
    try:
        info = engine_info(base)
    except Exception as e:  # noqa: BLE001
        print(f"no engine to read at {base} ({type(e).__name__}): start one first, "
              f"the pool is only known once it has booted")
        return 1
    pool = int(info.get("max_total_num_tokens") or 0)
    served = info.get("served_model_name") or ""
    provider = LANE_MODEL.get(served)
    if not pool or not provider:
        print(f"cannot fit: pool={pool}, served model={served!r} is not one of {sorted(LANE_MODEL)}")
        return 1
    env = subprocess.run(["systemctl", "show", "qwen38-keepalive.service", "-p", "Environment"],
                         capture_output=True, text=True).stdout
    ceiling = ceiling_from_env(env)
    context, output = fit(pool, ceiling)
    # Rounding to the kilo bottoms out at zero on an implausibly small pool, and
    # writing "context": 0 into opencode's config would break it far more loudly
    # than not writing anything. No real engine gets here (the smallest pool this
    # repo has measured is 382,706), so say so rather than invent a floor.
    if context <= 0 or output <= 0:
        print(f"cannot fit: pool {pool:,} is too small to derive usable limits "
              f"(computed context {context}, output {output}); nothing written")
        return 1
    print(f"engine: {info.get('model_path')} serving as {served}")
    print(f"pool {pool:,} tokens, usable share {USABLE:.0%}, boot margin {BOOT_MARGIN:.0%}"
          + (f", lane ceiling {ceiling:,}" if ceiling else ""))
    print(f"limits that fit: context {context:,}, output {output:,} "
          f"(worst case {context + output:,} of {pool:,})")
    if dry:
        print("dry run: nothing written")
        return 0
    rc = 0
    for target in (CONFIG_DIR / "opencode.json", Path.home() / ".config/opencode/opencode.json"):
        if not target.exists():
            continue
        out = subprocess.run([sys.executable, str(REPO_DIR / "oc-merge-limits.py"), str(target),
                              provider, served, str(context), str(output)],
                             capture_output=True, text=True)
        print(f"  {target}: {(out.stdout or out.stderr).strip().splitlines()[-1] if (out.stdout or out.stderr).strip() else 'no output'}")
        if out.returncode not in (0, 3):
            rc = out.returncode
    if rc == 0:
        print("opencode now asks for no more than this engine can serve; "
              "restart opencode to pick the new limits up")
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
