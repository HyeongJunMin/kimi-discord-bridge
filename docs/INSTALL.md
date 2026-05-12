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
     - View Channels
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

## 단계 4 — Moonshot API 키 확보

### 사용자에게 안내
- `MOONSHOT_API_KEY` 가지고 계신가요? (없으면 [https://platform.moonshot.ai](https://platform.moonshot.ai) 에서 발급 안내)
- **이 단계에서 도구에게 키 값을 전달하지 마세요.** 직접 갖고 계시다가 단계 7 에서 봇이 프롬프트할 때 입력합니다.

### 도구가 받아야 할 값
- 없음. *키 보유 여부*만 사용자에게 확인.

---

## 단계 5 — `.env` 파일 생성 (비밀 *없음*)

> 이 봇은 비밀(Discord 토큰, Moonshot API 키)을 `.env` 가 아닌 **macOS Keychain** 에 보관합니다. `.env` 에는 비밀이 아닌 설정 값만 들어갑니다. 이렇게 하면 AI 코딩 도구가 작업 도중 `.env` 를 읽어도 비밀이 도구 컨텍스트에 흘러들어가지 않습니다.

### 도구가 실행
1. `.env.example`을 복사하여 `.env`를 만든다.
   ```bash
   cp .env.example .env
   ```
2. `.env` 의 비밀이 아닌 값만 채워 넣는다 (Edit 도구 사용):
   - `DISCORD_GUILD_ID=` → 단계 3 에서 사용자가 알려준 경우에만 채움
   - `DEFAULT_WORK_DIR=` → 사용자에게 기본 작업 디렉터리를 묻는다 (기본 제안: `$HOME/IdeaProjects`)
3. **`.env` 에 `DISCORD_TOKEN`, `MOONSHOT_API_KEY` 값을 적지 않는다.** `.env.example` 에는 이 두 키가 아예 없거나 주석 처리되어 있어야 정상.

### 검증
```bash
grep -E '^DISCORD_TOKEN=' .env && echo "❌ .env 에 DISCORD_TOKEN 라인이 있습니다 — 삭제하세요" || echo "OK"
grep -E '^MOONSHOT_API_KEY=' .env && echo "❌ .env 에 MOONSHOT_API_KEY 라인이 있습니다 — 삭제하세요" || echo "OK"
```
둘 다 OK 가 떠야 함.

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

## 단계 7 — 봇 첫 실행 (사용자가 직접 실행, 토큰 1회 입력)

> ⚠️ **이 단계만 도구가 실행하지 않고 사용자에게 양도한다.** 이유: 비밀 입력 프롬프트는 사용자 터미널의 TTY 가 필요하고, 더 중요하게는 토큰값이 도구 컨텍스트에 흘러들어가지 않도록 사용자가 자기 터미널에 직접 타이핑해야 한다.

### 도구가 사용자에게 안내
사용자의 터미널에서 **다음 명령을 직접 실행해 주세요**:
```bash
./run-bot.sh
```
- 처음 실행 시 두 번 프롬프트가 뜬다:
  - `🔐 Discord 봇 토큰 (입력은 화면에 표시되지 않습니다):` → 단계 3 에서 받은 토큰 붙여넣기 후 Enter
  - `🔐 Moonshot API 키 (입력은 화면에 표시되지 않습니다):` → 단계 4 에서 갖고 있던 키 붙여넣기 후 Enter
- 두 값은 macOS Keychain (service=`kimi-bridge`) 에 저장된다. 이후 실행부턴 프롬프트 없이 silent 시작.
- 정상 시작 출력:
  ```
  ... INFO router.bot: synced N commands to guild ...
  ... INFO router.bot: bot online: <봇 이름>#1234
  ```
- 사용자는 봇이 떴는지 확인한 뒤 도구에게 "bot online 떴어" 정도만 알려주면 된다.

### 도구가 (자기 Bash 로) 검증
```bash
ps aux | grep router.bot | grep -v grep   # 프로세스 존재
tail -5 bot.log | grep "bot online"        # 로그에 online 라인
```
둘 다 통과하면 단계 7 OK.

### 분기
- `LoginFailure` / `Improper token` 가 봇 로그에 뜸 → 토큰 입력 오타. 사용자에게 토큰을 한 번 더 정확히 입력하라고 안내 + 잘못된 항목 삭제 명령 안내:
  ```bash
  security delete-generic-password -s kimi-bridge -a discord-token
  ./run-bot.sh   # 다시 프롬프트 → 새로 입력
  ```
- `DISCORD_TOKEN is not set` 가 떴다면 → `run-bot.sh` 가 `security` 명령으로 Keychain 조회에 실패했거나 export 가 안 됨. `security find-generic-password -s kimi-bridge -a discord-token` 이 항목 존재를 보여주는지 확인 (값은 출력하지 말 것).
- `bot online` 까지 뜨면 OK. 사용자에게 "Discord 서버에 봇이 온라인 상태로 보이는지" 확인 요청.

> 백그라운드 운영: 사용자가 봇을 띄운 채로 다른 작업을 계속하려면:
> ```bash
> nohup ./run-bot.sh > bot.log 2>&1 &
> ```
> Keychain 에 비밀이 이미 등록된 상태라면 프롬프트 없이 silent 시작하므로 nohup 호환됨.

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

---

## 단계 9 — 이미지 첨부 검증 (선택)

> 텍스트 외에 이미지를 kimi 에 보낼 수 있는지 한 번 확인하는 단계. 평소 자주 쓰는 기능이라면 이 단계도 거치는 게 좋다.

### 사용자에게 안내
1. 단계 8 에서 만든 스레드에 png 또는 jpg 이미지 한 장을 드래그해 업로드.
2. 메시지 본문은 비워도 되고 "이거 뭐야?" 같은 한 줄을 같이 보내도 됨.
3. 봇이 이미지를 `/tmp/kimi-uploads/<thread_id>/` 에 저장하고 `@<abspath>` 형태로 kimi 에 전달.
4. kimi 가 이미지를 해석한 응답을 스레드에 스트리밍.

### 검증
- 허용 확장자: `.png .jpg .jpeg .webp .gif`. 그 외는 `⚠️ 일부 첨부를 건너뛰었어요` 메시지로 거부됨.
- 한 장당 10 MiB 초과 시 거부.
- 봇 로그에 `attachment download failed` / `attachment save failed` 가 뜨면 디스크 권한, `/tmp` 용량 확인.

---

### 완료
여기까지 통과하면 설치 완료. 사용자에게 단계 10 (일상 운용) 을 안내한다.

---

## 단계 10 — 일상 운용 (재실행 / 재부팅 / 자동 시작 / 토큰 회전)

| 상황 | 명령 |
|---|---|
| 봇이 죽었거나 그냥 다시 띄우기 | `./run-bot.sh` (또는 백그라운드: `nohup ./run-bot.sh > bot.log 2>&1 &`) |
| Mac 재부팅 후 | cmux 띄우고 (`open -a cmux`) `./run-bot.sh` |
| 로그인 시 자동 시작 | launchd plist 등록 (아래 참조) |
| 토큰 갱신 (Discord 또는 Moonshot 한쪽) | `security delete-generic-password -s kimi-bridge -a discord-token` (또는 `-a moonshot-key`) 후 `./run-bot.sh` — 해당 항목만 다시 프롬프트 |
| 일상 사용 | `/new` → 스레드 대화 → 끝나면 `/kill` |
| 응답 중단 | `/stop` |
| 스레드 이름 + cmux 탭 이름 동시 변경 | `/rename <새 이름>` |
| 봇을 잠깐 내렸다 다시 띄울 때 (세션 복구) | cmux surface 는 살아있으므로 같은 스레드에서 `/attach` |

### launchd 자동 시작 (선택)

`~/Library/LaunchAgents/com.kimi-bridge.plist` 생성:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.kimi-bridge</string>
  <key>ProgramArguments</key>
  <array>
    <string>/<클론 경로>/run-bot.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/<클론 경로>/bot.log</string>
  <key>StandardErrorPath</key>
  <string>/<클론 경로>/bot.log</string>
</dict>
</plist>
```
등록:
```bash
launchctl load ~/Library/LaunchAgents/com.kimi-bridge.plist
```
**주의**: launchd 는 TTY 가 없어 첫 실행 프롬프트를 띄울 수 없다. 반드시 **단계 7 을 적어도 한 번 성공시킨 뒤** (= Keychain 에 비밀이 들어간 뒤) 에만 launchd 등록한다.

### 도구한테 떠넘겨도 되는 작업 / 떠넘기면 안 되는 작업

| 작업 | AI 도구가 해도 OK? |
|---|---|
| 봇 restart (`pkill -f router.bot` + `./run-bot.sh &`) | ✓ Keychain 에 이미 있으면 silent |
| `bot.log` tail / grep | ✓ 토큰은 로그에 안 찍힘 |
| cmux RPC 호출 (`surface.list` 등) | ✓ |
| 코드 수정 / 디버깅 | ✓ |
| `ps eww -p <bot_pid>` | ✗ 봇 env 전체(토큰 포함)가 stdout 으로 노출됨 |
| `security find-generic-password ... -w` | ✗ Keychain 값을 stdout 으로 끌어냄 |
| `printenv` / `env` 를 봇 띄운 셸에서 실행 | ✗ 토큰이 export 되어 있을 수 있음 |

위 ✗ 항목들은 [`CLAUDE.md`](../CLAUDE.md) 에 룰로 박혀 있으니 (있는 경우) Claude Code 가 자동으로 회피한다.

---

## 부록 A — 환경 변수 레퍼런스

| 변수 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `DISCORD_TOKEN` | ✓ | — | Discord 봇 토큰. **`.env` 에 적지 않음 — Keychain (service=`kimi-bridge`, account=`discord-token`) 에 저장. `run-bot.sh` 가 export 함.** |
| `MOONSHOT_API_KEY` | ✓ | — | kimi-cli 모델 인증. **`.env` 에 적지 않음 — Keychain (service=`kimi-bridge`, account=`moonshot-key`) 에 저장. `run-bot.sh` 가 export 함.** |
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
즉시 다음 순서로:
1. Discord Developer Portal 에서 **Reset Token** (또는 Moonshot 콘솔에서 키 회전).
2. Keychain 의 이전 항목 삭제:
   ```bash
   security delete-generic-password -s kimi-bridge -a discord-token   # 또는 -a moonshot-key
   ```
3. `./run-bot.sh` 다시 실행 → 새 토큰 입력 → Keychain 갱신 → 봇 재시작.

**Q. 봇을 재시작했더니 `/attach`가 "연결할 수 있는 kimi surface를 찾지 못했어요"라고 한다 — 분명히 cmux 에 kimi 가 떠 있는데.**
봇은 종료 시 cmux surface 를 의도적으로 살려두고 registry row 도 `status='active'` 그대로 둔다 (재시작 후 같은 thread 에서 `/attach` 로 재연결할 수 있도록 한 설계). 부작용으로, *다른* 채널에서 `/attach` 를 시도하면 그 surface UUID 가 "이미 등록됨" 으로 판단되어 후보에서 빠진다.

해결 순서:
1. 가장 깔끔한 길 — 원래 그 세션이 쓰던 thread 안으로 들어가 `/attach` (zombie row 가 정상 row 로 복구됨).
2. 그 thread 를 못 찾거나 이미 지웠다면 텍스트 채널에서 `/cleanup` 으로 zombie row 정리 후 다시 `/attach`.
3. 위 두 가지가 다 막혔다면 (긴급) SQLite 직접 수정:
   ```bash
   sqlite3 "$SESSION_DB_PATH" "UPDATE sessions SET status='dead' WHERE monitor_surface_id='<UUID>'"
   ```
   `<UUID>` 는 봇 로그 `/attach: scanning ... registered={...}` 라인에서 확인.

**Q. `.env.example` 의 `KIMI_CMD`, `DISCORD_CLIENT_ID` 는 뭐냐.**
주석으로 비활성화되어 있다 (코드가 직접 읽지 않음). `KIMI_CMD` 는 향후 kimi 바이너리 경로를 환경별로 분리하고 싶을 때를 위한 자리, `DISCORD_CLIENT_ID` 는 단계 3 의 OAuth2 URL 을 직접 만들고 싶을 때 참고용이다. 봇 동작에는 영향이 없다.
