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

Reference box, 2026-09-03, edp1096/Huihui-Qwen3.8-27B-abliterated-FP8, DFlash2 x8:
serial 60/60, concurrent 304/304. The FP8-head targets are clean at this shape.
"""
import json, time, urllib.request, threading, queue, argparse
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

def ask(prompt, model, timeout=180):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0, "top_p": 1, "max_tokens": 120,
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + KEY})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"].strip()

def norm(s):
    return " ".join(s.replace(",", " ").split()).strip().rstrip(".").lower()

def run(n, conc, model, label):
    jobs = queue.Queue()
    for i in range(n):
        jobs.put(TASKS[i % len(TASKS)])
    bad, errs, out, lock = [], [], [], threading.Lock()
    def worker():
        while True:
            try: name, prompt, want = jobs.get_nowait()
            except queue.Empty: return
            try: got = ask(prompt, model)
            except Exception as e:
                with lock: errs.append(f"{name}: {type(e).__name__}: {e}")
                continue
            ok = norm(got) == norm(want)
            with lock:
                out.append(ok)
                if not ok: bad.append((name, got[:140]))
    t0 = time.time()
    ts = [threading.Thread(target=worker) for _ in range(conc)]
    [t.start() for t in ts]; [t.join() for t in ts]
    dt = time.time() - t0
    print(f"{label:28} {len(out)-len(bad):4}/{len(out):4} exact  "
          f"{len(bad):3} faux  {len(errs):2} erreurs  {dt:6.1f}s")
    for name, got in bad[:6]:
        print(f"      FAUX [{name}] {got!r}")
    for e in errs[:3]:
        print(f"      ERR  {e}")
    return len(bad), len(errs)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--serial", type=int, default=40)
    p.add_argument("--conc-n", type=int, default=160)
    p.add_argument("--conc", type=int, default=8)
    a = p.parse_args()
    model = json.loads(urllib.request.urlopen(urllib.request.Request(
        BASE + "/get_model_info", headers={"Authorization": "Bearer " + KEY})).read())["model_path"]
    print(f"modele: {model}\n")
    b1, e1 = run(a.serial, 1, model, f"serie (c=1, n={a.serial})")
    b2, e2 = run(a.conc_n, a.conc, model, f"concurrent (c={a.conc}, n={a.conc_n})")
    print(f"\nverdict: serie {b1} faux, concurrent {b2} faux")
