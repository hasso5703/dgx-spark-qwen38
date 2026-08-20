#!/usr/bin/env bash
# Measure your decode tok/s against the running qwen38-sglang service.
# Deterministic greedy probes (repeatable ±0.2) + one eval-style peak probe.
set -euo pipefail
PORT="${PORT:-30000}"
KEY_FILE="$HOME/.config/qwen38/api-key"
[ -s "$KEY_FILE" ] || { echo "API key not found at $KEY_FILE. Run ./install.sh first"; exit 1; }
for i in 1 2 3; do
  curl -s -m 5 "http://127.0.0.1:$PORT/health" >/dev/null && break
  [ "$i" = 3 ] && { echo "server not up on :$PORT after 3 tries (systemctl status qwen38-sglang)"; exit 1; }
  sleep 5
done

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
    # Deliberate worst case, NOT in the median: free prose collapses draft
    # acceptance (~1.5-2.2 vs 3.3-5.6); see the README's content-dependence
    # section. If this one is slow but the others are fast, that is EXPECTED.
    ("free prose (greedy, worst-case, excluded from median)", 0.0,
     "Describe in detail a walk through an autumn forest: the colors of the leaves, the sounds, the smell after rain, and the thoughts that cross your mind.", 800),
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
            # thinking streams as reasoning_content (SGLang) or reasoning (vLLM >= 0.27);
            # missing a field name starts the clock after the thinking phase while
            # usage still counts it, inflating tok/s several-fold (issue #2)
            if delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning"):
                now = time.time()
                if t_first is None:
                    t_first = now
                t_last = now; n += 1
    out = usage.get("completion_tokens") or n
    return (out - 1) / (t_last - t_first) if t_first and t_last > t_first else -1

print("Qwen3.8-27B NVFP4+DFlash2 benchmark (batch 1 decode)")
print("reference box (DFlash2 v1.2+): ~50 greedy median (code 41-47 / reasoning 52-57 / math peak 50-60 / free prose ~23)\n")
all_greedy = []
for name, temp, prompt, mt in PROBES:
    speeds = [stream(prompt, temp, mt) for _ in range(2)]
    if temp == 0.0 and "worst-case" not in name:
        all_greedy += speeds
    print(f"  {name}: {speeds[0]:.1f} / {speeds[1]:.1f} tok/s")
    if max(speeds) > 90:
        print("    ^ above the block-8 speculative physical ceiling for this box (~9x the")
        print("      ~11 tok/s AR floor), almost certainly a measurement artifact, not")
        print("      real speed. Cross-check with bench-matrix.sh (wall-clock method).")
print(f"\n  greedy median: {statistics.median(all_greedy):.1f} tok/s")
print("  (numbers vary with content: acceptance length drives everything;")
print("   math/code accept ~3.3-5.6 tokens/step, free prose ~1.5-2.2 in any")
print("   language, see BENCHMARKS.md)")
PYEOF
