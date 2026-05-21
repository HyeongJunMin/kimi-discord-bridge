# kimi-discord-bridge

Discord 채널/스레드를 통해 로컬 [kimi-cli](https://github.com/MoonshotAI/kimi-cli) 세션을 원격으로 조종할 수 있게 해주는 브릿지 봇입니다. 외출 중이거나 다른 기기에서도 평소 쓰던 kimi 세션에 메시지를 보내고 응답을 받을 수 있습니다.

## 무엇을 하는가

```
[Discord 모바일/웹]  ──(메시지)──▶  [kimi-bridge 봇]  ──(cmux surface)──▶  [로컬 kimi-cli]
       ▲                                                                          │
       └─────────────────(스트리밍 응답)─────────────────────────────────────────┘
```

- 하나의 Discord 스레드 = 하나의 kimi-cli 세션
- 스레드에 메시지를 보내면 로컬 kimi-cli로 전달되고, kimi의 응답이 스레드에 스트리밍됨
- 같은 kimi 세션을 PC 앞 cmux 터미널과 Discord 양쪽에서 동시에 볼 수 있음
- cmux가 잠금 상태에서 terminal surface를 못 만들거나 못 읽으면, 같은 Kimi session id로 `kimi --wire` fallback을 열어 Discord 대화를 계속 처리하고, cmux가 다시 준비되면 `kimi --session <session_id>`로 같은 세션을 surface에 복구

> 이 봇을 왜 직접 만들었는지 (Claude Code Channels / kimi-cli 메신저 통합과의 비교, 운영상 발견한 한계) 는 [docs/WHY.md](docs/WHY.md) 참고.

## 주요 기능

### 슬래시 명령

| 명령 | 설명 |
|---|---|
| `/new` | 새 kimi 세션 + 전용 스레드 생성 |
| `/kill` | 세션 종료. 스레드 안에서 호출 시 해당 스레드 삭제, 텍스트 채널 안에서 호출 시 종료할 스레드를 선택 |
| `/stop` | 진행 중인 kimi 응답 중단 (ESC 전송) |
| `/rename <new_name>` | 현재 스레드 이름과 cmux 탭 이름을 동시에 변경 |
| `/clear` `/yolo` `/model` | kimi-cli에 단축 명령 전달 |
| `/list` `/status` | 활성 세션 조회 (전체 / 현재 스레드) |
| `/attach` | 이미 떠 있는 cmux surface에 사후 연결. 봇 재시작 후 세션 복구에도 사용. tty + 프로세스 매칭으로 진짜 살아있는 kimi-cli만 후보로 산출 |
| `/cleanup` | 고아 스레드 + 좀비 세션 일괄 정리 |
| `/cmux-run` | cmux 데몬이 꺼져있으면 실행 |
| `/rebind` | 현재 세션을 새 스레드로 이전 |

### 자동 동작

- **이미지 첨부 자동 전달** — 스레드에 png/jpg/jpeg/webp/gif 이미지를 올리면 봇이 `/tmp/kimi-uploads/<thread_id>/` 에 저장한 뒤 `@<absolute path>` 형태로 메시지에 끼워 kimi에 전달. 한 장당 10 MiB 상한. 세션 종료 시 디렉터리 정리.
- **cmux 탭 이름 자동 설정** — `/new`, `/attach`, `/rebind` 시 cmux surface 탭이 `<workspace 앞 3글자>-<thread 끝 4자리>` 형식으로 자동 명명. 여러 세션이 떠있을 때 PC 화면에서 식별 용이.
- **잠금 상태 유지 지원** — `SLEEP_GUARD_MODE=always` 또는 `active_sessions` 이면 브릿지가 `caffeinate -imsu` helper process를 실행해 idle/system sleep을 방지합니다. 디스플레이 sleep은 막지 않으므로 잠금 화면이나 화면 꺼짐 상태는 그대로 사용할 수 있습니다.
- **메시지 유실 방지 queue** — Discord에서 받은 사용자 메시지는 먼저 로컬 SQLite 파일(`sessions.sqlite3`, `SESSION_DB_PATH`로 변경 가능)에 `pending` 상태로 저장한 뒤 cmux/kimi에 전달합니다. cmux RPC가 일시적으로 timeout 되면 메시지는 pending으로 남고 worker가 재시도합니다. Discord 가 같은 메시지를 두 번 배달하거나 봇 재시작 직후 워커가 큐를 다시 훑는 상황은 `inbound_message_dedup` 으로 중복 처리를 차단하고, worker 가 메시지를 가져갈 때 row 를 lease 해서 응답이 두 번 가는 일도 막습니다.
- **cmux-first wire fallback** — 기본 경로는 계속 `Discord → bridge → cmux → kimi-cli` 입니다. cmux `send/read`가 실패하고 Kimi session id가 저장돼 있으면 bridge가 `kimi --wire` SDK 세션으로 전환해 같은 메시지를 처리합니다. 잠금해제 후 restore worker가 새 cmux surface를 만들고 `kimi --session <session_id>`를 실행해 cmux 경로로 되돌립니다.
- **오래된 메시지 자동 실행 차단** — Mac이 실제 sleep에 들어갔다 깨어나면 Discord 이벤트가 뒤늦게 들어올 수 있습니다. `QUEUE_MAX_MESSAGE_AGE_SEC` 보다 오래된 메시지는 kimi에 전달하지 않고 `skipped_stale` 로 기록한 뒤 thread에 재전송 안내를 남깁니다.
- **스레드 자동 보관 대응** — Discord가 스레드를 자동으로 archive하면 세션이 종료되고 cmux surface도 정리됨. 보관 해제 시 봇이 복구 방법(`/attach` 또는 `/new`)을 안내.
- **봇 재시작 시 세션 보존** — 봇이 종료되어도 cmux surface는 살려둠. 재시작 후 동일 스레드에서 `/attach`로 재연결 가능.

## 전원 상태 지원 범위

지원:
- 화면 잠금
- 디스플레이 꺼짐
- Mac이 깨어 있고 bridge/cmux/kimi-cli 프로세스가 살아 있는 상태

미지원:
- 실제 system sleep
- 뚜껑 닫힘으로 강제 sleep된 상태
- 전원 꺼짐 또는 네트워크 단절

실제 system sleep 중에는 로컬 프로세스가 실행되지 않으므로 Discord 메시지를 실시간으로 처리할 수 없습니다. 잠금 상태에서 원격 작업을 계속하려면 `SLEEP_GUARD_MODE=always` 를 권장합니다. 세션이 있을 때만 sleep을 막고 싶으면 `active_sessions`, 완전히 끄려면 `off` 를 사용하세요.

외부에서 `/new` 로 새 작업을 시작하려면 세션이 아직 없는 상태에서도 Mac이 깨어 있어야 하므로 `always` 가 가장 안전합니다. 뚜껑은 열어두거나 클램쉘처럼 Mac이 깨어 있는 구성을 사용하세요.

### 권장 전원 설정 (이중 안전망)

봇만으로는 봇이 죽는 순간 sleep 방지가 풀립니다. macOS 자체 토글도 같이 켜두면 봇 다운 중에도 Mac 이 깨어 있어 외부 ssh / launchd 등으로 봇을 다시 띄울 수 있습니다.

| 레이어 | 설정 | 효과 |
|---|---|---|
| 봇 | `SLEEP_GUARD_MODE=always` | 봇 실행 중 `caffeinate -imsu` 로 idle/system/disk sleep 차단 |
| macOS | 시스템 설정 → 배터리 → 옵션 → **"디스플레이가 꺼져 있을 때 전원 어댑터 사용 시 컴퓨터를 자동으로 잠자지 않게 하기"** ON | 봇이 죽어도 Mac 은 깨어 있음 (전원 어댑터 연결 시) |
| 하드웨어 | 전원 어댑터 연결 | 위 두 옵션 모두 AC 전원 전제 |

확인:
```bash
pmset -g assertions | grep -E "PreventUserIdleSystemSleep|PreventSystemSleep"
pmset -g | grep -E "^\s+(sleep|displaysleep)\s"
```
`kimi-bridge` 의 caffeinate 가 assertion 을 잡고 있어야 하고, `sleep` 이 0 이거나 그 토글이 잡혀 있어야 합니다.

## 사전 요구사항

- **macOS** (최근 버전, cmux가 Mac 전용 앱입니다)
- **Python 3.10 이상**
- **Node.js 20 이상** — Kimi Agent SDK wire fallback helper 실행용
- **[cmux.app](https://cmux.io/)** — `/Applications/cmux.app`에 설치되어 있어야 함
- **[kimi-cli](https://github.com/MoonshotAI/kimi-cli)** — `kimi` 명령이 PATH에 있어야 함
- **Discord Developer 계정** — 봇 토큰 발급 가능
- **Moonshot API 키** (kimi-cli 모델 인증용)

## 설치/실행

설치 과정은 단계가 많아 직접 따라 하기 번거롭습니다. 이 저장소에는 AI 코딩 도구(예: Claude Code, Cursor)가 사용자를 단계별로 안내하도록 설계된 가이드 문서가 들어있습니다.

**[docs/INSTALL.md](docs/INSTALL.md)** 파일을 Claude Code 같은 코딩 도구에 통째로 던지고 "이대로 설치 진행해줘"라고 요청하세요. 도구가 각 단계마다 필요한 입력을 물어보고 명령을 실행해서 끝까지 데려갑니다.

수동으로 진행하고 싶다면 같은 문서를 직접 따라 읽어도 됩니다.

### 비밀(토큰/API 키) 관리
- Discord 봇 토큰과 Moonshot API 키는 **macOS Keychain** 에 저장됩니다. `.env` 에는 비밀이 들어가지 않아 AI 도구가 작업 도중 `.env` 를 읽어도 비밀이 도구 컨텍스트로 흘러들지 않습니다.
- 봇은 동봉된 `run-bot.sh` 로 실행합니다. 최초 1회만 두 비밀을 프롬프트로 입력받아 Keychain 에 저장하고, 이후엔 silent 시작.
- 토큰 회전: `security delete-generic-password -s kimi-bridge -a discord-token` 또는 `-a moonshot-key` 후 `./run-bot.sh` 재실행.

## 트러블슈팅

- `/new`를 눌렀는데 응답이 없음 → 봇 인스턴스가 여러 개 떠 있는지 확인 (같은 토큰 공유 시 interaction 충돌)
- `cmux 호출 실패` → cmux.app이 떠 있지 않음. Discord에서 `/cmux-run` 호출 또는 직접 cmux.app 실행
- 메시지를 보냈는데 kimi에 늦게 들어감 → `/status`에서 `queue: pending=N`, `max_age`, `last_error` 확인. cmux timeout 등 짧은 일시 장애면 pending 메시지가 보존되고 worker가 재시도함. 오래된 메시지는 wake 후 자동 실행하지 않고 `skipped_stale` 처리됨.
- 첫 메시지 응답이 안 옴 → 보통 cmux surface 가 `wire.jsonl` 을 만들기 전이라 잠깐 대기 중. 잠금 상태처럼 cmux 가 응답을 줄 수 없는 환경이면 봇이 자동으로 `kimi --wire` SDK fallback 으로 전환해 답을 준다 (`/status` 에 `wire fallback` 표시). 그래도 60초 이상 무응답이면 cmux 창에서 해당 surface 를 열어 kimi-cli TUI 가 살아있는지 확인.
- `/attach`가 "연결할 수 있는 kimi surface를 찾지 못했어요"라고 응답 (분명히 surface가 떠 있는데) → 봇 재시작 직후 흔한 케이스. registry에 `status='active'`인 좀비 row가 남아있어 후보에서 제외됨. 같은 스레드에서 다시 `/attach`로 재연결되거나, 텍스트 채널에서 `/cleanup`으로 좀비 정리.
- 이미지 첨부를 올렸는데 kimi가 못 읽음 → 봇 로그의 `rejection` 라인 확인. 확장자(`.png/.jpg/.jpeg/.webp/.gif`)와 10 MiB 상한 체크.

## 개발자용 문서

- [`docs/WHY.md`](docs/WHY.md) — 봇 개발 배경, 기존 도구 (Claude Code Channels, kimi-cli) 와의 비교, 한계 분석
- [`router/README.md`](router/README.md) — 라우터 내부 구조, 모듈별 책임 설명

## 라이선스

MIT
