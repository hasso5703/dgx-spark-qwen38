#!/usr/bin/env python3
"""Does POST /v1/systemone hold its contract on a live lane, alone and in a crowd?

`tests/test_proxy_systemone.py` answers the same questions against a fake engine with
scripted distributions, which is where the edges belong: it can make the engine return a
NaN or an empty top-k on demand. What it cannot do is serve a real model. This tool is
the other half: every shape of the wire contract sent to a real lane, every refusal the
hosted API makes checked against what this one makes, every lever switched on, and the
whole thing again while ordinary chat traffic shares the engine.

The concurrency half is not decoration. The readout rides `/v1/chat/completions` with
`top_logprobs` precisely because the obvious implementation, `token_ids_logprob` on
`/generate`, kills this build's scheduler the first time a scoring request shares a batch
with an ordinary one (sglang#34719). That claim was read in the container's source. This
tool is where it gets tested on the box: typed decisions and ordinary completions, mixed,
on purpose, and both are graded.

  python3 systemone-check.py                        # shapes, refusals, aliases, mixed load
  python3 systemone-check.py --levers               # also spawn a proxy per lever
  python3 systemone-check.py --hosted               # also send every shape to api.typesafe.ai
  python3 systemone-check.py --mixed-seconds 60     # a longer crowd

Exit status is the number of failed checks, so it is usable as a gate. Every check names
what it expected and what it got; nothing is reported as passing because it did not
crash.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent
KEY = Path.home() / ".config/qwen38/api-key"
HOSTED = "https://api.typesafe.ai"
HOSTED_KEY = Path.home() / ".config/qwen38/typesafe-api-key"

TICKET = ("Hi, I have been trying to connect my Stripe account for three days and it keeps "
          "failing. I am losing sales every hour. Please help as soon as you can.")


def post(base, path, obj, key, timeout=300):
    """(status, headers, body, seconds). A refusal is an answer here, not an exception."""
    data = json.dumps(obj).encode()
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), json.loads(r.read().decode()), time.time() - t0
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            body = json.loads(raw.decode())
        except Exception:
            body = {"_raw": raw[:400].decode("utf-8", "replace")}
        return e.code, dict(e.headers), body, time.time() - t0
    except Exception as e:
        return 0, {}, {"_error": f"{type(e).__name__}: {e}"}, time.time() - t0


def close(a, b, tol=0.011):
    return isinstance(a, (int, float)) and abs(a - b) <= tol


def grade_answer(kind, ans, spec):
    """Every way one answer can be wrong, named. The shapes are the hosted model's own,
    read off live calls on 2026-09-18 and again on 2026-09-21."""
    bad = []
    if not isinstance(ans, dict):
        return [f"answer is {type(ans).__name__}, not an object"]
    if ans.get("type") != kind:
        bad.append(f"type={ans.get('type')!r}, expected {kind!r}")
    probs = ans.get("probabilities")
    if kind == "noul":
        p = ans.get("noul")
        if not isinstance(p, (int, float)):
            bad.append(f"noul={p!r} is not a number")
        elif not 0.0 <= p <= 1.0:
            bad.append(f"noul={p} outside [0, 1]")
        for key in ("choice", "score", "legend", "probabilities"):
            if key in ans:
                bad.append(f"a noul carries {key!r}, which the hosted answer does not")
        return bad
    if not isinstance(probs, dict) or not probs:
        bad.append(f"probabilities={probs!r} is not a non-empty object")
        return bad
    for name, p in probs.items():
        if not isinstance(p, (int, float)) or not 0.0 <= p <= 1.0:
            bad.append(f"probability {name!r}={p!r} outside [0, 1]")
    if not close(sum(v for v in probs.values() if isinstance(v, (int, float))), 1.0):
        bad.append(f"probabilities sum to {sum(probs.values()):.4f}, not 1")
    conf = ans.get("confidence")
    if not isinstance(conf, (int, float)) or not 0.0 <= conf <= 1.0:
        bad.append(f"confidence={conf!r} outside [0, 1]")
    if kind == "choice":
        want = set(spec["criteria"]) if isinstance(spec.get("criteria"), dict) else set()
        if set(probs) != want:
            bad.append(f"probabilities keys {sorted(probs)[:4]}... do not match the options asked")
        pick = ans.get("choice")
        if pick not in probs:
            bad.append(f"choice={pick!r} is not one of the options")
        elif probs[pick] != max(probs.values()):
            bad.append(f"choice={pick!r} does not carry the highest probability")
    if kind == "score":
        levels = spec.get("criteria") or []
        legend = ans.get("legend")
        if not isinstance(legend, dict) or len(legend) != len(levels):
            bad.append(f"legend has {len(legend or {})} entries for {len(levels)} levels")
        elif [legend[k] for k in sorted(legend, key=int)] != list(levels):
            bad.append("legend does not repeat the levels in the order they were given")
        if set(probs) != {str(i) for i in range(len(levels))}:
            bad.append(f"probability keys {sorted(probs)} are not the level indices")
        # The score is the expected level index, not a fraction: the hosted model returns
        # 8.45 on a ten-level scale (checked live, 2026-09-21), so the range is the scale's.
        s = ans.get("score")
        top = max(0, len(levels) - 1)
        if not isinstance(s, (int, float)) or not 0.0 <= s <= top:
            bad.append(f"score={s!r} outside [0, {top}] for {len(levels)} levels")
    return bad


def grade_envelope(body, headers, questions):
    bad = []
    if not isinstance(body.get("model"), str) or not body["model"]:
        bad.append(f"model={body.get('model')!r} does not name what answered")
    answers = body.get("answers")
    if not isinstance(answers, dict):
        return bad + [f"answers is {type(answers).__name__}, not an object"]
    if set(answers) != set(questions):
        bad.append(f"answered {sorted(answers)[:4]}... for {sorted(questions)[:4]}...")
    usage = body.get("usage") or {}
    for k in ("input_tokens", "output_tokens"):
        if not isinstance(usage.get(k), int):
            bad.append(f"usage.{k}={usage.get(k)!r} is not an integer")
    mass = headers.get("x-systemone-label-mass")
    if mass is not None:
        try:
            if not 0.0 <= float(mass) <= 1.0:
                bad.append(f"x-systemone-label-mass={mass} outside [0, 1]")
        except ValueError:
            bad.append(f"x-systemone-label-mass={mass!r} is not a number")
    return bad


def shapes(state=TICKET):
    """Every request shape worth sending, each with what the contract says comes back."""
    def choice(n, prefix="opt"):
        return {"type": "choice", "instructions": f"Pick one of {n}",
                "criteria": {f"{prefix}{i}": f"the {i}th option" for i in range(n)}}

    cases = {
        "noul, instructions only": {"q": {"type": "noul", "instructions": "The customer is upset"}},
        "noul, with criteria": {"q": {"type": "noul", "instructions": "Urgent",
                                      "criteria": {"true": "needs action today",
                                                   "false": "can wait"}}},
        "choice, 2 options": {"q": {"type": "choice", "instructions": "Which team",
                                    "criteria": {"billing": "money", "technical": "bugs"}}},
        "choice, 10 options": {"q": choice(10)},
        "choice, 26 options (past the single letters)": {"q": choice(26)},
        "choice, 64 options (two-letter labels)": {"q": choice(64)},
        "choice, 255 options (the cap)": {"q": choice(255)},
        "choice, 1 option (the hosted API answers it)": {"q": choice(1)},
        "choice, an option named the empty string": {
            "q": {"type": "choice", "instructions": "Pick", "criteria": {"": "empty name", "a": "a"}}},
        "choice, unicode option names": {
            "q": {"type": "choice", "instructions": "Choisir",
                  "criteria": {"facturation": "argent", "technique": "bogues", "autre": "reste"}}},
        "score, 2 levels": {"q": {"type": "score", "instructions": "How angry",
                                  "criteria": ["calm", "furious"]}},
        "score, 10 levels (the cap)": {"q": {"type": "score", "instructions": "How angry",
                                             "criteria": [f"level {i}" for i in range(10)]}},
        "score, 1 level (the hosted API answers it)": {
            "q": {"type": "score", "instructions": "How angry", "criteria": ["calm"]}},
        "all three types in one call": {
            "dept": {"type": "choice", "instructions": "Which team",
                     "criteria": {"billing": "money", "technical": "bugs"}},
            "anger": {"type": "score", "instructions": "How angry",
                      "criteria": ["calm", "annoyed", "furious"]},
            "urgent": {"type": "noul", "instructions": "Time sensitive"}},
        "13 questions on one state": {
            f"q{i}": {"type": "noul", "instructions": f"Statement number {i} is supported"}
            for i in range(13)},
        "a newline and a quote inside the instructions": {
            "q": {"type": "noul", "instructions": 'Line one\nLine two with "quotes" and a \\ backslash'}},
    }
    return {name: (state, qs) for name, qs in cases.items()}


# Every status here was read off api.typesafe.ai, not guessed: the first version of this
# table expected 422 for the three that parse and cannot be served, and the hosted API
# answers 400 for all three with, in one case, the same message to the character
# (checked 2026-09-21). A 422 carries a detail LIST naming the path that failed; a 400
# carries a detail object, or a plain string where the hosted API uses one.
REFUSALS = {
    "questions as a list, not a map": ({"state": "s", "model": "jev-latest",
                                        "questions": [{"type": "noul"}]}, 422),
    "no state at all": ({"model": "jev-latest", "questions": {"q": {"type": "noul",
                                                                    "instructions": "i"}}}, 422),
    "an unknown field at the top level": ({"state": "s", "model": "jev-latest", "extra": 1,
                                           "questions": {"q": {"type": "noul",
                                                               "instructions": "i"}}}, 400),
    "an unknown question type": ({"state": "s", "model": "jev-latest",
                                  "questions": {"q": {"type": "vibe", "instructions": "i"}}}, 400),
    "a noul with neither instructions nor criteria": (
        {"state": "s", "model": "jev-latest", "questions": {"q": {"type": "noul"}}}, 400),
    "an empty question id": ({"state": "s", "model": "jev-latest",
                              "questions": {"": {"type": "noul", "instructions": "i"}}}, 400),
    "a model that is not this lane and not Jev": (
        {"state": "s", "model": "jev-9", "questions": {"q": {"type": "noul",
                                                             "instructions": "i"}}}, 400),
    "eleven score levels": ({"state": "s", "model": "jev-latest", "questions": {
        "q": {"type": "score", "instructions": "i",
              "criteria": [f"l{i}" for i in range(11)]}}}, 400),
    "256 choices": ({"state": "s", "model": "jev-latest", "questions": {
        "q": {"type": "choice", "instructions": "i",
              "criteria": {f"o{i}": "d" for i in range(256)}}}}, 400),
}


class Report:
    def __init__(self):
        self.rows, self.failed = [], 0

    def check(self, name, problems, note=""):
        ok = not problems
        if not ok:
            self.failed += 1
        self.rows.append((ok, name, "; ".join(problems) if problems else note))
        mark = " ok " if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f"  {note}" if ok and note else ""))
        for p in problems:
            print(f"         {p}")
        sys.stdout.flush()
        return ok


def spawn_proxy(port, env_extra, upstream, key):
    env = dict(os.environ, UPSTREAM=upstream, QWEN38_UPSTREAM_API_KEY=key,
               PROMPT_CEILING_TOKENS="0", **env_extra)
    log = open(f"/tmp/systemone-check-{port}.log", "w")
    p = subprocess.Popen([sys.executable, str(REPO / "keepalive-proxy.py"), str(port)],
                         cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        if p.poll() is not None:
            # It died rather than stayed quiet, and the difference matters: the first
            # version of this said "never answered" for a port still held by the previous
            # run, which sends the reader looking in the wrong place entirely.
            log.flush()
            why = Path(f"/tmp/systemone-check-{port}.log").read_text().strip().splitlines()
            raise SystemExit(f"the proxy on :{port} exited with {p.returncode}: "
                             + (why[-1] if why else "no output"))
        try:
            urllib.request.urlopen(base + "/health", timeout=5).read()
            return p, base
        except urllib.error.HTTPError:
            return p, base                      # answered, which is all this needs
        except Exception:
            time.sleep(1)
    p.kill()
    raise SystemExit(f"the proxy on :{port} never answered in 60s")


def chat_answer_problem(text, stream):
    """None when a chat completion is a whole answer, else what is wrong with it. A stream
    counted as whole once it held "data:", and the proxy's keepalive frames carry that,
    as do a stream the engine cut mid-answer and the abort for corrupted output (found in
    review, 2026-09-24): whole means text, a finish_reason, the [DONE] terminator, and no
    error event on the way."""
    if not stream:
        try:
            message = json.loads(text)["choices"][0]["message"]
            said = message.get("content") or message.get("reasoning_content") or message.get("tool_calls")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            return f"not a chat completion: {text[:120]!r}"
        return None if said else "an empty message"
    got_text = finished = done = False
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            done = True
            continue
        try:
            event = json.loads(payload)
        except ValueError:
            return f"a frame that is not JSON: {payload[:120]!r}"
        if not isinstance(event, dict):
            return f"a frame that is not a JSON object: {payload[:120]!r}"
        if event.get("error"):
            return f"an error event: {json.dumps(event['error'])[:160]}"
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            got_text = got_text or bool(delta.get("content") or delta.get("reasoning_content"))
            finished = finished or bool(choice.get("finish_reason"))
    if not got_text:
        return "a stream with no text in it"
    if not finished or not done:
        return "a stream that ended without its finish_reason and [DONE]: cut short"
    return None


def classic_load(base, key, stop, stats, stream):
    """Ordinary chat traffic, the kind the lane exists for, while the crowd runs."""
    while not stop.is_set():
        body = {"model": stats["model"], "max_tokens": 48, "stream": stream,
                "messages": [{"role": "user", "content": "Name three colours, one per line."}],
                "chat_template_kwargs": {"enable_thinking": False}}
        t0 = time.time()
        try:
            req = urllib.request.Request(base + "/v1/chat/completions",
                                         data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=300) as r:
                raw = r.read()
            why = chat_answer_problem(raw.decode("utf-8", "replace"), stream)
            stats["bad" if why else "ok"] += 1
            if why:
                stats.setdefault("errors", []).append(why)
            stats["lat"].append(time.time() - t0)
        except Exception as e:
            stats["bad"] += 1
            stats.setdefault("errors", []).append(f"{type(e).__name__}: {e}")
        if stop.wait(0.5):
            return


def p(vals, q):
    if not vals:
        return float("nan")
    s = sorted(vals)
    return s[min(len(s) - 1, int(q * len(s)))]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.environ.get("QWEN38_SYSTEMONE_URL", "http://127.0.0.1:30001"))
    ap.add_argument("--upstream", default="http://127.0.0.1:30000")
    ap.add_argument("--model", default="jev-latest")
    ap.add_argument("--port", type=int, default=0, help="spawn a proxy of this worktree on this port")
    ap.add_argument("--levers", action="store_true", help="spawn one proxy per lever and check each")
    ap.add_argument("--hosted", action="store_true", help="send every shape to api.typesafe.ai too")
    ap.add_argument("--mixed-seconds", type=int, default=45)
    ap.add_argument("--door", type=int, default=12, help="simultaneous calls for the door check")
    args = ap.parse_args()

    key = KEY.read_text().strip()
    spawned = []
    base = args.base
    if args.port:
        proc, base = spawn_proxy(args.port, {}, args.upstream, key)
        spawned.append(proc)

    rep = Report()
    try:
        req = urllib.request.Request(base + "/v1/models", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            lane = json.loads(r.read().decode())["data"][0]["id"]
        print(f"lane: {lane}   proxy: {base}\n")

        print("SHAPES, one call at a time")
        for name, (state, questions) in shapes().items():
            st, hd, body, secs = post(base, "/v1/systemone",
                                      {"state": state, "model": args.model, "questions": questions}, key)
            if st != 200:
                rep.check(name, [f"HTTP {st}: {json.dumps(body)[:200]}"])
                continue
            bad = grade_envelope(body, hd, questions)
            for qid, spec in questions.items():
                bad += [f"{qid}: {b}" for b in grade_answer(spec["type"],
                                                            (body.get("answers") or {}).get(qid), spec)]
            rep.check(name, bad, f"{secs:.2f}s, {len(questions)} question(s)")

        print("\nREFUSALS, against the status the hosted API gives")
        for name, (body_in, want) in REFUSALS.items():
            st, _hd, body, _s = post(base, "/v1/systemone", body_in, key)
            bad = []
            if st != want:
                bad.append(f"HTTP {st}, expected {want}: {json.dumps(body)[:160]}")
            elif want == 422 and not isinstance(body.get("detail"), list):
                bad.append(f"a 422 must carry a detail list, got {json.dumps(body)[:160]}")
            elif want == 400 and "detail" not in body:
                bad.append(f"a 400 must carry detail, got {json.dumps(body)[:160]}")
            rep.check(name, bad, f"HTTP {st}")

        print("\nMODEL NAMES")
        for name in ("jev-latest", "jev-preview", "jev-1.13.0", lane):
            st, _hd, body, _s = post(base, "/v1/systemone",
                                     {"state": "s", "model": name,
                                      "questions": {"q": {"type": "noul", "instructions": "i"}}}, key)
            bad = [] if st == 200 else [f"HTTP {st}: {json.dumps(body)[:160]}"]
            if st == 200 and body.get("model") != lane:
                bad.append(f"answered as {body.get('model')!r}, not the lane {lane!r}")
            rep.check(f"{name} resolves to the lane and the answer says so", bad)

        if args.hosted and HOSTED_KEY.exists():
            print("\nTHE SAME BYTES TO THE HOSTED JEV")
            hkey = HOSTED_KEY.read_text().strip()
            for name, (state, questions) in list(shapes().items())[:6]:
                payload = {"state": state, "model": "jev-latest", "questions": questions}
                hs, _hh, hb, _ = post(HOSTED, "/v1/systemone", payload, hkey)
                os_, _oh, ob, _ = post(base, "/v1/systemone", payload, key)
                bad = []
                if hs != os_:
                    bad.append(f"hosted {hs}, here {os_}")
                elif hs == 200:
                    for qid in questions:
                        ha, oa = (hb.get("answers") or {}).get(qid), (ob.get("answers") or {}).get(qid)
                        if set(ha or {}) != set(oa or {}):
                            bad.append(f"{qid}: keys {sorted(ha or {})} vs {sorted(oa or {})}")
                rep.check(f"same contract: {name}", bad)

        if args.levers:
            print("\nLEVERS, each on its own proxy")
            levers = {
                "two option orders (SYSTEMONE_PERMUTATIONS=2)": {"SYSTEMONE_PERMUTATIONS": "2"},
                "flattened probabilities (SYSTEMONE_TEMPERATURE=1.5)": {"SYSTEMONE_TEMPERATURE": "1.5"},
                "a label-mass floor (SYSTEMONE_MIN_LABEL_MASS=0.2)": {"SYSTEMONE_MIN_LABEL_MASS": "0.2"},
                "a thinking budget (SYSTEMONE_THINK_TOKENS=256)": {"SYSTEMONE_THINK_TOKENS": "256"},
                "an answer prefix (SYSTEMONE_ANSWER_PREFIX)": {"SYSTEMONE_ANSWER_PREFIX": "The answer is"},
            }
            port = (args.port or 30099) + 1
            for name, env in levers.items():
                proc, lbase = spawn_proxy(port, env, args.upstream, key)
                spawned.append(proc)
                port += 1
                questions = {"dept": {"type": "choice", "instructions": "Which team",
                                      "criteria": {"billing": "money", "technical": "bugs"}},
                             "urgent": {"type": "noul", "instructions": "Time sensitive"}}
                st, hd, body, secs = post(lbase, "/v1/systemone",
                                          {"state": TICKET, "model": args.model,
                                           "questions": questions}, key)
                bad = [f"HTTP {st}: {json.dumps(body)[:160]}"] if st != 200 else \
                    grade_envelope(body, hd, questions)
                if st == 200:
                    for qid, spec in questions.items():
                        bad += [f"{qid}: {b}" for b in
                                grade_answer(spec["type"], body["answers"].get(qid), spec)]
                rep.check(name, bad, f"{secs:.2f}s")

        print(f"\nTHE DOOR, {args.door} calls at once")
        out = []
        qs = {f"q{i}": {"type": "noul", "instructions": f"Point {i} holds"} for i in range(12)}
        def one_call():
            out.append(post(base, "/v1/systemone",
                            {"state": TICKET, "model": args.model, "questions": qs}, key))
        threads = [threading.Thread(target=one_call) for _ in range(args.door)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        served = [r for r in out if r[0] == 200]
        held = [r for r in out if r[0] == 529]
        other = [r for r in out if r[0] not in (200, 529)]
        bad = [f"{len(other)} answered neither 200 nor 529: {[r[0] for r in other]}"] if other else []
        for st, hd, _b, _s in held:
            if not hd.get("Retry-After"):
                bad.append("a 529 arrived without Retry-After, so no SDK can retry it")
        if held and max(r[3] for r in held) > 5:
            bad.append(f"a refusal took {max(r[3] for r in held):.1f}s: a busy box must say no fast")
        rep.check("every call is served or told to come back, and the no is fast", bad,
                  f"{len(served)} served (p50 {p([r[3] for r in served], 0.5):.1f}s), "
                  f"{len(held)} held (max {max([r[3] for r in held] or [0]):.2f}s)")

        print(f"\nMIXED: typed decisions and ordinary chat on the same engine, {args.mixed_seconds}s")
        for stream in (False, True):
            label = "streamed" if stream else "plain"
            stats = {"ok": 0, "bad": 0, "lat": [], "model": lane}
            stop = threading.Event()
            loaders = [threading.Thread(target=classic_load, args=(base, key, stop, stats, stream))
                       for _ in range(2)]
            for t in loaders:
                t.start()
            t_end, s1 = time.time() + args.mixed_seconds, {"ok": 0, "bad": 0, "lat": [], "bad_why": []}
            while time.time() < t_end:
                questions = {"dept": {"type": "choice", "instructions": "Which team",
                                      "criteria": {"billing": "money", "technical": "bugs"}},
                             "anger": {"type": "score", "instructions": "How angry",
                                       "criteria": ["calm", "annoyed", "furious"]},
                             "urgent": {"type": "noul", "instructions": "Time sensitive"}}
                st, hd, body, secs = post(base, "/v1/systemone",
                                          {"state": TICKET, "model": args.model,
                                           "questions": questions}, key)
                problems = [f"HTTP {st}"] if st != 200 else grade_envelope(body, hd, questions)
                if st == 200:
                    for qid, spec in questions.items():
                        problems += grade_answer(spec["type"], body["answers"].get(qid), spec)
                if problems:
                    s1["bad"] += 1
                    s1["bad_why"] += problems[:2]
                else:
                    s1["ok"] += 1
                s1["lat"].append(secs)
            stop.set()
            for t in loaders:
                t.join()
            bad = []
            if s1["bad"]:
                bad.append(f"{s1['bad']} typed decisions came back wrong or refused: {s1['bad_why'][:3]}")
            if stats["bad"]:
                bad.append(f"{stats['bad']} ordinary completions failed: {stats.get('errors', [])[:2]}")
            if not s1["ok"] or not stats["ok"]:
                bad.append("one of the two sides never completed a request")
            rep.check(f"typed decisions and {label} chat, mixed on one engine", bad,
                      f"{s1['ok']} decisions (p50 {p(s1['lat'], 0.5):.2f}s) and "
                      f"{stats['ok']} completions (p50 {p(stats['lat'], 0.5):.2f}s) both clean")

        print("\nTHE ENGINE AFTER ALL OF IT")
        try:
            req = urllib.request.Request(args.upstream + "/health",
                                         headers={"Authorization": f"Bearer {key}"})
            code = urllib.request.urlopen(req, timeout=30).status
            rep.check("the engine is still serving", [] if code == 200 else [f"health {code}"])
        except Exception as e:
            rep.check("the engine is still serving", [f"{type(e).__name__}: {e}"])
    finally:
        for proc in spawned:
            proc.terminate()

    total = len(rep.rows)
    print(f"\n{total - rep.failed}/{total} checks passed" +
          ("" if not rep.failed else f", {rep.failed} FAILED"))
    return rep.failed


if __name__ == "__main__":
    sys.exit(min(main(), 125))
