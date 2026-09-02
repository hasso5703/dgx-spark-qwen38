#!/usr/bin/env python3
"""oc-fit-limits.py, offline: the limits it computes must be limits the proxy's
own oversize guard will relay, and the lane ceiling must actually reach the
computation. No engine, no network, no config touched.

The invariant that matters: opencode declares context+output as one worst-case
request, so context+output must stay under the same usable share of the pool the
proxy refuses above. Anything else fails mid-conversation rather than early."""
import importlib.util
import os
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load():
    spec = importlib.util.spec_from_file_location(
        "oc_fit_limits", os.path.join(REPO_DIR, "oc-fit-limits.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main() -> None:
    m = load()

    # 1. The worst case always fits the share the proxy will relay, across the
    #    whole range of pools this repo has measured (382k FP8 pre-fp8-KV, 771k
    #    FP8, 863k-913k NVFP4 across boots) plus small and huge edges.
    for pool in (50_000, 382_706, 771_139, 863_398, 913_334, 1_100_000, 4_000_000):
        ctx, out = m.fit(pool)
        assert ctx > 0 and out > 0, f"pool {pool}: non-positive limits {ctx}/{out}"
        assert ctx + out <= pool * m.USABLE, (
            f"pool {pool}: worst case {ctx + out} exceeds the guard's "
            f"{pool * m.USABLE:.0f}, the proxy would refuse mid-conversation")
        # rounding is to the kilo and downward, never up into the margin
        assert ctx % 1000 == 0 and out % 1000 == 0, f"pool {pool}: not rounded"

    # 1b. An implausibly small pool rounds to zero rather than inventing a floor;
    #     main() refuses to write those instead of putting "context": 0 in a config.
    assert m.fit(1_000) == (0, 0), "a 1k pool should not yield usable limits"

    # 2. Output is capped: a runaway generation must not eat the whole budget.
    ctx, out = m.fit(10_000_000)
    assert out == m.OUTPUT_CAP, f"output {out} should cap at {m.OUTPUT_CAP}"

    # 3. A lane ceiling caps the context, and only the context.
    free_ctx, free_out = m.fit(900_000)
    cap_ctx, cap_out = m.fit(900_000, ceiling=128_000)
    assert cap_ctx <= 128_000, f"ceiling ignored: {cap_ctx}"
    assert cap_ctx < free_ctx, "the ceiling should have bitten on this pool"
    assert cap_out == free_out, "the ceiling must not change the output budget"
    # a ceiling above what fits must not raise the context back up
    assert m.fit(900_000, ceiling=10_000_000)[0] == free_ctx, "ceiling raised the context"

    # 4. The ceiling has to survive systemd's own formatting. Only the FIRST
    #    variable carries the Environment= prefix, so a template reordering used
    #    to make the ceiling vanish silently and hand a flash lane 27B limits.
    first = "Environment=PROMPT_CEILING_TOKENS=128000 UPSTREAM=http://127.0.0.1:30000"
    later = "Environment=UPSTREAM=http://127.0.0.1:30000 PROMPT_CEILING_TOKENS=128000"
    assert m.ceiling_from_env(first) == 128_000, "ceiling missed when listed first"
    assert m.ceiling_from_env(later) == 128_000, "ceiling missed when listed later"
    assert m.ceiling_from_env("Environment=UPSTREAM=http://x") == 0, "invented a ceiling"
    assert m.ceiling_from_env("") == 0, "invented a ceiling from nothing"
    assert m.ceiling_from_env("Environment=PROMPT_CEILING_TOKENS=") == 0, "empty value"
    assert m.ceiling_from_env("Environment=PROMPT_CEILING_TOKENS=abc") == 0, "junk value"
    # the 27B lane ships 0, which must mean "no ceiling", not "ceiling of zero"
    assert m.fit(900_000, ceiling=m.ceiling_from_env(
        "Environment=UPSTREAM=http://x PROMPT_CEILING_TOKENS=0"))[0] == free_ctx

    print("test_oc_fit_limits: OK")


if __name__ == "__main__":
    sys.exit(main())
