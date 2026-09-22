# Client compatibility

The stack serves two dialects through one hardened entry point. This page
says which, what each client must send, and what is measured on the
reference box versus what is merely how a client is configured.

## The surface

Agent clients should connect to the **keepalive proxy** (default
`:30001`), not the engine directly: it injects the SSE keepalives that stop
client watchdogs from killing long prefills, aborts decodes when clients
disappear, caps and refuses what cannot be served with a body a client can
act on, and aborts decodes that emit the corruption marker. The engine
port (`:30000`) speaks the same APIs without that protection; the proxy is
the door this repo leaves unlocked-for-your-clients, the engine is the door
it assumes you walk through on a trusted network.

**The proxy also refuses the requests that take the engine down instead of being
refused by it.** One is a prompt past the KV pool ([sglang#36333](https://github.com/sgl-project/sglang/issues/36333)),
which is why sending a monster straight to `:30000` wedges the scheduler and sending it
through `:30001` gets a clean 400. Since v6.20 the other is an oversized logprob request:
OpenAI documents `top_logprobs` as "an integer between 0 and 20" and SGLang declares it
`Optional[int]` with no bound at all, so the number travels unexamined to
`logprobs.topk(max_k)` and, the moment it passes the vocabulary, raises `selected index k
out of range` **inside the scheduler**. One request from one client ends the engine for
everybody, and this box needs about nine minutes to boot again
([sglang#40076](https://github.com/sgl-project/sglang/issues/40076), open since
2026-09-18; the field is still unbounded in the served `v0.5.19`, checked in the image).
The proxy refuses anything above `TOP_LOGPROBS_CEILING` (1,024) on the three routes that
carry the number under three different names: `top_logprobs` on `/v1/chat/completions`,
`logprobs` on `/v1/completions`, and `top_logprobs_num` on `/generate`, the last one
element by element when a batch sends a list. It refuses rather than quietly lowering the
number, because a narrowed top-k answers a different question than the one that was
asked. The ceiling is not the vocabulary, which the engine publishes nowhere: no vocabulary
in use is smaller than 32k, OpenAI's own maximum is 20, and the System One readout asks 261
with its shipped caps (594 with the widest an operator can set), so nothing above 1,024 can
be a client asking for logprobs. `TOP_LOGPROBS_CEILING=0` turns the refusal off for an
operator who knows their build is patched; on this one it is not, so leave it alone.

Three more fields of that family were catalogued upstream in July with reproductions
([sglang#31597](https://github.com/sgl-project/sglang/issues/31597)) and are still
unbounded, because **both PRs that bounded them were closed without being merged**, which
is also why the logprob one had to be reported again in September. All three are reachable
from an ordinary chat request, and the proxy refuses them too: a `stop_token_ids` entry
past the vocabulary indexes a `scatter_add_` out of bounds whenever `min_new_tokens > 0`,
which on CUDA is a device-side assert that takes every in-flight request with it; an
`input_ids` entry does the same to the embedding (`_validate_input_ids_in_vocab` exists in
the image and has zero callers); and `n` becomes `parallel_sample_num` with no bound at
all, expanding a list before anything is scheduled, so it is a memory exhaustion rather
than a crash. `n` is held at `MAX_PARALLEL_SAMPLES` (128, which is OpenAI's own maximum).
The two id fields need the vocabulary, and since the engine publishes it nowhere the proxy
asks for it the only way it is offered: `logit_bias` is the one field SGLang does validate,
and it is refused with "logit_bias must has keys in [0, 248319]". One probe an hour, built
to be refused, so it never reaches the scheduler; it matched this checkpoint's own
`config.json` to the digit. **A negative id is refused whatever happens, and when the probe
cannot run the rest of the guard stands down rather than refuse traffic it cannot judge.**

| | path | dialect |
|---|---|---|
| chat | `POST /v1/chat/completions` | OpenAI |
| models | `GET /v1/models` | OpenAI |
| messages | `POST /v1/messages` | Anthropic-compatible (Claude Code, Claude-family SDKs) |
| health | `GET /health` | plain text; stays open even when client keys are on |

Authentication on every path except `/health`: `Authorization: Bearer
<key>`, the key in `~/.config/qwen38/api-key` (0600). The Anthropic
dialect is Bearer-only: an `x-api-key` header is not read. The engine
enforces the key; the proxy forwards it verbatim.

Served model names: `qwen3.8-27b` (27B lane, both context modes) and
`qwen3.8-flash-next` (flash lane). Send the name, not the checkpoint path.

Reasoning effort is a first-class field in both dialects (`low`, `medium`,
`xhigh`; the template maps `max`/`high` to `xhigh` and `minimal` to `low`,
see README "Reasoning-effort variants"). Long turns cost time at the
engine's speed, not yours: an `xhigh` turn can stream for minutes on a
correct answer, and several clients have wall-clock watchdogs that read
that as a hang. Prefer `medium` for interactive editing, `low` for
mechanical edits, and keep `xhigh` for the hard turn.

Oversize behavior differs by lane, deliberately: the 27B units pass
`--allow-auto-truncate` (an oversized prompt is truncated, which is often
what an agent wanted), the flash lane does not (it refuses instead), and
the flash proxy carries a 250,000-token ceiling that answers 413 with a
message naming the ceiling and the retry shape.

## Measured on the reference box

- **opencode**: the primary client; the installer writes its provider
  config (effort variants, default model follows the target). Everything
  in README applies through the proxy.
- **Claude Code (legacy support)**: works through the proxy on the
  Anthropic dialect. The env block that makes it behave:

  ```bash
  ANTHROPIC_BASE_URL=http://<host>:30001   ANTHROPIC_AUTH_TOKEN="<key>"
  CLAUDE_CODE_MAX_CONTEXT_TOKENS=262144    # or 1010000 with CONTEXT_MODE=1m
  API_TIMEOUT_MS=3600000
  CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS=1800000
  CLAUDE_STREAM_IDLE_TIMEOUT_MS=1800000
  ```

  The idle-timeout variables are the difference between "hangs" and "slow";
  they are named in issue #2 and in the failure family diagnosed in
  issue #1 (clients cap total turn duration, not idle time). Quality and
  tool calls were validated here; the client itself is not the maintained
  integration.
- **VS Code Copilot BYOK**: reports the same total-duration watchdog in
  issue #1 (VSCode Copilot gives no timeout env to widen it). The fix that
  worked for the reporter is the config shape in that issue: chat-completions
  apiType, the proxy URL, effort fields per the client's own schema; keep
  effort at `medium` or `low` while streaming turns are long.

## Standard OpenAI-compatible clients (configured here, not measured)

Anything that takes `base_url` + API key points at
`http://<host>:30001/v1` with the API key as the token, and gets the
`qwen3.8-27b` model name. Two examples because they are the ones asked for
most; the configurations are their vendors' standard OpenAI fields, and
the claims below about the engine side are the ones this repo has verified:

- **Open WebUI**: `OPENAI_API_BASE_URL=http://<host>:30001/v1`, key as
  above; model discovery via `GET /v1/models` answers through the proxy.
- **Cursor / Continue / Zed assistant**: OpenAI provider, same base and
  key. Vision requests pass the proxy's media pricing (image parts cost a
  declared token count each, see BENCHMARKS.md "What an image costs").

If you run one of these in anger and it disagrees with something here, a
box-report issue with the client's exact request body is the contribution
that moves this section.

## Optional TLS (v6.16)

For a client that will only speak TLS, or a network segment that requires
it, the proxy speaks TLS when you name the certificate, and only then. The
drop-in survives re-installs (`systemctl edit` writes a drop-in directory
that `install.sh` does not touch):

```bash
sudo systemctl edit qwen38-keepalive   # creates the override; content:
[Service]
Environment=QWEN38_TLS_CERT=/etc/ssl/qwen38/cert.pem
Environment=QWEN38_TLS_KEY=/etc/ssl/qwen38/key.pem
sudo systemctl restart qwen38-keepalive
```

Bring your own certificate: a tailnet CA cert, a LAN-trust one, whatever
your clients already trust. TLS 1.2 is the floor; a cert that cannot be
read or parsed makes the unit refuse to start rather than come up plain
(verified by `tests/test_proxy_tls.py`, including that no plaintext socket
survives beside the TLS one).

## Optional per-client identity (v6.16)

One key, one trust realm is the default, and it is fine for one operator.
To know which client sent what (and to stop sharing a single secret):

```bash
sudo mkdir -p /etc/qwen38 && sudo install -m 0600 keys.json /etc/qwen38/client-keys.json
sudo systemctl edit qwen38-keepalive
[Service]
Environment=QWEN38_CLIENT_KEYS_FILE=/etc/qwen38/client-keys.json
Environment=QWEN38_UPSTREAM_API_KEY=<the engine key from ~/.config/qwen38/api-key>
sudo systemctl restart qwen38-keepalive
```

`keys.json` is `{"<bearer-token>": "<label>", ...}`; each client sends its
own token as the Bearer value. Identity is two keys, not one, and the two
halves live in different places on purpose: the client's bearer names them
(the guard checks the map and labels the journal line), and
`QWEN38_UPSTREAM_API_KEY` admits them (the proxy sends the engine's key
upstream on relays and abort calls alike). Without the upstream key the
client's token goes through verbatim and meets the engine's own key check
there, which is correct only for an engine with no check of its own: the
proxy warns about the combination at startup. A request whose token is not
listed gets a 401 in its own dialect and never reaches the engine; the label
appears on every journal line of the request (`journalctl -u
qwen38-keepalive`), so per-client throughput and refusals become readable
without a telemetry component. `/health` stays open for monitoring. A
missing, empty or malformed keys file stops the unit at start: the wall is
present or the unit is down, never silently absent.

## Typed decisions: TypeSafe SDK and HTTP (v6.19)

`POST /v1/systemone` speaks the wire contract of TypeSafe's Jev, served by the lane behind the
proxy (README, "Typed decisions"). The official SDK works unchanged against this box:

```bash
pip install typesafe-sdk
export TYPESAFE_BASE_URL=http://127.0.0.1:30001          # or the tailnet address, or https:// with the optional TLS
export TYPESAFE_API_KEY=$(cat ~/.config/qwen38/api-key)  # the serving key; a client key from the identity map works too
```

```python
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

with TypeSafeClient() as client:
    r = client.system_one(
        state={"document": "I was charged twice. Please fix this ASAP."},
        questions={
            "billing": Noul(instructions="Is this ticket about billing?"),
            "tone": Choice(instructions="What is the customer's tone?", criteria={"calm": None, "frustrated": None, "angry": None}),
            "urgency": Score(instructions="How urgent is this ticket?", criteria=["can wait", "this week", "today"]),
        })
print(r.nouls["billing"].noul, r.choices["tone"].choice, r.scores["urgency"].score, r.model)
```

Verified with `typesafe-sdk` 0.7.0 (`tests/test_proxy_systemone.py` runs the SDK round trip when
the package is importable). The SDK's default timeout is 10 s; a very large state prefilled
cold can take longer on this box, so pass `timeout=` for those. `model` may be any of Jev's
aliases or the lane's own name; the response's `model` is what the lane served. The one SDK
call that does not translate is `client.models.list()`: `GET /v1/models` keeps the OpenAI shape
that opencode and `bench.sh` read. `model` takes Jev's three
aliases (`jev-latest`, `jev-preview`, `jev-1.13.0`) or the lane's own name, and any other name
is refused the way the hosted API refuses it.

Errors are the hosted API's, checked case by case against it (BENCHMARKS.md, "The same bad
request, the same refusal"): **422** with a `detail` list whose entries name the path that
failed, for a request that violates the schema; **400** with a `detail` that is a message or an
object, for a request that parses and cannot be served (an unknown model, a Noul with neither
instructions nor criteria, more than 255 options, a state longer than this lane's prompt
ceiling); **529** with `Retry-After` when `SYSTEMONE_MAX_CALLS` calls are already in progress,
which the SDK retries on its own; **503** with `Retry-After` while the engine restarts, in the
relay path's shape; **502** when the engine answered a branch with no usable distribution. The
SDK reads the message out of every one of those shapes.

Two behaviours worth knowing before you point a crowd at it. **Admission**: eight typed
decisions run at once and eight engine requests behind them, so the ninth caller gets 529
with `Retry-After` and the SDK sleeps it and sends the same call again; both numbers are
env vars (`SYSTEMONE_MAX_CALLS`, `SYSTEMONE_MAX_INFLIGHT`) and both came from a load curve
(BENCHMARKS.md, "Under load"). **A call you abandon stops**: the SDK's default timeout is
10 s, a cold fan-out on a large state takes longer, and when your socket closes the proxy
stops the fan-out and tells the engine to drop the branches it still holds. Pass a bigger
`timeout=` rather than relying on a retry to be cheaper than the first attempt.

