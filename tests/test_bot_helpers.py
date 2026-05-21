"""bot._kill_session_and_thread — session shutdown + surface verify + delete."""
from __future__ import annotations
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router import bot
from router.cmux_client import CmuxError


def _mock_thread(name: str = "kimi-test", thread_id: int = 12345) -> MagicMock:
    t = MagicMock()
    t.name = name
    t.id = thread_id
    t.delete = AsyncMock()
    return t


async def test_kill_active_session_happy_path():
    """surface_id given; surface.read_text raises CmuxError → status surface ✓ + thread 삭제 ✓."""
    thread = _mock_thread()
    with patch.object(bot.router, "shutdown_session", new=AsyncMock()) as mock_shutdown, \
         patch.object(bot, "surface_read_text",
                      new=AsyncMock(side_effect=CmuxError("gone"))):
        status, name = await bot._kill_session_and_thread(thread, "surf-1")

    assert name == "kimi-test"
    assert "surface ✓" in status
    assert "thread 삭제 ✓" in status
    mock_shutdown.assert_awaited_once_with(thread.id)
    thread.delete.assert_awaited_once()


async def test_kill_reports_warning_when_surface_still_alive():
    """surface.read_text returning text means surface is alive → close 실패 표시."""
    thread = _mock_thread()
    with patch.object(bot.router, "shutdown_session", new=AsyncMock()), \
         patch.object(bot, "surface_read_text",
                      new=AsyncMock(return_value="still here")):
        status, _ = await bot._kill_session_and_thread(thread, "surf-1")

    assert "surface ⚠️ 닫기 실패" in status
    assert "thread 삭제 ✓" in status
    thread.delete.assert_awaited_once()


async def test_kill_skips_surface_check_when_surface_id_is_none():
    """Orphan/dead case: no surface_id → status string omits the surface part."""
    thread = _mock_thread(name="orphan-thread")
    fake_read = AsyncMock()
    with patch.object(bot.router, "shutdown_session", new=AsyncMock()), \
         patch.object(bot, "surface_read_text", new=fake_read):
        status, name = await bot._kill_session_and_thread(thread, None)

    assert name == "orphan-thread"
    assert "surface" not in status
    assert "thread 삭제 ✓" in status
    fake_read.assert_not_awaited()
    thread.delete.assert_awaited_once()


async def test_kill_reports_thread_delete_failure():
    """thread.delete raising → still proceeds, but status flags the failure."""
    thread = _mock_thread()
    thread.delete = AsyncMock(side_effect=RuntimeError("forbidden"))
    with patch.object(bot.router, "shutdown_session", new=AsyncMock()), \
         patch.object(bot, "surface_read_text",
                      new=AsyncMock(side_effect=CmuxError("gone"))):
        status, _ = await bot._kill_session_and_thread(thread, "surf-1")

    assert "thread 삭제 ⚠️ 실패" in status
    # Status string still contains the surface verdict.
    assert "surface ✓" in status


async def test_kill_thread_name_is_captured_before_delete():
    """The returned name must be the thread's name even after deletion."""
    thread = _mock_thread(name="will-be-gone")
    with patch.object(bot.router, "shutdown_session", new=AsyncMock()), \
         patch.object(bot, "surface_read_text",
                      new=AsyncMock(side_effect=CmuxError("gone"))):
        _, name = await bot._kill_session_and_thread(thread, "surf-1")
    assert name == "will-be-gone"


# ── sleep guard mode parsing / routing ────────────────────────────────────

def test_sleep_guard_mode_defaults_to_off():
    assert bot._sleep_guard_mode_from_env({}) == "off"


def test_sleep_guard_mode_accepts_explicit_modes():
    assert bot._sleep_guard_mode_from_env({"SLEEP_GUARD_MODE": "always"}) == "always"
    assert (
        bot._sleep_guard_mode_from_env({"SLEEP_GUARD_MODE": "active_sessions"})
        == "active_sessions"
    )
    assert bot._sleep_guard_mode_from_env({"SLEEP_GUARD_MODE": "off"}) == "off"


def test_sleep_guard_mode_legacy_env_maps_to_active_sessions():
    assert (
        bot._sleep_guard_mode_from_env({"PREVENT_SLEEP_WHILE_ACTIVE": "1"})
        == "active_sessions"
    )


def test_sleep_guard_mode_invalid_value_falls_back_to_off():
    assert bot._sleep_guard_mode_from_env({"SLEEP_GUARD_MODE": "banana"}) == "off"


async def test_refresh_sleep_guard_always_starts_with_zero_sessions(
    tmp_sqlite_path, monkeypatch
):
    monkeypatch.setattr(bot, "REGISTRY_PATH", tmp_sqlite_path)
    monkeypatch.setattr(bot, "SLEEP_GUARD_MODE", "always")
    router = bot.Router()
    router.sleep_guard.start = AsyncMock()
    router.sleep_guard.refresh = AsyncMock()
    router.sleep_guard.stop = AsyncMock()

    await router.refresh_sleep_guard()

    router.sleep_guard.start.assert_awaited_once()
    router.sleep_guard.refresh.assert_not_awaited()
    router.sleep_guard.stop.assert_not_awaited()


async def test_refresh_sleep_guard_active_sessions_uses_session_count(
    tmp_sqlite_path, monkeypatch
):
    monkeypatch.setattr(bot, "REGISTRY_PATH", tmp_sqlite_path)
    monkeypatch.setattr(bot, "SLEEP_GUARD_MODE", "active_sessions")
    router = bot.Router()
    router.sleep_guard.start = AsyncMock()
    router.sleep_guard.refresh = AsyncMock()

    await router.refresh_sleep_guard()

    router.sleep_guard.refresh.assert_awaited_once_with(0)
    router.sleep_guard.start.assert_not_awaited()


async def test_refresh_sleep_guard_off_stops_guard(tmp_sqlite_path, monkeypatch):
    monkeypatch.setattr(bot, "REGISTRY_PATH", tmp_sqlite_path)
    monkeypatch.setattr(bot, "SLEEP_GUARD_MODE", "off")
    router = bot.Router()
    router.sleep_guard.stop = AsyncMock()

    await router.refresh_sleep_guard()

    router.sleep_guard.stop.assert_awaited_once()


# ── WireSession.send_to_surface ordering (wire.jsonl bug regression) ─────

def _make_wire_session_with_mock_relay(tail_started: bool = False):
    sess = bot.WireSession(
        thread_id=999,
        surface_id="surf-1",
        session_uuid="uuid-1",
        cwd="/tmp/x",
        owner_id=1,
    )
    relay = MagicMock()
    relay._tail_started = tail_started
    relay.ensure_tail = AsyncMock(return_value=True)
    relay.reset_message_anchor = MagicMock()
    sess.relay = relay
    return sess, relay


async def test_send_to_surface_sends_text_before_starting_tail():
    """Regression: surface_send_text must happen FIRST. ensure_tail second.

    wire.jsonl is created lazily by kimi-cli AFTER it receives the first user
    message. If we wait for the file first, we deadlock and time out.
    """
    sess, relay = _make_wire_session_with_mock_relay(tail_started=False)
    call_order: list[str] = []

    async def fake_send_text(surf_id, text):
        call_order.append("send")
        return None

    async def fake_ensure_tail(*args, **kwargs):
        call_order.append("tail")
        return True

    relay.ensure_tail = AsyncMock(side_effect=fake_ensure_tail)
    client = MagicMock()
    with patch.object(bot, "surface_send_text", new=fake_send_text):
        await sess.send_to_surface("hi", client)

    assert call_order == ["send", "tail"], (
        "surface_send_text must be called before ensure_tail")


async def test_send_to_surface_passes_from_beginning_true():
    """First tail start should be from_beginning=True to catch the response
    that kimi is already streaming for the message we just sent."""
    sess, relay = _make_wire_session_with_mock_relay(tail_started=False)
    relay.ensure_tail = AsyncMock(return_value=True)

    with patch.object(bot, "surface_send_text", new=AsyncMock()):
        await sess.send_to_surface("hi", MagicMock())

    relay.ensure_tail.assert_awaited_once()
    kwargs = relay.ensure_tail.await_args.kwargs
    assert kwargs.get("from_beginning") is True


async def test_send_to_surface_skips_ensure_tail_when_already_started():
    sess, relay = _make_wire_session_with_mock_relay(tail_started=True)
    relay.ensure_tail = AsyncMock()

    with patch.object(bot, "surface_send_text", new=AsyncMock()) as send_mock:
        await sess.send_to_surface("hi", MagicMock())

    send_mock.assert_awaited_once()
    relay.ensure_tail.assert_not_awaited()  # tail already running


async def test_send_to_surface_resets_message_anchor():
    """End-of-turn marker so the next response starts a fresh Discord message."""
    sess, relay = _make_wire_session_with_mock_relay(tail_started=True)
    with patch.object(bot, "surface_send_text", new=AsyncMock()):
        await sess.send_to_surface("hi", MagicMock())
    relay.reset_message_anchor.assert_called_once()


async def test_send_to_surface_wraps_text_in_bracketed_paste_with_cr_submit():
    """The TUI receives text wrapped in bracketed-paste escapes and a final
    \\r as the submit keystroke. This preserves internal newlines."""
    sess, _ = _make_wire_session_with_mock_relay(tail_started=True)
    send_mock = AsyncMock()
    with patch.object(bot, "surface_send_text", new=send_mock):
        await sess.send_to_surface("hi", MagicMock())
    send_mock.assert_awaited_once_with("surf-1", "\x1b[200~hi\x1b[201~\r")


async def test_send_to_surface_preserves_internal_newlines():
    """Multi-image and multi-line messages must keep their newlines so kimi
    sees them as one paste (one submit), not N separate Enter presses."""
    sess, _ = _make_wire_session_with_mock_relay(tail_started=True)
    send_mock = AsyncMock()
    payload = "@/tmp/a.png\n@/tmp/b.png\n두 이미지 봐줘"
    with patch.object(bot, "surface_send_text", new=send_mock):
        await sess.send_to_surface(payload, MagicMock())
    send_mock.assert_awaited_once_with(
        "surf-1", f"\x1b[200~{payload}\x1b[201~\r")


async def test_send_to_surface_strips_embedded_paste_escape_smuggling():
    """If a user manages to put \\x1b[200~ / \\x1b[201~ in their message text,
    those must be stripped so they can't break out of the paste wrapper
    (and prematurely submit, or inject control sequences)."""
    sess, _ = _make_wire_session_with_mock_relay(tail_started=True)
    send_mock = AsyncMock()
    with patch.object(bot, "surface_send_text", new=send_mock):
        await sess.send_to_surface(
            "before\x1b[201~middle\x1b[200~after", MagicMock())
    # Inner escapes should be gone; outer wrapper present once.
    sent = send_mock.await_args.args[1]
    assert sent.count("\x1b[200~") == 1
    assert sent.count("\x1b[201~") == 1
    assert sent.startswith("\x1b[200~before")
    assert sent.endswith("after\x1b[201~\r")


def _session_row_for_queue(thread_id: str = "123", **overrides) -> bot.SessionRow:
    data = dict(
        thread_id=thread_id,
        guild_id="g",
        channel_id="c",
        owner_user_id="7",
        workspace_id="ws",
        workspace_name="ws",
        cwd="/tmp",
        monitor_surface_id="surf-1",
        acp_session_id="sess-1",
        status="active",
        created_at=0,
        last_active_at=0,
    )
    data.update(overrides)
    return bot.SessionRow(**data)


async def test_relay_user_message_queues_then_delivers_when_worker_stopped(
    tmp_sqlite_path, monkeypatch
):
    """A Discord message is persisted before it is delivered to cmux."""
    monkeypatch.setattr(bot, "REGISTRY_PATH", tmp_sqlite_path)
    router = bot.Router()
    router.registry.insert(_session_row_for_queue("123"))
    sess = bot.WireSession(
        thread_id=123, surface_id="surf-1", session_uuid="sess-1",
        cwd="/tmp", owner_id=7)
    relay = MagicMock()
    relay._tail_started = True
    relay.reset_message_anchor = MagicMock()
    sess.relay = relay
    router.sessions[123] = sess

    send_mock = AsyncMock()
    with patch.object(bot, "surface_send_text", new=send_mock):
        msg_id = await router.relay_user_message(
            123, "queued hello", MagicMock(), author_user_id=7)

    assert msg_id > 0
    assert router.registry.count_pending_messages("123") == 0
    send_mock.assert_awaited_once()


async def test_relay_user_message_ignores_duplicate_discord_message_id(
    tmp_sqlite_path, monkeypatch
):
    monkeypatch.setattr(bot, "REGISTRY_PATH", tmp_sqlite_path)
    router = bot.Router()
    router.registry.insert(_session_row_for_queue("123"))
    sess = bot.WireSession(
        thread_id=123, surface_id="surf-1", session_uuid="sess-1",
        cwd="/tmp", owner_id=7)
    relay = MagicMock()
    relay._tail_started = True
    relay.reset_message_anchor = MagicMock()
    sess.relay = relay
    router.sessions[123] = sess

    send_mock = AsyncMock()
    with patch.object(bot, "surface_send_text", new=send_mock):
        first = await router.relay_user_message(
            123, "queued hello", MagicMock(), author_user_id=7,
            discord_message_id="m-1")
        second = await router.relay_user_message(
            123, "queued hello", MagicMock(), author_user_id=7,
            discord_message_id="m-1")

    assert second == first
    assert router.registry.count_pending_messages("123") == 0
    send_mock.assert_awaited_once()


async def test_relay_user_message_keeps_pending_on_cmux_failure(
    tmp_sqlite_path, monkeypatch
):
    """cmux failure without a session id leaves the message pending."""
    monkeypatch.setattr(bot, "REGISTRY_PATH", tmp_sqlite_path)
    router = bot.Router()
    router.registry.insert(_session_row_for_queue("123", acp_session_id=None))
    sess = bot.WireSession(
        thread_id=123, surface_id="surf-1", session_uuid="sess-1",
        cwd="/tmp", owner_id=7)
    relay = MagicMock()
    relay._tail_started = True
    sess.relay = relay
    router.sessions[123] = sess

    with patch.object(
        bot, "surface_send_text", new=AsyncMock(side_effect=CmuxError("timeout"))
    ):
        await router.relay_user_message(
            123, "do not lose me", MagicMock(), author_user_id=7)

    assert router.registry.count_pending_messages("123") == 1
    assert router.registry.last_delivery_error("123") == "timeout"


async def test_relay_user_message_falls_back_to_wire_on_cmux_failure(
    tmp_sqlite_path, monkeypatch
):
    """An existing cmux session with a stored session id can degrade to wire."""
    monkeypatch.setattr(bot, "REGISTRY_PATH", tmp_sqlite_path)
    router = bot.Router()
    router.registry.insert(_session_row_for_queue("123"))
    sess = bot.WireSession(
        thread_id=123, surface_id="surf-1", session_uuid="sess-1",
        cwd="/tmp", owner_id=7)
    relay = MagicMock()
    relay._tail_started = True
    relay.stop = AsyncMock()
    sess.relay = relay
    router.sessions[123] = sess

    thread = MagicMock()
    thread.send = AsyncMock()
    client = MagicMock()
    client.get_channel.return_value = thread

    wire_sess = MagicMock()
    wire_sess.session_id = "sess-1"
    wire_sess.prompt = AsyncMock()
    with patch.object(
        bot, "surface_send_text", new=AsyncMock(side_effect=CmuxError("timeout"))
    ), patch.object(
        router.wire_client,
        "create_session",
        new=AsyncMock(return_value=wire_sess),
    ) as create_wire:
        await router.relay_user_message(
            123, "fallback me", client, author_user_id=7)

    assert router.registry.count_pending_messages("123") == 0
    row = router.registry.get_by_thread("123")
    assert row.backend == "wire"
    assert row.monitor_surface_id is None
    assert row.abandoned_surface_id == "surf-1"
    create_wire.assert_awaited_once_with(
        name="thread-123", work_dir="/tmp", session_id="sess-1")
    wire_sess.prompt.assert_awaited_once_with("fallback me", thread)
    relay.stop.assert_awaited_once()


async def test_restore_wire_session_creates_cmux_surface_and_resumes_session(
    tmp_sqlite_path, monkeypatch
):
    monkeypatch.setattr(bot, "REGISTRY_PATH", tmp_sqlite_path)
    router = bot.Router()
    router.registry.insert(_session_row_for_queue(
        "123",
        backend="wire",
        monitor_surface_id=None,
        abandoned_surface_id="surf-old",
    ))

    wire_sess = MagicMock()
    wire_sess.active_turn = False
    wire_sess.close = AsyncMock()
    router.wire_sessions[123] = wire_sess

    thread = MagicMock()
    thread.id = 123
    thread.name = "kimi-kimi-hub-7777"
    thread.send = AsyncMock()
    client = MagicMock()
    client.get_channel.return_value = thread

    send_mock = AsyncMock()
    with patch.object(
        bot, "list_workspaces",
        new=AsyncMock(return_value=[{"id": "ws"}]),
    ), patch.object(
        bot, "create_surface", new=AsyncMock(return_value={"surface_id": "surf-new"})
    ), patch.object(
        bot, "surface_read_text",
        new=AsyncMock(return_value="$ Session: sess-1"),
    ), patch.object(
        bot, "surface_send_text", new=send_mock
    ), patch.object(
        bot, "rename_tab", new=AsyncMock()
    ):
        restored = await router.restore_wire_sessions_once(client)

    assert restored == 1
    row = router.registry.get_by_thread("123")
    assert row.backend == "cmux"
    assert row.monitor_surface_id == "surf-new"
    assert row.abandoned_surface_id == "surf-old"
    assert 123 in router.sessions
    assert 123 not in router.wire_sessions
    wire_sess.close.assert_awaited_once()
    sent_cmd = send_mock.await_args.args[1]
    assert "kimi --session sess-1" in sent_cmd
    assert "cd /tmp" in sent_cmd
    thread.send.assert_awaited_once()


async def test_restore_wire_session_skips_active_turn(
    tmp_sqlite_path, monkeypatch
):
    monkeypatch.setattr(bot, "REGISTRY_PATH", tmp_sqlite_path)
    router = bot.Router()
    router.registry.insert(_session_row_for_queue("123", backend="wire"))

    wire_sess = MagicMock()
    wire_sess.active_turn = True
    router.wire_sessions[123] = wire_sess
    client = MagicMock()

    with patch.object(bot, "create_surface", new=AsyncMock()) as create_mock:
        restored = await router.restore_wire_sessions_once(client)

    assert restored == 0
    create_mock.assert_not_awaited()
    assert router.registry.get_by_thread("123").backend == "wire"


async def test_restore_worker_retries_rows_left_in_restoring_state(
    tmp_sqlite_path, monkeypatch
):
    monkeypatch.setattr(bot, "REGISTRY_PATH", tmp_sqlite_path)
    router = bot.Router()
    router.registry.insert(_session_row_for_queue("123", backend="restoring"))
    router._restore_one_wire_session = AsyncMock(return_value=True)

    restored = await router.restore_wire_sessions_once(MagicMock())

    assert restored == 1
    router._restore_one_wire_session.assert_awaited_once()


async def test_relay_user_message_skips_stale_messages(
    tmp_sqlite_path, monkeypatch
):
    """Messages written long before wake-up must not execute later."""
    monkeypatch.setattr(bot, "REGISTRY_PATH", tmp_sqlite_path)
    monkeypatch.setattr(bot, "QUEUE_MAX_MESSAGE_AGE_SEC", 300)
    router = bot.Router()
    router.registry.insert(_session_row_for_queue("123"))
    sess = bot.WireSession(
        thread_id=123, surface_id="surf-1", session_uuid="sess-1",
        cwd="/tmp", owner_id=7)
    router.sessions[123] = sess
    client = MagicMock()
    thread = MagicMock()
    thread.send = AsyncMock()
    client.get_channel.return_value = thread

    send_mock = AsyncMock()
    with patch.object(bot, "surface_send_text", new=send_mock), \
         patch.object(bot.time, "time", return_value=10_000):
        await router.relay_user_message(
            123, "old command", client, author_user_id=7,
            source_created_at=9_000, discord_message_id="m-old")

    send_mock.assert_not_awaited()
    assert router.registry.count_pending_messages("123") == 0
    assert "stale" in (router.registry.last_delivery_error("123") or "")
    thread.send.assert_awaited_once()


# ── create_session renames cmux tab to thread name ────────────────────────

async def test_create_session_renames_cmux_tab_with_short_title(monkeypatch):
    """rename_tab is called with workspace_name[:3] + thread suffix."""
    thread = MagicMock()
    thread.id = 12345
    thread.name = "kimi-kimi-hub-3590"
    thread.guild = MagicMock(id=11111)
    thread.parent_id = 22222

    rename_mock = AsyncMock()
    fake_banner = "Session: 12345678-1234-1234-1234-123456789012\n"
    with patch.object(bot, "create_surface",
                      new=AsyncMock(return_value={"surface_id": "surf-XYZ"})), \
         patch.object(bot, "surface_send_text", new=AsyncMock()), \
         patch.object(bot, "surface_read_text",
                      new=AsyncMock(return_value=fake_banner)), \
         patch.object(bot, "rename_tab", new=rename_mock), \
         patch.object(bot.router.registry, "insert"):
        await bot.router.create_session(
            thread=thread, owner_id=999,
            workspace_id="ws-1", workspace_name="kimi-hub", cwd="/tmp")

    rename_mock.assert_awaited_once_with("surf-XYZ", "kim-3590")


# ── _short_tab_title pure helper ─────────────────────────────────────────

def test_short_tab_title_uses_workspace_prefix_and_thread_suffix():
    """For /new: workspace name first 3 chars + thread's trailing NNNN."""
    assert bot._short_tab_title("kimi-hub", "kimi-kimi-hub-3590") == "kim-3590"
    assert bot._short_tab_title("rv-sol", "kimi-rv-sol-9821") == "rv--9821"


def test_short_tab_title_falls_back_to_ws_when_workspace_name_is_none():
    """workspace_name=None happens when /new creates a brand-new workspace
    whose name isn't echoed back. Use 'ws' as a stable placeholder."""
    assert bot._short_tab_title(None, "kimi-anything-1234") == "ws-1234"


def test_short_tab_title_short_workspace_kept_intact():
    """Workspace name shorter than 3 chars is kept verbatim."""
    assert bot._short_tab_title("ab", "kimi-ab-1234") == "ab-1234"


def test_short_tab_title_returns_prefix_only_when_thread_has_no_suffix():
    assert bot._short_tab_title("workspace", "plain") == "wor"


# ── _do_rename helper ────────────────────────────────────────────────────

def _row_for_rename(surface_id="surf-1", status="active"):
    return bot.SessionRow(
        thread_id="1", guild_id="g", channel_id="c",
        owner_user_id="u", workspace_id="w", workspace_name=None,
        cwd="/tmp", monitor_surface_id=surface_id, acp_session_id="a",
        status=status, created_at=0, last_active_at=0,
    )


async def test_do_rename_active_session_renames_both():
    thread = MagicMock()
    thread.edit = AsyncMock()
    rename_mock = AsyncMock()
    with patch.object(bot, "rename_tab", new=rename_mock):
        status = await bot._do_rename(thread, "new-name", _row_for_rename())
    thread.edit.assert_awaited_once_with(name="new-name")
    rename_mock.assert_awaited_once_with("surf-1", "new-name")
    assert "thread ✓" in status
    assert "surface ✓" in status


async def test_do_rename_without_row_only_changes_thread():
    thread = MagicMock()
    thread.edit = AsyncMock()
    rename_mock = AsyncMock()
    with patch.object(bot, "rename_tab", new=rename_mock):
        status = await bot._do_rename(thread, "fresh", None)
    thread.edit.assert_awaited_once_with(name="fresh")
    rename_mock.assert_not_awaited()
    assert "thread ✓" in status
    assert "surface 없음" in status


async def test_do_rename_dead_session_skips_surface():
    """status='dead' rows still get thread rename but no surface ping."""
    thread = MagicMock()
    thread.edit = AsyncMock()
    rename_mock = AsyncMock()
    row = _row_for_rename(status="dead")
    with patch.object(bot, "rename_tab", new=rename_mock):
        status = await bot._do_rename(thread, "x", row)
    rename_mock.assert_not_awaited()
    assert "surface 없음" in status


async def test_do_rename_truncates_over_100_chars():
    thread = MagicMock()
    thread.edit = AsyncMock()
    long_name = "a" * 150
    with patch.object(bot, "rename_tab", new=AsyncMock()):
        status = await bot._do_rename(thread, long_name, _row_for_rename())
    sent_name = thread.edit.await_args.kwargs["name"]
    assert len(sent_name) == 100
    assert "100자 초과로 자름" in status


async def test_do_rename_reports_thread_failure():
    thread = MagicMock()
    thread.edit = AsyncMock(side_effect=RuntimeError("rate limited"))
    with patch.object(bot, "rename_tab", new=AsyncMock()) as rt:
        status = await bot._do_rename(thread, "x", _row_for_rename())
    assert "thread ⚠️ 실패" in status
    # surface rename still attempted independently
    rt.assert_awaited_once()
    assert "surface ✓" in status


async def test_do_rename_reports_surface_failure():
    thread = MagicMock()
    thread.edit = AsyncMock()
    with patch.object(bot, "rename_tab",
                      new=AsyncMock(side_effect=RuntimeError("cmux down"))):
        status = await bot._do_rename(thread, "x", _row_for_rename())
    assert "thread ✓" in status
    assert "surface ⚠️ 실패" in status


async def test_do_rename_fails_fast_on_discord_rate_limit(monkeypatch):
    """Discord rate-limits thread renames to 2/10min. discord.py would
    auto-sleep through the whole retry-after window (7+ min) which leaves
    the user staring at 'thinking…'. We cap with asyncio.wait_for and
    report the limit instead."""
    monkeypatch.setattr(bot, "RENAME_THREAD_TIMEOUT_S", 0.3)

    thread = MagicMock()

    async def slow_edit(**kwargs):
        # Simulate discord.py's internal rate-limit sleep.
        await asyncio.sleep(5)

    thread.edit = slow_edit

    # surface rename should still succeed independently.
    with patch.object(bot, "rename_tab", new=AsyncMock()) as rt:
        status = await bot._do_rename(thread, "x", _row_for_rename())

    assert "thread ⚠️ Discord 제한" in status
    assert "surface ✓" in status
    rt.assert_awaited_once()


async def test_create_session_continues_when_rename_fails(monkeypatch):
    """rename_tab raising must not abort session creation — it's cosmetic."""
    thread = MagicMock()
    thread.id = 99
    thread.name = "kimi-rename-fails"
    thread.guild = MagicMock(id=1)
    thread.parent_id = 2

    fake_banner = "Session: 12345678-1234-1234-1234-123456789012\n"
    with patch.object(bot, "create_surface",
                      new=AsyncMock(return_value={"surface_id": "surf-1"})), \
         patch.object(bot, "surface_send_text", new=AsyncMock()), \
         patch.object(bot, "surface_read_text",
                      new=AsyncMock(return_value=fake_banner)), \
         patch.object(bot, "rename_tab",
                      new=AsyncMock(side_effect=RuntimeError("cmux down"))), \
         patch.object(bot.router.registry, "insert") as ins:
        sess, _ = await bot.router.create_session(
            thread=thread, owner_id=1,
            workspace_id="ws-1", workspace_name="ws", cwd="/tmp")

    # Session still came back; registry insert still happened.
    assert sess is not None
    ins.assert_called_once()


# ── cleanup classification ───────────────────────────────────────────────

def _row(thread_id: str, surface_id: str | None = "surf",
         created_at: int = 0) -> bot.SessionRow:
    import time as _t
    return bot.SessionRow(
        thread_id=thread_id, guild_id="g", channel_id="c",
        owner_user_id="u", workspace_id="w", workspace_name=None,
        cwd="/tmp", monitor_surface_id=surface_id, acp_session_id="a",
        status="active", created_at=created_at,
        last_active_at=created_at or int(_t.time()),
    )


def _thread(thread_id: int) -> MagicMock:
    t = MagicMock()
    t.id = thread_id
    return t


async def test_classify_marks_unregistered_threads_as_orphans():
    threads = [_thread(1), _thread(2)]
    active = {"1": _row("1")}  # thread 2 has no registry row
    zombies, unreg = await bot._classify_threads_for_cleanup(
        threads, active, cmux_ok=True,
        surface_probe=AsyncMock(return_value="alive"),
        now=10_000)
    assert unreg == {"2"}
    assert zombies == set()


async def test_classify_marks_dead_surface_as_zombie():
    threads = [_thread(1)]
    active = {"1": _row("1", surface_id="surf-1", created_at=0)}
    # Old session (created_at=0, well past grace) + cmux ping raises.
    zombies, unreg = await bot._classify_threads_for_cleanup(
        threads, active, cmux_ok=True,
        surface_probe=AsyncMock(side_effect=CmuxError("gone")),
        now=10_000)
    assert zombies == {"1"}
    assert unreg == set()


async def test_classify_disables_zombie_detection_when_cmux_down():
    """Mass-delete protection: if cmux is unreachable, never flag zombies."""
    threads = [_thread(1), _thread(2)]
    active = {"1": _row("1"), "2": _row("2")}
    probe = AsyncMock(side_effect=CmuxError("never called"))
    zombies, unreg = await bot._classify_threads_for_cleanup(
        threads, active, cmux_ok=False, surface_probe=probe, now=10_000)
    assert zombies == set()
    assert unreg == set()
    probe.assert_not_awaited()


async def test_classify_respects_grace_period_for_new_sessions():
    """A registry row newer than grace_sec must not be probed/zombified."""
    threads = [_thread(1)]
    # created_at=99 → age 1s, well within 30s grace.
    active = {"1": _row("1", surface_id="surf-1", created_at=99)}
    probe = AsyncMock(side_effect=CmuxError("would-fail"))
    zombies, unreg = await bot._classify_threads_for_cleanup(
        threads, active, cmux_ok=True, surface_probe=probe, now=100,
        grace_sec=30)
    assert zombies == set()
    probe.assert_not_awaited()


async def test_classify_skips_rows_without_surface_id():
    """monitor_surface_id=None → can't verify → not flagged as zombie."""
    threads = [_thread(1)]
    active = {"1": _row("1", surface_id=None, created_at=0)}
    probe = AsyncMock(side_effect=CmuxError("would-fail"))
    zombies, unreg = await bot._classify_threads_for_cleanup(
        threads, active, cmux_ok=True, surface_probe=probe, now=10_000)
    assert zombies == set()
    probe.assert_not_awaited()
