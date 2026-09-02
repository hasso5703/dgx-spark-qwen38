#!/usr/bin/env python3
"""needle.sh must not report a lane's own prompt limit as a retrieval failure.

needle.sh talks to the keepalive proxy by default, so the oversize guard and the
lane's PROMPT_CEILING_TOKENS apply to it. Its default depths run to 140000 while
the flash lane's ceiling is 128000, so the tool used to print ERROR for a prompt
the box had correctly refused and exit 1, reading as long-context corruption.
A fake engine here answers small prompts and refuses big ones exactly as the
proxy does."""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEEDLE = os.path.join(REPO_DIR, "needle.sh")
BIG = 400000        # bytes of request body above which the fake refuses (calibration is ~210 KB)


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        if self.path.startswith("/flush_cache"):
            return self._send(200, {"ok": True})
        if len(body) > BIG:
            return self._send(400, {"error": {
                "type": "context_too_long",
                "message": "keepalive-proxy: 190000 prompt tokens; this lane serves at most 128000"}})
        text = json.loads(body)["messages"][0]["content"]
        pw = ""
        if "passphrase is " in text:
            pw = text.split("passphrase is ", 1)[1].split(".", 1)[0].strip()
        return self._send(200, {"choices": [{"message": {"content": pw or "OK"}}],
                                "usage": {"prompt_tokens": max(40, len(text) // 4 + 40)}})


def main() -> None:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    # needle.sh reads the API key from $HOME/.config/qwen38/api-key. Relying on
    # the developer's own key made this pass here and fail on a runner that has
    # none, so the test brings its own HOME.
    home = tempfile.mkdtemp()
    os.makedirs(os.path.join(home, ".config", "qwen38"))
    with open(os.path.join(home, ".config", "qwen38", "api-key"), "w") as f:
        f.write("test-key\n")
    env = {**os.environ, "PORT": str(port), "HOME": home}
    try:
        # A depth the fake refuses, mixed with one it serves.
        r = subprocess.run(["bash", NEEDLE, "--model", "m", "--depths", "1000 300000",
                            "--trials", "1", "--no-flush"],
                           capture_output=True, text=True, env=env, timeout=180)
        out = r.stdout + r.stderr
        assert "REFUSED by the lane" in out, f"a policy refusal was not labelled:\n{out}"
        assert "ERROR" not in out, f"a policy refusal was reported as an error:\n{out}"
        # the refused trial must not be counted as an attempted retrieval
        assert "1/1 exact retrievals" in out, f"the ratio counted the refusal:\n{out}"
        assert "refused by the lane's own prompt limit" in out, out
        assert "PORT=30000" in out, "the way past the ceiling is not explained"
        assert r.returncode == 1 or r.returncode == 0, f"unexpected rc={r.returncode}\n{out}"

        # Every depth refused: nothing was measured, and that is not a pass.
        r = subprocess.run(["bash", NEEDLE, "--model", "m", "--depths", "300000",
                            "--trials", "1", "--no-flush"],
                           capture_output=True, text=True, env=env, timeout=180)
        out = r.stdout + r.stderr
        assert r.returncode == 2, f"all-refused must not exit 0/1, got {r.returncode}\n{out}"
        assert "nothing was measured" in out, out
        print("test_needle_refusal: OK")
    finally:
        srv.shutdown()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
