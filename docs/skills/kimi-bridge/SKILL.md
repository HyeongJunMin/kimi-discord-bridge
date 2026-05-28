---
name: kimi-bridge
description: kimi-discord-bridge 봇 실행/상태/종료 제어. /kimi-bridge 호출 시 AskUserQuestion 으로 액션 선택.
user-invocable: true
version: 1.0.0
---

**필수: 이 스킬이 호출되면 가장 먼저 §1 의 AskUserQuestion 을 반드시 호출한다. 설명 텍스트나 요약을 먼저 출력하지 말 것.**

kimi-discord-bridge 봇을 한 스킬에서 start / status / stop.

## Usage

```
/kimi-bridge
```

## Constants

- 프로젝트 경로: `/Users/minhyeongjun/IdeaProjects/kimi-hub/kimi-discord-bridge-acp`
- 실행 스크립트: `./run-bot.sh` (Keychain 에 비밀 등록되어 있으면 silent 시작)
- 로그 파일: `bot.log`
- PID 파일: `router.bot.pid`
- 프로세스 패턴: `router.bot` (`python -m router.bot`)

## Instructions

### 1. 액션 선택 (AskUserQuestion 필수)

처음 호출 시 args 가 비어 있거나 `start|status|stop` 중 하나가 아니면 AskUserQuestion 으로 묻는다:

```
question: "어떤 작업을 할까요?"
header: "Action"
options:
  - label: "Status (Recommended)"
    description: "봇 프로세스/로그 현재 상태 확인 — 안전, 부작용 없음"
  - label: "Start"
    description: "봇을 백그라운드로 띄움. 이미 떠있으면 알려주고 종료."
  - label: "Stop"
    description: "봇 프로세스 종료. caffeinate sleep guard 도 같이 정리됨."
```

사용자가 선택하면 해당 분기로 진행. args 에 `start|status|stop` 가 직접 들어왔으면 묻지 말고 바로 실행.

### 2. Status 분기

```bash
cd /Users/minhyeongjun/IdeaProjects/kimi-hub/kimi-discord-bridge-acp

# 프로세스
PIDS=$(pgrep -f "python.*router\.bot" | tr '\n' ' ')
CAFFEINATE_PID=$(pgrep -f "caffeinate -imsu" | head -1)
PID_FILE=$(cat router.bot.pid 2>/dev/null || echo "(none)")

# 로그 마지막 10줄
tail -10 bot.log 2>/dev/null
```

출력 형식:
```
**kimi-bridge status**

| 항목 | 값 |
|---|---|
| router.bot PID | <PIDS or "(not running)"> |
| caffeinate PID | <CAFFEINATE_PID> |
| router.bot.pid 파일 | <PID_FILE> |
| 봇 살아있음? | ✅ / ❌ |

**최근 로그 (tail -10 bot.log)**
```
<로그>
```
```

PID 파일에 박힌 값이 실제 살아있는 프로세스랑 다르면 "stale pid file" 경고.

### 3. Start 분기

먼저 이미 떠 있는지 확인:
```bash
pgrep -f "python.*router\.bot"
```
1개 이상 떠있으면:
> ⚠️ 봇이 이미 실행 중입니다 (PID=...). 재시작하려면 먼저 Stop 후 Start 하세요.
종료.

안 떠있으면:
```bash
cd /Users/minhyeongjun/IdeaProjects/kimi-hub/kimi-discord-bridge-acp
rm -f router.bot.pid
nohup ./run-bot.sh > bot.log 2>&1 &
sleep 4
```

이후 검증:
```bash
pgrep -f "python.*router\.bot"
tail -15 bot.log
```

`bot online:` 라인이 보이면 ✅. 안 보이면 로그 마지막 부분과 함께 실패 보고. Keychain 에 비밀이 등록되어 있지 않으면 silent 시작 못 함 — `🔐` 프롬프트가 nohup 환경에서 막혀 hang 됨. 그 경우 "사용자 터미널에서 직접 `./run-bot.sh` 한 번 실행하여 비밀 입력 필요" 안내.

### 4. Stop 분기

```bash
PIDS=$(pgrep -f "python.*router\.bot")
```
안 떠있으면 "이미 종료 상태" 보고 후 종료.

떠있으면:
```bash
pkill -f "python.*router\.bot"
sleep 2
# 잔존 확인
pgrep -f "python.*router\.bot"
pgrep -f "caffeinate -imsu"  # 보통 봇이 자식으로 정리하지만 검증
```

router.bot 이 죽으면 sleep_guard 가 caffeinate 자식을 같이 정리한다. 5초 후에도 caffeinate 가 남아있으면 orphan — `kill <pid>` 로 직접 정리하고 사용자에게 알림.

종료 후 `router.bot.pid` 파일은 stale 이므로 그대로 두거나 정리:
```bash
rm -f router.bot.pid
```

## 분기별 출력 톤

- Status: 표 + 로그. 사실만.
- Start: "▶️ 시작 중... PID=N · bot online ✅" / 실패 시 로그 마지막 보여주고 원인 추정 (LoginFailure / KeychainMissing 등)
- Stop: "⏹ 종료 — PID=N 정리, caffeinate=정리됨"

## 주의

- 비밀 출력 금지: `ps eww`, `security ... -w`, `printenv` 등 사용 금지 (CLAUDE.md 참고).
- bot.log 인용 시 토큰 패턴 (`MTI...`, `sk-...`) 보이면 `***` 로 마스킹.
- `git add` / 커밋 같은 부수 작업 하지 말 것. 이 스킬은 봇 라이프사이클만 관여.
