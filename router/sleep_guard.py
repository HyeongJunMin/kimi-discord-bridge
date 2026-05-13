"""macOS idle-sleep guard backed by a `caffeinate` helper process."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Sequence

log = logging.getLogger(__name__)


@dataclass
class SleepGuardStatus:
    enabled: bool
    active: bool
    pid: int | None
    last_error: str | None


class SleepGuard:
    """Start `caffeinate` while active sessions exist.

    This guards against idle system sleep while allowing display sleep. It is
    intentionally process-scoped: if the bridge exits cleanly, the helper is
    terminated and macOS returns to its normal power policy.
    """

    def __init__(self, *, enabled: bool, command: Sequence[str] | None = None):
        self.enabled = enabled
        self.command = tuple(command or ("caffeinate", "-imsu"))
        self.proc: asyncio.subprocess.Process | None = None
        self.last_error: str | None = None

    def status(self) -> SleepGuardStatus:
        active = bool(self.proc and self.proc.returncode is None)
        pid = self.proc.pid if active and self.proc else None
        return SleepGuardStatus(
            enabled=self.enabled,
            active=active,
            pid=pid,
            last_error=self.last_error,
        )

    async def refresh(self, active_count: int) -> None:
        if not self.enabled:
            return
        if active_count > 0:
            await self.start()
        else:
            await self.stop()

    async def start(self) -> None:
        if not self.enabled:
            return
        if self.proc and self.proc.returncode is None:
            return
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self.last_error = None
            log.info("sleep guard started: pid=%s cmd=%s",
                     self.proc.pid, " ".join(self.command))
        except Exception as e:
            self.proc = None
            self.last_error = str(e)
            log.warning("sleep guard start failed: %s", e)

    async def stop(self) -> None:
        proc = self.proc
        self.proc = None
        if not proc or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        log.info("sleep guard stopped")

