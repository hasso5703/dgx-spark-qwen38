#!/usr/bin/env python3
"""Tool-calling probe: can an agent client execute what this checkpoint emits?

Speed probes and knowledge evals both miss the thing agent work actually runs
on. An agent turn is only useful if the model emits a call with the right name
and arguments that parse, and if it stays quiet when no tool applies. That is
also the first place a quantized or re-exported checkpoint degrades, because a
tool call is a long stretch of exact tokens: a JSON object, a quoted path, an
escaped newline, an enum spelled the way the schema spells it.

Fifteen cases through the repo's own serving surface (chat template, reasoning
parser, `--tool-call-parser qwen3_coder`), scored in four buckets that fail for
different reasons:

  called      a call was emitted where one was required
  well formed the parser produced tool_calls, and the arguments parse as JSON
  arguments   the values are the ones the request names
  restraint   no call at all on the three questions no tool answers

"well formed" separates two failures an agent feels very differently. A model
that emits nothing is inert; a model that emits a call the parser cannot turn
into `tool_calls` looks like a plain answer containing JSON, which clients paste
back to the user. When that happens this prints the raw content, since the
difference is the whole point of the bucket.

  ./tools-check.py                  # through the keepalive proxy, port 30001
  ./tools-check.py --port 30000     # straight at the engine
  ./tools-check.py --think          # leave reasoning on (default off)
  ./tools-check.py --min 14         # exit non-zero below that many correct cases

Reads the API key from ~/.config/qwen38/api-key. Exits 3 if the lane refuses or
cannot be reached, because a refused case is not a measurement.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import urllib.error
import urllib.request

CONFIG = os.path.expanduser("~/.config/qwen38")

WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
                "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
            },
            "required": ["city", "unit"],
        },
    },
}
CALC = {
    "type": "function",
    "function": {
        "name": "calculator",
        "description": "Arithmetic on two numbers.",
        "parameters": {
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
                "op": {"type": "string", "enum": ["add", "subtract", "multiply", "divide"]},
            },
            "required": ["a", "b", "op"],
        },
    },
}
GREP = {
    "type": "function",
    "function": {
        "name": "search_code",
        "description": "Search the repository for a pattern.",
        "parameters": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
            "required": ["pattern", "path"],
        },
    },
}
WRITE = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write exact bytes to a path.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
}
SQL = {
    "type": "function",
    "function": {
        "name": "run_sql",
        "description": "Run one SQL query.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}
EVENT = {
    "type": "function",
    "function": {
        "name": "create_event",
        "description": "Create a calendar event.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "attendees": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "date", "attendees"],
        },
    },
}
MODE = {
    "type": "function",
    "function": {
        "name": "set_mode",
        "description": "Set the indexer's effort level.",
        "parameters": {
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ["fast", "balanced", "thorough"]}},
            "required": ["mode"],
        },
    },
}
FX = {
    "type": "function",
    "function": {
        "name": "convert_currency",
        "description": "Convert an amount between two ISO 4217 currencies.",
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "source": {"type": "string", "description": "ISO 4217 code"},
                "target": {"type": "string", "description": "ISO 4217 code"},
            },
            "required": ["amount", "source", "target"],
        },
    },
}
ROWS = {
    "type": "function",
    "function": {
        "name": "filter_rows",
        "description": "Filter a column by a numeric range.",
        "parameters": {
            "type": "object",
            "properties": {
                "column": {"type": "string"},
                "criteria": {
                    "type": "object",
                    "properties": {"min": {"type": "number"}, "max": {"type": "number"}},
                    "required": ["min", "max"],
                },
            },
            "required": ["column", "criteria"],
        },
    },
}
LOGS = {
    "type": "function",
    "function": {
        "name": "get_logs",
        "description": "Recent logs of a service.",
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "since": {"type": "string", "description": "optional ISO timestamp"},
            },
            "required": ["service"],
        },
    },
}


def num(value):
    """The schema says number; a model may still send '1723'."""
    try:
        return float(str(value).replace(",", "").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def text(value) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def sql_text(value) -> str:
    """text(), with the two ways a quote survives being put inside a string."""
    return text(value).replace("''", "'").replace("\\'", "'")


CASES = [
    {"name": "two required arguments, one of them an enum",
     "tools": [WEATHER], "call": "get_weather",
     "ask": "What is the weather in Reykjavik right now? I want it in celsius.",
     "check": lambda a: ([] if "reykjav" in text(a.get("city")) else [f"city={a.get('city')!r}"])
                        + ([] if text(a.get("unit")) == "celsius" else [f"unit={a.get('unit')!r}"])},
    {"name": "numbers stay numbers",
     "tools": [CALC], "call": "calculator",
     "ask": "Use the calculator to multiply 1723 by 4891.",
     "check": lambda a: ([] if sorted(filter(None, (num(a.get("a")), num(a.get("b"))))) == [1723.0, 4891.0]
                         else [f"a={a.get('a')!r} b={a.get('b')!r}"])
                        + ([] if text(a.get("op")) == "multiply" else [f"op={a.get('op')!r}"])},
    {"name": "a path and a pattern, copied not paraphrased",
     "tools": [GREP], "call": "search_code",
     "ask": "Find every call to cudaMalloc under /src/kernels.",
     "check": lambda a: ([] if "cudamalloc" in text(a.get("pattern")) else [f"pattern={a.get('pattern')!r}"])
                        + ([] if "/src/kernels" in text(a.get("path")) else [f"path={a.get('path')!r}"])},
    {"name": "a newline and a quote inside a JSON string",
     "tools": [WRITE], "call": "write_file",
     "ask": ("Write the file /tmp/note.txt. Its content must be exactly these two lines:\n"
             "line one\n"
             'she said "no"'),
     "check": lambda a: ([] if text(a.get("path")) == "/tmp/note.txt" else [f"path={a.get('path')!r}"])
                        + ([] if "\n" in (a.get("content") or "") else ["content lost the newline"])
                        + ([] if '"no"' in (a.get("content") or "") else ["content lost the quotes"])},
    {"name": "an apostrophe inside SQL inside JSON",
     "tools": [SQL], "call": "run_sql",
     "ask": "Query the users table for every row whose name is O'Brien.",
     # A correct answer may escape the apostrophe the way SQL escapes it, by
     # doubling it, or the way a JSON writer might, with a backslash. Both are
     # the name; only a query that lost it is wrong. This checker demanded the
     # bare form and failed a model that wrote 'O''Brien', which is better SQL
     # than what was asked for (2026-09-18, first run against a real engine).
     "check": lambda a: ([] if "o'brien" in sql_text(a.get("query")) else [f"query={a.get('query')!r}"])
                        + ([] if "users" in text(a.get("query")) else ["query does not name the table"])},
    {"name": "a date normalized to the schema's format, and an array",
     "tools": [EVENT], "call": "create_event",
     "ask": ("Schedule a meeting titled Design review on March 3rd 2027, "
             "with alice@example.com and bob@example.com."),
     "check": lambda a: ([] if text(a.get("date")) == "2027-03-03" else [f"date={a.get('date')!r}"])
                        + ([] if isinstance(a.get("attendees"), list) and len(a["attendees"]) == 2
                           else [f"attendees={a.get('attendees')!r}"])
                        + ([] if "design review" in text(a.get("title")) else [f"title={a.get('title')!r}"])},
    {"name": "an enum value the request does not spell",
     "tools": [MODE], "call": "set_mode",
     "ask": "Put the indexer in its most careful and exhaustive setting.",
     "check": lambda a: [] if text(a.get("mode")) == "thorough" else [f"mode={a.get('mode')!r}"]},
    {"name": "currency names mapped to ISO codes",
     "tools": [FX], "call": "convert_currency",
     "ask": "How much is 250 euros in Japanese yen?",
     "check": lambda a: ([] if num(a.get("amount")) == 250.0 else [f"amount={a.get('amount')!r}"])
                        + ([] if text(a.get("source")) == "eur" else [f"source={a.get('source')!r}"])
                        + ([] if text(a.get("target")) == "jpy" else [f"target={a.get('target')!r}"])},
    {"name": "a nested object argument",
     "tools": [ROWS], "call": "filter_rows",
     "ask": "Filter the latency_ms column to rows between 20 and 80.",
     "check": lambda a: ([] if "latency" in text(a.get("column")) else [f"column={a.get('column')!r}"])
                        + ([] if isinstance(a.get("criteria"), dict)
                           and num(a["criteria"].get("min")) == 20.0
                           and num(a["criteria"].get("max")) == 80.0
                           else [f"criteria={a.get('criteria')!r}"])},
    {"name": "the right tool out of two",
     "tools": [WEATHER, CALC], "call": "calculator",
     "ask": "What is 4891 divided by 7? Use a tool.",
     "check": lambda a: ([] if num(a.get("a")) == 4891.0 else [f"a={a.get('a')!r}"])
                        + ([] if num(a.get("b")) == 7.0 else [f"b={a.get('b')!r}"])
                        + ([] if text(a.get("op")) == "divide" else [f"op={a.get('op')!r}"])},
    {"name": "an optional argument left out",
     "tools": [LOGS], "call": "get_logs",
     "ask": "Show me the logs for the proxy service.",
     "check": lambda a: ([] if "proxy" in text(a.get("service")) else [f"service={a.get('service')!r}"])
                        + ([] if not a.get("since") else [f"invented since={a.get('since')!r}"])},
    {"name": "two calls in one turn",
     "tools": [WEATHER], "call": "get_weather", "expect_calls": 2,
     "ask": "Give me the weather in Oslo and in Bergen, both in celsius.",
     "check": lambda a: [] if text(a.get("unit")) == "celsius" else [f"unit={a.get('unit')!r}"],
     # every call, not the first one only: Oslo asked twice passed (found in review, 2026-09-24)
     "check_all": lambda args: ([] if sorted(("oslo" in text(a.get("city"))) + 2 * ("bergen" in text(a.get("city")))
                                            for a in args) == [1, 2]
                                else [f"cities={[a.get('city') for a in args]!r}, expected Oslo and Bergen"])
                               + [f"unit={a.get('unit')!r}" for a in args if text(a.get("unit")) != "celsius"]},
    {"name": "restraint: a question no tool answers",
     "tools": [WEATHER, CALC], "expect_no_call": True,
     "ask": "In one sentence, what is a KV cache?"},
    {"name": "restraint: knowledge a weather tool cannot fetch",
     "tools": [WEATHER], "expect_no_call": True,
     "ask": "What is the capital of Iceland? Answer in one word."},
    {"name": "restraint: small talk",
     "tools": [WEATHER, CALC, GREP], "expect_no_call": True,
     "ask": "Say hello in one short sentence."},
]


def die(msg: str, code: int = 2):
    print(f"tools-check: {msg}", file=sys.stderr)
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
        die(f"the lane refused a case: HTTP {exc.code} {detail}", 3)
    except urllib.error.URLError as exc:
        die(f"cannot reach {url}: {exc.reason}. Is the lane up?", 3)
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        # a read that timed out, a connection the engine dropped: the case was not
        # answered, and that was a traceback and exit 1 (found in review, 2026-09-24)
        die(f"the lane stopped answering a case ({type(exc).__name__}: {exc}). Is it up?", 3)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=30001,
                    help="30001 is the keepalive proxy, which is what agent clients use")
    ap.add_argument("--model", default=None, help="default: whatever /v1/models serves")
    ap.add_argument("--max-tokens", type=int, default=600)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--think", action="store_true", help="leave reasoning on (default off)")
    ap.add_argument("--min", type=int, default=None,
                    help="exit 1 below this many fully correct cases (out of %d)" % len(CASES))
    args = ap.parse_args()

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
            die(f"cannot read {base}/v1/models ({exc}); pass --model", 3)

    print(f"tool-calling probe: {model} on {base}, reasoning "
          f"{'on' if args.think else 'off'}, {len(CASES)} cases")
    called = wellformed = correct = restrained = 0
    wanted_call = sum(1 for c in CASES if not c.get("expect_no_call"))
    wanted_quiet = len(CASES) - wanted_call
    fully_correct = 0

    for case in CASES:
        body = {"model": model, "max_tokens": args.max_tokens, "temperature": 0,
                "messages": [{"role": "user", "content": case["ask"]}],
                "tools": case["tools"], "tool_choice": "auto",
                "chat_template_kwargs": {"enable_thinking": args.think}}
        msg = (post(f"{base}/v1/chat/completions", key, body, args.timeout)
               .get("choices", [{}])[0].get("message", {}) or {})
        calls = msg.get("tool_calls") or []
        content = msg.get("content") or ""

        if case.get("expect_no_call"):
            if calls:
                print(f"  FAIL  {case['name']}: called {calls[0].get('function', {}).get('name')!r} anyway")
            else:
                restrained += 1
                fully_correct += 1
                print(f"  ok    {case['name']}")
            continue

        if not calls:
            # An unparsed call reads as a plain answer to a client. Name it.
            leaked = any(tag in content for tag in ("<tool_call", "\"name\":", "'name':", "function="))
            why = "emitted a call the parser did not parse" if leaked else "emitted no call"
            print(f"  FAIL  {case['name']}: {why}")
            print(f"        content: {content.strip()[:200]!r}")
            continue
        called += 1

        fn = calls[0].get("function", {}) or {}
        name = fn.get("name")
        raw = fn.get("arguments")
        try:
            # an empty string or no arguments at all is not a call anyone can run: every
            # tool here has required arguments, and "" parsed as {} (found in review)
            argd = raw if isinstance(raw, dict) else json.loads(raw)
            parsed = isinstance(argd, dict)
        except (TypeError, ValueError):
            argd, parsed = {}, False
        if not parsed:
            print(f"  FAIL  {case['name']}: arguments are not JSON: {str(raw)[:160]!r}")
            continue
        wellformed += 1

        problems = [] if name == case["call"] else [f"called {name!r}, not {case['call']!r}"]
        try:
            problems += case["check"](argd)
        except Exception as exc:  # noqa: BLE001
            # A checker reads the values the schema promised. A model that sends
            # an object where a string was asked for is a failure of this case,
            # not of this probe, and the other fourteen still have to run.
            problems.append(f"arguments have an unexpected shape "
                            f"({type(exc).__name__}: {exc})")
        if case.get("expect_calls") and len(calls) < case["expect_calls"]:
            problems.append(f"{len(calls)} call(s), expected {case['expect_calls']}")
        if case.get("check_all"):
            every = []
            for other in calls:
                f = other.get("function", {}) or {}
                if f.get("name") != case["call"]:
                    problems.append(f"a call to {f.get('name')!r}")
                try:
                    every.append(json.loads(f.get("arguments")) if not isinstance(f.get("arguments"), dict)
                                 else f["arguments"])
                except (TypeError, ValueError):
                    problems.append(f"arguments are not JSON: {str(f.get('arguments'))[:80]!r}")
            try:
                problems += case["check_all"]([a for a in every if isinstance(a, dict)])
            except Exception as exc:  # noqa: BLE001
                problems.append(f"arguments have an unexpected shape ({type(exc).__name__}: {exc})")
        if problems:
            print(f"  FAIL  {case['name']}: {'; '.join(problems)}")
        else:
            correct += 1
            fully_correct += 1
            print(f"  ok    {case['name']}")

    print()
    print(f"called      {called}/{wanted_call}")
    print(f"well formed {wellformed}/{wanted_call}")
    print(f"arguments   {correct}/{wanted_call}")
    print(f"restraint   {restrained}/{wanted_quiet}")
    print(f"TOOLS SUMMARY: {fully_correct}/{len(CASES)} cases fully correct")
    if args.min is not None and fully_correct < args.min:
        die(f"{fully_correct} correct, below the {args.min} asked for", 1)


if __name__ == "__main__":
    main()
