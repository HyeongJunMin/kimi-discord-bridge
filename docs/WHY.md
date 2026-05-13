# 왜 만들었나

원격에서 LLM 코딩 에이전트를 다루는 표준 솔루션들이 다음 빈틈을 동시에 메우지 못해 직접 만들게 되었습니다.

## Claude Code Channels (Discord 플러그인) 의 구조적 한계

Anthropic 공식 Claude Code 에는 데스크탑 세션에 외부 메시지를 push 하는 [Channels](https://code.claude.com/docs/en/channels) 기능이 있고 Discord 플러그인도 포함되어 있지만, 다음 제약이 있습니다.

### 하나의 봇 = 하나의 Claude Code 세션

Discord 채널 plugin 은 `claude --channels plugin:discord@...` 로 시작된 단일 Claude Code 세션 안에서 띄워지는 MCP subprocess 이고, 봇 토큰도 그 세션 안에서 `/discord:configure <token>` 으로 등록됩니다. 한 봇이 동시에 두 세션에 polling 할 수 없으므로 여러 Claude Code 세션을 Discord 에서 독립적으로 제어하려면 **세션마다 별도의 Discord 봇 (별도 토큰)** 을 만들어야 합니다.

### Discord thread 단위 세션 격리 없음

Discord 플러그인은 봇 DM 으로만 동작하며, 허용된 sender (`/discord:access pair` 로 페어링된 사람들) 의 메시지는 모두 동일한 단일 세션으로 흘러갑니다. 한 봇 = 한 세션 구조에 기인.

### 메시지가 모델 컨텍스트에 도달하는 방식

Channel 로 들어온 텍스트는 `<channel source="discord" ...>본문</channel>` 태그로 감싸여 system prompt 의 channel instructions 와 함께 모델에 주입됩니다. 공식 문서가 명시하듯 *"An ungated channel is a prompt injection vector"* 이고, 실측 결과 모델은 채널 출처 메시지에 대해 CLI 직접 입력 대비 더 보수적으로 행동합니다 — 거부 근거에 출처를 명시적으로 인용하고, 의심 시 Read 같은 데이터-페치 도구 호출 자체를 보류하며, 거부 텍스트도 짧고 단호합니다. 이 행동 차이는 공식 문서가 보장하는 *trust class* 가 아니라 system prompt instructions + 태그 라벨링에서 파생된 모델 추론의 산물이라, 모델 버전이나 프롬프트 변동에 따라 약화될 수 있음 — 보안 가정으로 의존하기는 부적절합니다.

그 결과로:

- `/model`, `/clear`, `/compact`, `/context` 같은 **CLI 레벨 내장 슬래시는 Skill 도구로 노출되지 않는 부류** 입니다 (공식 문서 인용: *"A few built-in commands are also available through the Skill tool, including `/init`, `/review`, and `/security-review`. Other built-in commands such as `/compact` are not."*). Discord 에서 "`/model` 바꿔줘" 라고 보내도 모델은 *호출할 도구가 없습니다.* 자연어 답변만 돌아오고 세션의 활성 모델은 그대로입니다.
- 사용자 정의 skill 중 `disable-model-invocation: true` 로 manual-only 지정된 것들(`/commit`, `/deploy`, `/send-slack-message` 같이 부수효과 있는 작업) 은 **모델이 자율 호출 불가** 카테고리입니다. description 조차 모델 컨텍스트에 들어가지 않아 모델이 그 skill 의 존재 자체를 모릅니다. Discord 채널로 "deploy 실행해줘" 식 메시지를 보내도 호출되지 않으며, 파일을 직접 읽도록 우회 시도해도 채널 출처 가산점 때문에 모델이 prompt injection 으로 판정하고 Read 호출 자체를 보류합니다 (실측 확인).
- 결과적으로 채널은 **sender gating (서버 측 allowlist) + reply tool 페어링 게이트 + 모델의 출처 인지 거부** 라는 3겹 방어를 갖지만, 이 방어가 *세션 라이프사이클이나 부수효과 있는 동작을 메신저에서 수행* 하려는 정당한 사용까지 같이 거부하게 만듭니다.

### Research preview 운영 조건

Claude Code v2.1.80+ 필요, claude.ai 또는 Anthropic Console API key 인증 전용 (Amazon Bedrock / Google Vertex AI / Microsoft Foundry 미지원), Team/Enterprise 는 어드민이 `channelsEnabled` 를 명시적으로 켜야 함. Events only arrive while the session is open — 즉 Claude Code 프로세스가 떠 있는 동안만 메시지가 도착합니다.

## kimi-cli 의 메신저 통로 부재

kimi-cli 가 공식 제공하는 통합은 IDE (ACP), VS Code 확장, Zsh, MCP 까지이고 모두 *PC 앞에 앉아 있을 때* 를 전제로 합니다. Discord/Slack/Telegram 같은 메신저 채널은 README 의 Key Features 어디에도 없습니다 (확인일자 기준). PC 를 떠난 순간 진행 중인 kimi 세션을 이어가거나 새 작업을 던질 통로가 없습니다.

## 이 봇이 채우는 자리

- **단일 봇 토큰 + 다중 thread = 다중 kimi 세션.** Discord 스레드 단위로 kimi-cli 세션을 격리하고, 봇이 자체 정의한 슬래시 커맨드(`/new`, `/attach`, `/rebind`, `/rename` 등) 로 세션 라이프사이클을 직접 제어합니다. 슬래시 명령이 Discord 측에서 trigger 되어 봇 코드가 결정론적으로 cmux/kimi 를 조작하므로, 채널 plugin 처럼 모델 자율 판단에 의존하지 않습니다.

- **cmux ↔ Discord 양방향 미러.** kimi-cli 는 cmux surface 안에서 평소 그대로 실행되며, 봇은 그 surface 의 `wire.jsonl` 을 tail 해 Discord 로 스트리밍합니다. PC 앞에서는 cmux 화면을 그대로 보고, 외출 중에는 Discord 모바일에서 같은 세션을 이어갑니다. 어느 쪽도 master/slave 가 아닙니다.

- **세션 이식성.** PC 앞에서 띄워 둔 kimi-cli 가 이미 있다면 `/attach` 로 사후 연결해 Discord 스레드에 묶을 수 있습니다. 봇이 재시작되더라도 cmux surface 와 wire.jsonl 은 그대로 살아있어 동일 스레드에서 `/attach` 로 재연결됩니다.

## 개발 중 풀어야 했던 과제

**Discord 에만 응답이 보이고 cmux 에는 세션이 안 뜨는 문제.** 초기 설계에서는 봇이 직접 `kimi acp` 프로세스를 spawn 해서 stdio JSON-RPC 로 다루는 ACP-only 경로를 시도했습니다. 이 경우 PC 앞 사용자는 같은 세션을 터미널에서 볼 수단이 없어졌습니다. 해결은 ACP 자리에 **cmux surface 안에서 실행되는 kimi-cli + wire.jsonl tail** 이라는 하이브리드 구조로 갈아탄 것이었습니다. kimi-cli 가 매 턴 기록하는 wire.jsonl 을 봇이 따라 읽어 Discord 로 debounce + edit-rollover 로 흘려보내, 같은 세션을 PC 와 모바일에서 동시에 관찰할 수 있게 되었습니다.
