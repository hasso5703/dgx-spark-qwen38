# `lean`: a fourth reasoning-effort level, and why the shipped default is worse

Qwen3.8-27B ships with `reasoning_effort` defaulting to `xhigh`, the most
expensive of its three levels. This repo adds a fourth, `lean`, and makes it the
default. Everything below was measured on one box; every number says which set
it came from, and the negative results are here too.

## What the levels actually are

Not modes, not decoding parameters: three sentences. Measured with
`prompt_tokens` on a one-word request, which counts exactly what the chat
template prepends to the system message.

| client sends | `prompt_tokens` | injected |
|---|---|---|
| nothing | 109 | `lean` (this repo's default) |
| `lean` | 109 | 98 tokens, the prompt below |
| `medium` | 11 | nothing at all |
| `low` | 41 | 30 tokens, Qwen's text |
| `xhigh` | 53 | 42 tokens, Qwen's text |

Qwen's three levels come out of the patch byte-identical. Ask for `medium` and
you get Qwen's `medium`, so numbers measured here stay comparable with anyone
else's. Only the default moves.

## The prompt

```
Answer immediately, with no reasoning, whenever the request asks for something
you can simply write down: a rename, a reformat, a definition, a lookup, a
single concrete edit. Reason only when producing the answer needs steps you
cannot skip.

If the request is underspecified, pick the most common sensible interpretation,
state it in one line, and proceed. If you notice yourself checking something
twice, or weighing the same options again, stop there and commit.
```

74 words. The code holds it in one place, `LEAN_INSTRUCTIONS` in
`patch-template.py` (this page quotes it), and a test fails if a copy appears in
the four scripts that install or serve it: `install.sh`, `run.sh`,
`switch-model.sh` and the proxy.

## Public benchmarks: 364 problems, published scoring

HumanEval (164, the dataset's unit tests executed) and GSM8K (200, exact final
number, seed 20260914). One run per problem per level.

| | HumanEval | GSM8K | both | thinking tokens | median s | truncated |
|---|---|---|---|---|---|---|
| `xhigh` (Qwen's default) | 93.9% | 95.0% | 94.5% | 255 | 12.1 | **5** |
| `medium` | 98.2% | 95.5% | 96.7% | 167 | 8.8 | 0 |
| `low` | 95.7% | 93.5% | 94.5% | 138 | 7.5 | 0 |
| **`lean`** | 97.6% | 94.0% | 95.6% | **96** | **6.2** | 0 |

Paired McNemar against `medium`, with a task-resampled bootstrap on tokens:

| | pass difference | p | thinking-token ratio |
|---|---|---|---|
| `xhigh` | -2.2 pts | 0.057 | **3.19x** [2.73, 3.69] |
| `low` | -2.2 pts | **0.021** | 0.89x [0.83, 0.95] |
| `lean` | -1.1 pts | 0.344 | **0.71x** [0.66, 0.77] |

Two things worth stating plainly. The shipped default costs 3.19x the thinking
tokens of `medium` and scores lower, and on HumanEval alone it is 4.3 points
lower at p=0.039, with five of its ten failures there being truncations: it
spent the whole 16,000-token budget thinking and returned nothing. And `low`,
the fix the community recommends, loses significantly (p=0.021) for an 11%
saving.

## OpenReq: 58 underspecified requests, 1,218 runs

HumanEval and GSM8K contain no request with two defensible readings, which is
the only shape that makes this model spiral. `openbench.py`, in the research notes
kept on the reference box and not in this repository, holds 58 that do, across six
families (implement, refactor, review, choose, debug, design), each prompt written
out in full. Three repeats each.

| | thinking tokens | median s | usable answer | empty answer | truncated |
|---|---|---|---|---|---|
| `xhigh` | 2,478 | 236.9 | 73.0% | **6.9%** | **10.3%** |
| `medium` | 420 | 73.0 | 89.1% | 0% | 0% |
| `low` | 351 | 58.1 | 89.1% | 0% | 0% |
| **`lean`** | **192** | **37.6** | **90.2%** | **0%** | **0%** |

Paired bootstrap over the 58 requests, 90% intervals:

| | vs `medium` | vs `xhigh` |
|---|---|---|
| thinking tokens | **0.436x** [0.40, 0.47] | **0.047x** [0.04, 0.06] |
| usable answers | +1.1 pts [-2.3, +4.6] | **+17.2 pts** [+10.9, +24.1] |

The token ratio and the falsification criterion were written down before these
runs (`PREREGISTER_OPENREQ.md`, in the same research notes, not in this repository): false if the ratio's
upper bound exceeded 0.5 or usable answers lost more than 10 points. Neither
happened.

## Executed, not inspected

For twelve of the requests the answer is a component, so the answer's code is
run: the probe discovers whatever API the answer chose and exercises it. A cache
must store, return and evict. A rate limiter must eventually refuse. A retry
wrapper must actually retry. Each probe is validated in three directions before
use: it accepts a reference implementation, rejects a hollow one, and accepts a
correct asynchronous one.

Answers are scored in three states, not two. An answer that needs a package this
environment lacks, or that exposes an API no probe can drive, is **not
checkable** rather than broken, and the comparison runs on the subset checkable
for every level. Conflating those two states scored a valid FastAPI paginator
and a valid SQLAlchemy one both as broken code.

| | works | not checkable |
|---|---|---|
| `xhigh` | 21.2% | 22.2% |
| `medium` | 33.3% | 2.8% |
| **`lean`** | **42.4%** | **0.0%** |

`lean` against `xhigh`: **+18.2 points** [+3.0, +36.4], interval excluding zero.
Against `medium`: +12.1 points [-9.1, +36.4], which does not exclude zero and is
therefore not a claim.

## Is the truncation an artefact of the 16,000-token cap?

It is the first thing to ask, so it was asked. Every run where `xhigh` truncated
was replayed with the cap tripled to 48,000. It then truncates 0 times out of 8,
and spends a median of **12,075 thinking tokens and 573 seconds** per request,
one run reaching 24,379 tokens and 22 minutes, and still fails 3 of 8. The cap
was not manufacturing the failures.

## What is claimed, and what is not

**Claimed.** The thinking-token ratio and the latency. They are counts, their
variance is small, and they reproduce across every set measured: 0.71x of
`medium` on public benchmarks, 0.436x on underspecified requests, 0.047x of the
shipped default there. On a live check of one request: 990 tokens and 19.0s for
`lean` against 9,084 tokens and 190.9s for `xhigh`.

**Not claimed.** That `lean` is more correct than `medium`. The measured
difference is -1.1 points at p=0.344, and two runs of the *same* configuration
on the same 364 problems differed by 1.5 points, which is larger than that. The
quality claim is non-inferiority only: the prompt does not cost correctness.

**Not available.** Greedy decoding does not fix the noise on this stack: five
identical `temperature=0` calls produced two distinct outputs, because SGLang's
kernels depend on batch composition and DFlash2 speculative verification adds a
second source of variation.

## What did not work

Reported because a list of only the wins is a sales page.

- **Four candidates designed from an error analysis** of `lean`'s own GSM8K
  failures (re-read the request, never state an uncomputed number, watch for
  qualifier words, compute-then-verify) all cost tokens and gained nothing.
- **An expert persona** at the top of the prompt, the widely recommended trick:
  no quality change on top of `lean`, and used alone it spends **337 thinking
  tokens against 163 for plain `medium`**, twice as much, in the wrong
  direction.
- **A decisiveness instruction** ("give the answer, not your uncertainty about
  it") did nothing, and the reason is visible in the data: `lean` already sits
  at **0.00** backtracking markers per thousand thinking tokens against 2.94 for
  `medium`. There was nothing left to remove.
- **Chain of Draft** ("minimum draft, five words per step"), the strongest
  published concise-reasoning prompt, cut the most tokens of anything tested and
  lost quality doing it.
- **Forbidding the backtracking words** helps everywhere except on hard
  problems, where the model legitimately needs to correct itself.

## What was prepared and not finished

A 557-problem held-out set (MBPP sanitized 257 + 300 GSM8K questions drawn with
a different seed and filtered against the design questions, overlap verified 0)
with a five-arm protocol including an identical twin of `medium` as a null
control, to measure what the apparatus reports as a difference when there is
none. The protocol is written in those research notes, not in this repository. It was not run. Until
it is, the quality statement rests on the 364 public problems and the 58
underspecified ones, which is what the tables above say.

## Reproducing this

Conditions, and no claim is made outside them: Qwen3.8-27B NVFP4 (RadixArk, rev
`52d1adc5`), SGLang with DFlash2 drafting, DGX Spark GB10, 262,144-token native
context, temperature 0.7, top_p 0.95, max_tokens 16,000. 7,008 runs in total.

`LEAN_DEFAULT=0 ./install.sh` installs the level without making it the default,
for a box that would rather keep Qwen's shipped behaviour until it has run its
own numbers.
