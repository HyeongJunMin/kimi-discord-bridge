from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from router.kimi_wire import KimiWireClient, KimiWireError, KimiWireSession, _split_message


class _FakeStdin:
    def write(self, _data):
        pass

    async def drain(self):
        pass


def _client_without_helper():
    """A client whose helper is 'running' but whose reader never delivers a
    reply — exercises the timeout / death paths without spawning node."""
    c = KimiWireClient(command=["true"])

    async def _noop_start():
        return None

    c.start = _noop_start  # type: ignore[assignment]
    c.proc = SimpleNamespace(stdin=_FakeStdin(), stdout=None, returncode=None)
    return c


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


async def test_create_session_raises_on_handshake_timeout():
    """An unresponsive helper must fail fast, not hang the /new flow forever."""
    c = _client_without_helper()
    c._handshake_timeout = 0.05

    with pytest.raises(KimiWireError):
        await c.create_session(name="s", work_dir="/tmp")
    assert c._pending == {}


async def test_create_session_raises_when_helper_dies_mid_wait():
    """If the helper exits while a request is in flight, the waiter must be
    woken with an error instead of blocking on queue.get() forever."""
    c = _client_without_helper()
    c._handshake_timeout = 5

    task = asyncio.create_task(c.create_session(name="s", work_dir="/tmp"))
    # Let create_session register its pending queue and block on the reply.
    while not c._pending:
        await asyncio.sleep(0)

    c._fail_all_pending("Kimi wire helper exited")

    with pytest.raises(KimiWireError):
        await task
    assert c._pending == {}


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
