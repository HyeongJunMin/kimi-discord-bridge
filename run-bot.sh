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

# 이중 기동 방지. 두 봇이 같은 sqlite 를 동시에 보면 restore_worker 가
# 두 배로 돌아 cmux surface 폭주를 만든다 (실제로 65개 누적된 사고 있음).
if pgrep -f "router\.bot" >/dev/null 2>&1; then
  echo "❌ router.bot already running:" >&2
  pgrep -fl "router\.bot" >&2
  echo "kill it first: pkill -9 -f router.bot" >&2
  exit 1
fi

# nohup/launchd 로 띄우면 PATH 가 stock /usr/bin:/bin 으로 축약된다.
# - brew node (/opt/homebrew/bin) → wire helper spawn
# - kimi-cli (~/.local/bin) → wire helper 의 SDK 가 spawn 하는 kimi 바이너리
# 둘 중 하나라도 없으면 wire fallback 이 ENOENT 로 죽고 restore_worker 가
# 재시도 폭주를 일으킨다.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:${PATH:-/usr/bin:/bin}"

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
