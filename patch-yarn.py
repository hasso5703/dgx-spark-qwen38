#!/usr/bin/env python3
"""Apply the 1M YaRN static-scaling patch to a cached checkpoint's config.json.

Sets max_position_embeddings to 1010000 and merges the YaRN keys
(rope_type "yarn", factor 4.0, original_max_position_embeddings 262144) into
the existing rope parameters, preserving every other key. Handles both config
shapes: keys nested under text_config (the target checkpoints) or at the root
(the DFlash2 draft; without the draft patch the server crashes at load).
Idempotent; the original file is backed up next to it as config.json.pre-yarn
on first patch.

--restore undoes a previous patch (a native install coming home from 1m
would otherwise crash at load on the patched config: the target claims
1010000 while the server derives 262144). It moves the backup over
config.json and consumes it; with no backup and an unpatched config it is a
no-op. --check reports without touching anything.

Usage: patch-yarn.py [--restore | --check] <hf_cache_dir> <repo_id> [revision]
When a revision is given (sha or ref name), that exact cached snapshot is
used; otherwise the newest one is (same selection rules as
patch-template.py). Exit codes: 0 clean (patched, restored, or not patched),
2 patched with a backup waiting (--check only), 3 patched with no backup
(the cache was hand-patched: re-download that checkpoint's config.json).
Field-tested on the reference box since 2026-08-22: a
690K-token request served, DFlash2 acceptance unchanged, reboot-persistent.
"""
import glob
import json
import os
import shutil
import sys

YARN_WINDOW = 1010000
NATIVE_WINDOW = 262144


def resolve_snapshot(hf_cache: str, repo: str, revision: str | None) -> str:
    repo_dir = f"{hf_cache}/hub/models--{repo.replace('/', '--')}"
    if revision:
        ref_file = f"{repo_dir}/refs/{revision}"
        if os.path.isfile(ref_file):  # ref name (e.g. 'main') -> resolve to the sha
            revision = open(ref_file, encoding="utf-8").read().strip()
        cand = f"{repo_dir}/snapshots/{revision}/config.json"
        if os.path.isfile(cand):
            return cand
        print(f"note: pinned revision {revision[:12]} has no config.json snapshot, "
              "falling back to the newest one")
    hits = glob.glob(f"{repo_dir}/snapshots/*/config.json")
    if not hits:
        sys.exit(f"cached config.json not found for {repo} under {hf_cache}. Run the checkpoint download first")
    return max(hits, key=os.path.getmtime)


def is_patched(config: dict) -> bool:
    tc = config.get("text_config", config)
    return tc.get("max_position_embeddings") == YARN_WINDOW


def main() -> None:
    args = sys.argv[1:]
    mode = "patch"
    if args and args[0] in ("--restore", "--check"):
        mode = args.pop(0)[2:]
    if len(args) not in (2, 3):
        sys.exit(__doc__)
    hf_cache, repo = args[0], args[1]
    revision = args[2] if len(args) == 3 else None
    path = resolve_snapshot(hf_cache, repo, revision)
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    backup = path + ".pre-yarn"
    if mode == "check":
        if not is_patched(config):
            print(f"native config, not patched, nothing to do: {path}")
            return
        if os.path.exists(backup):
            print(f"YaRN 1M patched (restorable from config.json.pre-yarn): {path}")
            sys.exit(2)
        print(f"YaRN 1M patched with no backup at {path}: re-download that "
              f"checkpoint's config.json (hf download {repo} config.json "
              f"--revision <sha>) or delete the snapshot and re-run the install",
              file=sys.stderr)
        sys.exit(3)
    if mode == "restore":
        if not is_patched(config):
            print(f"native config, not patched, nothing to do: {path}")
            return
        if not os.path.exists(backup):
            print(f"YaRN 1M patched with no backup at {path}: re-download that "
                  f"checkpoint's config.json (hf download {repo} config.json "
                  f"--revision <sha>) or delete the snapshot and re-run the install",
                  file=sys.stderr)
            sys.exit(3)
        shutil.move(backup, path)
        print(f"YaRN 1M patch reverted (original restored): {path}")
        return
    # Targets nest the model keys under text_config, the draft keeps them at the root.
    tc = config.get("text_config", config)
    if tc.get("max_position_embeddings") == YARN_WINDOW:
        print(f"YaRN 1M already applied: {path}")
        return
    if not os.path.exists(backup):
        shutil.copy(path, backup)
    tc["max_position_embeddings"] = YARN_WINDOW
    rp = tc.setdefault("rope_parameters", {})
    rp["rope_type"] = "yarn"
    rp["factor"] = 4.0
    rp["original_max_position_embeddings"] = NATIVE_WINDOW
    # Atomic write through a temp file in the same directory: a kill between
    # truncate and flush used to leave a truncated config.json behind (the
    # rerun then died parsing it instead of patching it). Same encoding as
    # the read above, so no round-trip surprises.
    tmp = path + ".tmp-yarn"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, path)
    print(f"YaRN 1M applied to {path} (original backed up as config.json.pre-yarn)")


if __name__ == "__main__":
    main()
