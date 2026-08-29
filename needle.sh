#!/usr/bin/env bash
# Long-context correctness probe: hides a passphrase deep inside filler text,
# asks the model to retrieve it, and reports the measured prompt depth.
# Catches the class of bug fixed in v1.5.2 (silent corruption deep in the
# context: runs of token id 0). Usage:
#   ./needle.sh [--depths "60000 120000"] [--trials 4] [--model qwen3.8-flash-next]
# Env: PORT (default 30001, the keepalive proxy), API key read from config.
set -euo pipefail
PORT="${PORT:-30001}"
DEPTHS="60000 100000 120000 140000"
TRIALS=2
MODEL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --depths) DEPTHS="$2"; shift 2 ;;
    --trials) TRIALS="$2"; shift 2 ;;
    --model)  MODEL="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
KEY="$(cat "${HOME}/.config/qwen38/api-key")"
BASE="http://127.0.0.1:${PORT}"
if [ -z "$MODEL" ]; then
  MODEL="$(curl -s -m 5 -H "Authorization: Bearer $KEY" "$BASE/v1/models" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])')"
fi
echo "needle probe: model=$MODEL endpoint=$BASE depths=[$DEPTHS] trials=$TRIALS"
export KEY BASE MODEL TRIALS DEPTHS
python3 - <<'PY'
import json, os, random, time, urllib.request

KEY, BASE, MODEL = os.environ["KEY"], os.environ["BASE"], os.environ["MODEL"]
TRIALS = int(os.environ["TRIALS"]); DEPTHS = [int(x) for x in os.environ["DEPTHS"].split()]
WORDS = ("harbor lantern meadow copper violin thunder saddle marble orchard "
         "pepper canyon willow falcon ember granite tulip anchor cinder velvet "
         "quartz").split()

def chat(messages, max_tokens=40):
    body = json.dumps({"model": MODEL, "messages": messages, "max_tokens": max_tokens,
                       "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(f"{BASE}/v1/chat/completions", body,
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=900) as r:
        out = json.loads(r.read().decode())
    return out, time.time() - t0

def filler(rng, n_sentences):
    return " ".join(f"The {rng.choice(WORDS)} near the {rng.choice(WORDS)} was counted {rng.randint(2,99)} times on day {rng.randint(1,28)}."
                    for _ in range(n_sentences))

# calibrate chars per token on a 4k-token-ish sample
rng = random.Random(1)
sample = filler(rng, 300)
out, _ = chat([{"role": "user", "content": sample + "\nReply with the single word OK."}], 5)
cpt = len(sample) / max(1, out["usage"]["prompt_tokens"] - 40)
print(f"calibration: {cpt:.2f} chars/token")

results = []
for depth in DEPTHS:
    for t in range(TRIALS):
        rng = random.Random(depth * 31 + t)
        pw = "-".join(rng.choice(WORDS) for _ in range(3)) + f"-{rng.randint(100,999)}"
        n_sent = int(depth * cpt / 88)          # ~88 chars per sentence
        text = filler(rng, n_sent)
        cut = int(len(text) * (0.35 + 0.3 * rng.random()))
        text = text[:cut] + f" IMPORTANT: the secret passphrase is {pw}. " + text[cut:]
        msgs = [{"role": "user", "content": text + "\n\nWhat is the secret passphrase mentioned above? Reply with only the passphrase."}]
        try:
            out, dt = chat(msgs, 40)
            reply = (out["choices"][0]["message"].get("content") or "").strip()
            ptok = out["usage"]["prefill_tokens" if "prefill_tokens" in out["usage"] else "prompt_tokens"]
            ok = pw in reply
            garbage = any(ch * 6 in reply for ch in "!\"#$%&'()*") or (reply and len(set(reply)) <= 2)
            verdict = "OK" if ok else ("CORRUPT" if garbage else "MISS")
            print(f"depth~{depth:>6} trial {t+1}: prompt_tokens={ptok} {dt:5.1f}s -> {verdict}  reply={reply[:60]!r}")
            results.append((depth, t, ptok, verdict))
        except Exception as e:  # noqa: BLE001
            print(f"depth~{depth:>6} trial {t+1}: ERROR {str(e)[:120]}")
            results.append((depth, t, None, "ERROR"))
ok = sum(1 for r in results if r[3] == "OK")
print(f"\nNEEDLE SUMMARY: {ok}/{len(results)} exact retrievals; "
      + ", ".join(f"{d}:{sum(1 for r in results if r[0]==d and r[3]=='OK')}/{sum(1 for r in results if r[0]==d)}" for d in DEPTHS))
raise SystemExit(0 if ok == len(results) else 1)
PY
