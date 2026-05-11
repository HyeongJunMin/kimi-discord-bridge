"""Async tail of kimi-cli wire.jsonl with queued event dispatch.

Why a queue: handlers do Discord I/O (rate-limited). If we ran them inline
in the tail loop, a single 429 would block reading from disk and pile up
back-pressure on a long response. The queue lets the file reader keep up
even when Discord is slow.
"""
from __future__ import annotations
import asyncio, json, logging
from pathlib import Path
from typing import Callable, Awaitable

log = logging.getLogger(__name__)

EventHandler = Callable[[dict], Awaitable[None]]

_QUEUE_MAXSIZE = 1000          # safety cap on buffered events
_HANDLER_TIMEOUT_S = 30.0      # per-event handler timeout


class WireTail:
    """Tails a wire.jsonl file and dispatches parsed JSON events to handlers.

    Tail loop reads from disk and pushes to an asyncio.Queue.
    Dispatcher drains the queue and awaits each handler with a timeout,
    so a wedged Discord call cannot stall the entire pipeline forever.
    """

    def __init__(self, wire_path: str | Path):
        self.path = Path(wire_path)
        self._reader_task: asyncio.Task | None = None
        self._dispatcher_task: asyncio.Task | None = None
        self._closed = False
        self._handlers: list[EventHandler] = []
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)

    def on_event(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    async def start(self, *, from_beginning: bool = False) -> None:
        self._closed = False
        self._reader_task = asyncio.create_task(
            self._reader_loop(from_beginning),
            name=f"wire-tail-reader:{self.path.name}",
        )
        self._dispatcher_task = asyncio.create_task(
            self._dispatcher_loop(),
            name=f"wire-tail-dispatcher:{self.path.name}",
        )

    async def stop(self) -> None:
        self._closed = True
        for t in (self._reader_task, self._dispatcher_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    async def _reader_loop(self, from_beginning: bool) -> None:
        """Read lines from wire.jsonl, push parsed events to the queue.

        Handles file rotation (inode change) and truncation (size shrunk
        below current offset) by reopening. Partial lines (no trailing
        newline) are not consumed — we seek back and retry next tick.
        """
        # Wait until file exists initially
        while not self.path.exists() and not self._closed:
            await asyncio.sleep(0.5)
        if self._closed:
            return

        f = None
        current_inode: int | None = None
        # First-time positioning: tail-from-EOF unless caller asked otherwise.
        skip_to_eof = not from_beginning
        try:
            while not self._closed:
                try:
                    st = self.path.stat()
                except FileNotFoundError:
                    # File got deleted; wait for it to reappear.
                    if f:
                        f.close()
                        f = None
                        current_inode = None
                        skip_to_eof = False  # treat next open as a fresh file
                    await asyncio.sleep(0.5)
                    continue

                # (Re)open conditions: first open, inode change (rotation),
                # or truncation below current read offset.
                need_reopen = (
                    f is None
                    or st.st_ino != current_inode
                    or (f.tell() > st.st_size)
                )
                if need_reopen:
                    if f:
                        f.close()
                        log.info("wire.jsonl rotated/truncated, reopening")
                    f = self.path.open("r", encoding="utf-8")
                    current_inode = st.st_ino
                    if skip_to_eof:
                        f.seek(0, 2)
                        skip_to_eof = False  # only on the very first open

                pos = f.tell()
                line = f.readline()
                if not line:
                    await asyncio.sleep(0.3)
                    continue
                if not line.endswith("\n"):
                    # Partial line — writer hasn't flushed end of record yet.
                    # Seek back and retry next tick so we don't fragment a
                    # JSON object across two readline() calls.
                    f.seek(pos)
                    await asyncio.sleep(0.1)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("bad jsonl: %s", line[:200])
                    continue
                try:
                    self._queue.put_nowait(ev)
                except asyncio.QueueFull:
                    # Dispatcher fell far behind; drop oldest, retry once.
                    try:
                        _ = self._queue.get_nowait()
                        log.warning("event queue full, dropped oldest")
                        self._queue.put_nowait(ev)
                    except asyncio.QueueEmpty:
                        pass
        finally:
            if f:
                f.close()

    async def _dispatcher_loop(self) -> None:
        """Drain the queue and fan out to handlers with per-call timeout."""
        while not self._closed:
            try:
                ev = await self._queue.get()
            except asyncio.CancelledError:
                raise
            for h in self._handlers:
                try:
                    await asyncio.wait_for(h(ev), timeout=_HANDLER_TIMEOUT_S)
                except asyncio.TimeoutError:
                    log.warning("handler timed out after %ss", _HANDLER_TIMEOUT_S)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("event handler failed")

    def __repr__(self) -> str:
        return f"WireTail({self.path})"
