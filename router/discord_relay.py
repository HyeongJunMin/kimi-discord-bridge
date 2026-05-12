"""Per-thread relay: tails wire.jsonl and posts to Discord with debouncing."""
from __future__ import annotations
import asyncio, hashlib, json, logging, os, re, time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord

from .wire_tail import WireTail

log = logging.getLogger(__name__)

DEBOUNCE_S = 0.8        # collect text deltas this long before flushing
MAX_MSG_CHARS = 1900    # leave headroom under Discord's 2000 limit
MAX_EDIT_CHARS = 1900   # threshold to roll over to a new message

SESSION_RE = re.compile(r"Session:\s+([0-9a-fA-F-]{36})")
DIRECTORY_RE = re.compile(r"Directory:\s+(\S+)")
KIMI_SESSIONS_DIR = Path.home() / ".kimi" / "sessions"


def work_dir_hash(cwd: str) -> str:
    expanded = os.path.expanduser(cwd)
    return hashlib.md5(os.path.abspath(expanded).encode()).hexdigest()


def compute_wire_path(session_uuid: str, cwd: str) -> Path:
    h = work_dir_hash(cwd)
    return KIMI_SESSIONS_DIR / h / session_uuid / "wire.jsonl"


async def wait_for_wire_jsonl(session_uuid: str, cwd: str, max_wait: float = 30.0) -> Path | None:
    wire_path = compute_wire_path(session_uuid, cwd)
    log.info("wait_for_wire_jsonl: cwd=%r hash=%s expected=%s", cwd, work_dir_hash(cwd), wire_path)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if wire_path.exists():
            log.info("wait_for_wire_jsonl: found at expected path")
            return wire_path
        await asyncio.sleep(0.5)
    # Fallback search
    proc = await asyncio.create_subprocess_exec(
        "find", str(KIMI_SESSIONS_DIR), "-type", "d", "-name", f"*{session_uuid}*",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    for line in out.decode().strip().splitlines():
        d = Path(line.strip())
        wire = d / "wire.jsonl"
        if wire.exists():
            log.info("wait_for_wire_jsonl: found via fallback at %s", wire)
            return wire
    log.warning("wait_for_wire_jsonl: not found for session %s cwd %s", session_uuid, cwd)
    return None


class ThreadRelay:
    """Owns one Discord thread; tails wire.jsonl and flushes text via debounce."""

    def __init__(self, thread: "discord.Thread", *, show_thoughts: bool = False,
                 show_tool_progress: bool = False):
        self.thread = thread
        self.show_thoughts = show_thoughts
        self.show_tool_progress = show_tool_progress

        self.tail: WireTail | None = None
        self._tail_started = False
        self._current_msg: "discord.Message | None" = None
        self._buffer: str = ""
        self._flush_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start_tail(self, wire_path: Path, client: "discord.Client") -> None:
        """Start tailing wire.jsonl. Call after the first turn has begun."""
        if self._tail_started:
            return
        self._tail_started = True

        self.tail = WireTail(wire_path)

        async def on_event(ev: dict):
            msg = ev.get("message", ev)
            ev_type = msg.get("type", "unknown")
            payload = msg.get("payload", {})

            if ev_type == "ContentPart" and payload.get("type") == "text":
                text = payload.get("text", "")
                if text:
                    await self._enqueue(text)
            elif ev_type == "ContentPart" and payload.get("type") == "think" and self.show_thoughts:
                think = payload.get("think", "")
                if think:
                    await self._send_now(f"*thinking…* {think[:200]}")
            elif ev_type == "ToolCall" and self.show_tool_progress:
                fn = payload.get("function", {})
                name = fn.get("name", "tool")
                await self._send_now(f"🔧 `{name}`")
            elif ev_type == "ToolResult" and self.show_tool_progress:
                rv = payload.get("return_value", {})
                is_err = rv.get("is_error", False)
                icon = "✗" if is_err else "✓"
                await self._send_now(f"{icon} `{payload.get('id', 'tool')}`")

        self.tail.on_event(on_event)
        await self.tail.start(from_beginning=False)
        log.info("tail started for thread %s", self.thread.id)

    async def ensure_tail(self, session_uuid: str, cwd: str, client: "discord.Client") -> bool:
        """Lazy-start tail when wire.jsonl becomes available."""
        if self._tail_started:
            return True
        wire_path = await wait_for_wire_jsonl(session_uuid, cwd, max_wait=30.0)
        if not wire_path:
            log.error("wire.jsonl not found for session %s", session_uuid)
            await self.thread.send("⚠️ wire.jsonl 찾기 실패 — 응답을 수신할 수 없습니다.")
            return False
        await self.start_tail(wire_path, client)
        return True

    # ── streaming text: debounce + roll-over edit ─────────────────────────────

    async def _enqueue(self, text: str) -> None:
        async with self._lock:
            self._buffer += text
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._flush_after_delay())

    async def _flush_after_delay(self) -> None:
        await asyncio.sleep(DEBOUNCE_S)
        await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            if not self._buffer:
                return
            text = self._buffer
            self._buffer = ""
        try:
            if self._current_msg is None:
                self._current_msg = await self.thread.send(text[:MAX_MSG_CHARS])
                overflow = text[MAX_MSG_CHARS:]
            else:
                combined = (self._current_msg.content or "") + text
                if len(combined) <= MAX_EDIT_CHARS:
                    await self._current_msg.edit(content=combined)
                    return
                # Roll over
                first_part_len = MAX_EDIT_CHARS - len(self._current_msg.content or "")
                if first_part_len > 0:
                    await self._current_msg.edit(
                        content=(self._current_msg.content or "") + text[:first_part_len])
                    overflow = text[first_part_len:]
                else:
                    overflow = text
                self._current_msg = await self.thread.send(overflow[:MAX_MSG_CHARS])
                overflow = overflow[MAX_MSG_CHARS:]

            while overflow:
                self._current_msg = await self.thread.send(overflow[:MAX_MSG_CHARS])
                overflow = overflow[MAX_MSG_CHARS:]
        except Exception:
            log.exception("flush failed")

    async def _send_now(self, text: str) -> None:
        await self.flush()
        try:
            await self.thread.send(text)
            self._current_msg = None
        except Exception:
            log.exception("send_now failed")

    def reset_message_anchor(self) -> None:
        """Call at end-of-turn so next stream starts a fresh message."""
        self._current_msg = None

    async def stop(self) -> None:
        # Cancel any pending debounce flush so it does not try to send
        # to a thread that may already be gone.
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("pending flush errored during stop")
        if self.tail:
            await self.tail.stop()
