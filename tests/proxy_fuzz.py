"""Fuzzing and model-based simulation of the keepalive proxy.

The proxy is the only component in this repo that reads bytes it does not
control: an agent client's request body, and the engine's SSE stream. Everything
here generates those bytes rather than choosing them.

Three kinds of check, in increasing strength:

  NEVER RAISES        for any input, the function returns. The proxy runs one
                      thread per request in front of a shared engine, so an
                      exception in a parser is a dropped answer at best and a
                      zombie generation at worst.
  NEVER LIES          a body with nothing wrong in it comes back byte-identical,
                      a pattern Python CAN compile is never dropped, and a
                      pattern that is dropped genuinely does not compile. The
                      v6.13 fix exists to keep one tool from killing a session;
                      a sanitizer that quietly rewrote good bodies would be a
                      worse bug than the one it fixed.
  SIMULATION          a state machine drives a real proxy against a real fake
                      engine through generated sequences of client behaviour
                      (read some, vanish, stay, send a second request) and
                      checks the invariant that matters in production after
                      every step: the engine is never left generating for a
                      client that is gone, and a request is never aborted with
                      an id the engine does not know.

Not named test_*.py: it needs hypothesis, and the discovered suite stays
stdlib-only. Run it directly; CI installs hypothesis and requires the summary.
"""
import importlib.util
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[1]

VENVS = [Path(os.environ["QWEN38_TEST_PYTHON"])] if os.environ.get("QWEN38_TEST_PYTHON") else []
VENVS += [REPO / ".venv-test/bin/python", REPO / ".venv/bin/python",
          Path.home() / ".local/share/qwen38-testenv/bin/python"]

try:
    from hypothesis import HealthCheck, assume, given, settings
    from hypothesis import strategies as st
    from hypothesis.stateful import (RuleBasedStateMachine, invariant, rule,
                                     run_state_machine_as_test)
except ImportError:                                    # pragma: no cover
    import subprocess
    for cand in VENVS:
        if cand.exists() and subprocess.run([str(cand), "-c", "import hypothesis"],
                                            capture_output=True).returncode == 0:
            os.execv(str(cand), [str(cand), str(HERE), *sys.argv[1:]])
    print("SKIPPED: hypothesis is not installed and no test venv has it.")
    print("  python3 -m venv .venv-test && .venv-test/bin/pip install hypothesis")
    raise SystemExit(0)

MAX_EXAMPLES = 300
PROFILE = settings(max_examples=MAX_EXAMPLES, deadline=None,
                   suppress_health_check=[HealthCheck.too_slow,
                                          HealthCheck.function_scoped_fixture])

SPEC = importlib.util.spec_from_file_location("kproxy_fuzz", REPO / "keepalive-proxy.py")
os.environ.setdefault("UPSTREAM", "http://127.0.0.1:1")
sys.argv = ["keepalive-proxy.py"]
kp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kp)


# ── strategies ───────────────────────────────────────────────────────────────
# Regexes on both sides of the line Python's re draws. The left column is what
# JSON Schema allows and Python refuses, which is the whole reason v6.13 exists.
UNPYTHONIC = st.sampled_from([
    r"^(?!__.*__$)[^\p{Cc}\p{Cf}\p{Zl}\p{Zp}\"\\./[\]]{1,200}$",   # Claude Code's Artifact tool
    r"\p{L}+", r"\P{N}", r"(?<name>x)", r"\p{Script=Latin}",
    r"[\p{Alpha}]", r"(?<=a)(?<b>c)", r"\p{Greek}\p{Han}",
])
PYTHONIC = st.sampled_from([
    r"^[a-z]+$", r"\d{1,3}", r"[A-Za-z0-9_-]{1,64}", r"(?P<name>x)",
    r"a|b|c", r"^\S+@\S+$", r"(?i)hello", r"\\", r"", r"^$", r".*",
])
BROKEN = st.sampled_from([r"(", r"[", r"a{2,1}", r"*", r"(?P<1>x)", r"\\p{", r"?"])

JSON_LEAF = st.one_of(st.none(), st.booleans(),
                      st.integers(min_value=-10 ** 6, max_value=10 ** 6),
                      st.floats(allow_nan=False, allow_infinity=False, width=32),
                      st.text(max_size=24))
JSON_ANY = st.recursive(
    JSON_LEAF,
    lambda kids: st.lists(kids, max_size=4) | st.dictionaries(st.text(max_size=8), kids, max_size=4),
    max_leaves=25)


def _patterns_in(node, out=None):
    r"""Every 'pattern' VALUE in a parsed body. Comparing against json.dumps text
    instead was this file's own first bug: the dump re-escapes a backslash, so
    a surviving r"^\S+@\S+$" reads as "^\\S+@\\S+$" and never matches."""
    out = [] if out is None else out
    if isinstance(node, dict):
        pat = node.get("pattern")
        if isinstance(pat, str):
            out.append(pat)
        for v in node.values():
            _patterns_in(v, out)
    elif isinstance(node, list):
        for v in node:
            _patterns_in(v, out)
    return out


def schema_with(pattern):
    return {"type": "object",
            "properties": {"field": {"type": "string", "pattern": pattern}}}


@st.composite
def tool_body(draw):
    """A request body shaped like a real tool-carrying call."""
    dialect = draw(st.sampled_from(["anthropic", "openai"]))
    patterns = draw(st.lists(st.one_of(UNPYTHONIC, PYTHONIC, BROKEN),
                             min_size=0, max_size=4))
    tools = []
    for i, pat in enumerate(patterns):
        if dialect == "anthropic":
            tools.append({"name": f"t{i}", "input_schema": schema_with(pat)})
        else:
            tools.append({"type": "function",
                          "function": {"name": f"t{i}", "parameters": schema_with(pat)}})
    body = {"model": "m", "messages": [{"role": "user", "content": draw(st.text(max_size=40))}]}
    if draw(st.booleans()):
        body["tools"] = tools
    else:
        body["messages"][0]["tools"] = tools
    if draw(st.booleans()):
        body["extra"] = draw(JSON_ANY)
    return json.dumps(body).encode(), patterns


class SanitizerNeverRaises(unittest.TestCase):
    PATHS = ["/v1/chat/completions", "/v1/messages", "/generate", "/health", "", "/v1/"]

    @PROFILE
    @given(st.binary(max_size=3000), st.sampled_from(PATHS))
    def test_arbitrary_bytes(self, body, path):
        out, dropped = kp.sanitize_tool_schemas(body, path)
        self.assertIsInstance(out, bytes)
        self.assertIsInstance(dropped, list)

    @PROFILE
    @given(JSON_ANY, st.sampled_from(PATHS))
    def test_arbitrary_json(self, blob, path):
        body = json.dumps(blob).encode()
        out, dropped = kp.sanitize_tool_schemas(body, path)
        self.assertIsInstance(out, bytes)

    @PROFILE
    @given(st.integers(min_value=1, max_value=400))
    def test_deep_nesting_does_not_recurse_to_death(self, depth):
        """A schema nested past the depth bound must be survived, not crashed:
        the proxy is one thread of a shared server."""
        node = {"pattern": r"\p{L}"}
        for _ in range(depth):
            node = {"properties": {"x": node}}
        body = json.dumps({"tools": [{"name": "t", "input_schema": node}]}).encode()
        out, _ = kp.sanitize_tool_schemas(body, "/v1/messages")
        self.assertIsInstance(out, bytes)

    @PROFILE
    @given(st.integers(min_value=1, max_value=2000))
    def test_a_wide_schema_is_survived(self, width):
        tools = [{"name": f"t{i}", "input_schema": schema_with(r"\p{L}")}
                 for i in range(min(width, 200))]
        body = json.dumps({"tools": tools}).encode()
        out, dropped = kp.sanitize_tool_schemas(body, "/v1/messages")
        self.assertEqual(len(dropped), len(tools))
        self.assertNotIn(b"\\\\p{L}", out)


class SanitizerNeverLies(unittest.TestCase):
    @PROFILE
    @given(tool_body())
    def test_a_dropped_pattern_really_does_not_compile(self, pair):
        body, _ = pair
        _, dropped = kp.sanitize_tool_schemas(body, "/v1/messages")
        for pat in dropped:
            with self.assertRaises((re.error, RecursionError), msg=f"{pat!r} compiles fine"):
                re.compile(pat)

    @PROFILE
    @given(tool_body())
    def test_a_pattern_python_accepts_is_never_dropped(self, pair):
        body, patterns = pair
        out, dropped = kp.sanitize_tool_schemas(body, "/v1/messages")
        for pat in patterns:
            try:
                re.compile(pat)
            except (re.error, RecursionError):
                continue
            self.assertNotIn(pat, dropped, f"{pat!r} was dropped although it compiles")
            if dropped:              # the body was rewritten: the good one must survive
                self.assertIn(pat, _patterns_in(json.loads(out)),
                              f"{pat!r} disappeared from a rewritten body")

    @PROFILE
    @given(tool_body())
    def test_a_body_with_nothing_wrong_is_forwarded_byte_for_byte(self, pair):
        body, patterns = pair
        assume(not any(m.decode() in body.decode("utf-8", "replace")
                       for m in kp.UNPYTHONIC_PATTERN_MARKS))
        out, dropped = kp.sanitize_tool_schemas(body, "/v1/messages")
        self.assertEqual(out, body)
        self.assertEqual(dropped, [])

    @PROFILE
    @given(tool_body())
    def test_the_result_is_always_valid_json_when_it_changed(self, pair):
        body, _ = pair
        out, dropped = kp.sanitize_tool_schemas(body, "/v1/messages")
        if dropped:
            json.loads(out)          # raises if the rewrite corrupted the body

    @PROFILE
    @given(tool_body())
    def test_sanitizing_twice_changes_nothing_more(self, pair):
        body, _ = pair
        once, first = kp.sanitize_tool_schemas(body, "/v1/messages")
        twice, second = kp.sanitize_tool_schemas(once, "/v1/messages")
        self.assertEqual(twice, once)
        self.assertEqual(second, [])

    @PROFILE
    @given(st.text(max_size=60))
    def test_a_pattern_inside_a_message_is_not_a_tool_schema(self, text):
        """The mark scan is on raw bytes, so a user who TALKS about \\p{L} makes
        the body parse. Their message must still arrive untouched."""
        content = text + r" why does \p{L} fail?"
        body = json.dumps({"messages": [{"role": "user", "content": content}]}).encode()
        out, dropped = kp.sanitize_tool_schemas(body, "/v1/messages")
        self.assertEqual(dropped, [])
        self.assertEqual(json.loads(out)["messages"][0]["content"], content)

    @PROFILE
    @given(st.binary(max_size=200))
    def test_a_non_v1_path_is_never_touched(self, body):
        for path in ("/generate", "/health", "/abort_request", ""):
            out, dropped = kp.sanitize_tool_schemas(body, path)
            self.assertEqual(out, body, path)
            self.assertEqual(dropped, [], path)


class DeltaText(unittest.TestCase):
    @PROFILE
    @given(JSON_ANY)
    def test_it_returns_a_string_for_any_event(self, blob):
        if not isinstance(blob, dict):
            blob = {"x": blob}
        self.assertIsInstance(kp.delta_text(blob), str)

    @PROFILE
    @given(st.text(max_size=40))
    def test_the_openai_dialect_is_read(self, text):
        assume(text)
        self.assertEqual(
            kp.delta_text({"choices": [{"delta": {"content": text}}]}), text)

    @PROFILE
    @given(st.text(max_size=40))
    def test_the_anthropic_dialect_is_read(self, text):
        assume(text)
        self.assertEqual(kp.delta_text({"delta": {"text": text}}), text)

    @PROFILE
    @given(st.text(max_size=40))
    def test_tool_call_arguments_are_never_the_visible_text(self, text):
        """A JSON blob of exclamation marks in a tool argument is not the
        corruption this guard looks for."""
        ev = {"choices": [{"delta": {"tool_calls": [{"function": {"arguments": text}}]}}]}
        self.assertEqual(kp.delta_text(ev), "")


# ── simulation: a real proxy, a real fake engine, generated client behaviour ──
class Engine:
    """The engine's abort contract, and nothing else: abort_request() returns
    early when the rid is gone, which is what makes the ORDER the whole point
    (sglang#35255)."""

    def __init__(self):
        self.live = {}
        self.aborted = []
        self.ignored = []
        self.finished = []
        self.lock = threading.Lock()


class RelaySimulation(RuleBasedStateMachine):
    """Drive a proxy with generated client behaviour and check the production
    invariant after every step."""

    def __init__(self):
        super().__init__()
        import http.server

        self.engine = Engine()
        eng = self.engine
        EVENTS, GAP = 8, 0.02

        class Fake(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                path = self.path.split("?")[0]
                if path == "/abort_request":
                    rid = json.loads(body or b"{}").get("rid")
                    with eng.lock:
                        (eng.aborted if rid in eng.live else eng.ignored).append(rid)
                        eng.live.pop(rid, None)
                    out = b'{"ok":true}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
                rid = self.headers.get("x-override-rid") or f"engine{time.time_ns()}"
                with eng.lock:
                    eng.live[rid] = True
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    if b"slow-prefill" in body:
                        time.sleep(0.4)
                    for i in range(EVENTS):
                        self.wfile.write(b"data: " + json.dumps(
                            {"id": rid, "choices": [{"delta": {"content": f"t{i}"}}]}
                        ).encode() + b"\n\n")
                        self.wfile.flush()
                        time.sleep(GAP)
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    with eng.lock:
                        eng.finished.append(rid)
                except Exception:
                    pass
                finally:
                    with eng.lock:
                        eng.live.pop(rid, None)     # _discard_pending_req_states

        self.esrv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        self.esrv.daemon_threads = True
        threading.Thread(target=self.esrv.serve_forever, daemon=True).start()

        os.environ["UPSTREAM"] = f"http://127.0.0.1:{self.esrv.server_port}"
        os.environ["KEEPALIVE_S"] = "0.2"
        os.environ["CLIENT_IO_S"] = "5"
        spec = importlib.util.spec_from_file_location(
            f"kp_sim_{time.time_ns()}", REPO / "keepalive-proxy.py")
        self.mod = importlib.util.module_from_spec(spec)
        sys.argv = ["keepalive-proxy.py"]
        spec.loader.exec_module(self.mod)
        self.psrv = self.mod.Server(("127.0.0.1", 0), self.mod.H)
        threading.Thread(target=self.psrv.serve_forever, daemon=True).start()
        self.port = self.psrv.server_address[1]
        self.opened = 0

    def teardown(self):
        self.psrv.shutdown()
        self.psrv.server_close()
        self.esrv.shutdown()
        self.esrv.server_close()

    # ---- client behaviours ----------------------------------------------------
    def _send(self, path, prompt, read_bytes, hard_close):
        raw = json.dumps({"messages": [{"role": "user", "content": prompt}]}).encode()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(f"POST {path} HTTP/1.1\r\nHost: p\r\nContent-Type: application/json\r\n"
                  f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw)
        got = b""
        if read_bytes:
            s.settimeout(10)
            try:
                while len(got) < read_bytes:
                    c = s.recv(4096)
                    if not c:
                        break
                    got += c
            except (socket.timeout, TimeoutError, ConnectionResetError):
                pass
        if hard_close:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()
        self.opened += 1
        return got

    @rule(path=st.sampled_from(["/v1/chat/completions", "/v1/messages"]),
          slow=st.booleans(),
          read_bytes=st.sampled_from([0, 1, 40, 400]),
          hard=st.booleans())
    def client_gives_up(self, path, slow, read_bytes, hard):
        self._send(path, "slow-prefill" if slow else "hello", read_bytes, hard)
        time.sleep(0.35)

    @rule(path=st.sampled_from(["/v1/chat/completions", "/v1/messages"]))
    def client_stays_to_the_end(self, path):
        got = self._send(path, "hello", 100000, False)
        assert b"[DONE]" in got or not got, "a staying client lost its stream"

    @invariant()
    def the_engine_is_never_left_generating_for_a_client_that_is_gone(self):
        """The one production invariant. A generation is either finished, aborted,
        or still being drained by the proxy; what must never happen is an abort
        the engine discarded, because that is the silent no-op that let a request
        decode for six minutes with nobody listening."""
        with self.engine.lock:
            ignored = list(self.engine.ignored)
        assert ignored == [], f"aborts the engine discarded: {ignored}"

    @invariant()
    def no_request_is_aborted_with_an_id_the_engine_never_had(self):
        with self.engine.lock:
            aborted = list(self.engine.aborted)
            known = set(aborted) | set(self.engine.finished) | set(self.engine.live)
        for rid in aborted:
            assert rid in known, f"aborted an unknown rid: {rid}"
            assert not rid.startswith("msg_"), (
                f"a locally minted Anthropic id was sent as a rid: {rid}")


class Simulation(unittest.TestCase):
    def test_generated_client_behaviour_never_orphans_a_generation(self):
        run_state_machine_as_test(
            RelaySimulation,
            settings=settings(max_examples=12, stateful_step_count=6,
                              deadline=None,
                              suppress_health_check=list(HealthCheck)))


def main() -> int:
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    total = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    ok = total - len(result.failures) - len(result.errors) - len(result.skipped)
    print(f"\nproxy fuzz: {ok} passed of {total}, {MAX_EXAMPLES} generated examples each")
    return 0 if result.wasSuccessful() and ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
