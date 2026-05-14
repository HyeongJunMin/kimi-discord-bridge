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
    assert sent == ["wire fallback: turn started", "hello world"]
    assert sess.active_turn is False


async def test_wire_session_prompt_formats_tool_result():
    thread = FakeThread()
    sess = KimiWireSession(
        name="s",
        session_id="sid",
        work_dir="/tmp",
        client=FakeWireClient([
            {"type": "ToolResult", "payload": {"return_value": {"ok": True}}},
        ]),
    )

    await sess.prompt("hi", thread)

    sent = thread.send.await_args.args[0]
    assert "Tool result" in sent
    assert '"ok": true' in sent


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
