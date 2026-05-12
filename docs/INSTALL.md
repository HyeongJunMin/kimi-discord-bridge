# kimi-discord-bridge 설치 가이드 (AI 코딩 도구용)

> 이 문서는 사람이 직접 따라 하기보다는, Claude Code / Cursor 같은 AI 코딩 도구에게 통째로 던져서 "이 문서대로 사용자를 안내해줘"라고 요청하는 용도로 작성되었습니다. 도구가 각 단계의 입력을 사용자에게 묻고, 명령을 대신 실행하고, 검증까지 끝낸 뒤 다음 단계로 넘어가도록 설계되어 있습니다.

---

## 가이드 도구(LLM)에게 — 작업 원칙

1. **단계별로 진행하라.** 한 단계를 끝낸 뒤에만 다음 단계로 넘어간다. 단계 안의 **검증(Verify)** 항목이 통과하지 못하면 다음 단계로 진행하지 말고 원인을 파악해서 사용자에게 보고하라.
2. **사용자에게 물어야 할 입력 값은 단계 본문에 명시되어 있다.** 필요한 값은 한 번에 모아 묻지 말고, 그 단계에 도달했을 때 묻는다 (앞 단계에서 결정된 값을 미리 묻지 않는다).
3. **명령은 사용자 셸 환경에 직접 실행하라.** 출력은 사용자에게 보여주고, 에러가 나면 무시하지 말고 분석해서 보고하라.
4. **파괴적이거나 외부에 영향을 주는 작업** (Discord 봇 초대, GitHub push 등)은 직전에 한 번 더 확인을 받아라.
5. **이 문서에 없는 임의의 단계를 추가하거나 우회하지 마라.** 누락된 정보가 있으면 사용자에게 명시적으로 묻고, 그 답을 기준으로 진행하라.

---

## 단계 0 — 전제 환경 확인

### 목표
사용자의 환경이 이 봇을 돌릴 수 있는 최소 조건을 갖췄는지 확인한다.

### 검증 명령 (도구가 실행)
```bash
sw_vers -productName            # macOS 인지 확인
python3 --version               # 3.10 이상이어야 함
ls /Applications/cmux.app       # 존재해야 함
which kimi                      # 비어 있으면 안 됨
git --version                   # 클론에 필요
```

### 분기
- `sw_vers` 결과가 `macOS`가 아니면 → **중단**. 이 봇은 macOS 전용임을 사용자에게 알리고 진행하지 않는다.
- python이 3.10 미만이면 → 사용자에게 업그레이드를 안내하고 진행하지 않는다 (`brew install python@3.12` 등).
- `/Applications/cmux.app`이 없으면 → [https://cmux.io](https://cmux.io)에서 설치하라고 안내하고 사용자가 설치한 뒤에 진행한다.
- `which kimi`가 비면 → kimi-cli 설치를 안내한다: `uv tool install kimi-cli` 또는 공식 설치 가이드 ([https://github.com/MoonshotAI/kimi-cli](https://github.com/MoonshotAI/kimi-cli)). 설치 후 이 단계를 다시 검증한다.

---

## 단계 1 — 저장소 클론 및 작업 디렉터리 결정

### 사용자에게 물을 것
- 어디에 클론할까요? (기본 제안: `~/IdeaProjects/kimi-discord-bridge`)

### 도구가 실행
```bash
git clone https://github.com/HyeongJunMin/kimi-discord-bridge <chosen-path>
cd <chosen-path>
```

### 검증
- `ls router/bot.py docs/INSTALL.md` 두 파일 모두 존재.

---

## 단계 2 — 파이썬 가상환경 + 의존성 설치

### 도구가 실행
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 검증
```bash
.venv/bin/python -c "import discord, dotenv; print(discord.__version__)"
```
- 버전이 출력되면 OK. 에러나면 pip 출력을 보고 원인 보고.

---

## 단계 3 — Discord 봇 만들기

> 이 단계는 사용자가 브라우저에서 직접 작업해야 한다. 도구는 사용자가 각 화면에서 무엇을 클릭/복사할지 정확히 안내하고, 결과 값(토큰, ID)을 받아 다음 단계에서 사용한다.

### 사용자에게 안내할 내용

1. [https://discord.com/developers/applications](https://discord.com/developers/applications) 접속 → **New Application** → 이름(예: `kimi-bridge`) → Create.
2. 좌측 메뉴 **Bot** → **Reset Token** → 토큰 복사. **이 토큰은 1회만 표시된다.** 도구에게 전달.
3. 같은 화면에서 **Privileged Gateway Intents** → **Message Content Intent** 토글을 **켠다**.
4. 좌측 **OAuth2 → URL Generator** →
   - Scopes: `bot`, `applications.commands` 둘 다 체크
   - Bot Permissions:
     - Send Messages
     - Send Messages in Threads
     - Create Public Threads
     - Manage Threads
     - Read Message History
     - Embed Links
     - Use Slash Commands
   - 하단의 Generated URL을 새 탭에서 열고 자신의 서버를 선택하여 봇을 초대한다.
5. (선택) 빠른 슬래시 명령 sync를 위해 Discord 서버 ID(Guild ID)를 알려달라고 사용자에게 요청.
   - Discord 클라이언트의 개발자 모드를 켠 뒤 서버를 우클릭 → ID 복사.

### 도구가 받아야 할 값
- `DISCORD_TOKEN` (필수)
- `DISCORD_GUILD_ID` (옵션, 권장)

---

## 단계 4 — Moonshot API 키

### 사용자에게 물을 것
- `MOONSHOT_API_KEY` 가지고 계신가요? (없으면 [https://platform.moonshot.ai](https://platform.moonshot.ai) 에서 발급 안내)

### 도구가 받아야 할 값
- `MOONSHOT_API_KEY` (필수)

---

## 단계 5 — `.env` 파일 생성

### 도구가 실행
1. `.env.example`을 복사하여 `.env`를 만든다.
   ```bash
   cp .env.example .env
   ```
2. 단계 3, 4에서 받은 값을 채워 넣는다. **Edit 도구를 사용해서 정확히 다음 키만 치환**한다 (다른 키는 손대지 않는다):
   - `DISCORD_TOKEN=` → `DISCORD_TOKEN=<단계3에서 받은 토큰>`
   - `DISCORD_GUILD_ID=` → 사용자가 알려준 경우에만 채움
   - (필요시) `DEFAULT_WORK_DIR=` → 사용자에게 기본 작업 디렉터리를 묻는다 (기본 제안: `$HOME/IdeaProjects`)
3. `MOONSHOT_API_KEY`는 `.env`에 넣어도 되고, 사용자 셸 rc에 넣어도 된다. 도구는 사용자에게 선택지를 주고 진행한다.
   - 옵션 A: `.env`에 `MOONSHOT_API_KEY=sk-...` 한 줄 추가 (kimi-cli가 이 봇과 같은 프로세스 트리에서 환경변수를 상속받으므로 동작함)
   - 옵션 B: `~/.zshrc` 등에 `export MOONSHOT_API_KEY=sk-...` 추가 후 새 셸에서 봇 실행

### 검증
```bash
grep -E '^DISCORD_TOKEN=.+' .env >/dev/null && echo OK
```

---

## 단계 6 — cmux 실행 확인

### 도구가 실행
```bash
/Applications/cmux.app/Contents/Resources/bin/cmux rpc workspace.list
```

### 분기
- JSON이 잘 출력되면 cmux 데몬이 떠 있음 → 다음 단계.
- `cmux rpc ... failed` 또는 timeout이면 cmux 앱이 안 떠 있는 것:
  ```bash
  open -a cmux
  ```
  사용자가 화면에서 cmux 창이 뜨는 것을 확인한 뒤, 위 검증 명령을 다시 실행해 OK가 되는지 확인.

---

## 단계 7 — 봇 첫 실행

### 도구가 실행
```bash
.venv/bin/python -m router.bot
```

### 정상 시작 시 보이는 출력
```
... INFO router.bot: synced N commands to guild ...
... INFO router.bot: bot online: <봇 이름>#1234
```

### 분기
- `DISCORD_TOKEN is not set` → 단계 5의 .env가 제대로 안 채워짐. 도구가 직접 점검.
- `LoginFailure` / `Improper token` → 토큰이 틀림. 단계 3-2에서 받은 값 재확인.
- `bot online` 까지 뜨면 OK. 사용자에게 "Discord 서버에 봇이 온라인 상태로 보이는지" 확인 요청.

> 이 단계 이후 봇 프로세스는 **백그라운드에서 계속 돌아야** 한다. 도구는 사용자에게 `nohup .venv/bin/python -m router.bot > bot.log 2>&1 &` 같은 방식으로 백그라운드 실행 옵션을 제시하고, 사용자의 선택대로 진행한다.

---

## 단계 8 — Discord에서 첫 세션 만들기 (검증)

### 사용자에게 안내
1. 봇을 초대한 서버에서 텍스트 채널을 하나 골라(예: #general) `/new` 입력.
2. 워크스페이스 선택 dropdown이 뜨면 **새 워크스페이스 생성** 또는 기존 항목 선택.
3. 잠시 후 `kimi-...-NNNN` 이름의 스레드가 생기고 **"준비 완료. session=… · cmux surface:N"** 메시지가 뜬다.
4. 스레드에 `hi` 한 줄 보내본다.
5. 1~수십 초 후 kimi의 응답이 스레드에 뜬다.

### 분기
- 스레드는 생겼는데 응답이 안 옴 → cmux 창에서 해당 surface를 열어 kimi-cli TUI가 정상 동작하는지 직접 확인. 첫 메시지의 wire.jsonl 생성에는 시간이 좀 걸릴 수 있으므로 60초 정도 기다린다.
- `cmux 호출 실패` → 단계 6 다시.
- `워크스페이스 선택`이 안 뜨고 에러 → 봇 로그 확인.

### 완료
이 단계까지 통과하면 설치 완료. 사용자에게 다음을 안내한다:
- 일상 사용: `/new`로 세션 시작 → 스레드에서 대화 → 끝나면 `/kill`
- 응답이 길어 보이거나 잘못된 방향이면 `/stop`으로 중단
- 봇을 영구적으로 띄워두려면 `launchd`나 `pm2` 등을 사용한 데몬화를 추가로 안내 (이 문서 범위 밖)

---

## 부록 A — 환경 변수 레퍼런스

| 변수 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `DISCORD_TOKEN` | ✓ | — | Discord 봇 토큰 |
| `MOONSHOT_API_KEY` | ✓ | — | kimi-cli 모델 인증 |
| `DISCORD_GUILD_ID` | | — | 설정 시 해당 길드에만 명령을 sync (빠름, 권장) |
| `DEFAULT_WORK_DIR` | | `$HOME` | 새 워크스페이스의 기본 cwd |
| `CMUX_CMD` | | `cmux` | cmux 바이너리 경로 (PATH에 없을 때만 절대경로 지정) |
| `SESSION_DB_PATH` | | `router.sqlite3` | 세션 메타데이터 sqlite 파일 |

---

## 부록 B — 자주 묻는 문제

**Q. 봇이 여러 개 떠 있다는 경고를 봇 로그에서 봤다.**
같은 토큰으로 두 개 이상의 프로세스가 떠 있으면 Discord interaction이 충돌해서 일부 `/new`가 "Unknown interaction" 에러로 죽는다. `ps aux | grep router.bot`으로 확인하고 잉여 프로세스를 종료한다.

**Q. `/cleanup`이 멀쩡한 thread를 좀비로 판정한다.**
방금 만든 세션이 cmux로 전파되기 전(30초 이내)이면 보호 grace period에 들어가 좀비 판정에서 제외된다. 그래도 false positive가 의심되면 봇 로그의 `cmux preflight failed` 라인을 확인.

**Q. 토큰을 노출했다.**
즉시 Developer Portal에서 **Reset Token**을 누르고 `.env`를 새 토큰으로 갱신한 뒤 봇 재시작.
