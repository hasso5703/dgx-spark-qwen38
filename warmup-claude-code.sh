#!/bin/bash
# Optional: pre-warm the SGLang radix cache with your Claude Code system prompt
# after each service (re)start, so your first real session starts hot.
# Installed by install.sh --with-claude-warmup (placeholders substituted there).
export HOME=__HOME__
export PATH=__HOME__/.local/bin:/usr/local/bin:/usr/bin:/bin
LOG=__HOME__/.config/qwen38/warmup.log
KEY=$(cat __HOME__/.config/qwen38/api-key)

for i in $(seq 1 300); do
  curl -s -m 2 "http://127.0.0.1:__PORT__/health" >/dev/null 2>&1 && break
  sleep 3
done

cd __HOME__
echo "$(date '+%F %T') warmup started" >> "$LOG"
timeout 420 env \
  ANTHROPIC_BASE_URL="http://127.0.0.1:__PORT__" \
  ANTHROPIC_AUTH_TOKEN="$KEY" \
  ANTHROPIC_API_KEY="" \
  ANTHROPIC_MODEL="qwen3.8-27b" \
  ANTHROPIC_DEFAULT_HAIKU_MODEL="qwen3.8-27b" \
  ANTHROPIC_DEFAULT_SONNET_MODEL="qwen3.8-27b" \
  ANTHROPIC_DEFAULT_OPUS_MODEL="qwen3.8-27b" \
  ANTHROPIC_DEFAULT_FABLE_MODEL="qwen3.8-27b" \
  CLAUDE_CODE_SUBAGENT_MODEL="qwen3.8-27b" \
  CLAUDE_CODE_MAX_CONTEXT_TOKENS=262144 \
  claude --settings '{"env":{"CLAUDE_CODE_ATTRIBUTION_HEADER":"0","CLAUDE_CODE_ENABLE_TELEMETRY":"0"}}' \
    --model qwen3.8-27b --exclude-dynamic-system-prompt-sections \
    -p "Reply with exactly: warmup-ok" >> "$LOG" 2>&1
echo "$(date '+%F %T') warmup finished rc=$?" >> "$LOG"
