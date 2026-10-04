#!/usr/bin/env python3
"""Does this box serve? A short battery of real requests, the way clients send them.

Usage: selftest.py             through the keepalive proxy on :30001, as clients reach it
       selftest.py --report    the same, as Markdown to paste into an issue
       selftest.py --force     run while the engine is busy (it is refused otherwise)
       selftest.py --port N    another proxy port

Seven checks: the model list, one answer, one streamed answer
that ends with [DONE], one tool call, a passphrase found in about 8,000 tokens of filler, four
questions at once, and one answer in the Anthropic dialect (the one Claude Code speaks). Each
says what it got. The deeper instruments are the repo's own: tools-check.py (15 tool cases),
needle.sh (retrieval up to the window), conc-check.py (40 and 80 exact answers), bench.sh.

It refuses to run while the engine says it is serving (its /metrics), because a test
then slows real work down and the work slows the test: --force runs it anyway, and metrics
that cannot answer say nothing, so the test runs. It reads the
API key from ~/.config/qwen38/api-key and never prints it, writes nothing, changes nothing.
Exit status: 0 when every check passed, 1 when one failed, 3 when it refused to run.
"""
import concurrent.futures
import http.client
import json
import os
import random
import re
import sys
import time

KEY_FILE = os.path.expanduser("~/.config/qwen38/api-key")
ENGINE_PORT = int(os.environ.get("PORT", "30000"))
TIMEOUT_S = 300
QUESTION = "What is 17 + 25? Answer with the number only."
WORDS = ("amber", "basalt", "cobalt", "delta", "ember", "fennel", "garnet", "harbor", "indigo",
         "juniper", "kestrel", "lagoon", "meadow", "nectar", "orchid", "pewter", "quarry", "russet")


class Client:
    """HTTP to the proxy and to the engine's metrics; injectable in the tests."""

    def __init__(self, port, key, host="127.0.0.1", engine_port=ENGINE_PORT):
        self.host, self.port, self.key, self.engine_port = host, port, key, engine_port

    def _conn(self, port=None):
        return http.client.HTTPConnection(self.host, port or self.port, timeout=TIMEOUT_S)

    def _headers(self):
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.key}",
                "anthropic-version": "2023-06-01"}

    def get(self, path, port=None):
        c = self._conn(port)
        try:
            c.request("GET", path, headers=self._headers())
            r = c.getresponse()
            return r.status, r.read().decode("utf-8", "replace")
        finally:
            c.close()

    def post(self, path, body):
        c = self._conn()
        try:
            c.request("POST", path, body=json.dumps(body).encode(), headers=self._headers())
            r = c.getresponse()
            return r.status, r.read().decode("utf-8", "replace")
        finally:
            c.close()

    def busy(self):
        """Requests the engine runs or queues, from its own /metrics; None when it cannot say."""
        try:
            status, text = self.get("/metrics", port=self.engine_port)
        except OSError:
            return None
        if status != 200:
            return None
        n = 0.0
        found = False
        for ln in text.splitlines():
            m = re.match(r"^sglang:num_(running|queue)_reqs(\{[^}]*\})?\s+(\S+)$", ln)
            if m:
                n += float(m.group(3))
                found = True
        return int(n) if found else None


def _chat(content, **extra):
    return {"model": "m", "max_tokens": 400, "temperature": 0,
            "messages": [{"role": "user", "content": content}], **extra}


def _answer(text):
    try:
        msg = json.loads(text)["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None, None
    return (msg.get("content") or ""), msg


def check_models(cl):
    status, text = cl.get("/v1/models")
    if status != 200:
        return False, f"HTTP {status}: {text[:120]}"
    try:
        ids = [m["id"] for m in json.loads(text)["data"]]
    except (ValueError, KeyError, TypeError):
        return False, f"not a model list: {text[:120]}"
    return bool(ids), ", ".join(ids) or "an empty list"


def check_answer(cl):
    status, text = cl.post("/v1/chat/completions", _chat(QUESTION))
    content, _ = _answer(text)
    if status != 200 or content is None:
        return False, f"HTTP {status}: {text[:160]}"
    return "42" in content, f"answered {content.strip()[:60]!r}"


def check_stream(cl):
    status, text = cl.post("/v1/chat/completions", _chat(QUESTION, stream=True))
    if status != 200:
        return False, f"HTTP {status}: {text[:160]}"
    content, finish, done = "", None, False
    for ev in text.split("\n\n"):
        ev = ev.strip()
        if ev == "data: [DONE]":
            done = True
            continue
        if not ev.startswith("data: "):
            continue
        try:
            j = json.loads(ev[6:])
        except ValueError:
            continue
        if j.get("error"):
            return False, f"error event: {str(j['error'])[:160]}"
        for ch in j.get("choices") or []:
            content += (ch.get("delta") or {}).get("content") or ""
            finish = ch.get("finish_reason") or finish
    ok = done and "42" in content and finish == "stop"
    return ok, f"streamed {content.strip()[:60]!r}, finish {finish}, [DONE] {'seen' if done else 'missing'}"


TOOL = {"type": "function", "function": {
    "name": "get_weather", "description": "The current weather in a city.",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}


def check_tool(cl):
    status, text = cl.post("/v1/chat/completions", _chat(
        "What is the weather in Paris right now? Use the tool.", tools=[TOOL]))
    _, msg = _answer(text)
    if status != 200 or msg is None:
        return False, f"HTTP {status}: {text[:160]}"
    calls = msg.get("tool_calls") or []
    if not calls:
        return False, f"no tool call; content {(msg.get('content') or '')[:100]!r}"
    fn = calls[0].get("function") or {}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except ValueError:
        return False, f"arguments that are not JSON: {fn.get('arguments')!r:.100}"
    ok = fn.get("name") == "get_weather" and "paris" in str(args.get("city", "")).lower()
    return ok, f"{fn.get('name')}({json.dumps(args)[:60]})"


def check_long_context(cl, words=2200, seed=None):
    rnd = random.Random(seed if seed is not None else time.time_ns())
    phrase = "-".join(rnd.choice(WORDS) for _ in range(3)) + f"-{rnd.randint(1000, 9999)}"
    filler = [f"{rnd.choice(WORDS)}{rnd.randint(0, 99)}" for _ in range(words)]
    filler.insert(len(filler) // 2, f"The passphrase is {phrase}.")
    prompt = " ".join(filler) + "\n\nWhat is the passphrase given above? Reply with the passphrase only."
    status, text = cl.post("/v1/chat/completions", _chat(prompt))
    content, _ = _answer(text)
    if status != 200 or content is None:
        return False, f"HTTP {status}: {text[:160]}"
    try:
        usage = json.loads(text).get("usage") or {}
    except ValueError:
        usage = {}
    tokens = usage.get("prompt_tokens")
    where = f"{tokens:,} prompt tokens" if isinstance(tokens, int) else f"{words:,} words"
    return phrase in content, f"{'found' if phrase in content else 'not found'} in {where}"


def check_concurrent(cl, n=4):
    pairs = [(11 + 7 * i, 23 + 5 * i) for i in range(n)]

    def ask(p):
        status, text = cl.post("/v1/chat/completions",
                               _chat(f"What is {p[0]} + {p[1]}? Answer with the number only."))
        content, _ = _answer(text)
        return status == 200 and content is not None and str(p[0] + p[1]) in content

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        right = sum(pool.map(ask, pairs))
    return right == n, f"{right} of {n} right"


def check_anthropic(cl):
    status, text = cl.post("/v1/messages", {"model": "m", "max_tokens": 400,
                                            "messages": [{"role": "user", "content": QUESTION}]})
    if status != 200:
        return False, f"HTTP {status}: {text[:160]}"
    try:
        blocks = json.loads(text)["content"]
    except (ValueError, KeyError, TypeError):
        return False, f"not a message: {text[:160]}"
    said = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    return "42" in said, f"answered {said.strip()[:60]!r}"


CHECKS = (("models", check_models), ("answer", check_answer), ("stream", check_stream),
          ("tool call", check_tool), ("long context", check_long_context),
          ("4 at once", check_concurrent), ("Anthropic dialect", check_anthropic))


def run_checks(cl, checks=CHECKS, clock=time.monotonic):
    results = []
    for name, fn in checks:
        t0 = clock()
        try:
            ok, detail = fn(cl)
        except (OSError, http.client.HTTPException) as e:
            ok, detail = False, f"{type(e).__name__}: {e}"[:200]
        results.append({"check": name, "ok": bool(ok), "seconds": round(clock() - t0, 1), "detail": detail})
    return results


def render(results, port, report=False):
    failed = [r for r in results if not r["ok"]]
    verdict = (f"All {len(results)} checks passed." if not failed
               else f"{len(failed)} of {len(results)} checks failed: {', '.join(r['check'] for r in failed)}.")
    if report:
        lines = ["<details><summary>selftest.py --report</summary>", "",
                 "| check | result | seconds | what it got |", "|---|---|---|---|"]
        lines += [f"| {r['check']} | {'ok' if r['ok'] else 'FAILED'} | {r['seconds']} | "
                  f"{r['detail'].replace('|', '/')} |" for r in results]
        lines += ["", verdict, "", "</details>", ""]
        return "\n".join(lines)
    out = [f"Self-test through the proxy on :{port}", ""]
    out += [f"  {'ok' if r['ok'] else 'FAIL':<5} {r['check']:<18} {r['seconds']:>6.1f} s  {r['detail']}"
            for r in results]
    return "\n".join(out + ["", verdict]) + "\n"


def main(argv=None, client=None, out=sys.stdout):
    args = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in args):
        out.write(__doc__)
        return 0
    report, force, port = "--report" in args, "--force" in args, 30001
    rest = [a for a in args if a not in ("--report", "--force")]
    if rest[:1] == ["--port"] and len(rest) == 2 and rest[1].isdigit():
        port, rest = int(rest[1]), []
    if rest:
        out.write(__doc__.split("\n\n", 1)[0] + "\n")
        return 2
    if client is None:
        try:
            key = open(KEY_FILE).read().strip()
        except OSError:
            out.write(f"No API key at {KEY_FILE}: is this box installed? (./install.sh)\n")
            return 3
        client = Client(port, key)
    busy = client.busy()
    if busy and not force:
        out.write(f"The engine is serving {busy} request(s) now. A self-test would slow them, and they "
                  f"would slow it: run it when the lane is idle, or with --force.\n")
        return 3
    results = run_checks(client)
    out.write(render(results, port, report=report))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
