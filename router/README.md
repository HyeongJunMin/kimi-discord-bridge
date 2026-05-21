# router — Discord ↔ kimi-cli bridge (cmux + wire hybrid)

## What this is
Discord 스레드 한 개 = kimi-cli 세션 한 개로 묶어 양방향 중계하는 봇.
기본 경로는 cmux.app 의 surface 에 키 입력을 보내고 `~/.kimi/wire.jsonl`
을 tail 해서 응답을 받는 구조이며, cmux 가 잠금/sleep 으로 응답을 못 줄
때는 동일 Kimi session id 로 `kimi --wire` SDK 세션을 띄워 fallback 으로
처리한다. 잠금이 풀리면 같은 session id 를 다시 cmux surface 로 복구한다.

인증은 PYTHONPATH 로 주입되는 sitecustomize 패치가 kimi-cli 의 OAuth 게이트를
우회시켜 `MOONSHOT_API_KEY` 만으로 동작하게 한다.

## Layout
- `bot.py`            — 엔트리포인트. 슬래시 명령, 메시지 라우팅, restore worker, singleton lock(`BOT_PID_FILE`)
- `cmux_client.py`    — `cmux rpc` 래퍼 (workspace.* / surface.*)
- `surface_io.py`     — surface 키 입력 송신 (bracketed paste, multiline 보존)
- `wire_tail.py`      — `~/.kimi/wire.jsonl` tail → 이벤트 디코드
- `kimi_wire.py`      — `kimi --wire` SDK 기반 fallback 클라이언트 (Python ↔ Node helper 브리지)
- `kimi_wire_bridge.mjs` — `@moonshot/kimi-cli` SDK 를 호출하는 Node helper (stdio JSON 프로토콜)
- `registry.py`       — sqlite 세션 테이블 + `inbound_messages` durable queue + `inbound_message_dedup`
- `sleep_guard.py`    — `caffeinate -imsu` 라이프사이클 관리 (`off`/`active_sessions`/`always`)
- `discord_relay.py`  — per-thread debounce + edit-roll-over 출력
- `auth_bypass/`      — PYTHONPATH 로 주입되는 sitecustomize 패치 (글로벌 변형 없음)

## Prerequisites
- **cmux.app** (macOS) — `/Applications/cmux.app`. 봇은 cmux 의 Unix socket RPC
  (`cmux rpc workspace.list` 등)에 의존. 꺼져 있으면 `/cmux-run` 으로 기동.
- **kimi-cli** — `MOONSHOT_API_KEY` 로 인증.
- **Node.js 20+** — `kimi_wire_bridge.mjs` (Kimi Agent SDK wire fallback) 실행용.

## Run
`run-bot.sh` 가 macOS Keychain 에서 비밀(`DISCORD_TOKEN`, `MOONSHOT_API_KEY`)
을 꺼내 봇 프로세스에만 export 한다. 최초 1회 프롬프트 입력 후 silent.
자세한 설치 절차는 [`docs/INSTALL.md`](../docs/INSTALL.md).

```sh
pip install -r requirements.txt
# .env 에 DISCORD_GUILD_ID, DEFAULT_WORK_DIR 등 비밀 *아닌* 값만 채움
./run-bot.sh
```

## Slash commands
- `/new`           새 kimi 세션 + Discord thread 생성
- `/kill`          세션 종료 (thread 내부/외부 모두)
- `/stop`          진행 중인 kimi 응답 중단 (ESC 전송)
- `/rename`        현재 thread 이름 + cmux 탭 이름 동시 변경
- `/list`          내 활성 세션 목록
- `/status`        현재 thread 세션 상세 (queue/dedup/wire fallback 상태 포함)
- `/clear`,`/yolo`,`/model`  kimi-cli 측에 단축 명령 전달
- `/attach`        기존 cmux surface 에 연결 (tty + 프로세스 매칭으로 살아있는 kimi-cli 만 후보로)
- `/cleanup`       고아 thread(미등록 또는 좀비 세션) 일괄 삭제
- `/rebind`        현재 세션을 새 thread 로 이전
- `/cmux-run`      cmux 데몬이 꺼져 있으면 실행

## Hybrid backend (cmux + wire fallback)

기본 경로:
```
Discord → bot.py → surface_io (cmux send) → kimi-cli TUI → wire.jsonl → wire_tail → Discord
```

cmux RPC 가 실패하고 `sessions.kimi_session_id` 가 저장돼 있으면:
```
Discord → bot.py → kimi_wire.KimiWireClient → kimi_wire_bridge.mjs → @moonshot/kimi-cli SDK
        ← (assistant/tool 이벤트) ←
```
잠금이 풀리면 `bot.py` 의 restore worker(`RESTORE_WATCH_INTERVAL_SEC` 주기)
가 새 cmux surface 를 만들고 `kimi --session <session_id>` 를 실행해 같은
세션을 cmux 경로로 되돌린다. 중간에 surface 가 누락되거나 응답이 중복되는
경우를 막기 위한 lease/dedup 로직이 함께 동작한다.

## 메시지 dedup & 큐
- 인바운드 Discord 메시지는 `inbound_messages` 테이블에 `pending` 으로 저장된 뒤 worker 가 cmux/wire 로 전달한다.
- 같은 Discord message id 가 두 번 들어오면 `inbound_message_dedup` 가 두 번째 처리를 차단해 wire 응답이 두 번 가는 일을 막는다.
- worker 가 메시지를 가져갈 땐 row 를 lease 해서 다른 worker/재시작 직후 인스턴스가 중복 전송하지 않도록 보호한다.
- `QUEUE_MAX_MESSAGE_AGE_SEC` 보다 오래된 메시지는 `skipped_stale` 로 마킹하고 thread 에 재전송 안내를 남긴다.

## Out-of-scope (deferred)
- Multi-user ACL beyond owner-only
- Monitor surface in cmux (read-only viewer)
- `/afk`, `/compact` slash commands surfaced from availableCommands

## Known caveats
- `--config '{"default_yolo": false}'` 만으로는 yolo 가 실제 비활성화되지 않음 (smoke 시 tools auto-approved 관찰). kimi-cli config 병합 순서 관련.
- wire.jsonl 첫 이벤트가 생성되기 전에 첫 메시지가 도착하면 wire_tail 이 잠시 대기. 첫 응답이 늦어 보일 수 있음.
