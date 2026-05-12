"""discord_relay: wire path computation, file polling, debounce flush."""
from __future__ import annotations
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from router import discord_relay
from router.discord_relay import (
    ThreadRelay,
    compute_wire_path,
    wait_for_wire_jsonl,
    work_dir_hash,
)


def test_work_dir_hash_is_stable_for_same_path():
    h1 = work_dir_hash("/tmp/a")
    h2 = work_dir_hash("/tmp/a")
    h3 = work_dir_hash("/tmp/b")
    assert h1 == h2
    assert h1 != h3


def test_work_dir_hash_resolves_user_expansion():
    """~/x and $HOME/x produce the same hash."""
    import os
    home = os.path.expanduser("~")
    h_tilde = work_dir_hash("~/some/path")
    h_full = work_dir_hash(f"{home}/some/path")
    assert h_tilde == h_full


def test_compute_wire_path_includes_session_uuid_and_hash():
    p = compute_wire_path("uuid-1234", "/tmp/work")
    parts = p.parts
    assert "uuid-1234" in parts
    assert p.name == "wire.jsonl"
    assert work_dir_hash("/tmp/work") in parts


async def test_wait_for_wire_jsonl_finds_at_expected_path(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(discord_relay, "KIMI_SESSIONS_DIR", tmp_path)

    cwd = "/tmp/proj"
    session_uuid = "abc-123"
    expected = tmp_path / work_dir_hash(cwd) / session_uuid / "wire.jsonl"
    expected.parent.mkdir(parents=True)
    expected.write_text("")

    found = await wait_for_wire_jsonl(session_uuid, cwd, max_wait=2.0)
    assert found == expected


async def test_wait_for_wire_jsonl_times_out_when_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(discord_relay, "KIMI_SESSIONS_DIR", tmp_path)

    found = await wait_for_wire_jsonl("nope-uuid", "/tmp/nowhere", max_wait=0.6)
    assert found is None


async def test_wait_for_wire_jsonl_appears_during_polling(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(discord_relay, "KIMI_SESSIONS_DIR", tmp_path)

    cwd = "/tmp/proj"
    session_uuid = "lazy"
    expected = tmp_path / work_dir_hash(cwd) / session_uuid / "wire.jsonl"

    async def create_after_delay():
        await asyncio.sleep(0.3)
        expected.parent.mkdir(parents=True)
        expected.write_text("")

    asyncio.create_task(create_after_delay())
    found = await wait_for_wire_jsonl(session_uuid, cwd, max_wait=3.0)
    assert found == expected


# ── ThreadRelay debounce flush ────────────────────────────────────────────

def _make_relay() -> tuple[ThreadRelay, MagicMock]:
    """Build a ThreadRelay backed by a mock discord.Thread."""
    thread = MagicMock()
    thread.id = 999
    sent_msg = MagicMock()
    sent_msg.content = ""
    sent_msg.edit = AsyncMock()
    thread.send = AsyncMock(return_value=sent_msg)
    return ThreadRelay(thread), thread


async def test_relay_flush_sends_single_short_message():
    relay, thread = _make_relay()
    await relay._enqueue("hello")
    await asyncio.sleep(1.0)
    thread.send.assert_awaited_once_with("hello")


async def test_relay_flush_coalesces_back_to_back_chunks():
    relay, thread = _make_relay()
    await relay._enqueue("a ")
    await relay._enqueue("b ")
    await relay._enqueue("c")
    await asyncio.sleep(1.0)
    assert thread.send.await_count == 1
    sent_text = thread.send.await_args.args[0]
    assert sent_text == "a b c"


async def test_relay_reset_message_anchor_breaks_edit_chain():
    relay, thread = _make_relay()
    await relay._enqueue("first")
    await asyncio.sleep(1.0)
    assert thread.send.await_count == 1

    relay.reset_message_anchor()
    await relay._enqueue("second")
    await asyncio.sleep(1.0)
    assert thread.send.await_count == 2


async def test_send_now_flushes_pending_buffer_first():
    relay, thread = _make_relay()
    await relay._enqueue("buffered ")
    await relay._send_now("🔧 ping")
    sends = [c.args[0] for c in thread.send.await_args_list]
    assert "buffered " in sends
    assert "🔧 ping" in sends


async def test_flush_rollover_creates_second_message_when_over_limit():
    from router.discord_relay import MAX_EDIT_CHARS
    relay, thread = _make_relay()
    almost_full_text = "A" * (MAX_EDIT_CHARS - 10)
    original_msg = MagicMock()
    original_msg.content = almost_full_text
    original_msg.edit = AsyncMock()
    relay._current_msg = original_msg

    await relay._enqueue("B" * 30)
    await asyncio.sleep(1.0)

    original_msg.edit.assert_awaited()
    thread.send.assert_awaited()


async def test_flush_appends_via_edit_when_within_limit():
    relay, thread = _make_relay()
    existing = MagicMock()
    existing.content = "previous text"
    existing.edit = AsyncMock()
    relay._current_msg = existing

    await relay._enqueue(" more")
    await asyncio.sleep(1.0)

    existing.edit.assert_awaited_once_with(content="previous text more")
    thread.send.assert_not_awaited()
