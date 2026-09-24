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

    # 5. --restart-agent. opencode-web reads opencode.json only at startup, and both
    #    callers used to leave it on the old limits: install.sh restarted it BEFORE the
    #    fit, the cockpit's button never did, and the page's check (which reads the
    #    files) said the limits fitted. The restart goes through the exact sudoers line,
    #    only when the server runs, and only when a limit actually changed.
    import subprocess as sp
    import tempfile
    from pathlib import Path as P
    for active in (True, False):
        with tempfile.TemporaryDirectory() as d:
            log = P(d) / "calls"
            (P(d) / "systemctl").write_text(
                f'#!/bin/sh\necho "systemctl $*" >> {log}\n[ "$1" = is-active ] && exit {0 if active else 3}\nexit 0\n')
            (P(d) / "sudo").write_text(f'#!/bin/sh\necho "sudo $*" >> {log}\nexit 0\n')
            for f in ("systemctl", "sudo"):
                (P(d) / f).chmod(0o755)
            old = os.environ["PATH"]
            os.environ["PATH"] = f"{d}:{old}"
            try:
                said = m.restart_agent()
            finally:
                os.environ["PATH"] = old
            calls = log.read_text().splitlines() if log.exists() else []
            restarted = "sudo -n /usr/bin/systemctl restart opencode-web.service" in calls
            assert restarted == active, (active, calls, said)
    sudoers = P(REPO_DIR, "dashboard", "sudoers-cockpit.template").read_text()
    assert "NOPASSWD: /usr/bin/systemctl restart opencode-web.service" in sudoers
    install = P(REPO_DIR, "install.sh").read_text()
    assert 'oc-fit-limits.py" --engine "http://127.0.0.1:$PORT" --restart-agent' in install, \
        "install.sh fits without restarting the agent"
    cockpit = P(REPO_DIR, "dashboard", "cockpit.py").read_text()
    assert 'oc-fit-limits.py"), "--restart-agent"]' in cockpit, "the cockpit's button fits without restarting it"

    # 6. Nothing changed, nothing restarted: a fit that finds the limits already right
    #    must not cut a reply the Agent tab is writing.
    real_run, restarts = m.subprocess.run, []
    m.engine_info = lambda base: {"max_total_num_tokens": 914_573, "served_model_name": "qwen3.8-27b",
                                  "model_path": "x"}
    m.restart_agent = lambda: restarts.append(1) or "restarted"
    for merge_said, want in (("qwen38/qwen3.8-27b limits already 567000/189000: unchanged", 0),
                             ("qwen38/qwen3.8-27b limits: 700000/200000 -> 567000/189000", 1)):
        restarts.clear()

        def fake(argv, **kw):
            text = merge_said if "oc-merge-limits.py" in " ".join(map(str, argv)) else ""
            return sp.CompletedProcess(argv, 0, stdout=text, stderr="")
        m.subprocess.run = fake
        with tempfile.TemporaryDirectory() as d:
            m.CONFIG_DIR = P(d)
            (P(d) / "opencode.json").write_text("{}")
            assert m.main(["--restart-agent"]) == 0
        assert len(restarts) == min(want, 1), (merge_said, restarts)
    m.subprocess.run = real_run

    # 7. main() hands the ceiling the proxy applies to the lane that serves to fit(): the
    #    main() runs above had a `systemctl show` that answered "", so a main() calling
    #    fit(pool), or reading the ceiling without the served name, passed (found in
    #    review, 2026-09-24). The 27B on a 1M window, so no lane pair caps it.
    info = {"max_total_num_tokens": 914_573, "served_model_name": "qwen3.8-27b",
            "context_length": 1_010_000, "model_path": "x"}
    m.engine_info = lambda base: info
    for env_line, ceiling in (
            ("Environment=UPSTREAM=http://127.0.0.1:30000 PROMPT_CEILING_TOKENS=300000", 300_000),
            # the flash lane's own ceiling is not the 27B's
            ("Environment=PROMPT_CEILING_TOKENS=0 FLASH_PROMPT_CEILING_TOKENS=250000", 0)):
        merges = []

        def fake(argv, **kw):
            argv = [str(a) for a in argv]
            if argv[:2] == ["systemctl", "show"]:
                return sp.CompletedProcess(argv, 0, stdout=env_line + "\n", stderr="")
            if any(a.endswith("oc-merge-limits.py") for a in argv):
                merges.append(argv)
                return sp.CompletedProcess(argv, 0, stdout="limits: a -> b", stderr="")
            return sp.CompletedProcess(argv, 0, stdout="", stderr="")
        m.subprocess.run, home = fake, os.environ["HOME"]
        try:
            with tempfile.TemporaryDirectory() as d:
                m.CONFIG_DIR, os.environ["HOME"] = P(d), d      # and no look at the real one
                (P(d) / "opencode.json").write_text("{}")
                assert m.main([]) == 0
        finally:
            m.subprocess.run, os.environ["HOME"] = real_run, home
        want = m.fit(914_573, ceiling, 1_010_000)
        assert merges and merges[0][-2:] == [str(want[0]), str(want[1])], (env_line, merges, want)
        if ceiling:
            assert want[0] <= ceiling - m.CEILING_MARGIN < m.fit(914_573, 0, 1_010_000)[0], want

    # 9. The pool is read from /server_info; the deprecated /get_server_info only on an
    #    engine that answers 404 to it (SGLang warns on every call of the old route).
    import http.server
    import json
    import threading
    for routes, want_seen in (({"/server_info"}, ["/server_info"]),
                              ({"/get_server_info"}, ["/server_info", "/get_server_info"])):
        seen = []

        class Engine(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(self.path)
                if self.path in routes:
                    out = json.dumps({"max_total_num_tokens": 901109}).encode()
                    self.send_response(200); self.send_header("Content-Length", str(len(out)))
                    self.end_headers(); self.wfile.write(out); return
                self.send_response(404); self.end_headers()

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        fresh = load()                    # the sections above replaced engine_info on m
        try:
            with tempfile.TemporaryDirectory() as d:
                fresh.CONFIG_DIR = P(d)
                (P(d) / "api-key").write_text("k\n")
                info = fresh.engine_info(f"http://127.0.0.1:{srv.server_address[1]}")
        finally:
            srv.shutdown(); srv.server_close()
        assert info["max_total_num_tokens"] == 901109, info
        assert seen == want_seen, (routes, seen)

    # 10. The engine's window holds the prompt AND the answer. SGLang refuses any request
    #     where input + max_new_tokens passes context_length (validate_total_tokens, on in
    #     both images), opencode asks for max_tokens = limit.output, and the prompt that
    #     reaches it is opencode's threshold (limit.input - min(20,000, output)) plus one
    #     worst step. fit() used to see only the pool: 225,000/116,000 on a big flash pool,
    #     refused from a 146,144-token prompt on (found in review, 2026-09-24).
    fresh = load()
    W = 262_144

    def worst(ctx, out):
        return ctx - min(fresh.COMPACTION_RESERVE, out) + fresh.WORST_STEP + out

    tbl = {}
    for choice, sel in (("flash", "context"), ("flash", "concurrency"), ("flash", "throughput"),
                        ("stock", "native"), ("fp8", "native")):
        o = sp.run(["bash", os.path.join(REPO_DIR, "oc-limits.sh"), choice, sel],
                   capture_output=True, text=True).stdout.split()
        tbl[(choice, sel)] = (int(o[0]), int(o[1]))
        assert worst(*tbl[(choice, sel)]) <= W, (choice, sel, tbl[(choice, sel)], worst(*tbl[(choice, sel)]))
    for pool in (189_056, 249_408, 280_000, 459_000, 468_480, 564_352, 900_000):
        ctx, out = fresh.fit(pool, 250_000, W, tbl[("flash", "context")])
        assert ctx > 0 and worst(ctx, out) <= W, (pool, ctx, out, worst(ctx, out))
        assert (ctx, out) <= tbl[("flash", "context")] and out <= 32_000, (pool, ctx, out)
    assert fresh.fit(564_352, 250_000, W, tbl[("flash", "context")]) == tbl[("flash", "context")], \
        "a pool bigger than the window gets the lane's own pair, not more"
    for pool in (357_706, 600_000, 900_000):
        ctx, out = fresh.fit(pool, 0, W, tbl[("stock", "native")])
        assert worst(ctx, out) <= W and out <= 64_000, (pool, ctx, out)
    # the 1M lane is bounded by its pool, and nothing about it moves
    for pool in (832_993, 887_797, 922_094):
        assert fresh.fit(pool, 0, 1_010_000) == fresh.fit(pool), pool
        assert worst(*fresh.fit(pool)) <= 1_010_000

    # and main() reads the window and the lane's pair itself: a flash engine that booted a
    # big pool, a launcher on the context tier, the proxy's 250,000 ceiling
    merged = []
    fresh.engine_info = lambda base: {"max_total_num_tokens": 564_352, "context_length": W,
                                      "served_model_name": "qwen3.8-flash-next", "model_path": "x"}

    def fake_run(argv, **kw):
        joined = " ".join(map(str, argv))
        if "systemctl show" in joined:
            return sp.CompletedProcess(argv, 0, stdout="Environment=PROMPT_CEILING_TOKENS=250000\n", stderr="")
        if "oc-merge-limits.py" in joined:
            merged.append(list(map(str, argv)))
            return sp.CompletedProcess(argv, 0, stdout="limits written", stderr="")
        return real_run(argv, **kw)       # oc-limits.sh runs for real
    fresh.subprocess.run = fake_run
    try:
        with tempfile.TemporaryDirectory() as d:
            fresh.CONFIG_DIR = P(d)
            (P(d) / "opencode.json").write_text("{}")
            (P(d) / "launch-flash.sh").write_text("docker run x python3 -m sglang.launch_server --model-path y\n")
            assert fresh.main([]) == 0
    finally:
        fresh.subprocess.run = real_run
    assert merged, "nothing was merged"
    ctx, out = int(merged[0][-2]), int(merged[0][-1])
    assert worst(ctx, out) <= W, (ctx, out, worst(ctx, out))
    assert (ctx, out) == tbl[("flash", "context")], (ctx, out)

    print("test_oc_fit_limits: OK")


if __name__ == "__main__":
    sys.exit(main())
