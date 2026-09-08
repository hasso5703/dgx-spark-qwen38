#!/usr/bin/env python3
"""Build the reduced draft vocabulary SGLang serves as --speculative-token-map.

Why this exists. On this box decode is bandwidth bound, and the draft head of a
speculative step reads the model's `lm_head` in full: `[248320, 2560]` in BF16
is 1.21 GiB, read once per draft step, three times in an MTP-3 engine step.
SGLang can serve the draft a *sliced* head instead: pass a list of token ids and
`eagle_worker_v2.init_lm_head` clones the target head and keeps only those rows
(`head.data = head.data[hot_token_id]`), while the proposal path maps the draft's
local indices back to global ids (`topk_index = hot_token_id[topk_index]`). The
target still verifies over the full vocabulary, so **output quality is unchanged
by construction**: a token the draft can no longer propose is not a wrong token,
it is a draft the target would have had to reject or accept on its own.

At 65,536 rows the same three reads cost 0.31 GiB each instead of 1.21, which is
2.7 GiB removed from every engine step. Two independent single-Spark
reimplementations of this idea (MiaAI Lab's `MTP_DRAFT_VOCAB`, tonyd2wild's
"reduced-vocabulary MTP draft") report it as one of their two largest levers,
around +25% decode. This script is the SGLang-native way to get it: the engine
already implements the mechanism, it only needs the list.

Where the ranking comes from, in priority order:

  1. Every special and added token, unconditionally. There are a few hundred and
     they sit at exactly the structural boundaries where drafting is easiest.
  2. Tokens observed in a corpus, most frequent first. The corpus that matters
     is the model's own output distribution, because that is what the drafter
     has to predict; `--corpus` takes one or more .jsonl files with a "text"
     field, or plain .txt.
  3. Everything else by the tokenizer's own construction order: byte-level base
     tokens first, then in BPE merge order. A BPE merge table IS a frequency
     ranking, learned over the tokenizer's training corpus, so this is a real
     frequency prior and it needs no corpus at all. It is what makes a map
     usable on a box that has no corpus to hand.

The report prints corpus coverage, which is the number to tune on: the tokens a
map misses are drafts the target rejects, not errors.

Usage (inside the serving image, which has the right transformers and torch):

  python3 build-token-map.py --snapshot /root/.cache/huggingface/hub/models--.../snapshots/<sha> \\
      --out /out/token-map-65536.pt --size 65536 [--corpus /out/corpus.jsonl]

`install.sh` runs it for you; see SPEC_TOKEN_MAP in the README.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

CHUNK = 1 << 20  # tokenize ~1 MiB at a time


def die(msg: str) -> None:
    print(f"build-token-map: {msg}", file=sys.stderr)
    raise SystemExit(2)


def load_tokenizer(snapshot: str):
    try:
        from transformers import AutoTokenizer
    except ImportError:
        die("transformers is not importable. Run this inside the serving image "
            "(install.sh does), not on the host.")
    try:
        return AutoTokenizer.from_pretrained(snapshot, use_fast=True)
    except Exception as exc:  # noqa: BLE001
        die(f"could not load the tokenizer from {snapshot}: {exc}")


def construction_rank(snapshot: str, vocab: dict[str, int], vocab_size: int) -> dict[int, int]:
    """id -> rank, from the tokenizer's own construction order.

    Base tokens (produced by no merge) rank 0; a token produced by merge i ranks
    i + 1. A BPE merge table is ordered by the frequency of the pair in the
    tokenizer's training corpus, so this is a frequency prior over the vocabulary
    that costs nothing to compute and needs no corpus.
    """
    merges_path = os.path.join(snapshot, "merges.txt")
    rank: dict[int, int] = {}
    if not os.path.exists(merges_path):
        # Not a merges-file tokenizer (unigram, or merges folded into
        # tokenizer.json). Fall back to id order, which for most vocabularies is
        # also roughly construction order.
        print("note: no merges.txt; ranking the tail by token id instead")
        return {i: i for i in range(vocab_size)}
    with open(merges_path, encoding="utf-8") as handle:
        index = 0
        for line in handle:
            line = line.rstrip("\n")
            if not line or line.startswith("#version"):
                continue
            parts = line.split(" ")
            if len(parts) != 2:
                continue
            index += 1
            merged = parts[0] + parts[1]
            tid = vocab.get(merged)
            if tid is not None and tid not in rank:
                rank[tid] = index
    # Everything with no merge of its own is a base token: rank 0.
    for tid in range(vocab_size):
        rank.setdefault(tid, 0)
    return rank


def iter_texts(path: str):
    """Yield bounded chunks of a corpus. `path` may carry a `:N` repeat weight so
    a small in-distribution corpus can be given the same say as a large one."""
    repeat = 1
    if ":" in path and path.rsplit(":", 1)[1].isdigit():
        path, repeat = path.rsplit(":", 1)
        repeat = int(repeat)
    if not os.path.exists(path):
        die(f"corpus not found: {path}")
    for _ in range(repeat):
        if path.endswith(".jsonl"):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        try:
                            yield json.loads(line).get("text", "")
                        except json.JSONDecodeError:
                            continue
        else:
            with open(path, encoding="utf-8", errors="replace") as handle:
                while True:
                    block = handle.read(CHUNK)
                    if not block:
                        break
                    yield block


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", required=True,
                    help="the model snapshot directory (tokenizer + merges.txt live there)")
    ap.add_argument("--out", required=True, help="output path for the torch-saved id list")
    ap.add_argument("--size", type=int, default=65536,
                    help="how many token ids to keep (default 65536)")
    ap.add_argument("--corpus", nargs="*", default=[],
                    help="optional corpus files, each optionally suffixed :N to repeat it")
    args = ap.parse_args()

    try:
        import torch
    except ImportError:
        die("torch is not importable. Run this inside the serving image.")

    tok = load_tokenizer(args.snapshot)
    vocab = tok.get_vocab()
    vocab_size = len(vocab)
    if args.size < 1024:
        die(f"--size {args.size} is too small to draft with; use at least 1024")
    if args.size >= vocab_size:
        die(f"--size {args.size} is not a reduction: the vocabulary has {vocab_size} tokens")

    # 1. specials and added tokens, unconditionally
    specials: set[int] = set(int(i) for i in (tok.all_special_ids or []))
    added = getattr(tok, "added_tokens_decoder", None) or {}
    specials |= {int(i) for i in added}
    specials = {i for i in specials if 0 <= i < vocab_size}

    # 2. corpus frequencies
    counts: Counter[int] = Counter()
    total_occurrences = 0
    for path in args.corpus:
        for text in iter_texts(path):
            if not text:
                continue
            ids = tok(text, add_special_tokens=False)["input_ids"]
            counts.update(ids)
            total_occurrences += len(ids)

    # 3. the construction-order tail
    rank = construction_rank(args.snapshot, vocab, vocab_size)

    chosen: list[int] = []
    seen: set[int] = set()

    def take(tid: int) -> bool:
        if tid in seen or not 0 <= tid < vocab_size:
            return False
        seen.add(tid)
        chosen.append(tid)
        return len(chosen) >= args.size

    full = False
    for tid in sorted(specials):
        full = take(tid)
        if full:
            break
    if not full:
        for tid, _n in counts.most_common():
            full = take(int(tid))
            if full:
                break
    if not full:
        for tid in sorted(range(vocab_size), key=lambda t: (rank.get(t, vocab_size), t)):
            full = take(tid)
            if full:
                break

    ids = sorted(chosen)  # the engine treats this as a set; sorted is reproducible

    # Self-checks. A malformed map is not a slow server, it is a wrong one.
    if len(ids) != args.size:
        die(f"internal error: built {len(ids)} ids, wanted {args.size}")
    if len(set(ids)) != len(ids):
        die("internal error: duplicate ids in the map")
    if min(ids) < 0 or max(ids) >= vocab_size:
        die(f"internal error: id out of range for a {vocab_size}-token vocabulary")
    missing_specials = sorted(specials - set(ids))
    if missing_specials:
        die(f"internal error: {len(missing_specials)} special tokens were dropped: "
            f"{missing_specials[:8]}")

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir and not os.path.isdir(out_dir):
        die(f"output directory does not exist: {out_dir}")
    torch.save(ids, args.out)

    # Read it back the way SGLang will, so a map that cannot be loaded fails here
    # and not nine minutes into a boot.
    reread = torch.load(args.out, weights_only=True)
    back = torch.tensor(reread, dtype=torch.int64)
    if back.numel() != args.size or int(back.min()) < 0 or int(back.max()) >= vocab_size:
        die("the written map does not read back as SGLang would read it")

    covered = sum(n for tid, n in counts.items() if tid in seen)
    print(f"vocabulary        {vocab_size}")
    print(f"map               {args.size} ids ({100.0 * args.size / vocab_size:.1f}% of the vocabulary)")
    print(f"specials kept     {len(specials)}")
    print(f"lm_head per read  {vocab_size * 2560 * 2 / 2**30:.2f} GiB -> "
          f"{args.size * 2560 * 2 / 2**30:.2f} GiB")
    if total_occurrences:
        print(f"corpus            {total_occurrences} token occurrences, "
              f"{len(counts)} distinct ids")
        print(f"corpus coverage   {100.0 * covered / total_occurrences:.3f}% of occurrences")
        uncovered = [(n, tid) for tid, n in counts.items() if tid not in seen]
        uncovered.sort(reverse=True)
        if uncovered:
            worst = ", ".join(
                f"{tok.decode([tid])!r}x{n}" for n, tid in uncovered[:5])
            print(f"most missed        {worst}")
    else:
        print("corpus            none given: the map is specials + construction order")
    print(f"written           {args.out}")


if __name__ == "__main__":
    main()
