#!/usr/bin/env bash
# Workload matrix benchmark: engine-agnostic.
# Runs a FIXED, versioned probe battery (v1: 8 workloads × 3 languages spread)
# against ANY OpenAI-compatible endpoint, so results are comparable across
# engines, boxes and months. Decode is measured net of prefill with a
# two-call delta (80 vs 680 max_tokens, same prompt), which cancels TTFT and
# needs no streaming support.
#
#   ./bench-matrix.sh                                   # this repo's service
#   BASE_URL=http://127.0.0.1:8000 ./bench-matrix.sh    # llama.cpp, vLLM, ...
#   MODEL=my-model API_KEY= ./bench-matrix.sh           # other model id / no auth
#
# Output: human table + bench-matrix-<label>.json next to it.
set -euo pipefail
PORT="${PORT:-30000}"
BASE_URL="${BASE_URL:-http://127.0.0.1:$PORT}"
MODEL="${MODEL:-qwen3.8-27b}"
KEY_FILE="${API_KEY_FILE:-$HOME/.config/qwen38/api-key}"
API_KEY="${API_KEY-$( [ -s "$KEY_FILE" ] && cat "$KEY_FILE" || true )}"
LABEL="${LABEL:-$(echo "$BASE_URL" | tr -dc 'a-zA-Z0-9' | tail -c 12)}"

for i in 1 2 3; do
  curl -s -m 5 "$BASE_URL/v1/models" >/dev/null && break
  [ "$i" = 3 ] && { echo "no server at $BASE_URL (need /v1/models to answer)"; exit 1; }
  sleep 5
done

BASE_URL="$BASE_URL" MODEL="$MODEL" API_KEY="$API_KEY" LABEL="$LABEL" python3 - <<'PYEOF'
import json, os, time, urllib.request

BASE, MODEL, KEY, LABEL = (os.environ[k] for k in ("BASE_URL", "MODEL", "API_KEY", "LABEL"))

# Probe battery v1: DO NOT edit prompts in place, results stop being
# comparable. Add a v2 battery instead if it ever needs to change.
BATTERY_VERSION = 1
PROBES = [
    ("math (EN, eval-style)",   "Natalia sold clips to 48 friends in April, half as many in May, and 3x May's amount in June. How many clips did she sell in total? Think step by step."),
    ("code (EN)",               "Write a Python class implementing an LRU cache with O(1) get and put, with docstrings and a small usage example."),
    ("code (DE)",               "Schreibe eine Python-Klasse RingBuffer mit push/pop in O(1), Docstrings und einem kleinen Test."),
    ("technical explain (FR)",  "Explique en détail comment fonctionne un index B-tree dans une base de données relationnelle, avec un exemple concret d'insertion."),
    ("reasoning (FR)",          "Un train part de Paris à 14h00 à 160 km/h vers Lyon (465 km). Un autre part de Lyon à 14h30 à 120 km/h vers Paris. À quelle heure se croisent-ils ? Montre ton raisonnement."),
    ("free prose (EN)",         "Describe in detail a walk through an autumn forest: the colors of the leaves, the sounds, the smell after rain, and the thoughts that cross your mind."),
    ("free prose (FR)",         "Décris en détail une promenade dans une forêt d'automne : les couleurs des feuilles, les sons, l'odeur après la pluie, et les pensées qui traversent l'esprit."),
    ("free prose (DE)",         "Beschreibe ausführlich einen Spaziergang durch einen herbstlichen Wald: die Farben der Blätter, die Geräusche, den Geruch nach Regen, und die Gedanken, die einem dabei durch den Kopf gehen."),
]

def call(prompt, max_tok):
    body = {"model": MODEL, "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0, "max_tokens": max_tok}
    headers = {"Content-Type": "application/json"}
    if KEY:
        headers["Authorization"] = f"Bearer {KEY}"
    req = urllib.request.Request(f"{BASE}/v1/chat/completions", json.dumps(body).encode(), headers)
    t0 = time.time()
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=900).read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code} from {BASE}: {e.read()[:300]!r}")
    u = r.get("usage") or {}
    ct = u.get("completion_tokens")
    if ct is None:
        raise SystemExit("endpoint returns no usage.completion_tokens, cannot measure")
    return time.time() - t0, ct

served = "?"
try:
    req = urllib.request.Request(f"{BASE}/v1/models",
                                 headers={"Authorization": f"Bearer {KEY}"} if KEY else {})
    served = json.loads(urllib.request.urlopen(req, timeout=10).read())["data"][0]["id"]
except Exception:
    pass

print(f"Workload matrix v{BATTERY_VERSION}, endpoint {BASE} (model id: {served})")
print("greedy, fresh single-turn prompts, decode net of prefill (two-call delta)\n")
# Discarded warmup: the very first request after a model load pays one-time
# costs (mmap page-in, graph/kernel warmup) that would poison the first probe.
call("Say OK.", 24)
results = []
for name, prompt in PROBES:
    d1, c1 = call(prompt, 80)
    d2, c2 = call(prompt, 680)
    if c2 - c1 < 50 or d2 - d1 < 2.0:
        print(f"  {name:24s}: unreliable sample (Δtok={c2-c1}, Δt={d2-d1:.2f}s: short answer,"
              " cold start or cache artifact), skipped")
        results.append({"probe": name, "tok_s": None})
        continue
    tps = (c2 - c1) / (d2 - d1)
    results.append({"probe": name, "tok_s": round(tps, 1)})
    print(f"  {name:24s}: {tps:5.1f} tok/s")

out = {"battery_version": BATTERY_VERSION, "endpoint": BASE, "model_id": served,
       "method": "two-call delta, temperature 0, max_tokens 80/680", "results": results}
path = f"bench-matrix-{LABEL}.json"
json.dump(out, open(path, "w"), indent=1)
print(f"\nwritten: {path}   (post it: results are comparable across engines/boxes)")
PYEOF
