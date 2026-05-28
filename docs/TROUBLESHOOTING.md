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
