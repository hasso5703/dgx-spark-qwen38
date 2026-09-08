#!/usr/bin/env python3
"""Agent-loop benchmark: the shape agent work actually has, which no tok/s number captures.

Every published number for this model, this repo's included, sends a fresh
prompt and measures the answer. An agent client does the opposite: it resends
the whole conversation every turn and asks for a short answer. What decides how
that feels is not decode speed, it is whether the engine can reuse the prefix it
just served, and how much of the turn is spent before the first token.

That distinction is not theoretical. On vLLM, a third party measured the drafter
ranking *invert* between the two shapes: MTP had the best decode on their box
(38.6-40.6 tok/s) and the worst agent loop (47.8 ms/tok), worse than no
speculation at all (43.4), because vLLM's scheduler drops one cacheable block
per request when a speculative drafter is configured, so every turn re-prefills
what it just cached (jschmied, notes/which-drafter-for-agent-work.md and
notes/mtp-vs-prefix-cache.md, 2026-08-31). Selecting a config on tok/s can pick
exactly the wrong one, so this repo measures the other shape too.

  ./bench-agent.py                        # 8 turns on an 8k prefix, through the proxy
  ./bench-agent.py --turns 12 --prefix-tokens 30000
  ./bench-agent.py --port 30000           # straight at the engine
  ./bench-agent.py --flush                # cold: flush the prefix cache first
  ./bench-agent.py --no-pin-work          # let each turn stop where it wants

Work is PINNED by default: every turn generates exactly --turn-tokens tokens
(`ignore_eos`, verified against this engine). Without that, ms/tok is not a
comparable number, because a turn that answers in 14 tokens amortizes its fixed
TTFT over 14 and one that answers in 37 over 37, and the ranking follows the
answer lengths instead of the engine. The published figure this is meant to be
read against pins the same way ("work pinned at 8 x 130 tokens, ms/tok reported,
unequal work refused"), so this tool refuses its own summary if the turns did
not do equal work.

Reads the API key from ~/.config/qwen38/api-key. Prints one line per turn and a
summary; exits non-zero if the lane refused a turn, because a partial loop is
not a measurement.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

CONFIG = os.path.expanduser("~/.config/qwen38")

FILLER = (
    "The unified memory pool of a GB10 is shared between the host and the GPU, "
    "which changes how a serving stack has to budget its caches: the KV cache, "
    "the recurrent state of the linear-attention layers and the page cache all "
    "come out of the same 128 GB. "
)

TURN_QUESTIONS = [
    "Name one consequence of that for the KV cache. One sentence.",
    "And one consequence for prefill. One sentence.",
    "Which of the two costs more host memory? One sentence.",
    "Give one number a reader should check. One sentence.",
    "What would you measure first? One sentence.",
    "Name a failure mode of the shared pool. One sentence.",
    "How would you bound it? One sentence.",
    "What stays true if the pool doubles? One sentence.",
    "What breaks if the pool halves? One sentence.",
    "One sentence on why page cache matters here.",
    "One sentence on what an agent client changes.",
    "One sentence to close.",
]


def die(msg: str, code: int = 2):
    print(f"bench-agent: {msg}", file=sys.stderr)
    raise SystemExit(code)


def api_key() -> str:
    path = os.path.join(CONFIG, "api-key")
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        die(f"no API key at {path}. Install the stack first, or pass one in QWEN38_API_KEY.")


def post(url: str, key: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        die(f"the lane refused a turn: HTTP {exc.code} {detail}\n"
            f"A refused turn makes the loop unmeasurable. If it is the proxy's prompt "
            f"ceiling, lower --prefix-tokens or --turns.", 3)
    except urllib.error.URLError as exc:
        die(f"cannot reach {url}: {exc.reason}. Is the lane up?", 4)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=30001,
                    help="30001 is the keepalive proxy, which is what agent clients use")
    ap.add_argument("--model", default=None, help="default: whatever /v1/models serves")
    ap.add_argument("--turns", type=int, default=8)
    ap.add_argument("--turn-tokens", type=int, default=130,
                    help="answer length per turn, pinned so turns are comparable")
    ap.add_argument("--prefix-tokens", type=int, default=8000,
                    help="approximate size of the shared prefix the conversation starts from")
    ap.add_argument("--flush", action="store_true",
                    help="flush the engine's prefix cache first (measures the cold shape)")
    ap.add_argument("--think", action="store_true", help="leave reasoning on (default off)")
    ap.add_argument("--pin-work", dest="pin_work", action="store_true", default=True,
                    help="every turn generates exactly --turn-tokens tokens (default)")
    ap.add_argument("--no-pin-work", dest="pin_work", action="store_false",
                    help="let each turn stop at its natural end; ms/tok stops being comparable")
    args = ap.parse_args()

    if args.turns < 2:
        die("--turns must be at least 2: the point is how turn N behaves after turn N-1")
    if args.turns > len(TURN_QUESTIONS):
        die(f"--turns is capped at {len(TURN_QUESTIONS)} by the built-in question list")

    key = os.environ.get("QWEN38_API_KEY") or api_key()
    base = f"http://{args.host}:{args.port}"

    model = args.model
    if model is None:
        req = urllib.request.Request(f"{base}/v1/models",
                                     headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                model = json.loads(resp.read())["data"][0]["id"]
        except Exception as exc:  # noqa: BLE001
            die(f"cannot read {base}/v1/models ({exc}); pass --model")

    if args.flush:
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"http://{args.host}:30000/flush_cache", data=b"",
                headers={"Authorization": f"Bearer {key}"}, method="POST"), timeout=20)
            print("prefix cache flushed")
        except Exception as exc:  # noqa: BLE001
            # A flush is refused while anything is in flight, by design.
            print(f"note: flush refused ({str(exc)[:60]}); measuring on the cache as it is")

    # ~3.4 characters per token on this tokenizer, measured; the exact size does
    # not matter, only that it is the same for every turn of a run.
    reps = max(1, int(args.prefix_tokens * 3.4 / len(FILLER)))
    prefix = FILLER * reps
    messages = [{"role": "user", "content":
                 prefix + "\n\nRead the passage above. Answer the questions that follow, "
                          "one short sentence each."},
                {"role": "assistant", "content": "Understood."}]

    print(f"agent loop: {args.turns} turns, ~{args.prefix_tokens} token prefix, "
          f"{args.turn_tokens} tokens per answer"
          f"{' (pinned)' if args.pin_work else ' (not pinned)'}, {base}, model {model}")
    print(f"{'turn':>4}  {'prompt':>8}  {'out':>4}  {'ttft':>7}  {'total':>7}  {'ms/tok':>7}")

    rows = []
    for turn in range(args.turns):
        messages.append({"role": "user", "content": TURN_QUESTIONS[turn]})
        body = {"model": model, "max_tokens": args.turn_tokens, "temperature": 0,
                "messages": messages, "stream": True,
                "chat_template_kwargs": {"enable_thinking": args.think},
                "stream_options": {"include_usage": True}}
        if args.pin_work:
            # Exactly max_tokens tokens per turn, so ms/tok compares engines and
            # not answer lengths. The text runs past its natural end and the
            # conversation carries that, which is the point: every turn of every
            # run does the same work.
            body["ignore_eos"] = True
        req = urllib.request.Request(
            f"{base}/v1/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        t0 = time.time()
        ttft = None
        text = []
        usage = {}
        try:
            with urllib.request.urlopen(req, timeout=1200) as resp:
                for raw in resp:
                    line = raw.decode(errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        piece = (choice.get("delta") or {}).get("content") or ""
                        if piece:
                            if ttft is None:
                                ttft = time.time() - t0
                            text.append(piece)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            die(f"turn {turn + 1} refused: HTTP {exc.code} {detail}", 3)
        total = time.time() - t0
        answer = "".join(text)
        if not answer:
            die(f"turn {turn + 1} produced no content in {total:.1f}s; the loop is not measurable", 3)
        out_tokens = usage.get("completion_tokens") or 0
        prompt_tokens = usage.get("prompt_tokens") or 0
        if ttft is None:
            ttft = total
        # ms per output token, the number that ranks configurations on this shape
        ms_per_tok = 1000.0 * total / out_tokens if out_tokens else float("nan")
        rows.append({"turn": turn + 1, "prompt": prompt_tokens, "out": out_tokens,
                     "ttft": ttft, "total": total, "ms_per_tok": ms_per_tok})
        print(f"{turn + 1:>4}  {prompt_tokens:>8}  {out_tokens:>4}  "
              f"{ttft:>6.2f}s  {total:>6.2f}s  {ms_per_tok:>7.1f}")
        messages.append({"role": "assistant", "content": answer})

    # Unequal work makes ms/tok a statement about answer lengths, so say so
    # rather than print a ranking nobody can compare.
    outs = {r["out"] for r in rows}
    if args.pin_work and outs != {args.turn_tokens}:
        die(f"the turns did not do equal work ({sorted(outs)} tokens against a pinned "
            f"{args.turn_tokens}); the engine did not honour ignore_eos, so ms/tok is "
            f"not comparable and no summary is printed", 5)
    # Turn 1 pays the cold prefix; the loop is what turns 2..N do.
    loop = rows[1:]
    med = statistics.median(r["ms_per_tok"] for r in loop)
    ttfts = [r["ttft"] for r in loop]
    growth = ttfts[-1] - ttfts[0]
    print()
    print(f"cold turn        {rows[0]['ms_per_tok']:.1f} ms/tok, TTFT {rows[0]['ttft']:.2f}s")
    print(f"loop median      {med:.1f} ms/tok over {len(loop)} turns "
          f"({statistics.mean(r['ms_per_tok'] for r in loop):.1f} mean)")
    print(f"TTFT in the loop {min(ttfts):.2f}s to {max(ttfts):.2f}s, "
          f"{growth:+.2f}s from first to last")
    print(f"prompt grew      {loop[0]['prompt']} to {loop[-1]['prompt']} tokens "
          f"(+{loop[-1]['prompt'] - loop[0]['prompt']})")
    if not args.pin_work:
        print(f"work per turn    {sorted(outs)} tokens: NOT pinned, so ms/tok ranks "
              f"answer lengths as much as engines")
    # A prefix cache that works keeps TTFT flat while the prompt grows. One that
    # is bypassed makes TTFT track the prompt, which is the failure this measures.
    if loop[-1]["prompt"] > loop[0]["prompt"]:
        per_1k = 1000.0 * growth / (loop[-1]["prompt"] - loop[0]["prompt"])
        print(f"TTFT per 1k added prompt tokens  {per_1k:+.0f} ms")
        print("A prefix cache that is being reused keeps this near zero: the added "
              "tokens are\nthe only ones prefilled. A number that tracks the full "
              "prompt means the cache is\nnot being hit, which is what to check before "
              "blaming decode.")


if __name__ == "__main__":
    main()
