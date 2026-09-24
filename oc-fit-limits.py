#!/usr/bin/env python3
"""Fit the opencode limits to the engine that is actually serving.

Usage: oc-fit-limits.py [--dry-run] [--restart-agent] [--engine http://127.0.0.1:30000]

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
import urllib.error
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
# (250,000 - 25,000 = 225,000) instead of one grid step apart.
#
# Context == ceiling means the proxy refuses before compaction fires, which is
# the exact failure this tool exists to prevent.
COMPACTION_RESERVE = int(os.environ.get("OC_COMPACTION_RESERVE") or "20_000")
WORST_STEP = int(os.environ.get("OC_WORST_STEP") or "43_863")
_MARGIN_FLOOR = max(0, WORST_STEP - COMPACTION_RESERVE)
CEILING_MARGIN = int(os.environ.get("OC_CEILING_MARGIN") or
                     -(-_MARGIN_FLOOR // 5000) * 5000)
LANE_MODEL = {"qwen3.8-27b": "qwen38", "qwen3.8-flash-next": "flashnext"}
AGENT_UNIT = "opencode-web.service"
# The engine's window is the third bound, and it holds the prompt PLUS the answer:
# SGLang refuses any request where input + max_new_tokens exceeds its context_length
# (validate_total_tokens, set on in both serving images; checked live on the 27B lane,
# 2026-09-24), and opencode asks for max_tokens = min(limit.output, its output cap),
# a cap the oc launcher and the Agent tab lift to 200,000. The prompt that reaches it is
# the threshold + one worst step derived above, so on top of the answer:
#
#     context - min(COMPACTION_RESERVE, output) + WORST_STEP + output <= window
#
# The pool was the only bound here until v1.18.7: on a 262,144 window a big flash pool
# came out at 225,000/116,000, and every prompt past 146,144 got a 400 that opencode
# reads as an overflow and compacts on (found in review, 2026-09-24).
NATIVE_WINDOW = 262_144
SGL_UNIT = Path(os.environ.get("OC_SGL_UNIT", "/etc/systemd/system/qwen38-sglang.service"))


def step_margin(output: int) -> int:
    """How far past limit.context one request can reach before opencode compacts: its
    threshold is limit.input - min(COMPACTION_RESERVE, output), and one worst step comes
    after the check. Rounded up to 5,000 like CEILING_MARGIN, which it equals for every
    output of 20,000 or more."""
    floor = max(0, WORST_STEP - min(COMPACTION_RESERVE, output))
    return -(-floor // 5000) * 5000


def lane_cap(served: str) -> tuple[int, int] | None:
    """The static pair oc-limits.sh gives the lane that serves, read from its installed
    invocation (the flash tier from the launcher, the 27B mode from the unit). A lane on
    a native window keeps that pair as a ceiling: its answer budget is chosen per lane
    (32,000 on flash, 64,000 on the 27B), and a pool bigger than the window is no reason
    to grow it."""
    choice, invocation = {"qwen3.8-27b": ("stock", SGL_UNIT),
                          "qwen3.8-flash-next": ("flash", CONFIG_DIR / "launch-flash.sh")}.get(served, (None, None))
    if not choice:
        return None
    r = subprocess.run(["bash", str(REPO_DIR / "oc-limits.sh"), choice, "--from", str(invocation)],
                       capture_output=True, text=True)
    parts = r.stdout.split()
    if r.returncode != 0 or len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    return int(parts[0]), int(parts[1])


def restart_agent() -> str:
    """Make the Agent tab's opencode server run the limits just written.

    It parses opencode.json once, at startup, and never again (measured 2026-09-13:
    the file said 225,000 while the running server still answered 175,000), so limits
    written under it change nothing until it restarts. install.sh restarted it before
    this tool ran and the cockpit's button never did: both left the Agent tab on the
    limits this tool exists to replace, while the page's check, which reads the files,
    said they fitted. Only a running server is restarted, through the one sudoers line
    the cockpit already holds for it."""
    if subprocess.run(["systemctl", "is-active", "--quiet", AGENT_UNIT]).returncode != 0:
        return f"{AGENT_UNIT} is not running: it reads the new limits when it starts"
    r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "restart", AGENT_UNIT],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return f"{AGENT_UNIT} restarted: the Agent tab now runs the new limits"
    return (f"NOTE: could not restart {AGENT_UNIT} ({(r.stderr or '').strip()[:120]}); "
            f"restart it by hand, or the Agent tab keeps the old limits")


def engine_info(base: str) -> dict:
    """/server_info, and the deprecated /get_server_info only on an engine that answers
    404 to it: SGLang logs a warning for each call of the old route and says it will go."""
    key = (CONFIG_DIR / "api-key").read_text().strip()
    for path in ("/server_info", "/get_server_info"):
        req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code != 404 or path == "/get_server_info":
                raise
    raise RuntimeError("unreachable")


def fit(pool: int, ceiling: int = 0, window: int = 0,
        cap: tuple[int, int] | None = None) -> tuple[int, int]:
    """(context, output) that one request can always hold: in this pool, under the lane's
    prompt ceiling, inside the engine's window, and at most the lane's own pair `cap`."""
    budget = int(pool * USABLE * (1.0 - BOOT_MARGIN))
    output = min(OUTPUT_CAP, int(budget * OUTPUT_SHARE))
    context = budget - output
    if cap:                              # a native-window lane never goes above its pair
        output = min(output, cap[1])
        context = min(budget - output, cap[0])
    if ceiling > 0:                      # a per-lane prompt ceiling caps the context too
        context = min(context, max(0, ceiling - CEILING_MARGIN))
    if window > 0:                       # and the window holds the prompt and the answer
        context = min(context, max(0, window - step_margin(output) - output))
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
    restart = "--restart-agent" in argv
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
    window = int(info.get("context_length") or 0)
    cap = lane_cap(served) if 0 < window <= NATIVE_WINDOW else None
    context, output = fit(pool, ceiling, window, cap)
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
          + (f", lane ceiling {ceiling:,}" if ceiling else "")
          + (f", window {window:,}" if window else "")
          + (f", lane pair {cap[0]:,}/{cap[1]:,}" if cap else ""))
    print(f"limits that fit: context {context:,}, output {output:,} "
          f"(worst case {context + output:,} of {pool:,})")
    if dry:
        print("dry run: nothing written")
        return 0
    rc, changed = 0, False
    for target in (CONFIG_DIR / "opencode.json", Path.home() / ".config/opencode/opencode.json"):
        if not target.exists():
            continue
        out = subprocess.run([sys.executable, str(REPO_DIR / "oc-merge-limits.py"), str(target),
                              provider, served, str(context), str(output)],
                             capture_output=True, text=True)
        said = (out.stdout or out.stderr).strip()
        print(f"  {target}: {said.splitlines()[-1] if said else 'no output'}")
        if out.returncode not in (0, 3):
            rc = out.returncode
        elif out.returncode == 0 and "unchanged" not in said:
            changed = True
    if rc == 0 and not changed:
        print("opencode already asks for no more than this engine can serve: nothing to restart")
    elif rc == 0 and restart:
        print("opencode now asks for no more than this engine can serve")
        print(restart_agent())
    elif rc == 0:
        print("opencode now asks for no more than this engine can serve; "
              "restart opencode to pick the new limits up")
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
