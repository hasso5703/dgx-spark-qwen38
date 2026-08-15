#!/usr/bin/env bash
# Measure your decode tok/s against the running qwen38-sglang service.
# Deterministic greedy probes (repeatable ±0.2) + one eval-style peak probe.
set -euo pipefail
PORT="${PORT:-30000}"
KEY_FILE="$HOME/.config/qwen38/api-key"
[ -s "$KEY_FILE" ] || { echo "API key not found at $KEY_FILE — run ./install.sh first"; exit 1; }
curl -s -m 3 "http://127.0.0.1:$PORT/health" >/dev/null || { echo "server not up on :$PORT (systemctl status qwen38-sglang)"; exit 1; }

KEY="$(cat "$KEY_FILE")" PORT="$PORT" python3 - <<'PYEOF'
import json, os, statistics, time, urllib.request

KEY, PORT = os.environ["KEY"], os.environ["PORT"]
BASE = f"http://127.0.0.1:{PORT}"

PROBES = [
    ("code (greedy, deterministic)", 0.0,
     "Write a Python class LRUCache with O(1) get/put using OrderedDict, plus a small test.", 1200),
    ("reasoning (greedy, deterministic)", 0.0,
     "A train leaves Paris at 14:00 at 160 km/h toward Lyon (465 km). Another leaves Lyon at 14:30 at 120 km/h toward Paris. When do they meet? Show your work.", 1200),
    ("math peak (temp 0.6, like SGLang's eval setting)", 0.6,
     "Natalia sold clips to 48 friends in April, half as many in May, and 3x May's amount in June. How many clips did she sell in total? Think step by step.", 2048),
]

def stream(prompt, temp, max_tokens):
    body = {"model": "qwen3.8-27b",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": temp,
            "stream": True, "stream_options": {"include_usage": True}}
    req = urllib.request.Request(BASE + "/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    t_first = t_last = None; n = 0; usage = {}
    try:
        resp = urllib.request.urlopen(req, timeout=900)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SystemExit("401/403: the key in ~/.config/qwen38/api-key does not match the "
                             "server's (was the service installed with a different key? re-run ./install.sh)")
        raise SystemExit(f"HTTP {e.code} from the server: {e.read()[:300]!r}")
    with resp as r:
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            d = json.loads(line[6:])
            if d.get("usage"):
                usage = d["usage"]
            if not d.get("choices"):
                continue
            delta = d["choices"][0].get("delta", {})
            if delta.get("content") or delta.get("reasoning_content"):
                now = time.time()
                if t_first is None:
                    t_first = now
                t_last = now; n += 1
    out = usage.get("completion_tokens") or n
    return (out - 1) / (t_last - t_first) if t_first and t_last > t_first else -1

print("Qwen3.8-27B NVFP4+DSpark benchmark (batch 1 decode) — reference box: ~34 avg / 45+ math peak\n")
all_greedy = []
for name, temp, prompt, mt in PROBES:
    speeds = [stream(prompt, temp, mt) for _ in range(2)]
    if temp == 0.0:
        all_greedy += speeds
    print(f"  {name}: {speeds[0]:.1f} / {speeds[1]:.1f} tok/s")
print(f"\n  greedy median: {statistics.median(all_greedy):.1f} tok/s")
print("  (numbers vary with content — acceptance length drives everything;")
print("   math/code accept ~4-4.9 tokens/step, free prose ~2.7-3.3)")
PYEOF
