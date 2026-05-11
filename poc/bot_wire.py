#!/usr/bin/env python3
"""PoC Discord bot using wire.jsonl + cmux instead of ACP.

Usage:
  cd kimi-discord-bridge-acp/
  source .venv/bin/activate
  python3 poc/bot_wire.py

Then in Discord:
  1. Type /new in a text channel
  2. Select a workspace
  3. A thread is created — type a message there
  4. Watch the cmux surface for ghost typing + kimi response
  5. Watch Discord thread for kimi's reply
"""
from __future__ import annotations
import asyncio, hashlib, json, logging, os, re, time
from pathlib import Path

import discord
from discord import app_commands

# Add parent for router/ and poc/ imports
sys = __import__("sys")
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "router"))

from surface_io import create_workspace, create_surface, surface_send_text, surface_read_text, close_surface  # noqa: E402
from wire_tail import WireTail  # noqa: E402
from cmux_client import list_workspaces, create_workspace as ws_create, CmuxError  # noqa: E402
from registry import Registry, SessionRow  # noqa: E402

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("poc.bot_wire")

GUILD_ID = int(os.environ["DISCORD_GUILD_ID"]) if os.environ.get("DISCORD_GUILD_ID") else None
DEFAULT_WORK_DIR = os.environ.get("DEFAULT_WORK_DIR", os.path.expanduser("~"))
REGISTRY_PATH = os.environ.get("SESSION_DB_PATH", "poc_wire.sqlite3")

SESSION_RE = re.compile(r"Session:\s+([0-9a-fA-F-]{36})")
KIMI_SESSIONS_DIR = Path.home() / ".kimi" / "sessions"


def work_dir_hash(cwd: str) -> str:
    return hashlib.md5(os.path.abspath(cwd).encode()).hexdigest()


def compute_wire_path(session_uuid: str, cwd: str) -> Path:
    """Directly compute wire.jsonl path from session uuid and cwd."""
    h = work_dir_hash(cwd)
    return KIMI_SESSIONS_DIR / h / session_uuid / "wire.jsonl"


async def wait_for_wire_jsonl(session_uuid: str, cwd: str, max_wait: float = 30.0) -> Path | None:
    """Wait for wire.jsonl to appear (created when first turn starts)."""
    wire_path = compute_wire_path(session_uuid, cwd)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if wire_path.exists():
            return wire_path
        await asyncio.sleep(0.5)
    # Fallback: search
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


# ── Per-session state ─────────────────────────────────────────────────────────

class WireSession:
    def __init__(self, thread_id: int, surface_id: str, session_uuid: str,
                 cwd: str, owner_id: int):
        self.thread_id = thread_id
        self.surface_id = surface_id
        self.session_uuid = session_uuid
        self.cwd = cwd
        self.owner_id = owner_id
        self.tail: WireTail | None = None
        self._tail_started = False
        self._current_msg: discord.Message | None = None
        self._buffer: str = ""
        self._flush_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def _ensure_tail_started(self, client: discord.Client):
        if self._tail_started:
            return
        self._tail_started = True

        wire_path = await wait_for_wire_jsonl(self.session_uuid, self.cwd, max_wait=30.0)
        if not wire_path:
            log.error("wire.jsonl not found for session %s", self.session_uuid)
            thread = client.get_channel(self.thread_id)
            if thread:
                await thread.send("⚠️ wire.jsonl 찾기 실패 — 응답을 수신할 수 없습니다.")
            return

        self.tail = WireTail(wire_path)

        async def on_event(ev: dict):
            msg = ev.get("message", ev)
            ev_type = msg.get("type", "unknown")
            if ev_type != "ContentPart":
                return
            payload = msg.get("payload", {})
            if payload.get("type") != "text":
                return
            text = payload.get("text", "")
            if not text:
                return
            await self._enqueue(text, client)

        self.tail.on_event(on_event)
        await self.tail.start(from_beginning=False)
        log.info("tail started for thread %s", self.thread_id)

    async def _enqueue(self, text: str, client: discord.Client):
        async with self._lock:
            self._buffer += text
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._flush_after_delay(client))

    async def _flush_after_delay(self, client: discord.Client):
        await asyncio.sleep(0.8)
        await self._flush(client)

    async def _flush(self, client: discord.Client):
        async with self._lock:
            if not self._buffer:
                return
            text = self._buffer
            self._buffer = ""
        try:
            thread = client.get_channel(self.thread_id)
            if not thread:
                return
            if self._current_msg is None:
                self._current_msg = await thread.send(text[:1900])
            else:
                combined = (self._current_msg.content or "") + text
                if len(combined) <= 1900:
                    await self._current_msg.edit(content=combined)
                else:
                    self._current_msg = await thread.send(text[:1900])
        except Exception:
            log.exception("flush failed")

    def reset_anchor(self):
        self._current_msg = None

    async def send_to_surface(self, text: str, client: discord.Client):
        await self._ensure_tail_started(client)
        await surface_send_text(self.surface_id, text + "\n")

    async def stop(self):
        if self.tail:
            await self.tail.stop()
        try:
            await close_surface(self.surface_id)
        except Exception as e:
            log.warning("close_surface failed: %s", e)


# ── Bot ───────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

registry = Registry(REGISTRY_PATH)
sessions: dict[int, WireSession] = {}


@tree.command(name="new", description="새 kimi-cli 세션을 시작합니다 (cmux + wire.jsonl)")
async def new_session_cmd(interaction: discord.Interaction):
    try:
        wss = await list_workspaces()
    except CmuxError as e:
        await interaction.response.send_message(f"cmux 호출 실패: {e}", ephemeral=True)
        return

    _ws_cwd_map: dict[str, str] = {}
    options = []
    for w in wss[:24]:
        label = (w.get("title") or w.get("id") or "?")[:100]
        ws_id = w.get("id") or w.get("ref")
        cwd = w.get("current_directory") or DEFAULT_WORK_DIR
        _ws_cwd_map[ws_id] = cwd
        options.append(discord.SelectOption(
            label=label, value=ws_id, description=cwd[:100]))
    options.append(discord.SelectOption(
        label="새 워크스페이스 생성", value="__new__",
        description=f"cwd: {DEFAULT_WORK_DIR[:80]}"))

    select = discord.ui.Select(placeholder="워크스페이스 선택", options=options)

    async def on_select(inter: discord.Interaction):
        val = select.values[0]
        if val == "__new__":
            ws = await ws_create(name=f"kimi-{inter.user.name}", cwd=DEFAULT_WORK_DIR)
            ws_id = ws.get("id") or ws.get("workspace_id")
            ws_name = ws.get("title") or ws.get("name")
            cwd = ws.get("current_directory") or ws.get("cwd") or DEFAULT_WORK_DIR
        else:
            ws_id = val
            cwd = _ws_cwd_map.get(ws_id, DEFAULT_WORK_DIR)
            ws_name = next((o.label for o in options if o.value == val), None)

        if not isinstance(inter.channel, discord.TextChannel):
            await inter.response.send_message("텍스트 채널에서만 가능해요.", ephemeral=True)
            return
        thread = await inter.channel.create_thread(
            name=f"kimi-{ws_name or ws_id}-{int(time.time())%10000}",
            type=discord.ChannelType.public_thread,
            auto_archive_duration=1440,
        )
        await inter.response.send_message(f"✓ 세션 생성: {thread.mention}", ephemeral=True)
        await thread.send(f"🚀 `kimi` TUI 기동 중... (cwd: `{cwd}`)")

        try:
            # 1. Create workspace if needed (already have ws_id)
            # 2. Create surface
            surf = await create_surface(workspace_id=ws_id, focus=True)
            surf_id = surf.get("surface_id") or surf.get("id")
            surf_ref = surf.get("surface_ref") or surf_id

            # 3. Start kimi
            await surface_send_text(surf_id, "kimi\n")

            # 4. Wait for session uuid
            session_uuid = await wait_for_session_uuid(surf_id, timeout=25.0)
            if not session_uuid:
                await thread.send("⚠️ session uuid 추출 실패")
                await close_surface(surf_id)
                return

            # 5. Create session object (wire.jsonl found lazily on first message)
            sess = WireSession(
                thread_id=thread.id,
                surface_id=surf_id,
                session_uuid=session_uuid,
                cwd=cwd,
                owner_id=inter.user.id,
            )
            sessions[thread.id] = sess

            # 7. Register in DB
            registry.insert(SessionRow(
                thread_id=str(thread.id),
                guild_id=str(thread.guild.id) if thread.guild else None,
                channel_id=str(thread.parent_id),
                owner_user_id=str(inter.user.id),
                workspace_id=ws_id,
                workspace_name=ws_name,
                cwd=cwd,
                monitor_surface_id=surf_id,
                acp_session_id=session_uuid,
                status="active",
                created_at=int(time.time()),
                last_active_at=int(time.time()),
            ))

            await thread.send(
                f"준비 완료. session=`{session_uuid[:8]}…` · surface `{surf_ref}`\n"
                f"이 스레드의 메시지는 cmux 터미널로 전달되고,\n"
                f"kimi의 응답은 이 스레드에 표시됩니다.")

        except Exception as e:
            log.exception("session start failed")
            await thread.send(f"⚠️ 세션 시작 실패: `{e}`")

    select.callback = on_select
    view = discord.ui.View(timeout=120)
    view.add_item(select)
    await interaction.response.send_message("워크스페이스 선택:", view=view, ephemeral=True)


@client.event
async def on_message(msg: discord.Message):
    if msg.author.bot:
        return
    if not isinstance(msg.channel, discord.Thread):
        return
    row = registry.get_by_thread(str(msg.channel.id))
    if not row or row.status != "active":
        return
    if str(msg.author.id) != row.owner_user_id:
        return

    sess = sessions.get(msg.channel.id)
    if not sess:
        return

    log.info("Discord → Surface: thread=%s text=%r", msg.channel.id, msg.content)
    await sess.send_to_surface(msg.content, client)
    sess.reset_anchor()
    registry.touch(str(msg.channel.id))


@client.event
async def on_thread_update(before: discord.Thread, after: discord.Thread):
    if not before.archived and after.archived:
        sess = sessions.pop(after.id, None)
        if sess:
            await sess.stop()
        registry.update_status(str(after.id), "dead")


@client.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
    else:
        await tree.sync()
    log.info("bot online: %s", client.user)


def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set")
    client.run(token)


if __name__ == "__main__":
    main()
