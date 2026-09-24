#!/usr/bin/env bash
# The engine's key, where SGLang reads it without a command line. This writes
# engine-secrets.yaml next to the api-key file, and the units, the flash launcher and
# run.sh hand it to the server as `--config /out/engine-secrets.yaml`: SGLang merges
# that file into its arguments in memory, so the key is in no process's argv. As
# --api-key "$(cat ...)" it was in the docker client's and in the server's, which any
# local user reads in /proc (found in review, 2026-09-24). It runs before every start,
# so a key changed by hand still reaches the engine at its next start, as it did.
# Usage: engine-secrets.sh [config dir]   (default: the folder this script sits in)
set -euo pipefail
d="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
key="$(cat "$d/api-key")"
case "$key" in
  "" | *[\"\\]* | *[[:cntrl:]]*)
    echo "engine-secrets.sh: $d/api-key is empty, or holds a quote, a backslash or a control character" >&2
    exit 1 ;;
esac
want="api-key: \"$key\""
# Written only when it changes: install.sh keeps an engine whose inputs are older than its
# start, and a rewrite of the same bytes would make it restart the engine at every run.
if [ -f "$d/engine-secrets.yaml" ] && [ "$(cat "$d/engine-secrets.yaml")" = "$want" ]; then
  chmod 600 "$d/engine-secrets.yaml"
  exit 0
fi
umask 077
printf '%s\n' "$want" > "$d/.engine-secrets.yaml.tmp"
mv -f "$d/.engine-secrets.yaml.tmp" "$d/engine-secrets.yaml"
