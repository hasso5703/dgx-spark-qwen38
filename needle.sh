#!/usr/bin/env bash
# Long-context correctness probe: hides a passphrase deep inside filler text,
# asks the model to retrieve it, and reports the measured prompt depth.
# Catches the class of bug fixed in v1.5.2 (silent corruption deep in the
# context: runs of token id 0). Usage:
#   ./needle.sh [--depths "60000 120000"] [--trials 4] [--model qwen3.8-flash-next] [--no-flush] [--mem]
# Env: PORT (default 30001, the keepalive proxy), API key read from config.
# The engine's radix cache is flushed before each trial (retrieval is what is
# measured, not caching); --no-flush keeps the cache, which on a near-full pool
# exercises the eviction path instead.
set -euo pipefail
PORT="${PORT:-30001}"
DEPTHS="60000 100000 120000 140000"
TRIALS=2
MODEL=""
FLUSH=1
MEM=0
while [ $# -gt 0 ]; do
  case "$1" in
    --depths) DEPTHS="$2"; shift 2 ;;
    --trials) TRIALS="$2"; shift 2 ;;
    --model)  MODEL="$2"; shift 2 ;;
    --no-flush) FLUSH=0; shift ;;
    --mem) MEM=1; shift ;;   # sample host MemAvailable during each prompt (the flash lane's real ceiling is memory, see README)
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
KEY="$(cat "${HOME}/.config/qwen38/api-key")"
BASE="http://127.0.0.1:${PORT}"
if [ -z "$MODEL" ]; then
  MODEL="$(curl -s -m 5 -H "Authorization: Bearer $KEY" "$BASE/v1/models" \
    | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["data"][0]["id"])
except Exception: pass' 2>/dev/null)"
  [ -n "$MODEL" ] || { echo "needle: no model served at $BASE (engine down or booting); pass --model or retry" >&2; exit 2; }
fi
echo "needle probe: model=$MODEL endpoint=$BASE depths=[$DEPTHS] trials=$TRIALS"
export KEY BASE MODEL TRIALS DEPTHS FLUSH MEM
python3 -u - <<'PY'
import json, os, random, threading, time, urllib.error, urllib.request

KEY, BASE, MODEL = os.environ["KEY"], os.environ["BASE"], os.environ["MODEL"]
TRIALS = int(os.environ["TRIALS"]); DEPTHS = [int(x) for x in os.environ["DEPTHS"].split()]
WORDS = ("harbor lantern meadow copper violin thunder saddle marble orchard "
         "pepper canyon willow falcon ember granite tulip anchor cinder velvet "
         "quartz").split()

class Refused(Exception):
    """The lane refused this prompt by policy, which is not a retrieval result.

    Running through the keepalive proxy (the default port) means the oversize
    guard and the lane's PROMPT_CEILING_TOKENS apply. A depth above the ceiling
    comes back as 400 context_too_long, and counting that as a missed needle
    reported a correctness failure where the box had simply protected itself:
    ./needle.sh with its default depths does exactly that on the flash lane,
    whose ceiling is 128000."""


def chat(messages, max_tokens=40):
    body = json.dumps({"model": MODEL, "messages": messages, "max_tokens": max_tokens,
                       "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(f"{BASE}/v1/chat/completions", body,
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        kind = ""
        try:
            kind = ((json.loads(raw).get("error") or {}).get("type")) or ""
        except ValueError:
            pass
        if e.code == 400 and kind == "context_too_long":
            raise Refused(raw[:180]) from e
        raise
    return out, time.time() - t0

MEM = os.environ.get("MEM") == "1" and os.path.exists("/proc/meminfo")

def mem_avail_gib():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1048576
    return None

class MemSampler:
    """MemAvailable every 0.5 s while a prompt runs: the floor is what the prefill cost the box."""
    def __init__(self):
        self.floor = None; self.before = mem_avail_gib(); self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True); self._t.start()
    def _run(self):
        while not self._stop.is_set():
            v = mem_avail_gib()
            if v is not None and (self.floor is None or v < self.floor):
                self.floor = v
            self._stop.wait(0.5)
    def stop(self):
        self._stop.set(); self._t.join(timeout=2)
        return self.before, self.floor

def flush():
    if os.environ.get("FLUSH") != "1":
        return
    try:
        req = urllib.request.Request(f"{BASE}/flush_cache", b"", {"Authorization": f"Bearer {KEY}"}, method="POST")
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # noqa: BLE001
        print(f"flush_cache failed: {str(e)[:80]}")

def filler(rng, n_sentences):
    return " ".join(f"The {rng.choice(WORDS)} near the {rng.choice(WORDS)} was counted {rng.randint(2,99)} times on day {rng.randint(1,28)}."
                    for _ in range(n_sentences))

# calibrate chars per token on a ~30k-token sample (small samples under-estimate
# by 30 %: the ratio drifts with numbers and repeated words), then re-fit after
# every trial so each depth lands within a few percent of its target
rng = random.Random(1)
sample = filler(rng, 3500)
try:
    out, _ = chat([{"role": "user", "content": sample + "\nReply with the single word OK."}], 5)
except Exception as e:  # noqa: BLE001
    print(f"needle: calibration request failed ({str(e)[:100]}); engine down, booting or refusing")
    raise SystemExit(2)
cpt = len(sample) / max(1, out["usage"]["prompt_tokens"] - 40)
cps = len(sample) / 3500                      # measured chars per filler sentence
print(f"calibration: {cpt:.2f} chars/token, {cps:.1f} chars/sentence, on {out['usage']['prompt_tokens']} tokens")

results = []
mem_floors = []
for depth in DEPTHS:
    for t in range(TRIALS):
        rng = random.Random(depth * 31 + t)
        pw = "-".join(rng.choice(WORDS) for _ in range(3)) + f"-{rng.randint(100,999)}"
        n_sent = int(depth * cpt / cps)         # sentences needed for this depth
        text = filler(rng, n_sent)
        cut = int(len(text) * (0.35 + 0.3 * rng.random()))
        text = text[:cut] + f" IMPORTANT: the secret passphrase is {pw}. " + text[cut:]
        msgs = [{"role": "user", "content": text + "\n\nWhat is the secret passphrase mentioned above? Reply with only the passphrase."}]
        flush()
        sampler = MemSampler() if MEM else None
        try:
            out, dt = chat(msgs, 40)
            reply = (out["choices"][0]["message"].get("content") or "").strip()
            ptok = out["usage"]["prefill_tokens" if "prefill_tokens" in out["usage"] else "prompt_tokens"]
            cpt = 0.5 * cpt + 0.5 * (len(msgs[0]["content"]) / max(1, ptok))   # re-fit
            ok = pw in reply
            garbage = any(ch * 6 in reply for ch in "!\"#$%&'()*") or (reply and len(set(reply)) <= 2)
            verdict = "OK" if ok else ("CORRUPT" if garbage else "MISS")
            memnote = ""
            if sampler:
                before, floor = sampler.stop()
                if before is not None and floor is not None:
                    memnote = f"  mem: floor {floor:.1f} GiB, {before - floor:.1f} GiB below the {before:.1f} GiB before"
                    mem_floors.append(floor)
            print(f"depth~{depth:>6} trial {t+1}: prompt_tokens={ptok} {dt:5.1f}s -> {verdict}  reply={reply[:60]!r}{memnote}")
            results.append((depth, t, ptok, verdict))
        except Refused as e:
            if sampler:
                sampler.stop()
            print(f"depth~{depth:>6} trial {t+1}: REFUSED by the lane, not attempted: {str(e)[:110]}")
            results.append((depth, t, None, "REFUSED"))
        except Exception as e:  # noqa: BLE001
            if sampler:
                sampler.stop()
            print(f"depth~{depth:>6} trial {t+1}: ERROR {str(e)[:120]}")
            results.append((depth, t, None, "ERROR"))
attempted = [r for r in results if r[3] != "REFUSED"]
refused = [r for r in results if r[3] == "REFUSED"]
ok = sum(1 for r in attempted if r[3] == "OK")
print(f"\nNEEDLE SUMMARY: {ok}/{len(attempted)} exact retrievals"
      + (f" ({len(refused)} refused by the lane's own prompt limit, not measured)" if refused else "") + "; "
      + ", ".join(f"{d}:{sum(1 for r in results if r[0]==d and r[3]=='OK')}/{sum(1 for r in results if r[0]==d and r[3]!='REFUSED')}"
                  for d in DEPTHS))
if refused:
    print("Refused depths were never sent to the engine: they exceed this lane's "
          "PROMPT_CEILING_TOKENS or the proxy's share of the KV pool. To probe past it, "
          "either lower --depths or talk to the engine directly with PORT=30000 "
          "(no keepalive, no guard: that is how the ceiling itself was measured).")
if MEM and mem_floors:
    print(f"MEM SUMMARY: lowest MemAvailable during a prompt {min(mem_floors):.1f} GiB, now {mem_avail_gib():.1f} GiB "
          "(the flash lane keeps the memory it took; under ~10 GiB the box is near the livelock edge, see README)")
if not attempted:
    print("nothing was measured: every depth was refused by the lane's own limit")
    raise SystemExit(2)
raise SystemExit(0 if ok == len(attempted) else 1)
PY
