"""Smoke test: cmux surface + wire.jsonl tail without Discord.

Usage:
  cd kimi-discord-bridge-acp/
  pip install -r requirements.txt
  python smoke.py
"""
import asyncio, sys, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Load .env so KIMI_CMD / MOONSHOT_API_KEY pick up
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

from router.cmux_client import create_workspace, create_surface, surface_send_text, surface_read_text, close_surface
from router.discord_relay import SESSION_RE, wait_for_wire_jsonl


async def main():
    cwd = str(Path(__file__).resolve().parent)
    print(f"Smoke test: cwd={cwd}", flush=True)

    # 1. Create workspace
    ws = await create_workspace(name="smoke-test", cwd=cwd)
    ws_id = ws.get("id") or ws.get("workspace_id")
    print(f"workspace {ws_id} created", flush=True)

    # 2. Create surface
    surf = await create_surface(ws_id, focus=True)
    surf_id = surf.get("surface_id") or surf.get("id")
    print(f"surface {surf_id} created", flush=True)

    try:
        # 3. Start kimi
        await surface_send_text(surf_id, "kimi\n")
        print("kimi started", flush=True)

        # 4. Wait for session UUID
        deadline = asyncio.get_event_loop().time() + 25
        session_uuid = None
        while asyncio.get_event_loop().time() < deadline:
            text = await surface_read_text(surf_id)
            if text:
                m = SESSION_RE.search(text)
                if m:
                    session_uuid = m.group(1)
                    break
            await asyncio.sleep(0.5)

        if not session_uuid:
            print("FAIL: session UUID not found", flush=True)
            return False
        print(f"session uuid={session_uuid}", flush=True)

        # 5. Send prompt (triggers wire.jsonl creation)
        await surface_send_text(surf_id, "Reply with exactly: SMOKE_OK\n")
        print("prompt sent", flush=True)

        # 6. Wait for wire.jsonl
        wire_path = await wait_for_wire_jsonl(session_uuid, cwd, max_wait=30.0)
        if not wire_path:
            print("FAIL: wire.jsonl not found", flush=True)
            return False
        print(f"wire.jsonl={wire_path}", flush=True)

        # 7. Tail wire.jsonl
        from router.wire_tail import WireTail
        texts = []
        turn_ended = False

        async def on_event(ev: dict):
            nonlocal turn_ended
            msg = ev.get("message", ev)
            ev_type = msg.get("type", "unknown")
            if ev_type == "TurnEnd":
                turn_ended = True
            if ev_type == "ContentPart":
                p = msg.get("payload", {})
                if p.get("type") == "text":
                    texts.append(p.get("text", ""))

        tail = WireTail(wire_path)
        tail.on_event(on_event)
        await tail.start(from_beginning=False)

        # 8. Wait for TurnEnd
        deadline = asyncio.get_event_loop().time() + 60
        while asyncio.get_event_loop().time() < deadline and not turn_ended:
            await asyncio.sleep(0.5)

        await tail.stop()

        full = "".join(texts)
        print(f"response: {full!r}", flush=True)

        ok = "SMOKE_OK" in full
        print(f"\n=== smoke {'PASS' if ok else 'FAIL'} ===", flush=True)
        return ok

    finally:
        await close_surface(surf_id)


ok = asyncio.run(main())
sys.exit(0 if ok else 1)
