"""Discord bot wiring: /new slash command, message routing.

Replaces ACP with cmux surface + wire.jsonl tail.
"""
from __future__ import annotations
import asyncio, logging, os, time
from pathlib import Path
from typing import Optional

# Load .env from project root before importing modules that read os.environ.
try:
    from dotenv import load_dotenv
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent
    load_dotenv(_PROJECT_ROOT / ".env")
except ImportError:
    pass

import discord
from discord import app_commands

from .cmux_client import (list_workspaces, create_workspace, create_surface,
                          surface_send_text, close_surface, CmuxError)
from .registry import Registry, SessionRow
from .discord_relay import ThreadRelay, SESSION_RE, wait_for_wire_jsonl

log = logging.getLogger("router.bot")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

REGISTRY_PATH = os.environ.get("SESSION_DB_PATH", "router.sqlite3")
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"]) if os.environ.get("DISCORD_GUILD_ID") else None
DEFAULT_WORK_DIR = os.environ.get("DEFAULT_WORK_DIR", os.path.expanduser("~"))


class WireSession:
    """Holds per-thread state (surface + relay)."""

    def __init__(self, thread_id: int, surface_id: str, session_uuid: str,
                 cwd: str, owner_id: int):
        self.thread_id = thread_id
        self.surface_id = surface_id
        self.session_uuid = session_uuid
        self.cwd = cwd
        self.owner_id = owner_id
        self.relay: Optional[ThreadRelay] = None

    async def start_relay(self, thread: discord.Thread, client: discord.Client):
        self.relay = ThreadRelay(thread, show_thoughts=False, show_tool_progress=True)
        # Tail is started lazily when first message triggers wire.jsonl creation
        return self.relay

    async def send_to_surface(self, text: str, client: discord.Client):
        # Ensure relay tail is running before sending
        if self.relay and not self.relay._tail_started:
            await self.relay.ensure_tail(self.session_uuid, self.cwd, client)
        await surface_send_text(self.surface_id, text + "\n")
        if self.relay:
            self.relay.reset_message_anchor()

    async def stop(self):
        if self.relay:
            await self.relay.stop()
        try:
            await close_surface(self.surface_id)
        except CmuxError as e:
            log.warning("close_surface failed: %s", e)


class Router:
    """Holds per-thread state."""

    def __init__(self):
        self.registry = Registry(REGISTRY_PATH)
        self.sessions: dict[int, WireSession] = {}   # thread_id → WireSession

    async def create_session(self, *, thread: discord.Thread, owner_id: int,
                             workspace_id: str, workspace_name: str | None,
                             cwd: str) -> tuple[WireSession, dict | None]:
        # Create a terminal surface in the chosen workspace
        surface_info: dict | None = None
        try:
            surface_info = await create_surface(workspace_id, focus=True)
        except CmuxError as e:
            log.warning("surface creation failed: %s", e)

        surf_id = (surface_info or {}).get("surface_id") or (surface_info or {}).get("id")
        if not surf_id:
            raise RuntimeError("Failed to create cmux surface")

        # Start kimi in the surface
        await surface_send_text(surf_id, "kimi\n")

        # Wait for banner and extract session UUID
        session_uuid = await self._wait_for_session_uuid(surf_id)
        if not session_uuid:
            await close_surface(surf_id)
            raise RuntimeError("Failed to extract session UUID from banner")

        sess = WireSession(
            thread_id=thread.id,
            surface_id=surf_id,
            session_uuid=session_uuid,
            cwd=cwd,
            owner_id=owner_id,
        )
        await sess.start_relay(thread, client)
        self.sessions[thread.id] = sess

        self.registry.insert(SessionRow(
            thread_id=str(thread.id),
            guild_id=str(thread.guild.id) if thread.guild else None,
            channel_id=str(thread.parent_id),
            owner_user_id=str(owner_id),
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            cwd=cwd,
            monitor_surface_id=surf_id,
            acp_session_id=session_uuid,
            status="active",
            created_at=int(time.time()),
            last_active_at=int(time.time()),
        ))
        return sess, surface_info

    async def _wait_for_session_uuid(self, surface_id: str, timeout: float = 25.0) -> str | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            from .cmux_client import surface_read_text
            text = await surface_read_text(surface_id)
            if text:
                m = SESSION_RE.search(text)
                if m:
                    return m.group(1)
            await asyncio.sleep(0.5)
        return None

    async def relay_user_message(self, thread_id: int, text: str,
                                 client: discord.Client) -> None:
        sess = self.sessions.get(thread_id)
        if not sess:
            return
        log.info("Discord → Surface: thread=%s text=%r", thread_id, text)
        await sess.send_to_surface(text, client)
        self.registry.touch(str(thread_id))

    async def shutdown_session(self, thread_id: int) -> None:
        sess = self.sessions.pop(thread_id, None)
        if sess:
            await sess.stop()
        self.registry.update_status(str(thread_id), "dead")


# ── Discord bot setup ─────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
router = Router()


@tree.command(name="new", description="새 kimi-cli 세션을 시작합니다 (워크스페이스 선택)")
async def new_session_cmd(interaction: discord.Interaction):
    try:
        wss = await list_workspaces()
    except CmuxError as e:
        await interaction.response.send_message(f"cmux 호출 실패: {e}", ephemeral=True)
        return

    _ws_cwd_map: dict[str, str] = {}
    options = []
    for w in wss[:24]:  # Discord select limit: 25 options
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
            ws = await create_workspace(name=f"kimi-{inter.user.name}",
                                         cwd=DEFAULT_WORK_DIR)
            ws_id = ws.get("id") or ws.get("workspace_id")
            ws_name = ws.get("title") or ws.get("name")
            cwd = ws.get("current_directory") or ws.get("cwd") or DEFAULT_WORK_DIR
        else:
            ws_id = val
            cwd = _ws_cwd_map.get(ws_id, DEFAULT_WORK_DIR)
            ws_name = next((o.label for o in options if o.value == val), None)

        # Create thread off the channel where /new was invoked
        if not isinstance(inter.channel, discord.TextChannel):
            await inter.response.send_message(
                "텍스트 채널에서만 가능해요.", ephemeral=True)
            return
        thread = await inter.channel.create_thread(
            name=f"kimi-{ws_name or ws_id}-{int(time.time())%10000}",
            type=discord.ChannelType.public_thread,
            auto_archive_duration=1440,
        )
        await inter.response.send_message(
            f"✓ 세션 생성: {thread.mention}", ephemeral=True)

        await thread.send(f"🚀 `kimi` TUI 기동 중... (cwd: `{cwd}`)")
        try:
            sess, surface = await router.create_session(
                thread=thread, owner_id=inter.user.id,
                workspace_id=ws_id, workspace_name=ws_name, cwd=cwd)
            surf_line = (f" · cmux `{surface.get('surface_ref')}`"
                         if surface else " · (cmux surface 생성 실패)")
            await thread.send(
                f"준비 완료. session=`{sess.session_uuid[:8]}…`{surf_line}\n"
                f"메시지를 보내면 kimi에 전달됩니다.")
        except Exception as e:
            log.exception("session start failed")
            await thread.send(f"⚠️ 세션 시작 실패: `{e}`")

    select.callback = on_select
    view = discord.ui.View(timeout=120)
    view.add_item(select)
    await interaction.response.send_message("워크스페이스 선택:", view=view, ephemeral=True)


# ── Additional slash commands ─────────────────────────────────────────────────

@tree.command(name="kill", description="세션을 종료합니다 (thread 내/외 모두 사용 가능)")
async def kill_cmd(interaction: discord.Interaction):
    # Case 1: used inside a thread → kill this thread's session
    if isinstance(interaction.channel, discord.Thread):
        row = router.registry.get_by_thread(str(interaction.channel.id))
        if not row or row.status != "active":
            await interaction.response.send_message("활성 세션이 없어요.", ephemeral=True)
            return
        if str(interaction.user.id) != row.owner_user_id:
            await interaction.response.send_message("세션 소유자만 종료할 수 있어요.", ephemeral=True)
            return
        await interaction.response.send_message("🛑 세션 종료 중...", ephemeral=True)
        await router.shutdown_session(interaction.channel.id)
        await interaction.channel.send("✓ 세션이 종료되었습니다.")
        return

    # Case 2: used outside a thread → show dropdown of active sessions
    active = router.registry.list_active()
    owner_sessions = [r for r in active if r.owner_user_id == str(interaction.user.id)]
    if not owner_sessions:
        await interaction.response.send_message("종료할 활성 세션이 없어요.", ephemeral=True)
        return

    options = []
    for r in owner_sessions[:25]:
        thread = client.get_channel(int(r.thread_id))
        label = (thread.name if thread else f"thread:{r.thread_id}")[:100]
        options.append(discord.SelectOption(
            label=label,
            value=r.thread_id,
            description=f"{r.cwd[:50]} · {r.acp_session_id[:8]}…"))

    select = discord.ui.Select(placeholder="종료할 세션 선택", options=options)

    async def on_select(inter: discord.Interaction):
        thread_id = int(select.values[0])
        thread = client.get_channel(thread_id)
        await inter.response.send_message("🛑 세션 종료 중...", ephemeral=True)
        await router.shutdown_session(thread_id)
        if thread:
            await thread.send("✓ 이 세션이 종료되었습니다.")

    select.callback = on_select
    view = discord.ui.View(timeout=120)
    view.add_item(select)
    await interaction.response.send_message("종료할 세션을 선택하세요:", view=view, ephemeral=True)


@tree.command(name="list", description="내 활성 세션 목록을 표시합니다")
async def list_cmd(interaction: discord.Interaction):
    active = router.registry.list_active()
    owner_sessions = [r for r in active if r.owner_user_id == str(interaction.user.id)]
    if not owner_sessions:
        await interaction.response.send_message("활성 세션이 없어요.", ephemeral=True)
        return
    lines = [f"**활성 세션 ({len(owner_sessions)}개)**"]
    for r in owner_sessions:
        thread = client.get_channel(int(r.thread_id))
        thread_link = thread.mention if thread else f"thread:{r.thread_id}"
        lines.append(
            f"• {thread_link} — `{r.acp_session_id[:8]}…` · `{r.cwd[:30]}`"
        )
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@tree.command(name="status", description="현재 세션 상태를 확인합니다")
async def status_cmd(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.Thread):
        await interaction.response.send_message("thread에서만 사용 가능해요.", ephemeral=True)
        return
    row = router.registry.get_by_thread(str(interaction.channel.id))
    if not row:
        await interaction.response.send_message("이 thread에 등록된 세션이 없어요.", ephemeral=True)
        return
    sess = router.sessions.get(interaction.channel.id)
    tail_status = "running" if (sess and sess.relay and sess.relay._tail_started) else "not started"
    msg = (
        f"**세션 상태**\n"
        f"```\n"
        f"session_id: {row.acp_session_id}\n"
        f"surface_id: {row.monitor_surface_id}\n"
        f"cwd:        {row.cwd}\n"
        f"workspace:  {row.workspace_name or row.workspace_id}\n"
        f"status:     {row.status}\n"
        f"tail:       {tail_status}\n"
        f"```"
    )
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="clear", description="kimi 컨텍스트를 리셋합니다 (/clear)")
async def clear_cmd(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.Thread):
        await interaction.response.send_message("thread에서만 사용 가능해요.", ephemeral=True)
        return
    row = router.registry.get_by_thread(str(interaction.channel.id))
    if not row or row.status != "active":
        await interaction.response.send_message("활성 세션이 없어요.", ephemeral=True)
        return
    if str(interaction.user.id) != row.owner_user_id:
        await interaction.response.send_message("세션 소유자만 사용할 수 있어요.", ephemeral=True)
        return
    sess = router.sessions.get(interaction.channel.id)
    if not sess:
        await interaction.response.send_message("세션 객체를 찾을 수 없어요.", ephemeral=True)
        return
    await sess.send_to_surface("/clear", client)
    await interaction.response.send_message("🧹 `/clear` 전송 완료", ephemeral=True)


@tree.command(name="yolo", description="승인 모드를 토글합니다 (/yolo)")
async def yolo_cmd(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.Thread):
        await interaction.response.send_message("thread에서만 사용 가능해요.", ephemeral=True)
        return
    row = router.registry.get_by_thread(str(interaction.channel.id))
    if not row or row.status != "active":
        await interaction.response.send_message("활성 세션이 없어요.", ephemeral=True)
        return
    if str(interaction.user.id) != row.owner_user_id:
        await interaction.response.send_message("세션 소유자만 사용할 수 있어요.", ephemeral=True)
        return
    sess = router.sessions.get(interaction.channel.id)
    if not sess:
        await interaction.response.send_message("세션 객체를 찾을 수 없어요.", ephemeral=True)
        return
    await sess.send_to_surface("/yolo", client)
    await interaction.response.send_message("⚡ `/yolo` 전송 완료", ephemeral=True)


@tree.command(name="model", description="모델을 변경합니다 (/model)")
@app_commands.describe(name="변경할 모델 이름 (예: kimi-k2.6, kimi-k2.5)")
async def model_cmd(interaction: discord.Interaction, name: str):
    if not isinstance(interaction.channel, discord.Thread):
        await interaction.response.send_message("thread에서만 사용 가능해요.", ephemeral=True)
        return
    row = router.registry.get_by_thread(str(interaction.channel.id))
    if not row or row.status != "active":
        await interaction.response.send_message("활성 세션이 없어요.", ephemeral=True)
        return
    if str(interaction.user.id) != row.owner_user_id:
        await interaction.response.send_message("세션 소유자만 사용할 수 있어요.", ephemeral=True)
        return
    sess = router.sessions.get(interaction.channel.id)
    if not sess:
        await interaction.response.send_message("세션 객체를 찾을 수 없어요.", ephemeral=True)
        return
    await sess.send_to_surface(f"/model {name}", client)
    await interaction.response.send_message(f"🤖 `/model {name}` 전송 완료", ephemeral=True)


@tree.command(name="cleanup", description="고아 세션을 정리합니다 (삭제/아카이브된 thread)")
async def cleanup_cmd(interaction: discord.Interaction):
    active = router.registry.list_active()
    cleaned = 0
    for r in active:
        try:
            thread = client.get_channel(int(r.thread_id))
            if thread is None:
                # thread가 삭제됨
                router.sessions.pop(int(r.thread_id), None)
                router.registry.update_status(r.thread_id, "dead")
                cleaned += 1
                log.info("cleanup: thread %s deleted, marked dead", r.thread_id)
            elif thread.archived:
                # thread가 아카이브됨
                await router.shutdown_session(int(r.thread_id))
                cleaned += 1
                log.info("cleanup: thread %s archived, shutdown", r.thread_id)
        except Exception as e:
            log.warning("cleanup error for thread %s: %s", r.thread_id, e)

    await interaction.response.send_message(
        f"🧹 정리 완료: {cleaned}개의 고아 세션을 정리했습니다.", ephemeral=True)


@tree.command(name="rebind", description="현재 세션을 새 Discord thread로 옮깁니다")
async def rebind_cmd(interaction: discord.Interaction):
    """Migrate an existing session to a new Discord thread.

    The old thread is left with a farewell message; the new thread
    receives the same cmux surface + wire.jsonl tail.
    """
    if not isinstance(interaction.channel, discord.Thread):
        await interaction.response.send_message("thread에서만 사용 가능해요.", ephemeral=True)
        return

    old_thread_id = interaction.channel.id
    row = router.registry.get_by_thread(str(old_thread_id))
    if not row or row.status != "active":
        await interaction.response.send_message("활성 세션이 없어요.", ephemeral=True)
        return
    if str(interaction.user.id) != row.owner_user_id:
        await interaction.response.send_message("세션 소유자만 사용할 수 있어요.", ephemeral=True)
        return

    old_sess = router.sessions.get(old_thread_id)
    if not old_sess:
        await interaction.response.send_message("세션 객체를 찾을 수 없어요.", ephemeral=True)
        return

    await interaction.response.send_message(
        "🔄 새 thread를 생성하고 세션을 옮깁니다...", ephemeral=True)

    # Create a new thread in the parent channel
    parent = interaction.channel.parent
    if not parent:
        await interaction.followup.send("부모 채널을 찾을 수 없어요.", ephemeral=True)
        return

    new_thread = await parent.create_thread(
        name=f"kimi-rebind-{int(time.time()) % 10000}",
        type=discord.ChannelType.public_thread,
        auto_archive_duration=1440,
    )

    # Move the session: update in-memory dict
    router.sessions.pop(old_thread_id, None)
    old_sess.thread_id = new_thread.id
    router.sessions[new_thread.id] = old_sess

    # Update registry
    router.registry.conn.execute(
        "UPDATE sessions SET thread_id=?, channel_id=? WHERE thread_id=?",
        (str(new_thread.id), str(new_thread.parent_id), str(old_thread_id)),
    )
    router.registry.conn.commit()

    # Update relay's thread reference
    if old_sess.relay:
        old_sess.relay.thread = new_thread

    # Farewell old thread, welcome new thread
    await interaction.channel.send(
        f"✓ 세션이 {new_thread.mention} 로 이동되었습니다.")
    await new_thread.send(
        f"🔄 세션이 이곳으로 이동되었습니다.\n"
        f"session=`{old_sess.session_uuid[:8]}…` · surface `{old_sess.surface_id[:8]}…`\n"
        f"메시지를 별낸면 kimi에 전달됩니다.")

    log.info("rebind: session %s moved from thread %s to %s",
             old_sess.session_uuid, old_thread_id, new_thread.id)


@client.event
async def on_message(msg: discord.Message):
    if msg.author.bot:
        return
    if not isinstance(msg.channel, discord.Thread):
        return
    row = router.registry.get_by_thread(str(msg.channel.id))
    if not row or row.status != "active":
        return
    if str(msg.author.id) != row.owner_user_id:
        return  # only owner
    await router.relay_user_message(msg.channel.id, msg.content, client)


@client.event
async def on_thread_update(before: discord.Thread, after: discord.Thread):
    if not before.archived and after.archived:
        await router.shutdown_session(after.id)


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
        raise SystemExit("DISCORD_TOKEN is not set (check .env)")
    client.run(token)


if __name__ == "__main__":
    main()
