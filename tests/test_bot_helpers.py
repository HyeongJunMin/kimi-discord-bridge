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
    assert "surface ✓" in status


async def test_kill_thread_name_is_captured_before_delete():
    """The returned name must be the thread's name even after deletion."""
    thread = _mock_thread(name="will-be-gone")
    with patch.object(bot.router, "shutdown_session", new=AsyncMock()), \
         patch.object(bot, "surface_read_text",
                      new=AsyncMock(side_effect=CmuxError("gone"))):
        _, name = await bot._kill_session_and_thread(thread, "surf-1")
    assert name == "will-be-gone"


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
    active = {"1": _row("1")}
    zombies, unreg = await bot._classify_threads_for_cleanup(
        threads, active, cmux_ok=True,
        surface_probe=AsyncMock(return_value="alive"),
        now=10_000)
    assert unreg == {"2"}
    assert zombies == set()


async def test_classify_marks_dead_surface_as_zombie():
    threads = [_thread(1)]
    active = {"1": _row("1", surface_id="surf-1", created_at=0)}
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
