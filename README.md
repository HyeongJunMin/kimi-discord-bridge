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

| 슬래시 명령 | 설명 |
|---|---|
| `/new` | 새 kimi 세션 + 전용 스레드 생성 |
| `/kill` | 세션 종료 + 스레드 자동 삭제 |
| `/stop` | 진행 중인 kimi 응답 중단 (ESC 전송) |
| `/clear` `/yolo` `/model` | kimi-cli에 단축 명령 전달 |
| `/list` `/status` | 활성 세션 조회 |
| `/attach` | 이미 떠 있는 cmux surface에 사후 연결 |
| `/cleanup` | 고아 스레드 + 좀비 세션 일괄 정리 |
| `/cmux-run` | cmux 데몬이 꺼져있으면 실행 |
| `/rebind` | 현재 세션을 새 스레드로 이전 |

## 사전 요구사항

- **macOS** (cmux가 Mac 전용 앱입니다)
- **Python 3.10 이상**
- **[cmux.app](https://cmux.io/)** — `/Applications/cmux.app`에 설치되어 있어야 함
- **[kimi-cli](https://github.com/MoonshotAI/kimi-cli)** — `kimi` 명령이 PATH에 있어야 함
- **Discord Developer 계정** — 봇 토큰 발급 가능
- **Moonshot API 키** (kimi-cli 모델 인증용)

## 설치/실행

설치 과정은 단계가 많아 직접 따라 하기 번거롭습니다. 이 저장소에는 AI 코딩 도구(예: Claude Code, Cursor)가 사용자를 단계별로 안내하도록 설계된 가이드 문서가 들어있습니다.

**[docs/INSTALL.md](docs/INSTALL.md)** 파일을 Claude Code 같은 코딩 도구에 통째로 던지고 "이대로 설치 진행해줘"라고 요청하세요. 도구가 각 단계마다 필요한 입력을 물어보고 명령을 실행해서 끝까지 데려갑니다.

수동으로 진행하고 싶다면 같은 문서를 직접 따라 읽어도 됩니다.

## 트러블슈팅

- `/new`를 눌렀는데 응답이 없음 → 봇 인스턴스가 여러 개 떠 있는지 확인 (같은 토큰 공유 시 interaction 충돌)
- `cmux 호출 실패` → cmux.app이 떠 있지 않음. Discord에서 `/cmux-run` 호출 또는 직접 cmux.app 실행
- 첫 메시지 응답이 안 옴 → 봇이 wire.jsonl을 못 찾는 상황. 60초 기다리면 에러 메시지가 뜨고, 두 번째 메시지부터 정상화됨 (한 번 보낸 첫 메시지는 cmux 화면에서 확인 가능)

## 개발자용 문서

- [`router/README.md`](router/README.md) — 라우터 내부 구조, 모듈별 책임 설명

## 라이선스

MIT
