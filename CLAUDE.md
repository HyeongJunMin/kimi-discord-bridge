# CLAUDE.md — kimi-discord-bridge 작업 규칙

이 저장소에서 AI 도구(Claude Code 등)가 작업할 때 지켜야 할 규칙.

## 비밀(secrets) 취급 — 절대 금지 명령

봇 운영 비밀(`DISCORD_TOKEN`, `MOONSHOT_API_KEY`) 은 macOS Keychain 에 저장되며,
런타임엔 봇 프로세스의 env 로만 존재합니다. 다음 명령은 비밀을 평문 stdout
으로 끌어내므로 **호출하지 않습니다.** 만약 실수로 호출했다면 결과를 사용자/API
어느 쪽으로도 출력하거나 인용하지 않고, 즉시 그 사실을 사용자에게 알립니다.

- `ps eww -p <bot_pid>` / `ps Eww -p <bot_pid>` — 봇 프로세스 env 전체 노출
- `ps auxe`, `ps -A -E` 등 환경변수가 함께 찍히는 모든 변형
- `security find-generic-password -s kimi-bridge ... -w` — Keychain 값 stdout 출력. **존재 확인** 용도라면 `-w` 없이 호출 가능 (값은 안 보임).
- 봇이 export 된 셸에서 `env`, `printenv`, `set`, `echo $DISCORD_TOKEN`, `echo $MOONSHOT_API_KEY` 같은 env 덤프
- `cat .env`/`cat ~/.zshrc`/`cat ~/.bashrc` 등 — `.env` 에는 비밀이 없도록 설계돼 있지만, 사용자 환경에 따라 rc 파일에 잘못 적혀있을 수 있음. 필요할 때만 읽고, 토큰 패턴(`sk-...`, 64자 hex 등) 이 보이면 출력을 마스킹하고 사용자에게 경고.

봇 로그(`bot.log`) 의 토큰은 discord.py 가 일반적으로 마스킹하지만, 드물게
raw 가 찍힐 수 있습니다. 로그를 사용자/API 에 인용할 땐 명백한 토큰 패턴
(`MTI...`, `OD...`, `sk-...`) 이 보이면 해당 부분을 `***` 로 치환.

## 비밀 입력은 사용자 직접 — 도구가 대신 받지 않음

최초 설치 시 사용자가 `./run-bot.sh` 를 자기 터미널에서 직접 실행해 토큰을
프롬프트에 타이핑합니다 (`docs/INSTALL.md` 단계 7). 도구는 이 단계를
대신 실행하지 않습니다:

- 도구의 Bash tool 은 TTY 가 없어 `read -s` 프롬프트를 못 받습니다.
- 더 본질적인 이유: 토큰값이 도구 컨텍스트(= 운영사 서버 로그) 에 들어가지 않게 하기 위함.

도구가 사용자에게 토큰을 직접 받아 처리하려고 시도하면 안 됩니다. 사용자가
도구 채팅창에 토큰을 적었다면 즉시 "토큰을 채팅에 적지 말고 단말기 프롬프트에
직접 입력하세요" 라고 알리고, 적힌 토큰값을 무시합니다.

## 봇 재시작 / 운영

`run-bot.sh` 는 Keychain 에 비밀이 이미 등록되어 있으면 silent 시작합니다.
따라서 도구가 봇 재시작 같은 일상 운영은 자유롭게 수행 가능:

```bash
pkill -f "router.bot"
sleep 2
nohup ./run-bot.sh > bot.log 2>&1 &
```

이때 비밀은 도구 컨텍스트에 절대 들어가지 않습니다.

## 코드 변경 후 staging 정책

- 파일을 Edit/Write/rm 한 직후, sub-agent 가 수정한 직후, 검증 통과 전이라도
  변경된 파일들을 즉시 `git add <명시 경로>` 로 staged 상태로 옮길 것.
- 사용자가 `git status` 로 진행 상황을 한눈에 보길 원함. unstaged 로 방치 금지.
- 커밋은 사용자가 명시 요청할 때만. `git add -A` / `git add .` 금지 (원치 않는
  파일 포함 위험).

## 테스트 실행 — 절대 전체 한 방 금지

전체 테스트를 한 프로세스(`pytest`로 모든 파일 동시)에서 돌리면 실제 OS
프로세스를 띄우는 slow 테스트 + 파일별 asyncio 루프가 누적돼 샌드박스 메모리
천장을 치고 **SIGKILL(exit 137) 로 세션 자체가 죽습니다.** Claude Code·kimi
어디서 작업하든 동일.

- **기본 실행은 안전**: `pytest` 는 `pytest.ini` 의 `-m "not slow"` + `--timeout=60`
  덕에 slow 테스트 제외·테스트당 60s 상한이 걸려 있음. 그냥 `pytest` 는 OK.
- **slow 테스트는 파일 하나씩만**: `pytest -m slow tests/test_lock_restore_flow.py`.
  여러 파일을 `-m slow` 로 한꺼번에 돌리지 말 것.
- **전체 커버리지가 필요하면**: `bash tests/run-safe.sh` (파일을 순차로 1개씩
  실행해 메모리 누적 방지). 절대 `pytest`(전체) + `-m ""`/`-m slow` 조합으로
  한 방에 돌리지 말 것.
- `pytest-timeout` 필요 (`requirements-dev.txt` 에 포함). venv 에 없으면
  `--timeout` addopts 때문에 모든 실행이 깨지므로 `pip install -r requirements-dev.txt`.
