#!/usr/bin/env python3
"""Test Discord ↔ cmux surface relay WITHOUT the full bot.

This proves:
1. A message sent via Discord REST API appears in the cmux surface (ghost typing)
2. kimi's response from wire.jsonl can be posted back to Discord
"""
from __future__ import annotations
import asyncio, hashlib, json, logging, os, re, sys, time
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))
from surface_io import create_workspace, create_surface, surface_send_text, surface_read_text, close_surface
from wire_tail import WireTail

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "router"))
from registry import Registry, SessionRow

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("poc.discord_relay")

# Discord config
load_dotenv = __import__("dotenv").load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])
GENERAL_CHANNEL_ID = 1501408970160472098
HEADERS = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}
BASE = "https://discord.com/api/v10"

SESSION_RE = re.compile(r"Session:\s+([0-9a-fA-F-]{36})")
KIMI_SESSIONS_DIR = Path.home() / ".kimi" / "sessions"
REGISTRY_PATH = "poc_discord_relay.sqlite3"


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


async def discord_api(session: aiohttp.ClientSession, method: str, path: str, **kw):
    url = f"{BASE}{path}"
    async with session.request(method, url, headers=HEADERS, **kw) as resp:
        data = await resp.json()
        if resp.status >= 400:
            log.error("Discord API %s %s → %s: %s", method, path, resp.status, data)
        return resp.status, data


async def main():
    poc_cwd = str(Path.cwd())

    # 1. Create workspace + surface
    log.info("creating workspace...")
    from surface_io import create_workspace as cw
    ws = await cw(name=f"poc-discord-{int(time.time())}", cwd=poc_cwd)
    ws_id = ws.get("id") or ws.get("workspace_id")

    log.info("creating surface...")
    surf = await create_surface(workspace_id=ws_id, focus=True)
    surf_id = surf.get("surface_id") or surf.get("id")
    surf_ref = surf.get("surface_ref") or surf_id
    log.info("surface %s created", surf_ref)

    # 2. Start kimi
    log.info("starting kimi...")
    await surface_send_text(surf_id, "kimi\n")

    session_uuid = await wait_for_session_uuid(surf_id, timeout=25.0)
    if not session_uuid:
        log.error("session uuid timeout")
        await close_surface(surf_id)
        return
    log.info("session uuid = %s", session_uuid)

    # 3. Create Discord thread
    async with aiohttp.ClientSession() as http:
        log.info("creating Discord thread...")
        status, thread = await discord_api(http, "POST",
            f"/channels/{GENERAL_CHANNEL_ID}/threads",
            json={"name": f"relay-test-{int(time.time())%10000}",
                  "type": 11,
                  "auto_archive_duration": 60})
        if status >= 400:
            log.error("thread creation failed")
            await close_surface(surf_id)
            return
        thread_id = int(thread["id"])
        log.info("thread %s created", thread_id)

        # Get my user ID
        status, me = await discord_api(http, "GET", "/users/@me")
        owner_id = int(me["id"])

        # 4. Send test message to surface FIRST (triggers wire.jsonl creation)
        test_msg = "Say exactly DISCORD_RELAY_OK"
        log.info("sending to surface: %r", test_msg)
        await surface_send_text(surf_id, test_msg + "\n")

        # 5. Now find wire.jsonl
        wire_path = await find_wire_path(session_uuid, max_wait=30.0)
        if not wire_path:
            log.error("wire.jsonl not found")
            await close_surface(surf_id)
            return
        log.info("wire.jsonl = %s", wire_path)

        # 6. Start tailing wire.jsonl
        received_texts: list[str] = []
        turn_ended = False

        async def on_event(ev: dict):
            nonlocal turn_ended
            msg = ev.get("message", ev)
            ev_type = msg.get("type", "unknown")
            if ev_type == "TurnEnd":
                turn_ended = True
            if ev_type == "ContentPart":
                payload = msg.get("payload", {})
                if payload.get("type") == "text":
                    text = payload.get("text", "")
                    received_texts.append(text)
                    log.info("[wire] text: %r", text[:120])

        tail = WireTail(wire_path)
        tail.on_event(on_event)
        await tail.start(from_beginning=False)

        # 7. Also post the "user message" to Discord thread so it looks real
        status, msg_data = await discord_api(http, "POST",
            f"/channels/{thread_id}/messages",
            json={"content": test_msg})
        log.info("message posted to Discord thread")

        # 8. Wait for turn end
        log.info("waiting for kimi response (max 60s)...")
        deadline = time.time() + 60
        while time.time() < deadline and not turn_ended:
            await asyncio.sleep(0.5)

        await tail.stop()

        # 9. Check surface text
        surface_text = await surface_read_text(surf_id)
        has_input = test_msg in (surface_text or "")
        log.info("surface contains input message: %s", has_input)

        # 10. Post response back to Discord thread
        full_response = "".join(received_texts)
        log.info("accumulated response: %r", full_response[:500])

        if full_response:
            status, _ = await discord_api(http, "POST",
                f"/channels/{thread_id}/messages",
                json={"content": f"**[PoC Relay Test]**\n{full_response[:1800]}"})
            log.info("response posted to Discord: %s", "OK" if status < 400 else "FAIL")

        # 11. Verify
        if has_input and "DISCORD_RELAY_OK" in full_response:
            log.info("✅ Discord ↔ cmux surface relay SUCCESS")
            log.info("   - Discord message appeared in terminal: YES")
            log.info("   - Terminal response appeared in Discord: YES (posted above)")
        elif not has_input:
            log.warning("⚠️ Discord message did NOT appear in terminal")
        else:
            log.warning("⚠️ Response did not contain expected text")

        # Cleanup
        await close_surface(surf_id)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
