#!/usr/bin/env python3
"""PoC — 하이브리드: wire.jsonl tail + PreToolUse hook round-trip (simplified)."""
from __future__ import annotations
import asyncio, json, logging, os, re, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from surface_io import create_workspace, create_surface, surface_send_text, surface_read_text, close_surface
from wire_tail import WireTail

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("poc.hybrid")

SESSION_RE = re.compile(r"Session:\s+([0-9a-fA-F-]{36})")
KIMI_SESSIONS_DIR = Path.home() / ".kimi" / "sessions"
HOOK_IN = Path("/tmp/poc_hook_in.jsonl")
HOOK_OUT = Path("/tmp/poc_hook_out.jsonl")


async def find_wire_path(session_uuid: str, max_wait: float = 30.0) -> Path | None:
    deadline = time.time() + max_wait
    while time.time() < deadline:
        proc = await asyncio.create_subprocess_exec(
            "find", str(KIMI_SESSIONS_DIR), "-type", "d", "-name", f"*{session_uuid}*",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        for line in out.decode().strip().splitlines():
            d = Path(line.strip())
            wire = d / "wire.jsonl"
            if wire.exists():
                return wire
        await asyncio.sleep(0.5)
    return None


async def wait_for_session_uuid(surface_id: str, timeout: float = 25.0) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = await surface_read_text(surface_id)
        if text:
            m = SESSION_RE.search(text)
            if m:
                return m.group(1)
        await asyncio.sleep(0.5)
    return None


async def main():
    # 0. Prepare hook files
    if HOOK_IN.exists():
        HOOK_IN.unlink()
    if HOOK_OUT.exists():
        HOOK_OUT.unlink()

    # 1. Create workspace
    log.info("creating workspace...")
    ws = await create_workspace(name=f"poc-hybrid-{int(time.time())}",
                                 cwd=str(Path.cwd()))
    ws_id = ws.get("id") or ws.get("workspace_id")
    log.info("workspace %s created", ws_id)

    # 2. Create surface
    log.info("creating surface...")
    surf = await create_surface(workspace_id=ws_id, focus=True)
    surf_id = surf.get("surface_id") or surf.get("id")
    log.info("surface %s created (%s)", surf_id, surf.get("surface_ref"))

    try:
        # 3. Start kimi
        log.info("starting kimi...")
        await surface_send_text(surf_id, "kimi\n")

        # 4. Wait for banner
        session_uuid = await wait_for_session_uuid(surf_id, timeout=25.0)
        if not session_uuid:
            log.error("timed out waiting for session uuid")
            return
        log.info("session uuid = %s", session_uuid)

        # 5. Send prompt FIRST
        prompt = "Create a file named hybrid_test.txt with content 'hello from hybrid'.\n"
        log.info("sending prompt: %r", prompt)
        await surface_send_text(surf_id, prompt)

        # 6. Find wire.jsonl
        wire_path = await find_wire_path(session_uuid, max_wait=30.0)
        if not wire_path:
            log.error("wire.jsonl not found for session %s", session_uuid)
            return
        log.info("wire.jsonl = %s", wire_path)

        # 7. Tail wire.jsonl
        received_events: list[dict] = []
        async def on_event(ev: dict):
            msg = ev.get("message", ev)
            ev_type = msg.get("type", "unknown")
            if ev_type in ("TurnBegin", "TurnEnd", "StepBegin", "ToolCall",
                           "ToolResult", "ContentPart"):
                received_events.append(msg)
                payload = msg.get("payload", {})
                if ev_type == "ContentPart":
                    log.info("[%s] type=%s text=%r", ev_type, payload.get("type"),
                             payload.get("text", payload.get("think", ""))[:120])
                elif ev_type == "ToolCall":
                    fn = payload.get("function", {})
                    log.info("[%s] %s", ev_type, fn.get("name"))
                elif ev_type == "ToolResult":
                    rv = payload.get("return_value", {})
                    log.info("[%s] is_error=%s output=%r", ev_type,
                             rv.get("is_error"), rv.get("output", "")[:200])

        tail = WireTail(wire_path)
        tail.on_event(on_event)
        await tail.start(from_beginning=False)

        # 8. Wait a bit for hook to fire, then write BLOCK
        log.info("waiting for hook request (max 10s)...")
        deadline = time.time() + 10
        hook_tool = None
        while time.time() < deadline:
            if HOOK_IN.exists():
                text = HOOK_IN.read_text()
                lines = [l.strip() for l in text.splitlines() if l.strip()]
                if lines:
                    try:
                        data = json.loads(lines[-1])
                        hook_tool = data.get("tool_name")
                    except json.JSONDecodeError:
                        pass
            if hook_tool:
                break
            await asyncio.sleep(0.3)

        if hook_tool:
            log.info("hook requested for %s → writing BLOCK", hook_tool)
            with open(HOOK_OUT, "a") as f:
                f.write("block\n")
        else:
            log.warning("no hook request observed")

        # 9. Wait for TurnEnd
        log.info("waiting for turn end (max 60s)...")
        deadline = time.time() + 60
        while time.time() < deadline:
            if any(e.get("type") == "TurnEnd" for e in received_events):
                break
            await asyncio.sleep(0.5)

        await tail.stop()

        # 10. Analyze
        tool_results = [e for e in received_events if e.get("type") == "ToolResult"]
        tool_calls = [e for e in received_events if e.get("type") == "ToolCall"]
        texts = [e.get("payload", {}).get("text", "")
                 for e in received_events
                 if e.get("type") == "ContentPart" and e.get("payload", {}).get("type") == "text"]
        full_text = "".join(texts)

        log.info("tool_calls: %d, tool_results: %d", len(tool_calls), len(tool_results))
        log.info("text response: %r", full_text[:500])

        blocked = False
        if tool_calls and not tool_results:
            blocked = True
        elif tool_results:
            for tr in tool_results:
                if tr.get("payload", {}).get("return_value", {}).get("is_error"):
                    blocked = True

        if hook_tool and blocked:
            log.info("✅ HYBRID PoC SUCCESS: Hook round-trip + wire.jsonl tail both work")
        elif hook_tool and not blocked:
            log.info("⚠️ Hook fired but tool was not blocked")
        else:
            log.info("⚠️ No hook activity observed")

    finally:
        log.info("closing surface %s...", surf_id)
        try:
            await close_surface(surf_id)
        except Exception as e:
            log.warning("close_surface failed: %s", e)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
