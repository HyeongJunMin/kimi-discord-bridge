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


async def find_live_kimi_surface_ids() -> set[str]:
    """Return cmux surface UUIDs whose tty is currently owned by a kimi-cli
    process.

    Cross-reference:
      1. cmux `system.tree` RPC → per-surface `tty` (e.g. "ttys011")
      2. `ps -axo tty,command` filtered to kimi-cli (setproctitle: "Kimi Code")
      3. Intersect on tty.

    A surface in this set is *guaranteed* to be running a live kimi-cli
    (not just displaying a stale banner from a closed session). This is
    the strongest liveness signal we have — stronger than wire.jsonl
    mtime, since kimi closes the file between writes so an idle but
    live session shows old mtime.
    """
    from .cmux_client import system_tree, CmuxError  # local import to avoid cycle

    # Step 1 — kimi-cli ttys from ps
    proc = await asyncio.create_subprocess_exec(
        "ps", "-axo", "tty,command",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    kimi_ttys: set[str] = set()
    for line in out.decode().splitlines():
        # kimi-cli uses setproctitle("Kimi Code"); be liberal about case
        # and also accept "kimi-cli" in case proctitle isn't set.
        if "Kimi Code" not in line and "kimi-cli" not in line.lower():
            continue
        parts = line.split(None, 1)
        if not parts:
            continue
        tty = parts[0].strip()
        if not tty or tty == "??" or tty == "TTY":
            continue
        # ps prints "ttys011"; cmux reports the same. Normalize: strip any
        # leading "/dev/" just in case.
        kimi_ttys.add(tty.removeprefix("/dev/"))
    if not kimi_ttys:
        return set()

    # Step 2 — cmux tree → surface uuid → tty
    try:
        tree = await system_tree()
    except CmuxError:
        return set()

    live: set[str] = set()

    def walk(o):
        if isinstance(o, dict):
            if o.get("type") == "terminal" and o.get("tty") and o.get("id"):
                if o["tty"] in kimi_ttys:
                    live.add(o["id"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(tree)
    return live


async def wait_for_wire_jsonl(session_uuid: str, cwd: str, max_wait: float = 60.0) -> Path | None:
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

    async def start_tail(self, wire_path: Path, client: "discord.Client",
                         *, from_beginning: bool = False) -> None:
        """Start tailing wire.jsonl. Call after the first turn has begun.

        from_beginning=True is used on the very first tail start so we can
        replay events kimi-cli wrote while we were waiting for the file.
        """
        if self._tail_started:
            return
        self._tail_started = True

        self.tail = WireTail(wire_path)
        self.tail.on_event(self._handle_wire_event)
        await self.tail.start(from_beginning=from_beginning)
        log.info("tail started for thread %s (from_beginning=%s)",
                 self.thread.id, from_beginning)

    async def _handle_wire_event(self, ev: dict) -> None:
        """Dispatch a single wire.jsonl event to the appropriate output path.

        Extracted from start_tail's closure so it can be unit-tested directly.
        """
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

    async def ensure_tail(self, session_uuid: str, cwd: str, client: "discord.Client",
                          *, from_beginning: bool = False) -> bool:
        """Lazy-start tail when wire.jsonl becomes available."""
        if self._tail_started:
            return True
        wire_path = await wait_for_wire_jsonl(session_uuid, cwd, max_wait=60.0)
        if not wire_path:
            log.error("wire.jsonl not found for session %s", session_uuid)
            await self.thread.send("⚠️ wire.jsonl 찾기 실패 — 응답을 수신할 수 없습니다.")
            return False
        await self.start_tail(wire_path, client, from_beginning=from_beginning)
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
