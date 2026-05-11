"""PoC: async tail of kimi-cli wire.jsonl."""
from __future__ import annotations
import asyncio, json, logging
from pathlib import Path
from typing import Callable, Awaitable

log = logging.getLogger(__name__)

EventHandler = Callable[[dict], Awaitable[None]]


class WireTail:
    """Tails a wire.jsonl file and fires parsed JSON events."""

    def __init__(self, wire_path: str | Path):
        self.path = Path(wire_path)
        self._task: asyncio.Task | None = None
        self._closed = False
        self._handlers: list[EventHandler] = []

    def on_event(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    async def start(self, *, from_beginning: bool = False) -> None:
        self._closed = False
        self._task = asyncio.create_task(self._tail_loop(from_beginning))

    async def stop(self) -> None:
        self._closed = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _tail_loop(self, from_beginning: bool) -> None:
        # Wait until file exists
        while not self.path.exists() and not self._closed:
            await asyncio.sleep(0.5)
        if self._closed:
            return

        with self.path.open("r", encoding="utf-8") as f:
            if not from_beginning:
                f.seek(0, 2)  # jump to EOF
            while not self._closed:
                line = f.readline()
                if not line:
                    await asyncio.sleep(0.3)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("bad jsonl: %s", line[:200])
                    continue
                for h in self._handlers:
                    try:
                        await h(ev)
                    except Exception:
                        log.exception("event handler failed")

    def __repr__(self) -> str:
        return f"WireTail({self.path})"
