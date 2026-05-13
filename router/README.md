# router — Discord ↔ kimi-cli ACP bridge MVP

## What this is
Spawns one `kimi acp` per Discord thread, multiplexed by a single bot.
Bypasses kimi-cli's OAuth gate via PYTHONPATH-injected sitecustomize patch
so authentication uses `MOONSHOT_API_KEY` only.

## Layout
- `acp_client.py`     — async JSON-RPC client over `kimi acp` stdio
- `auth_bypass/`      — PYTHONPATH'd sitecustomize patch (no global mutation)
- `cmux_client.py`    — `cmux rpc` wrapper for workspace.* / surface.*
- `registry.py`       — sqlite session table (thread_id ↔ acp session)
- `sleep_guard.py`    — optional `caffeinate -imsu` helper while sessions are active
- `discord_relay.py`  — per-thread debounce + edit-roll-over output
- `bot.py`            — entry point: `/new`, message routing, permission buttons

## Prerequisites
- **cmux.app** (macOS) — `/Applications/cmux.app` 설치 필요. 봇은 cmux의 Unix
  socket RPC(`cmux rpc workspace.list` 등)에 의존합니다. 데몬이 꺼져 있으면
  Discord에서 `/cmux-run` 슬래시 커맨드로 실행 가능 (`open -a cmux` 호출).
- **kimi-cli** — `MOONSHOT_API_KEY`로 인증.

## Run
```sh
pip install discord.py python-dotenv
export DISCORD_BOT_TOKEN=...
export MOONSHOT_API_KEY=sk-...   # kimi-cli reads this for model auth
export GUILD_ID=...              # optional: guild-only sync (faster)
python -m router.bot
```

## Slash commands
- `/new`           새 kimi 세션 + Discord thread 생성
- `/kill`          세션 종료 (thread 내부/외부 모두)
- `/stop`          진행 중인 kimi 응답 중단 (ESC 전송)
- `/rename`        현재 thread 이름 + cmux 탭 이름 동시 변경
- `/list`          내 활성 세션 목록
- `/status`        현재 thread 세션 상세
- `/clear`,`/yolo`,`/model`  kimi-cli 측에 단축 명령 전달
- `/attach`        기존 cmux surface에 연결 (tty + 프로세스 매칭으로 살아있는 kimi-cli만 후보로)
- `/cleanup`       고아 thread(미등록 또는 좀비 세션) 일괄 삭제
- `/rebind`        현재 세션을 새 thread로 이전
- `/cmux-run`      cmux 데몬이 꺼져 있으면 실행

## Implemented since initial MVP
- 이미지 첨부 자동 전달 (`png/jpg/jpeg/webp/gif`, 한 장 ≤10 MiB → `@<abspath>` 로 kimi 에 패스)
- 봇 종료 시 cmux surface 보존 + 재시작 후 `/attach` 로 재연결 (restart recovery)
- Discord 메시지 durable queue (SQLite `inbound_messages`) + worker 재시도
- 활성 세션 동안 macOS idle/system sleep 방지 (`PREVENT_SLEEP_WHILE_ACTIVE=1`)
- /yolo 슬래시 명령 (availableCommands 발견 없이 직접 ESC seq 전송)
- cmux 탭 이름 자동 명명 (`<workspace[:3]>-<thread_id 끝 4자리>`)

## Out-of-scope (deferred)
- Multi-user ACL beyond owner-only
- Monitor surface in cmux (read-only viewer)
- /afk, /compact slash commands surfaced from availableCommands
- ACP `session/load` to resume a saved session

## Known caveats observed during probe
- `--config '{"default_yolo": false}'` did not actually disable yolo in
  smoke tests; tools auto-approved despite the override. We ship with
  `yolo=False` configured but emit a warning if `session/request_permission`
  never fires for a destructive prompt — would need to investigate
  kimi-cli config merge order.
- ACP `tool_call_update` streams arg JSON one character at a time
  (~100 events per call). We suppress `in_progress` updates and only
  surface `tool_call` start + `completed`/`failed` end states.
