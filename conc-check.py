#!/usr/bin/env python3
"""Does this lane still answer correctly when several requests share the engine?

sglang#36548 reports DFlash2 corrupting state under concurrency, and a DGX Spark
measurement on sglang#35860 puts numbers on it: with the packed-FP4-head NVFP4
target at concurrency 8, 111 of 304 greedy answers were wrong, against 0 of 100
served one at a time, 1 of 304 with the dense BF16-head export, and 0 of 304 with
speculation off. This repo's default target is a packed-FP4-head checkpoint served
with --max-running-requests 8, so the question is not academic.

Four deterministic ordering tasks, greedy, thinking off, exactly one right answer
each. The serial pass proves the model can do them at all; the concurrent pass is
the measurement. Any gap between the two is the engine, not the model.

  python3 conc-check.py                      # 60 serial, 304 at concurrency 8
  python3 conc-check.py --conc 4 --conc-n 96

Measured on the reference box, 2026-09-03, both heads, DFlash2 x8 and
--max-running-requests 8, on the 1M unit (fp8 KV, mem fraction 0.70, chunk 8192):

  edp1096/Huihui-Qwen3.8-27B-abliterated-FP8  serial 60/60   concurrent 304/304
  RadixArk/Qwen3.8-27B-NVFP4 (packed FP4 head)  serial 60/60   concurrent 304/304
  RadixArk/Qwen3.8-27B-NVFP4, --pad 3000        serial 40/40   concurrent 303/304
                                                (the one miss was verbose, not wrong)

So this repo's configuration does not reproduce the 111/304, on the same hardware
and the same target that produced it. What differs is the rest of the recipe: this
repo serves a pinned DFlash2 image and drafter revision with an fp8 KV cache, mem
fraction 0.70 and a 8192 chunked prefill, where the cookbook cell is a plain 0.80 /
2048 configuration. A negative result is not a proof of absence, so the probe ships
and the numbers stay reproducible.
"""
import json, random, time, urllib.request, threading, queue, argparse
from pathlib import Path

BASE = "http://127.0.0.1:30000"
KEY = (Path.home() / ".config/qwen38/api-key").read_text().strip()

TASKS = [
    ("sort", "Sort these numbers in ascending order: 47 3 91 18 60 7 25 88 12 54 33 76. "
             "Answer with the sorted numbers separated by single spaces, nothing else.",
     "3 7 12 18 25 33 47 54 60 76 88 91"),
    ("rev",  "Reverse this list: alpha bravo charlie delta echo foxtrot golf hotel. "
             "Answer with the reversed words separated by single spaces, nothing else.",
     "hotel golf foxtrot echo delta charlie bravo alpha"),
    ("dates", "Sort these dates oldest first: 1999-04-02 1987-11-30 2011-01-15 1993-07-08 2004-09-21. "
              "Answer with the dates separated by single spaces, nothing else.",
     "1987-11-30 1993-07-08 1999-04-02 2004-09-21 2011-01-15"),
    ("alpha", "Put these words in alphabetical order: zebra mango apple orange kiwi banana. "
              "Answer with the words separated by single spaces, nothing else.",
     "apple banana kiwi mango orange zebra"),
]

# --hard: what #36548 actually describes is one request's content surfacing inside
# another's answer, which needs distinct contexts large enough to be worth confusing.
# Each request gets its own filler, so nothing is shared through the radix cache, and
# the task sits at the end where a mixed-up context would show.
WORDS = ("ledger anchor tundra sparrow cobalt maple harbor quartz lantern drift "
         "cinder willow marble ferry basalt orchid pebble cedar meadow flint").split()


def filler(rng, approx_tokens):
    return " ".join(rng.choice(WORDS) for _ in range(int(approx_tokens * 0.75)))


def ask(prompt, model, timeout=180):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0, "top_p": 1, "max_tokens": 400,
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + KEY})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"].strip()

def norm(s):
    return " ".join(s.replace(",", " ").split()).strip().rstrip(".").lower()


def grade(got, want, name):
    """exact | verbose (right answer inside a rambling one) | wrong | contaminated.

    "contaminated" is the finding of sglang#36548: another request's answer surfacing
    in this one. It is the only outcome here that accuses the engine rather than the
    model, so it is counted apart."""
    g, w = norm(got), norm(want)
    if g == w:
        return "exact"
    foreign = [n for n, _, other in TASKS if n != name and norm(other) in g]
    if foreign:
        return "contaminated:" + foreign[0]
    return "verbose" if w in g else "wrong"

def run(n, conc, model, label, pad=0):
    jobs = queue.Queue()
    rng = random.Random(1234)
    for i in range(n):
        name, prompt, want = TASKS[i % len(TASKS)]
        if pad:
            prompt = (f"Notes {i} (ignore them, they are filler): {filler(rng, pad)}\n\n"
                      + prompt)
        jobs.put((name, prompt, want))
    bad, errs, out, lock = [], [], [], threading.Lock()
    def worker():
        while True:
            try: name, prompt, want = jobs.get_nowait()
            except queue.Empty: return
            try: got = ask(prompt, model)
            except Exception as e:
                with lock: errs.append(f"{name}: {type(e).__name__}: {e}")
                continue
            verdict = grade(got, want, name)
            with lock:
                out.append(verdict)
                if verdict != "exact":
                    bad.append((name, verdict, got[:160]))
    t0 = time.time()
    ts = [threading.Thread(target=worker) for _ in range(conc)]
    [t.start() for t in ts]; [t.join() for t in ts]
    dt = time.time() - t0
    exact = out.count("exact")
    verbose = out.count("verbose")
    contam = sum(1 for v in out if v.startswith("contaminated"))
    wrong = out.count("wrong")
    print(f"{label:52} {exact:4}/{len(out):4} exact, {verbose:3} verbeux, "
          f"{wrong:3} faux, {contam:3} contamines, {len(errs):2} erreurs  {dt:6.1f}s")
    for name, verdict, got in bad[:6]:
        print(f"      {verdict.upper()} [{name}] {got!r}")
    for e in errs[:3]:
        print(f"      ERR  {e}")
    return wrong + contam, contam

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--serial", type=int, default=40)
    p.add_argument("--conc-n", type=int, default=160)
    p.add_argument("--conc", type=int, default=8)
    p.add_argument("--pad", type=int, default=0,
                   help="approximate filler tokens per request, unique to each request")
    a = p.parse_args()
    model = json.loads(urllib.request.urlopen(urllib.request.Request(
        BASE + "/get_model_info", headers={"Authorization": "Bearer " + KEY})).read())["model_path"]
    print(f"modele: {model}\n")
    tag = f", {a.pad} tokens de contexte propre" if a.pad else ""
    b1, c1 = run(a.serial, 1, model, f"serie (c=1, n={a.serial}{tag})", a.pad)
    b2, c2 = run(a.conc_n, a.conc, model, f"concurrent (c={a.conc}, n={a.conc_n}{tag})", a.pad)
    print(f"\nverdict: serie {b1} faux/contamines, concurrent {b2} faux/contamines, "
          f"{c1 + c2} contamination(s) croisee(s)")
