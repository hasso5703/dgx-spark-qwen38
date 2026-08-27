#!/usr/bin/env python3
"""Apply the 1M YaRN static-scaling patch to a cached checkpoint's config.json.

Sets max_position_embeddings to 1010000 and merges the YaRN keys
(rope_type "yarn", factor 4.0, original_max_position_embeddings 262144) into
the existing rope parameters, preserving every other key. Handles both config
shapes: keys nested under text_config (the target checkpoints) or at the root
(the DFlash2 draft; without the draft patch the server crashes at load).
Idempotent; the original file is backed up next to it as config.json.pre-yarn
on first patch (undo: move the backup over config.json).

Usage: patch-yarn.py <hf_cache_dir> <repo_id> [revision]
When a revision is given (sha or ref name), that exact cached snapshot is
patched; otherwise the newest one is (same selection rules as
patch-template.py). Field-tested on the reference box since 2026-08-22: a
690K-token request served, DFlash2 acceptance unchanged, reboot-persistent.
"""
import glob
import json
import os
import shutil
import sys


def main() -> None:
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    hf_cache, repo = sys.argv[1], sys.argv[2]
    revision = sys.argv[3] if len(sys.argv) == 4 else None
    repo_dir = f"{hf_cache}/hub/models--{repo.replace('/', '--')}"
    path = None
    if revision:
        ref_file = f"{repo_dir}/refs/{revision}"
        if os.path.isfile(ref_file):  # ref name (e.g. 'main') -> resolve to the sha
            revision = open(ref_file, encoding="utf-8").read().strip()
        cand = f"{repo_dir}/snapshots/{revision}/config.json"
        if os.path.isfile(cand):
            path = cand
        else:
            print(f"note: pinned revision {revision[:12]} has no config.json snapshot, "
                  "falling back to the newest one")
    if path is None:
        hits = glob.glob(f"{repo_dir}/snapshots/*/config.json")
        if not hits:
            sys.exit(f"cached config.json not found for {repo} under {hf_cache}. Run the checkpoint download first")
        path = max(hits, key=os.path.getmtime)
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    # Targets nest the model keys under text_config, the draft keeps them at the root.
    tc = config.get("text_config", config)
    if tc.get("max_position_embeddings") == 1010000:
        print(f"YaRN 1M already applied: {path}")
        return
    backup = path + ".pre-yarn"
    if not os.path.exists(backup):
        shutil.copy(path, backup)
    tc["max_position_embeddings"] = 1010000
    rp = tc.setdefault("rope_parameters", {})
    rp["rope_type"] = "yarn"
    rp["factor"] = 4.0
    rp["original_max_position_embeddings"] = 262144
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
    print(f"YaRN 1M applied to {path} (original backed up as config.json.pre-yarn)")


if __name__ == "__main__":
    main()
