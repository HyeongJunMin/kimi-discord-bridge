"""bot._kill_session_and_thread — session shutdown + surface verify + delete."""
from __future__ import annotations
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
