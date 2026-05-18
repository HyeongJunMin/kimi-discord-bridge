"""Kimi Agent SDK fallback client.

Python cannot import the Node-only Kimi Agent SDK directly, so this module
talks to a tiny Node helper over JSONL stdio. The helper owns SDK sessions
(`kimi --wire` child processes); the Python side owns routing and Discord IO.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_HELPER = Path(__file__).with_name("kimi_wire_bridge.mjs")
MAX_DISCORD_MESSAGE = 1900

# Wire fallback exists to keep a Kimi session usable when cmux is unreachable
# (e.g. screen locked during /new). By default we mirror the cmux relay's
# user-facing output and hide internal noise: tool plumbing, raw thinking
# blocks, and per-turn debug breadcrumbs. Opt-in flags re-enable them for
# debugging.
_SHOW_TOOLS = os.environ.get("KIMI_WIRE_SHOW_TOOLS", "").lower() in {"1", "true", "yes"}
_SHOW_THINKING = os.environ.get("KIMI_WIRE_SHOW_THINKING", "").lower() in {"1", "true", "yes"}

# Common absolute paths where `node` lives on macOS dev machines. Tried in
# order when PATH does not contain node — happens whenever the bot is
# launched from a stock-PATH shell (launchd, `nohup ./run-bot.sh`) that
# never sourced ~/.zshrc.
_NODE_FALLBACK_PATHS = (
    "/opt/homebrew/bin/node",
    "/usr/local/bin/node",
)


def _resolve_node_cmd() -> str:
    """Locate the node binary. Honors KIMI_NODE_CMD, then PATH, then a
    short list of well-known absolute paths. Returns the literal "node" as
    a last resort so spawn raises a clear FileNotFoundError instead of
    silently doing something else."""
    explicit = os.environ.get("KIMI_NODE_CMD")
    if explicit:
        return explicit
    found = shutil.which("node")
    if found:
        return found
    for candidate in _NODE_FALLBACK_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "node"


class KimiWireError(RuntimeError):
    pass


def _split_message(text: str, limit: int = MAX_DISCORD_MESSAGE) -> list[str]:
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return chunks


def _content_part_text(payload: dict[str, Any]) -> str:
    part_type = payload.get("type")
    if part_type == "text":
        return str(payload.get("text") or "")
    if part_type == "think":
        if not _SHOW_THINKING:
            return ""
        text = str(payload.get("think") or "")
        if len(text) > 3500:
            text = text[:3500] + "..."
        return f"*Thinking...*\n```\n{text}\n```\n"
    if part_type in {"image_url", "audio_url", "video_url"}:
        url_obj = payload.get(part_type)
        url = ""
        if isinstance(url_obj, dict):
            url = str(url_obj.get("url") or "")
        else:
            url = str(payload.get("url") or "")
        return f"[{part_type}] {url}\n"
    return ""


def _tool_result_text(payload: dict[str, Any]) -> str:
    raw = payload.get("return_value", "")
    if isinstance(raw, str):
        text = raw
    else:
        try:
            text = json.dumps(raw, ensure_ascii=False, indent=2)
        except Exception:
            text = str(raw)
    if len(text) > 1500:
        text = text[:1500] + "..."
    return f"\nTool result:\n```\n{text}\n```\n"


def _is_ignorable_wire_error(event: dict[str, Any], payload: dict[str, Any]) -> bool:
    message = str(event.get("message") or payload.get("message") or "")
    return message in {
        "Unknown event type: MCPLoadingBegin",
        "Unknown event type: MCPLoadingEnd",
    }


async def _send_text(thread: Any, text: str) -> None:
    for chunk in _split_message(text):
        result = thread.send(chunk)
        if asyncio.iscoroutine(result):
            await result


@dataclass
class KimiWireSession:
    name: str
    session_id: str
    work_dir: str
    client: "KimiWireClient"
    active_turn: bool = False

    @property
    def session_uuid(self) -> str:
        """Compatibility alias for the existing cmux-backed session object."""
        return self.session_id

    async def prompt(self, content: str, thread: Any) -> None:
        self.active_turn = True
        buffer: list[str] = []
        try:
            async for event in self.client.prompt(self.name, content):
                event_type = event.get("type")
                payload = event.get("payload") or {}
                if event_type == "TurnBegin":
                    # Internal turn boundary — no user-facing noise.
                    log.debug("wire TurnBegin")
                elif event_type == "ContentPart":
                    buffer.append(_content_part_text(payload))
                elif event_type == "ToolCall":
                    if _SHOW_TOOLS:
                        fn = ((payload.get("function") or {}).get("name")
                              if isinstance(payload, dict) else None)
                        await _send_text(thread, f"🔧 `{fn or 'tool'}`")
                    else:
                        log.debug("hidden wire ToolCall: %s", payload)
                elif event_type == "ToolResult":
                    if _SHOW_TOOLS:
                        buffer.append(_tool_result_text(payload))
                    else:
                        log.debug("hidden wire ToolResult")
                elif event_type == "ApprovalRequest":
                    await _send_text(
                        thread,
                        "Approval requested in wire fallback. "
                        "Use cmux after restore or approve via a later bridge command.",
                    )
                elif event_type == "QuestionRequest":
                    questions = payload.get("questions") or []
                    await _send_text(thread, "Question: " + " / ".join(map(str, questions)))
                elif event_type == "TurnEnd":
                    if buffer:
                        await _send_text(thread, "".join(buffer))
                        buffer.clear()
                elif event_type in {"TurnError", "ParseError", "error"}:
                    if _is_ignorable_wire_error(event, payload):
                        log.debug("ignored Kimi wire status parse error: %s", event)
                        continue
                    message = event.get("message") or payload.get("message") or event
                    await _send_text(thread, f"wire fallback error: {message}")
                else:
                    log.debug("ignored Kimi wire event: %s", event_type)
            if buffer:
                await _send_text(thread, "".join(buffer))
        finally:
            self.active_turn = False

    async def close(self) -> None:
        await self.client.close(self.name)


class KimiWireClient:
    def __init__(self, command: list[str] | None = None):
        helper = os.environ.get("KIMI_WIRE_BRIDGE_CMD")
        if command is not None:
            self.command = command
        elif helper:
            self.command = shlex.split(helper)
        else:
            self.command = [_resolve_node_cmd(), str(DEFAULT_HELPER)]
        self.proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Queue[dict[str, Any]]] = {}

    async def start(self) -> None:
        if self.proc and self.proc.returncode is None:
            return
        # `os.setpgrp` puts the node helper in its own process group so that
        # `stop()` can kill the helper *and* its kimi-cli grandchild as a
        # unit. Without this, SIGKILLing the bot leaves the node helper and
        # its spawned kimi-cli orphaned to launchd (PPID=1). Those orphans
        # stay attached to the same Kimi session_id the cmux side later
        # resumes, producing duplicate Discord responses.
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setpgrp,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        asyncio.create_task(self._read_stderr())

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        async for raw in self.proc.stdout:
            try:
                msg = json.loads(raw.decode())
            except Exception:
                log.warning("invalid Kimi wire helper output: %r", raw)
                continue
            req_id = msg.get("id")
            queue = self._pending.get(req_id)
            if queue:
                await queue.put(msg)

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        async for raw in self.proc.stderr:
            log.warning("Kimi wire helper stderr: %s", raw.decode().rstrip())

    async def _send(self, payload: dict[str, Any]) -> asyncio.Queue[dict[str, Any]]:
        await self.start()
        if not self.proc or not self.proc.stdin:
            raise KimiWireError("Kimi wire helper is not running")
        req_id = self._next_id
        self._next_id += 1
        payload = {"id": req_id, **payload}
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._pending[req_id] = queue
        self.proc.stdin.write((json.dumps(payload) + "\n").encode())
        await self.proc.stdin.drain()
        return queue

    async def create_session(self, *, name: str, work_dir: str,
                             session_id: str | None = None,
                             model: str | None = None,
                             yolo: bool | None = None) -> KimiWireSession:
        queue = await self._send({
            "op": "start",
            "name": name,
            "workDir": work_dir,
            "sessionId": session_id,
            "model": model,
            "yolo": yolo,
        })
        msg: dict[str, Any] = {}
        try:
            msg = await queue.get()
            if msg.get("error"):
                raise KimiWireError(str(msg["error"]))
            sid = msg.get("sessionId")
            if not isinstance(sid, str) or not sid:
                raise KimiWireError("Kimi wire helper returned no sessionId")
            return KimiWireSession(name=name, session_id=sid,
                                   work_dir=work_dir, client=self)
        finally:
            self._pending.pop(msg.get("id", -1), None)

    async def prompt(self, name: str, content: str):
        queue = await self._send({"op": "prompt", "name": name, "content": content})
        req_id: int | None = None
        try:
            while True:
                msg = await queue.get()
                req_id = msg.get("id")
                if msg.get("event"):
                    yield msg["event"]
                    continue
                if msg.get("error"):
                    raise KimiWireError(str(msg["error"]))
                if msg.get("done"):
                    break
        finally:
            if req_id is not None:
                self._pending.pop(req_id, None)

    async def close(self, name: str) -> None:
        queue = await self._send({"op": "close", "name": name})
        msg = await queue.get()
        self._pending.pop(msg.get("id", -1), None)
        if msg.get("error"):
            raise KimiWireError(str(msg["error"]))

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            pid = self.proc.pid
            # The helper was started with its own pgrp (see `start()`).
            # Signal the entire group so the kimi-cli grandchild dies with
            # the node helper — single `terminate()` would only hit node and
            # leave kimi-cli orphaned.
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as e:
                log.warning("killpg SIGTERM failed for helper pid=%s: %s", pid, e)
                self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    self.proc.kill()
                await self.proc.wait()
        if self._reader_task:
            self._reader_task.cancel()
