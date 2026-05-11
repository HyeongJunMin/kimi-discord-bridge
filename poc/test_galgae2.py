#!/usr/bin/env python3
"""PoC — 갈래 2 (TUI scraping): cmux surface + wire.jsonl tail end-to-end."""
from __future__ import annotations
import asyncio, hashlib, json, logging, os, re, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from surface_io import create_workspace, create_surface, surface_send_text, surface_read_text, close_surface
from wire_tail import WireTail

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("poc.galgae2")

SESSION_RE = re.compile(r"Session:\s+([0-9a-fA-F-]{36})")
KIMI_SESSIONS_DIR = Path.home() / ".kimi" / "sessions"


def work_dir_hash(cwd: str) -> str:
    return hashlib.md5(os.path.abspath(cwd).encode()).hexdigest()


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


async def wait_for_session_uuid(surface_id: str, timeout: float = 20.0) -> str | None:
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
    poc_cwd = str(Path.cwd())
    ws_hash = work_dir_hash(poc_cwd)
    log.info("cwd=%s hash=%s", poc_cwd, ws_hash)

    # 1. Create a throw-away workspace
    log.info("creating workspace...")
    ws = await create_workspace(name=f"poc-galgae2-{int(time.time())}",
                                 cwd=poc_cwd)
    ws_id = ws.get("id") or ws.get("workspace_id")
    log.info("workspace %s created", ws_id)

    # 2. Create a terminal surface (focus=True so it stays alive)
    log.info("creating surface...")
    surf = await create_surface(workspace_id=ws_id, focus=True)
    surf_id = surf.get("surface_id") or surf.get("id")
    surf_ref = surf.get("surface_ref") or surf_id
    log.info("surface %s created (%s)", surf_id, surf_ref)

    try:
        # 3. Start kimi via send_text
        log.info("starting kimi via send_text...")
        await surface_send_text(surf_id, "kimi\n")

        # 4. Wait for kimi banner and extract session UUID
        log.info("waiting for session uuid in banner...")
        session_uuid = await wait_for_session_uuid(surf_id, timeout=25.0)
        if not session_uuid:
            log.error("timed out waiting for session uuid")
            text = await surface_read_text(surf_id)
            log.info("last surface text:\n%s", text or "(empty)")
            return
        log.info("session uuid = %s", session_uuid)

        # 5. Send a prompt FIRST (wire.jsonl is created when a turn starts)
        prompt = "Say exactly 'PONG from wire.jsonl' in Korean.\n"
        log.info("sending prompt: %r", prompt)
        await surface_send_text(surf_id, prompt)

        # 6. Find wire.jsonl
        wire_path = await find_wire_path(session_uuid, max_wait=30.0)
        if not wire_path:
            log.error("wire.jsonl not found for session %s", session_uuid)
            return
        log.info("wire.jsonl = %s", wire_path)

        # 7. Start tailing wire.jsonl
        received_events: list[dict] = []

        async def on_event(ev: dict):
            msg = ev.get("message", ev)
            ev_type = msg.get("type", "unknown")
            payload = msg.get("payload", {})

            if ev_type in ("TurnBegin", "TurnEnd", "StepBegin", "ToolCall",
                           "ToolResult", "ContentPart"):
                if ev_type == "ContentPart":
                    log.info("[%s] part_type=%s text=%r",
                             ev_type, payload.get("type"),
                             payload.get("text", payload.get("think", ""))[:120])
                elif ev_type == "ToolCall":
                    fn = payload.get("function", {})
                    log.info("[%s] %s args=%r", ev_type,
                             fn.get("name"), fn.get("arguments", "")[:200])
                elif ev_type == "ToolResult":
                    rv = payload.get("return_value", {})
                    log.info("[%s] is_error=%s output=%r", ev_type,
                             rv.get("is_error"), rv.get("output", "")[:200])
                else:
                    log.info("[%s] %s", ev_type, json.dumps(payload)[:200])
                received_events.append(msg)

        tail = WireTail(wire_path)
        tail.on_event(on_event)
        await tail.start(from_beginning=False)
        log.info("tail started")

        # 8. Wait for TurnEnd
        log.info("waiting for response (max 60s)...")
        deadline = time.time() + 60
        while time.time() < deadline:
            if any(e.get("type") == "TurnEnd" for e in received_events):
                break
            await asyncio.sleep(0.5)

        log.info("total events captured: %d", len(received_events))
        # Summarize text parts
        texts = []
        for e in received_events:
            if e.get("type") == "ContentPart":
                p = e.get("payload", {})
                if p.get("type") == "text":
                    texts.append(p.get("text", ""))
        full_text = "".join(texts)
        log.info("accumulated text response: %r", full_text)

        # 9. Also read surface plain text for comparison
        surface_text = await surface_read_text(surf_id)
        log.info("surface read_text snapshot (last 500 chars): ...%s",
                 (surface_text or "")[-500:])

        # Result
        if "PONG" in full_text or "pong" in full_text.lower():
            log.info("✅ PoC SUCCESS: wire.jsonl tail + cmux send_text works")
        else:
            log.warning("⚠️ PoC INCONCLUSIVE: expected 'PONG' not found in text")

        await tail.stop()

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
