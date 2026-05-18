from __future__ import annotations

from unittest.mock import AsyncMock

from router.kimi_wire import KimiWireSession, _split_message


class FakeWireClient:
    def __init__(self, events):
        self.events = events

    async def prompt(self, name, content):
        for event in self.events:
            yield event

    async def close(self, name):
        return None


class FakeThread:
    def __init__(self):
        self.send = AsyncMock()


def test_split_message_prefers_newline():
    chunks = _split_message("a\n" + "b" * 10, limit=5)

    assert chunks == ["a\nbbb", "bbbbb", "bb"]


async def test_wire_session_prompt_sends_content_at_turn_end():
    thread = FakeThread()
    sess = KimiWireSession(
        name="s",
        session_id="sid",
        work_dir="/tmp",
        client=FakeWireClient([
            {"type": "TurnBegin", "payload": {}},
            {"type": "ContentPart", "payload": {"type": "text", "text": "hello"}},
            {"type": "ContentPart", "payload": {"type": "text", "text": " world"}},
            {"type": "TurnEnd", "payload": {}},
        ]),
    )

    await sess.prompt("hi", thread)

    sent = [call.args[0] for call in thread.send.await_args_list]
    assert sent == ["hello world"]
    assert sess.active_turn is False


async def test_wire_session_prompt_hides_tool_events_by_default():
    """Tool plumbing is noise in Discord; cmux relay already hides it,
    wire fallback must match."""
    thread = FakeThread()
    sess = KimiWireSession(
        name="s",
        session_id="sid",
        work_dir="/tmp",
        client=FakeWireClient([
            {"type": "TurnBegin", "payload": {}},
            {"type": "ToolCall", "payload": {"function": {"name": "ReadFile"}}},
            {"type": "ToolResult", "payload": {"return_value": {"ok": True}}},
            {"type": "ContentPart", "payload": {"type": "text", "text": "done"}},
            {"type": "TurnEnd", "payload": {}},
        ]),
    )

    await sess.prompt("hi", thread)

    sent = [call.args[0] for call in thread.send.await_args_list]
    assert sent == ["done"], f"unexpected wire fallback output: {sent}"


async def test_wire_session_prompt_hides_thinking_by_default():
    thread = FakeThread()
    sess = KimiWireSession(
        name="s",
        session_id="sid",
        work_dir="/tmp",
        client=FakeWireClient([
            {"type": "ContentPart", "payload": {"type": "think", "think": "uhh"}},
            {"type": "ContentPart", "payload": {"type": "text", "text": "hi"}},
            {"type": "TurnEnd", "payload": {}},
        ]),
    )

    await sess.prompt("hi", thread)

    sent = [call.args[0] for call in thread.send.await_args_list]
    assert sent == ["hi"]


async def test_wire_session_prompt_ignores_mcp_loading_parse_errors():
    thread = FakeThread()
    sess = KimiWireSession(
        name="s",
        session_id="sid",
        work_dir="/tmp",
        client=FakeWireClient([
            {"type": "error", "code": "UNKNOWN_EVENT_TYPE", "message": "Unknown event type: MCPLoadingBegin"},
            {"type": "ContentPart", "payload": {"type": "text", "text": "done"}},
            {"type": "error", "code": "UNKNOWN_EVENT_TYPE", "message": "Unknown event type: MCPLoadingEnd"},
            {"type": "TurnEnd", "payload": {}},
        ]),
    )

    await sess.prompt("hi", thread)

    sent = [call.args[0] for call in thread.send.await_args_list]
    assert sent == ["done"]


async def test_wire_session_prompt_reports_questions():
    thread = FakeThread()
    sess = KimiWireSession(
        name="s",
        session_id="sid",
        work_dir="/tmp",
        client=FakeWireClient([
            {"type": "QuestionRequest", "payload": {"questions": ["continue?"]}},
        ]),
    )

    await sess.prompt("hi", thread)

    thread.send.assert_awaited_once_with("Question: continue?")
