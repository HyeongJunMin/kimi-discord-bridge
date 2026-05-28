# 시행착오 / 트러블슈팅 기록

실제로 터졌던 문제와 그 원인·해결·교훈을 모아둔다. 같은 함정을 다시 밟지 않기
위한 기록이므로, 새 사건이 생기면 위에 추가한다 (최신이 맨 위).

---

## `pytest` 전체 한 방 실행 → 세션 SIGKILL (exit 137)

**증상**
- `pytest` 로 전체 테스트를 한 프로세스에서 돌리면 도중에 죽는다.
- Claude Code / kimi 작업 세션 자체가 강제 종료되고, resume 하면 같은 무거운
  컨텍스트가 재실행되며 반복해서 또 죽는다.

**원인**
- 실제 OS 프로세스를 띄우는 테스트(`bash run-bot.sh` 실행, 실 node 헬퍼 spawn 등)
  + 파일마다 생성되는 asyncio 이벤트 루프가 한 프로세스에 **누적**된다.
- 샌드박스 메모리 천장을 치면 커널/샌드박스가 프로세스 트리째 SIGKILL(=137) 한다.
- 개별 파일은 각각 통과한다. 문제는 "한 방에 전부" 라는 실행 방식 자체.

**해결**
- `pytest.ini`: 기본 `addopts` 에 `-m "not slow"` + `--timeout=60` 적용
  → 그냥 `pytest` 는 무거운 테스트 제외·테스트당 60s 상한이 걸려 안전.
- 실 프로세스 테스트에 `@pytest.mark.slow` 부착 (현재 `test_lock_restore_flow.py`
  의 `run-bot.sh`/실 node spawn 2개).
- `pytest-timeout` 을 `requirements-dev.txt` 에 추가 (addopts 의 `--timeout` 의존).
- 전체 커버리지가 필요하면 `bash tests/run-safe.sh` — 파일을 1개씩 순차 실행해
  메모리 누적을 막는다.

**교훈**
- 샌드박스/제한된 환경에선 "테스트 전체 동시 실행" 이 곧 세션 자살 버튼일 수 있다.
- 기본 실행 경로를 안전하게 만들고(`-m "not slow"`), 위험한 건 opt-in(`-m slow`,
  파일 단위)으로 분리. 안내는 양쪽 도구가 읽는 곳(`CLAUDE.md`, `conftest.py`
  docstring)에 박아 사람·에이전트 누구든 보게 한다.
- 자세한 규칙: `CLAUDE.md` "테스트 실행 — 절대 전체 한 방 금지" 섹션.

---

## wire fallback 무한 멈춤 → 유령 thread (`/status` "등록된 세션 없음")

**증상**
- `/new` 로 스레드를 만들고 kimi 를 띄웠는데, "🚀 기동 중…" 이후 "준비 완료" 도
  "세션 시작 실패" 안내도 **아무것도 안 뜨고** 조용히 끝난다.
- 그 스레드에서 `/status` 를 치면 "이 thread에 등록된 세션이 없어요." 가 나온다.
- 스레드는 Discord 에 멀쩡히 보이지만 봇 입장에선 등록된 세션이 없는 유령 상태.

**원인**
1. cmux surface 가 깨져 있어(`Terminal surface not found`) cmux 경로가 실패 →
   봇이 **wire fallback**(별도 node 헬퍼로 kimi 구동)으로 넘어간다.
2. 그런데 node 헬퍼가 응답을 주지 않았다. `KimiWireClient.create_session()` 의
   `queue.get()` 에 **타임아웃이 없어** 영원히 대기.
3. 대기에서 안 풀리니 `registry.insert()` 까지 도달 못 함 → DB 미등록.
4. 예외도 안 나서 `/new` 핸들러의 `except` 도 안 걸림 → 사용자에게 실패 안내조차
   못 보냄.
5. 결과적으로 "세션이 없다" 가 아니라 "세션 만들다가 조용히 멈춰서 등록이 안 된"
   상태. `/status` 는 그 미등록을 보고 위 메시지를 출력.

**해결** (commit `7293987`)
- `router/kimi_wire.py`:
  - `_read_stdout` 을 `try/finally` 로 감싸 헬퍼 stdout EOF(=프로세스 종료) 시
    `_fail_all_pending()` 이 대기 중인 모든 요청 큐에 에러를 주입 → 대기자가
    무한 대기 대신 즉시 `KimiWireError` 로 깨어난다.
  - 요청-응답형 op(`create_session`/`close`/`interrupt`)에 핸드셰이크 타임아웃
    (기본 30s, `KIMI_WIRE_HANDSHAKE_TIMEOUT`) 추가.
  - 스트리밍 op(`prompt`)는 정상적인 긴 턴(툴 실행 등)을 끊지 않기 위해
    타임아웃을 두지 않고, 위 사망 감지로만 보호.
- `router/bot.py`: `/new` 세션 시작 실패 시 안내 메시지 + 빈 thread 자동 archive
  → 유령 thread 가 활성 목록에 안 남는다.

**교훈**
- 외부 프로세스/IPC 응답 대기에는 **반드시 타임아웃 + 상대 프로세스 사망 감지**를
  같이 둔다. 둘 중 하나만으론 "살아있지만 무응답" 또는 "죽었는데 안 깨어남" 중
  한쪽이 샌다.
- 요청-응답 op 와 스트리밍 op 는 타임아웃 정책을 분리해야 한다 (스트리밍에 짧은
  타임아웃을 걸면 정상 장시간 작업을 죽인다).
- fallback 경로일수록 실패가 조용해지기 쉽다. fallback 안에서도 실패는 사용자에게
  명시적으로 보이게 하고, 부분 생성된 리소스(여기선 Discord thread)는 정리한다.
- 근본 트리거였던 cmux surface 손상 자체는 별개 사안. 이 수정은 그게 터졌을 때
  봇이 조용히 얼어붙지 않고 빠르게 실패를 알리고 정리하게 만든 것.

---

# 과거 사건 아카이브 (커밋 로그에서 정리)

아래는 이번 작업 이전 커밋들에서 추린, 같은 부류의 함정들. 원인이 비직관적이라
재발 위험이 있는 것 위주로 기록한다.

---

## cmux surface 2000+ 유령 누수 + 중복 봇 / 고아 프로세스 (commit `0e5783f`)

**증상**
- 실패한 restore 가 반복되며 cmux 화면(surface)이 2000개 넘게 쌓였다.
- 이전 크래시가 남긴 node 헬퍼/kimi-cli 가 같은 세션에 다시 붙어 Discord 응답이
  중복으로 왔다.

**원인**
- restore 가 `create_surface` 이후 검증(banner/attach 확인)에 실패해도 방금 만든
  surface 를 안 닫음 → 다음 워커 틱이 또 만들어 무한 누수.
- 봇이 두 개 떠서 같은 sqlite 를 공유하고 restore_worker 트래픽이 2배.
- 크래시로 PPID=1·tty 없는 고아 헬퍼/kimi-cli 가 세션에 잔존.

**해결**
- restore: cmux resume 전에 wire 헬퍼를 닫고, `kimi --session` 이 실제 attach
  됐는지 검증, 실패 시 만든 surface 를 즉시 close + 백오프.
- `router.bot.pid` flock 싱글톤 + `run-bot.sh` 의 pgrep 가드로 봇 이중 기동 차단.
- 시작 시 고아 wire 헬퍼/kimi-cli(PPID=1, no tty) 수확(reap).
- 헬퍼를 자체 pgrp 로 띄우고 stop 시 killpg → kimi-cli 손자까지 같이 종료.

**교훈**
- "리소스를 만들었는데 검증 실패" 경로에서는 **반드시 만든 것을 되돌린다.** 안 그러면
  재시도 루프가 곧 누수 루프가 된다.
- 단일 인스턴스 가정이 있는 프로세스는 OS 레벨 락(flock/pgrep)으로 강제.
- 자식 프로세스는 process group 으로 묶어 부모와 생사를 같이하게 한다.

---

## 같은 메시지 중복 전달 / 중복 enqueue (commit `e7c8124`)

**증상**: 재시도된 Discord 이벤트가 두 번 큐에 들어가거나, 딜리버리 워커가 같은
행을 틱마다 두 번 보냄.

**원인**: dedup 키 부재 + 비원자적 SELECT→전송. 워커가 한 행을 집는 사이 다른
틱이 같은 행을 또 집음.

**해결**: `(thread_id, discord_message_id)` dedup 테이블 + `claim_pending_messages`
가 SELECT 와 inflight 표시를 한 트랜잭션(리스)으로 원자 처리. `restoring` 등으로
보류된 건 `release_message` 로 순서 잃지 않고 pending 복귀.

**교훈**: 큐 워커는 "집기(claim)"를 원자적 리스로 만들어야 한다. 외부 이벤트는
재전송될 수 있으니 멱등 키로 dedup.

---

## `/attach` 가 살아있는 kimi surface 를 놓침 / 엉뚱한 걸 잡음 (commit `2b00b61`)

**증상**: 오래 굴린 kimi surface 가 `/attach` 후보에서 누락(welcome banner 가
스크롤돼 `SESSION_RE` 매칭 실패). scrollback 만 켜면 Claude/zsh 화면의 우연한
UUID 문자열까지 잡혀 false positive.

**원인**: banner 텍스트/`wire.jsonl` mtime 같은 약한 신호로 liveness 판정.

**해결**: `system.tree` RPC 로 surface↔tty 매핑 + `ps -axo tty,command` 의 kimi-cli
tty 를 **교집합** → "지금 실제로 kimi-cli 가 도는 surface" 만 후보. 그 위에서
scrollback 으로 banner 검색.

**교훈**: liveness 는 텍스트 흔적이 아니라 **프로세스 사실(tty↔proc)** 로 판정.
약한 신호(파일 mtime)는 idle 세션에서 거짓을 만든다.

---

## `/rename` 이 무한 "thinking" 으로 멈춤 (commit `53abc20`)

**증상**: `/rename` 후 응답이 안 오고 'thinking…' 만 길게(7~8분) 표시.

**원인**: Discord 는 thread 이름 변경을 10분당 2회로 제한. discord.py HTTP 계층이
**client-side 에서 retry-after 만큼 자동 sleep** → followup 이 그 시간만큼 안 나감.
멈춘 게 아니라 라이브러리가 조용히 기다리는 중이었음.

**해결**: `thread.edit` 를 `asyncio.wait_for(timeout=10s)` 로 감싸 10초 초과 시
rate-limit 으로 판단하고 즉시 명확한 안내. surface(cmux) 쪽 rename 은 Discord
제한과 무관하므로 그대로 진행.

**교훈**: 라이브러리의 **암묵적 자동 재시도/sleep** 이 "hang" 처럼 보일 수 있다.
사용자 대면 작업엔 짧은 타임아웃을 씌워 "기다리는 중"을 "명시적 안내"로 전환.

---

## `/rename` 이 거짓 ✓ 보고 (commit `fc0e5de`)

**증상**: 사용자가 cmux 에서 surface 를 직접 닫은 뒤 `/rename` 하면 'surface ✓'
가 떠 성공으로 보임(실제론 실패).

**원인**: `rename_tab` 이 `cmux rename-tab` 의 non-zero 종료를 warning 로그만 남기고
정상 반환 → 호출자의 try/except 가 잡을 게 없음.

**해결**: `rename_tab` 이 rc≠0 시 `CmuxError` raise. 코스메틱 호출자(/new 등)는
이미 try/except 라 영향 없고, `/rename` 은 실제 실패를 잡아 정확히 ⚠️ 보고.

**교훈**: 실패를 삼키고 정상 반환하면 상위가 거짓 성공을 보고한다. **실패는
raise 로 전파**하고, 무시할 호출자만 국소적으로 삼키게 한다.

---

## 봇 재시작이 사용 중인 cmux 세션을 죽임 (commit `f9fdfd2`)

**증상**: 봇을 단순 재기동만 해도 쓰던 화면의 kimi-cli 가 같이 죽어 매번 `/new`
로 처음부터.

**원인**: `_shutdown` 이 SIGTERM/SIGINT 에서 모든 세션 `shutdown_session` →
`close_surface` 까지 호출.

**해결**: 종료 시엔 in-process relay flush task 만 cancel 하고 `close_surface` 는
호출 안 함. registry 행도 `active` 유지 → 재기동 후 `/attach` 로 이어가기. 명시
종료(`/kill`, `/cleanup`, archive)는 그대로 surface 종료.

**교훈**: "프로세스 종료" 와 "세션 자원 정리" 를 분리하라. 재시작이 곧 데이터/세션
파괴가 되면 안 된다. (단, 이로 인해 zombie 'active' row 가 남는 트레이드오프는
`/cleanup` 으로 회수 — `2b00b61` 의 알려진 함정 참고.)

---

## 멀티라인 입력이 한 줄씩 조기 제출됨 (commit `17d0a54`)

**증상**: 멀티이미지 첨부(`@/path1\n@/path2\n질문`)나 Shift+Enter 멀티라인이
kimi 에 한 줄씩 쪼개져 들어가, 첫 사진만 처리되고 본문은 누락.

**원인**: `text + "\n"` 를 그대로 surface 에 보냄. kimi-cli TUI 입력 박스에서
`\n` 은 **Enter(제출)** 로 해석돼 부분 버퍼가 조기 제출됨.

**해결**: payload 를 bracketed-paste 이스케이프(`\x1b[200~ … \x1b[201~`)로 감싸고
끝에 `\r` 한 번만 추가 → TUI 가 paste 모드로 내부 `\n` 을 줄바꿈으로 보존. 입력에
끼어든 paste 마커는 미리 제거(이스케이프 탈출 방지).

**교훈**: TUI 에 텍스트를 주입할 때 개행은 제어문자다. 멀티라인은 bracketed paste
로 감싸 "한 번의 붙여넣기"로 전달. 주입 경로엔 마커 스머글링 방어도 같이.

---

## 첫 응답 손실 — wire.jsonl 닭과 달걀 (commit `aa3e92a`)

**증상**: `/new` 후 첫 메시지('hi')에 응답이 안 오고, 두 번째 메시지를 쳐야
응답이 옴. 그때도 첫 응답은 누락.

**원인**: `send_to_surface` 가 `ensure_tail`(=wire.jsonl 생성 30초 대기)을 **먼저**
호출하고 그 다음 사용자 텍스트를 보냈음. 그런데 wire.jsonl 은 **kimi 가 첫 입력을
받은 후에야** 생기는 파일 → 닭과 달걀 → 타임아웃 에러 → 뒤늦게 텍스트 전송됐지만
tail 은 시작 안 됨.

**해결**: 순서 역전 — `surface_send_text` 먼저, 그 다음 `ensure_tail`. 첫 tail 은
`from_beginning=True` 로 시작해 방금 보낸 메시지의 응답을 처음부터 재생. 대기
타임아웃 30s→60s.

**교훈**: "출력 파일을 기다린 뒤 입력" 같은 순서 가정이 닭-달걀을 만들 수 있다.
부산물(wire.jsonl)이 입력의 **결과**로 생긴다면 입력을 먼저 트리거해야 한다.

---

## `/new`·smoke 가 세션 UUID 대기에서 타임아웃 (commit `34c5d07`)

**증상**: 새 kimi-cli 릴리스가 있으면 `/new` 와 smoke 가 세션 UUID 를 못 받고
타임아웃.

**원인**: 업그레이드가 있을 때 kimi-cli 대화형 셸이 welcome banner 전에 차단형
`[Enter/q/s]` 프롬프트를 띄움 → `SESSION_RE` 가 영영 매칭 안 됨.

**해결**: 실행 커맨드에 `KIMI_CLI_NO_AUTO_UPDATE=1` 을 인라인 주입(전역 셸 환경은
오염 안 시킴). 버전 전환 구간에서도 깨끗이 부팅.

**교훈**: 의존 CLI 의 **대화형 게이트(업그레이드/약관 등)** 가 자동화 파싱을 막을 수
있다. 해당 도구가 제공하는 비대화형 env 플래그를 찾아 주입 — 단, 사용자 전역
환경이 아니라 우리 실행 커맨드에만.
