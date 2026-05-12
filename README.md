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
- **스레드 자동 보관 대응** — Discord가 스레드를 자동으로 archive하면 세션이 종료되고 cmux surface도 정리됨. 보관 해제 시 봇이 복구 방법(`/attach` 또는 `/new`)을 안내.
- **봇 재시작 시 세션 보존** — 봇이 종료되어도 cmux surface는 살려둠. 재시작 후 동일 스레드에서 `/attach`로 재연결 가능.

## 사전 요구사항

- **macOS** (최근 버전, cmux가 Mac 전용 앱입니다)
- **Python 3.10 이상**
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
- 첫 메시지 응답이 안 옴 → 봇이 wire.jsonl을 못 찾는 상황. 60초 기다리면 에러 메시지가 뜨고, 두 번째 메시지부터 정상화됨 (한 번 보낸 첫 메시지는 cmux 화면에서 확인 가능)
- `/attach`가 "연결할 수 있는 kimi surface를 찾지 못했어요"라고 응답 (분명히 surface가 떠 있는데) → 봇 재시작 직후 흔한 케이스. registry에 `status='active'`인 좀비 row가 남아있어 후보에서 제외됨. 같은 스레드에서 다시 `/attach`로 재연결되거나, 텍스트 채널에서 `/cleanup`으로 좀비 정리.
- 이미지 첨부를 올렸는데 kimi가 못 읽음 → 봇 로그의 `rejection` 라인 확인. 확장자(`.png/.jpg/.jpeg/.webp/.gif`)와 10 MiB 상한 체크.

## 개발자용 문서

- [`router/README.md`](router/README.md) — 라우터 내부 구조, 모듈별 책임 설명

## 라이선스

MIT
