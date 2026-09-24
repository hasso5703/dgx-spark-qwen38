# The 1M context mode (27B lanes)

What a plain 27B install serves since v1.12.1: a 1,010,000-token window, what it costs, and how the limits are fitted to the pool your own boot got. Moved out of the README in v1.14.2 so the README could stay an entry point.

Since v1.12.1 this is what a plain 27B install serves. It was opt-in behind an
env var until then, which meant the window this whole stack is built around was
off for anybody who had not read this page.

```bash
curl -fsSL .../get.sh | bash        # 1M, and combines freely with MODEL_CHOICE=uncensored
CONTEXT_MODE=native ./install.sh    # the 262144 window instead
```

The flash lane and `--no-service` cannot serve it and stay native with no
refusal, because a default must never reject something the operator did not
type. A re-run keeps the mode already installed, in both directions: an update
does not patch YaRN into the configs of a box that chose native.

This is, as one converging command, the exact preset that serves the reference box
daily since 2026-08-22:

- **1,010,000-token window** via YaRN static scaling (factor 4.0,
  `original_max_position_embeddings: 262144`) patched into **both** cached `config.json`
  files by `patch-yarn.py` (target model AND DFlash2 draft, or the draft crashes at load;
  originals backed up next to them as `config.json.pre-yarn`), plus
  `--context-length 1010000` and `SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN=1`.
- **`--mem-fraction-static 0.76`** (0.70 before v1.14, see [the memory trap](gb10-memory.md)): the KV pool is
  a boot lottery, and the measurement campaigns on this box do not agree. Five
  boots in the v1.3 era reported **917K-1019K** tokens; three around 2026-08-30
  reported **863,398, 893,479 and 913,334** for the same checkpoint; four on the
  official image at 0.76, on 2026-09-17 and 18, reported **902,398, 889,131,
  889,722 and 887,797**. Over sixteen 27B boots here the pool ranged from
  **832,993 to 922,094 tokens**, so no campaign's floor is the floor: plan against
  the pool your own boot reports, which is what the fit below does. DFlash2
  acceptance is unchanged either way, and a real 690K-token
  request has been served (cold prefill 40 min, then cached).
- **The limits are fitted to your own boot, automatically.** The generated
  opencode limits below are static, and their 1m worst case (compaction at 680K,
  one agent step of up to 43,863 tokens after it, and 200K of output: 923,863)
  sits **above every pool measured here**, so left as is a long session can
  outgrow the pool mid-conversation, which is the field case that produced this
  tool. Since v1.12.1, `install.sh` runs `oc-fit-limits.py` itself at
  the end of every 1m install, once the engine is up: it reads the pool your boot
  actually got and rewrites the limits to fit it, up or down. Run it by hand (or
  press the cockpit's button) after any later reboot you want re-fitted. The FP8 targets ship lower static limits (480,000/160,000),
  set when their pool measured about 92,000 tokens smaller; on the official image
  the gap all but closed (881,895 against 887,797 on 2026-09-18), so theirs are
  conservative now.
- **The keepalive proxy becomes load-bearing.** Every service install ships it (see
  "opencode integration"), but at 1M it is not optional: a cold 690K-token prefill can
  keep the wire silent for tens of minutes. The proxy injects the official Anthropic
  `ping` event on `/v1/messages` and an authentic empty chunk on the OpenAI dialect,
  every 10 s, only at SSE event boundaries (a keepalive inside an event corrupts the
  JSON, measured); it closes the upstream the moment the client disconnects, and
  reports an explicit SSE error after 3600 s of true upstream silence (above the worst
  legitimate prefill). **Agent clients must use the proxy port**; the direct server
  port stays for curl and benches.
- **`HF_HUB_OFFLINE=1`** in the unit, so no Hub metadata check can re-resolve a
  checkpoint and silently undo the YaRN-patched configs (see [Operations](operations.md)).
- **`Restart=always`**: a crash that exits 0 (a Triton compile crash measured 2026-08-22
  ended in `SystemExit: 0`) still gets relaunched; `on-failure` would not.
- **A corruption tripwire** (proxy v6.11). When a decode path loses its state on this
  hardware it does not stop: it emits runs of token id 0, which is `!` in the Qwen
  tokenizer, and the client reads a wall of exclamation marks as if it were an answer
  (sglang [#36537](https://github.com/sgl-project/sglang/issues/36537),
  [#36558](https://github.com/sgl-project/sglang/issues/36558),
  [#36806](https://github.com/sgl-project/sglang/pull/36806),
  [#36845](https://github.com/sgl-project/sglang/pull/36845)). The proxy counts those
  characters across the stream and, past `CORRUPTION_RUN` of them in a row (128 by
  default, `0` disables), aborts the generation upstream and sends an explicit
  `corrupted_output` error instead. It reads only the delta text it already relays,
  never tool-call arguments, so a model writing `!!!` in prose is untouched.
- **No zombie generations** (proxy v6.14). A client that gives up leaves the engine
  decoding unless the abort reaches it in time, and it cannot: `abort_request()` returns
  early once the rid has left `rid_to_state`, which the disconnect itself empties
  ([sglang#35255](https://github.com/sgl-project/sglang/pull/35255), merged upstream
  2026-09-04 and in neither image this repo serves). Measured here on 2026-09-09: 6,582
  `state was deleted in TokenizerManager` lines in one day, one request decoding 6 min for
  nobody. The proxy now names every request itself (`x-override-rid`, which the engine
  honours because the units pass `SGLANG_ENABLE_REQUEST_HEADER_OVERRIDES=1`), so a request
  abandoned during prefill still has a name, and it aborts **before** closing the socket
  rather than in a thread racing it. Where no rid can exist, on `/v1/messages` (the
  Anthropic route mints its own `msg_<uuid>` and applies no header overrides), the answer
  is drained to its end instead of being orphaned. Same request through both proxies: 223
  flood lines and a held slot before, 0 after.
- **A tool-schema guard** (proxy v6.13). The engine validates every tool's parameters
  with `jsonschema`, whose `regex` format check compiles `pattern` with Python's `re`.
  JSON Schema says `pattern` is ECMA-262, which has Unicode property escapes (`\p{Cc}`)
  that `re` rejects outright, so a single such tool makes the engine answer `400` to
  **every** request of the session (measured 2026-09-09 against Claude Code 2.1.266 and
  its `Artifact` tool). The proxy removes only the patterns Python cannot compile, only
  inside tool parameter schemas, and forwards every other body untouched and unparsed.
- The generated opencode config starts from `context/input 700000, output 200000`
  (compaction fires at 680000; with one worst agent step and the answer on top,
  923,863, above every pool measured here), and the fit at the end of the install
  lowers it to the pool the boot got.

Quality past the native 262144 window is not formally evaluated here: treat it as an
experimental preset. Proof it holds up operationally, one continuous **opencode** session
(reasoning effort `xhigh`, output cap lifted) built a playable 3D zombie FPS from a single
prompt by YouTuber Bijan Bowen:

- **535,361 tokens** of context reached in one session, twice the native window, zero compaction
- **~360K tokens generated**, 239 agent steps, 274 tool calls, no retry, no manual rescue
- Result, single HTML file: **https://subway-fps.vercel.app**

Back to native: `CONTEXT_MODE=native ./install.sh` (keeps the proxy, which every
service install ships, and restores the pre-YaRN `config.json.pre-yarn` originals over the patched
target and draft configs; a native server crashes at load on a patched config,
so `run.sh` refuses a patched cache early instead of ten minutes into the
boot, and the installer refuses with a re-download fix-it when a backup is
gone).

[Back to the README](../README.md)
