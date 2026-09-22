# Typed decisions: a System One endpoint

The full section. The README carries the short version: what the endpoint is, the one
call that shows it, and what it measured against the hosted model. This is everything
else, starting with the contract it speaks.

Since v1.15 the keepalive proxy answers **`POST /v1/systemone`** with the wire contract of
TypeSafe's Jev ([docs.typesafe.ai/api](https://docs.typesafe.ai/api)): one `state` (a string
or JSON), a map of typed `questions` (`choice`, `score`, `noul`), and one typed answer per
question with its probabilities and a confidence. The answers come from **the lane you already
run**. Every question becomes one chat completion of exactly one token, the options are named
by single-token letters, and `top_logprobs` hands back the probability of each letter. Nothing
is generated, nothing is parsed, and the state is the shared prefix the radix cache reuses from
one question to the next: ask twenty questions about one document in one call and the document
is prefilled once, then served from the cache (measured below: one question on a 53,770-character
state, 10,799 tokens, answers in 0.2 s once cached).

```bash
curl -s http://127.0.0.1:30001/v1/systemone \
  -H "Authorization: Bearer $(cat ~/.config/qwen38/api-key)" -H 'Content-Type: application/json' -d '{
  "state": "Hi, I have been trying to connect my Stripe account for 3 days and it keeps failing. I am losing sales. Please help ASAP.",
  "model": "jev-latest",
  "questions": {
    "department":  {"type": "choice", "instructions": "Which team should handle this",
                    "criteria": {"billing": "Payment or subscription issues", "technical": "Bugs or integration problems", "sales": "Pricing or account questions"}},
    "frustration": {"type": "score", "instructions": "How frustrated the customer appears",
                    "criteria": ["Calm, just stating facts", "Frustrated but civil", "Very angry, strong language"]},
    "is_urgent":   {"type": "noul", "instructions": "The message conveys urgency or time-sensitivity"}
  }}'
```

The TypeSafe SDK (`pip install typesafe-sdk`) runs against this box with one base URL changed,
because Jev's aliases (`jev-latest`, `jev-preview`, `jev-1.13.0`) resolve to whatever the lane
serves and the response names it:

```bash
export TYPESAFE_BASE_URL=http://127.0.0.1:30001 TYPESAFE_API_KEY=$(cat ~/.config/qwen38/api-key)
```

**What is the hosted API's, to the digit.** The request and response shapes, the refusals
(both of them: 422 with a `detail` list naming the path that failed, 400 for a request that
parses and cannot be served), the limits, the two confidence statistics: they were read off the live `jev-1.13.0` on
2026-09-18 rather than off its docs, because the docs' normalized entropy reproduces their two
worked examples and not one live answer (those pages were written for `jev-1.12`). A Choice's
confidence is `(p_max * N - 1) / (N - 1)`, the top probability normalized between uniform and
certainty (46 live pairs, all within 0.018); a Score's is `1 - N * MAD_mode / floor(N^2 / 4)`,
one minus the mean absolute distance of the levels from the modal level (120 live pairs, within
0.030; the hosted probabilities come rounded to two decimals, which is where the residual lives).

**What is different, on purpose or by measurement.** The hosted model's probabilities are
sample frequencies: ten identical calls moved one option by a standard deviation of 0.014 to
0.027 and a score by 0.025. This readout is a softmax, which on this engine is not a constant
either: SGLang's kernels depend on batch composition (see `LEAN.md`), and five repeats of one
13-question call at concurrency 4 moved a yes/no probability by a standard deviation of 0.015
to 0.06 on the 27B lane (the measurements below have the concurrency 1 figure). The hosted model spends
17 plus about 7 output tokens per option of a Choice inside (2,412 for 255 options); this path
spends one token per question whatever the option count, and takes Jev's 255 options per
Choice (asked for 255 `top_logprobs`, this build returned 255, and 261 for 261). Score takes
one to ten levels, which is what the live API answers rather than what its docs describe, `GET /v1/models` keeps the OpenAI shape this box's other clients depend on (the SDK's
`models.list()` is the one call that does not translate), and three headers outside the
contract report what it has no room for: `x-systemone-label-mass` (how much of the model's
first-token probability landed on any label; near 1 means the prompt worked),
`x-systemone-branches`, and `x-systemone-cached-tokens` when the engine runs with
`--enable-cache-report` (neither lane does today, so the fan-out table below reads the cache
through latency instead).

**Why `top_logprobs` on `/v1/chat/completions`, and never `token_ids_logprob` on `/generate`.**
On the served build, `get_token_ids_logprobs_raw` appends a bare `[]` for a co-batched request
that asked for nothing and `batch_result_processor.py` calls `.tolist()` on every entry, so the
first batch that mixes one scoring request with one ordinary chat request kills the scheduler
([sglang#34719](https://github.com/sgl-project/sglang/issues/34719); the guard in #35052 is
still open). The top-k arm slices a tensor for every request, an empty tensor at k=0, and
survives the mix. A shared lane mixes on every step, so this is the only path the proxy takes;
the receipts are line numbers in `keepalive-proxy.py`'s "System One endpoint" section.

**Measured on the reference box** (`./bench-systemone.py`, byte-identical payloads to this box
and to the hosted Jev, public datasets at pinned revisions; the full protocol and every table
are in [BENCHMARKS.md](BENCHMARKS.md#typed-decisions-v115-the-system-one-endpoint-against-the-hosted-jev)):

| task (public data, pinned revision) | hosted Jev (`jev-1.13.0`), accuracy [95% CI] | this lane, raw readout | this lane, two option orders (`SYSTEMONE_PERMUTATIONS=2`) | Jev minus raw, paired | ECE: Jev / raw / raw after a fitted temperature | p50 latency: Jev from this box / raw on loopback, 4 concurrent clients |
|---|---:|---:|---:|---:|---:|---:|
| BoolQ, 3,270 yes/no (Noul) | 91.9% [90.9, 92.8] | 86.7% [85.5, 87.9] | 89.3% [88.2, 90.3] | +5.2 pts [+4.2, +6.2] | 2.4% / 4.7% / 2.7% (T 1.50) | 0.61 s / 0.43 s |
| MMLU-Pro, 1,000 MCQ, up to 10 options (Choice) | 83.8% [81.4, 86.2] | 58.1% [55.1, 61.1] | 62.1% [58.9, 65.0] | +25.7 pts [+22.5, +29.1] | 7.2% / 8.3% / 5.6% (T 1.25) | 0.62 s / 0.54 s |
| XNLI-fr, 500 entailment pairs, French (Choice, 3) | 78.2% [74.4, 81.6] | 71.2% [67.2, 75.0] | 71.4% [67.4, 75.2] | +7.0 pts [+3.8, +10.4] | 12.2% / 16.1% / 4.5% (T 2.05) | 0.62 s / 0.45 s |
| MMMLU-fr, 500 MCQ A to D, French (Choice) | 87.4% [84.6, 90.2] | 73.0% [69.2, 76.8] | 74.6% [70.4, 78.4] | +14.4 pts [+10.8, +18.2] | 3.7% / 9.8% / 7.1% (T 1.40) | 0.62 s / 0.43 s |
| TypeSafe's 20 public cases, 408 questions, four business workflows, against the frontier references | 93.2% (309 scorable) | 90.6% and 90.9% (the same run twice) | **92.9%** | | | 0.64 s per node / 7.5 s per node (about 9 questions each) |
| GDPR, the cookbook's 13 questions over a 54k-character article, 5 repeats | 50 of 50 reference answers | 50 of 50 | | | | 1.04 s per 13-question call / 5.5 s |

Read it plainly. On the **judgment work the hosted model is sold for**, TypeSafe's own public cases, the
27B lane with two option orders is within a point of the hosted model (92.9% against 93.2%, same
references, same questions, and it agrees with the hosted answers on 89.2% of them). A point is not
a result on its own here: the raw readout run twice, an hour apart, gave 90.6% and 90.9%, so the
noise floor of this comparison is about three tenths of a point and the gap sits inside it. On **yes/no over a passage** it is 2.6 points
behind with the lever and better calibrated than the hosted model (ECE 1.2% against 2.4%). On
**knowledge and calculation MCQ** it is far behind: 25.7 points on MMLU-Pro, 14.4 in French, and the
per-category table in BENCHMARKS.md says where (biology 0.90 against 0.98, math 0.49 against 0.88):
a single forward pass of a 27B does not compute, and whatever the hosted model does per option
(17 plus 7 output tokens each) it settles those. The raw readout is over-confident everywhere
(+3.5 to +16 points); a temperature fitted on half the items fixes most of it on the other half
without moving one answer, and the two-order lever fixes it for free on the tasks where position
bias lives. Latency: a single question answers in 0.2 s warm whatever the state size (80,000
characters included), 13 questions in about 1 s, 50 in about 3 s; the hosted model is flat at
0.6 s from this box, so a many-question call is where it still wins on time. Cost: the hosted runs
above billed 3.5 million input tokens, about 15 cents; the local runs billed nothing and sent nothing.


Two more measurements decided two defaults. The thinking budget: `SYSTEMONE_THINK_TOKENS=1024`
on the first 200 MMLU-Pro rows scores **80.0% [74.0, 85.5]** against the hosted model's 84.0%
[78.5, 89.0] on the same rows (an interval that holds zero) and the raw readout's 57.5%, at 8.8 s
a question (p95 31 s); 27 of the 200 thoughts hit the cap and those score 63%, so the budget is
what they need. The warm-first send this proxy shipped with in draft lost every cold and warm
comparison (13 questions on a never-seen 10,800-token state: 14.9 s with it, 5.4 s without) and
is off by default. And on repeatability: five identical 13-question calls at concurrency 1
moved an uncertain yes/no probability by a standard deviation of up to 0.11 and a settled one by
under 0.01, so on this engine two calls do not return the same second decimal on a close
question; the two-order lever averages two readouts and halves that.

Four levers ship **off** and are decided by that benchmark. `SYSTEMONE_PERMUTATIONS=2` asks
every question twice, once with the options in the given order and once reversed, and averages
(a letter readout prefers some positions; two orders cancel the first-order effect at the price
of one more single-token branch in the same fan-out). `SYSTEMONE_TEMPERATURE` scales the label
logits (a value fitted on labeled data is a calibration step; `report --fit-temperature` fits
one on half the items and scores the other half). `SYSTEMONE_MIN_LABEL_MASS` refuses a question
whose label mass falls under it with a 502 instead of answering from the crumbs, which is what
TypeSafe's CEO said a masked readout should do. `SYSTEMONE_THINK_TOKENS=N` is the one that
leaves System One: the model thinks first, in its native thinking mode, stopped at the end of
the thought or at N tokens, then the same turn is continued with the thought closed and the
label is read off the next token exactly as before; it costs seconds of decode and it is what
closes the gap on questions a single forward pass cannot settle (the measurements above have
the number). The thought is never returned, the contract has no field for it. Set them on the
service with a drop-in (`sudo systemctl edit qwen38-keepalive.service`, an `[Service]` block of
`Environment=` lines), the way `install.sh` sets the prompt ceiling.

**Open it to a crowd and the door holds.** `SYSTEMONE_MAX_CALLS` (8) typed-decision calls
run at once and `SYSTEMONE_MAX_INFLIGHT` (8) engine requests behind them; the next caller is
answered **529 with `Retry-After`**, the status the hosted API uses for overload and the one its
SDK retries with backoff. Both numbers came from a curve, 32 clients in a closed loop against
the 27B lane at four settings (BENCHMARKS.md, "Under load"): the engine is the bottleneck either
way, near one answer a second at every setting, so the door does not cost throughput, it decides
who waits where. Wide open, the median one-question call took 28.3 s and an ordinary streamed
completion on the same lane went from 4.6 s to 57.0 s. At eight, the same call came back in
3.8 s and nothing failed. A caller alone still gets the whole fan-out.

**The state is fenced, and a caller that leaves takes its work with it.** The state is
third-party text by design (a ticket, a document, a tool result), so it is wrapped in a
token drawn at start-up that a caller cannot close from inside: a state carrying its own
"QUESTION / OPTIONS / Reply with the label" block used to produce a branch with two of
each, the attacker's first. On eighteen injected states, six of them written that way,
this endpoint followed the injected instruction zero times and the hosted Jev once. And
because the SDK gives up after 10 s by default while a cold fan-out can take longer, every
branch carries a request id this proxy imposes: when the caller's socket closes, the
fan-out stops, the engine is told to drop the branches it still holds, and the admission
slot comes back at once.

**A bad request reads the same as it does on the hosted API.** Fifty malformed and edge
requests were sent to both, the same bytes: fifty times the same status, the same envelope and,
where the answer is a list of paths, the same path. That includes the places where the hosted
API is more permissive than a reader of its docs expects, and refusing them here would have
broken a client that only changed its base URL: a Choice with a single option is answered, a
Score with one level too, an unknown key inside a Noul's criteria is ignored, `""` is an option
name. It also includes the places where it says no and this endpoint used to say yes, the loud
one being an unknown model name: `jev-9` is a 400 there and is a 400 here now, instead of being
quietly answered by the local lane.

What it is not: a reasoning step by default (the model answers in one token, with thinking off,
unless `SYSTEMONE_THINK_TOKENS` is set), a vision input (text only, like Jev), or a judgment the
box makes on its own about your data. Every
question a client asks stays on this box.

