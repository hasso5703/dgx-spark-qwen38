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

    print("test_oc_fit_limits: OK")


if __name__ == "__main__":
    sys.exit(main())
