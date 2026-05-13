from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from router.sleep_guard import SleepGuard


class FakeProc:
    def __init__(self, pid: int = 123):
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


async def test_sleep_guard_starts_caffeinate_when_active(monkeypatch):
    proc = FakeProc()
    create = AsyncMock(return_value=proc)
    monkeypatch.setattr("router.sleep_guard.asyncio.create_subprocess_exec", create)

    guard = SleepGuard(enabled=True)
    await guard.refresh(1)

    create.assert_awaited_once_with(
        "caffeinate", "-imsu",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    status = guard.status()
    assert status.active is True
    assert status.pid == 123


async def test_sleep_guard_stops_when_no_active_sessions(monkeypatch):
    proc = FakeProc()
    monkeypatch.setattr(
        "router.sleep_guard.asyncio.create_subprocess_exec",
        AsyncMock(return_value=proc),
    )

    guard = SleepGuard(enabled=True)
    await guard.refresh(1)
    await guard.refresh(0)

    assert proc.terminated is True
    assert guard.status().active is False


async def test_sleep_guard_disabled_is_noop(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr("router.sleep_guard.asyncio.create_subprocess_exec", create)

    guard = SleepGuard(enabled=False)
    await guard.refresh(1)

    create.assert_not_awaited()
    assert guard.status().enabled is False
