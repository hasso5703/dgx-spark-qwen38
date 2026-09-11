#!/usr/bin/env python3
"""bench.sh must name the lane it is measuring.

It printed "Qwen3.8-27B NVFP4+DFlash2" and the 27B reference line on a box
serving the flash lane, so a perfectly normal 41.9 tok/s read as a regression
against a baseline belonging to another model. It also sent a hardcoded
"model": "qwen3.8-27b", which only worked because SGLang does not enforce the
field. A fake engine here names itself and checks what the script does with it.
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH = os.path.join(REPO_DIR, "bench.sh")
SERVED = []          # the model name each generation request asked for


class Fake(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    model = "qwen3.8-flash-next"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send(200, {"ok": True})
        if self.path.startswith("/v1/models"):
            return self._send(200, {"data": [{"id": Fake.model}]})
        self._send(404, {})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        SERVED.append(json.loads(body).get("model"))
        # two tokens then usage: enough for the script's timing to be defined
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for i in range(3):
            ev = {"choices": [{"delta": {"content": f"t{i} "}}]}
            self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
        self.wfile.write(b'data: ' + json.dumps(
            {"choices": [{"delta": {}}], "usage": {"completion_tokens": 3}}).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def run_bench(model):
    Fake.model = model
    SERVED.clear()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # bench.sh reads $HOME/.config/qwen38/api-key. A throwaway HOME with a
    # throwaway key, so this runs the same on a GitHub runner and on a box that
    # has a real one, and can never read the developer's own key.
    home = tempfile.mkdtemp(prefix="bench-lane-home-")
    os.makedirs(os.path.join(home, ".config", "qwen38"), exist_ok=True)
    with open(os.path.join(home, ".config", "qwen38", "api-key"), "w") as fh:
        fh.write("test-key\n")
    try:
        r = subprocess.run(["bash", BENCH], capture_output=True, text=True, timeout=180,
                           cwd=REPO_DIR,
                           env={**os.environ, "PORT": str(srv.server_port), "HOME": home})
    finally:
        srv.shutdown()
        shutil.rmtree(home, ignore_errors=True)
    return r.stdout + r.stderr


def main():
    fails = []

    out = run_bench("qwen3.8-flash-next")
    if "Flash-Next" not in out:
        fails.append(f"the flash lane is not named in the header:\n{out[:400]}")
    if "Qwen3.8-27B NVFP4+DFlash2" in out:
        fails.append("the 27B header was printed for the flash lane")
    if "code 64-66 / reasoning 65-66" in out:
        fails.append("the 27B reference line was printed for the flash lane")
    if "DISCARD THE FIRST BATCH" not in out:
        fails.append("the flash lane's warm-up protocol was not printed")
    if set(SERVED) != {"qwen3.8-flash-next"}:
        fails.append(f"the served model name was not used in the request: {set(SERVED)}")

    out = run_bench("qwen3.8-27b")
    if "Qwen3.8-27B NVFP4+DFlash2" not in out:
        fails.append(f"the 27B header is gone:\n{out[:400]}")
    if "Flash-Next" in out:
        fails.append("the flash header was printed for the 27B lane")
    if set(SERVED) != {"qwen3.8-27b"}:
        fails.append(f"the 27B lane sent {set(SERVED)}")

    # An engine that does not answer /v1/models must not stop the benchmark.
    out = run_bench("")
    if "unknown" not in out:
        fails.append(f"an unnamed model is not reported as unknown:\n{out[:400]}")
    if "greedy median" not in out:
        fails.append("the benchmark did not run when the engine would not name itself")

    for f in fails:
        print("FAIL", f)
    print(f"bench lane detection: {'FAILED' if fails else 'ok'} ({len(fails)} problem(s))")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
