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
