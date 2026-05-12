#!/usr/bin/env bash
# Bot launcher with macOS Keychain-backed secret resolution.
#
# - First run (any secret missing): prompts user interactively, stashes
#   value into Keychain (service=kimi-bridge), then launches the bot.
# - Subsequent runs: silent — secret comes back from Keychain, never
#   touches disk in plaintext, never appears on stdin/stdout outside the
#   one-time prompt session.
#
# The bot itself inherits the secrets as env vars only; AI coding tools
# attached to this repo can run, restart and inspect the bot without
# ever seeing the literal secret values (as long as they don't read the
# bot process env via `ps eww` or call `security ... -w` themselves).
#
# Rotate a secret:
#   security delete-generic-password -s kimi-bridge -a discord-token
#   security delete-generic-password -s kimi-bridge -a moonshot-key
# Next ./run-bot.sh will prompt again.

set -euo pipefail
cd "$(dirname "$0")"

get_secret() {
  local label=$1 prompt=$2
  if v=$(security find-generic-password -s kimi-bridge -a "$label" -w 2>/dev/null); then
    printf '%s' "$v"
    return 0
  fi
  printf '🔐 %s (입력은 화면에 표시되지 않습니다): ' "$prompt" >&2
  IFS= read -r -s value
  printf '\n' >&2
  if [[ -z "${value:-}" ]]; then
    echo "❌ 빈 값은 저장하지 않습니다. 종료." >&2
    exit 1
  fi
  security add-generic-password -s kimi-bridge -a "$label" -w "$value" >/dev/null
  printf '%s' "$value"
}

export DISCORD_TOKEN=$(get_secret discord-token "Discord 봇 토큰")
export MOONSHOT_API_KEY=$(get_secret moonshot-key "Moonshot API 키")

exec .venv/bin/python -m router.bot
