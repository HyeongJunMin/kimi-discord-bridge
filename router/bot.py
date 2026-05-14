"""Discord bot wiring: /new slash command, message routing.

Replaces ACP with cmux surface + wire.jsonl tail.
"""
from __future__ import annotations
import asyncio, logging, os, signal, sqlite3, time
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
                          surface_send_text, surface_read_text, close_surface,
                          CmuxError, list_surfaces, ensure_cmux_running,
                          rename_tab)
from .registry import Registry, SessionRow
from .discord_relay import (
    ThreadRelay, SESSION_RE, DIRECTORY_RE, wait_for_wire_jsonl,
    find_live_kimi_surface_ids,
)
from .sleep_guard import SleepGuard

log = logging.getLogger("router.bot")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

REGISTRY_PATH = os.environ.get("SESSION_DB_PATH", "router.sqlite3")
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"]) if os.environ.get("DISCORD_GUILD_ID") else None
DEFAULT_WORK_DIR = os.environ.get("DEFAULT_WORK_DIR", os.path.expanduser("~"))
SLEEP_GUARD_MODES = {"off", "active_sessions", "always"}


def _truthy_env(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _sleep_guard_mode_from_env(env: dict[str, str] = os.environ) -> str:
    """Resolve sleep guard policy, preserving the legacy boolean env var."""
    mode = env.get("SLEEP_GUARD_MODE")
    if mode is not None:
        normalized = mode.strip().lower()
        if normalized in SLEEP_GUARD_MODES:
            return normalized
        log.warning("invalid SLEEP_GUARD_MODE=%r; falling back to off", mode)
        return "off"
    if _truthy_env(env.get("PREVENT_SLEEP_WHILE_ACTIVE")):
        return "active_sessions"
    return "off"


SLEEP_GUARD_MODE = _sleep_guard_mode_from_env()

# Image attachment relay (Discord → kimi).
# /tmp on macOS is auto-cleaned (com.apple.tmp_cleaner runs daily at 00:00
# and deletes files untouched for >3 days) which gives us belt-and-suspenders
# cleanup; we still explicitly remove the per-thread dir on shutdown_session.
UPLOADS_ROOT = Path("/tmp/kimi-uploads")
ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MiB — generous for camera-roll photos


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
        self.relay = ThreadRelay(thread, show_thoughts=False, show_tool_progress=False)
        # Tail is started lazily when first message triggers wire.jsonl creation
        return self.relay

    async def send_to_surface(self, text: str, client: discord.Client):
        # wire.jsonl is created lazily by kimi-cli on the first conversation
        # turn — i.e. only AFTER we send the first user message. So the order
        # must be: send first, then wait for the file. Reversing this caused
        # a 60s timeout on the first user message and silently lost replies.
        #
        # Wrap the payload in bracketed-paste escapes so embedded newlines
        # stay as literal newlines inside the kimi-cli input box instead of
        # being interpreted as Enter (which would submit the partial buffer
        # one line at a time). The trailing \r is the actual submit
        # keystroke. Verified against kimi-cli's TUI in a live probe — it
        # acknowledges paste mode explicitly. Any stray \x1b[200~/\x1b[201~
        # in the user text is stripped first so the wrapper can't be
        # smuggled past.
        clean = text.replace("\x1b[200~", "").replace("\x1b[201~", "")
        payload = f"\x1b[200~{clean}\x1b[201~\r"
        await surface_send_text(self.surface_id, payload)
        if self.relay and not self.relay._tail_started:
            # First tail start: read from beginning so we don't miss the
            # response that kimi is already streaming for the message we
            # just sent.
            await self.relay.ensure_tail(self.session_uuid, self.cwd, client,
                                         from_beginning=True)
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
        self.sleep_guard_mode = SLEEP_GUARD_MODE
        self.sleep_guard = SleepGuard(enabled=self.sleep_guard_mode != "off")
        self._delivery_task: asyncio.Task | None = None
        self._delivery_wakeup: asyncio.Event | None = None
        self._delivery_lock = asyncio.Lock()

    async def refresh_sleep_guard(self) -> None:
        if self.sleep_guard_mode == "always":
            await self.sleep_guard.start()
        elif self.sleep_guard_mode == "active_sessions":
            await self.sleep_guard.refresh(len(self.sessions))
        else:
            await self.sleep_guard.stop()

    def start_delivery_worker(self, client: discord.Client) -> None:
        if self._delivery_task and not self._delivery_task.done():
            return
        self._delivery_wakeup = asyncio.Event()
        self._delivery_task = asyncio.create_task(
            self._delivery_worker(client),
            name="inbound-message-delivery",
        )

    async def stop_delivery_worker(self) -> None:
        task = self._delivery_task
        self._delivery_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def wake_delivery_worker(self) -> None:
        if self._delivery_wakeup:
            self._delivery_wakeup.set()

    async def _delivery_worker(self, client: discord.Client) -> None:
        while True:
            try:
                await self.deliver_pending_once(client)
                if self._delivery_wakeup:
                    self._delivery_wakeup.clear()
                    await asyncio.wait_for(self._delivery_wakeup.wait(), timeout=1.0)
                else:
                    await asyncio.sleep(1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("delivery worker iteration failed")
                await asyncio.sleep(1.0)

    async def deliver_pending_once(self, client: discord.Client, *,
                                   limit: int = 20) -> int:
        delivered = 0
        async with self._delivery_lock:
            for item in self.registry.list_pending_messages(limit=limit):
                row = self.registry.get_by_thread(item.thread_id)
                if not row or row.status != "active":
                    self.registry.mark_message_failed(
                        item.id, "session is not active", terminal=True)
                    continue
                try:
                    thread_id = int(item.thread_id)
                except ValueError:
                    self.registry.mark_message_failed(
                        item.id, "invalid thread id", terminal=True)
                    continue
                sess = self.sessions.get(thread_id)
                if not sess:
                    self.registry.mark_message_failed(
                        item.id, "session is active but not loaded in memory")
                    continue
                try:
                    log.info("Deliver queued Discord message: id=%s thread=%s",
                             item.id, item.thread_id)
                    await sess.send_to_surface(item.content, client)
                    self.registry.mark_message_delivered(item.id)
                    self.registry.touch(item.thread_id)
                    delivered += 1
                except Exception as e:
                    log.warning("queued delivery failed for id=%s: %s",
                                item.id, e)
                    self.registry.mark_message_failed(item.id, str(e))
        return delivered

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

        # Tag the cmux tab with the Discord thread name so the user can
        # eyeball which surface belongs to which thread (cmux defaults to a
        # generic title like "Kimi Code" otherwise).
        try:
            await rename_tab(surf_id, _short_tab_title(workspace_name, thread.name))
        except Exception as e:
            log.warning("rename_tab failed: %s", e)

        # Start kimi in the surface.
        # KIMI_CLI_NO_AUTO_UPDATE=1 suppresses the interactive upgrade gate
        # that blocks the welcome banner (and thus session UUID extraction)
        # when a new kimi-cli release exists. See kimi_cli/ui/shell/update.py.
        await surface_send_text(surf_id, "KIMI_CLI_NO_AUTO_UPDATE=1 kimi\n")

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
        await self.refresh_sleep_guard()
        return sess, surface_info

    async def _wait_for_session_uuid(self, surface_id: str, timeout: float = 25.0) -> str | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = await surface_read_text(surface_id)
            if text:
                m = SESSION_RE.search(text)
                if m:
                    return m.group(1)
            await asyncio.sleep(0.5)
        return None

    async def relay_user_message(self, thread_id: int, text: str,
                                 client: discord.Client,
                                 *, author_user_id: int) -> int:
        log.info("Discord → Queue: thread=%s text=%r", thread_id, text)
        message_id = self.registry.enqueue_message(
            thread_id=str(thread_id),
            author_user_id=str(author_user_id),
            content=text,
        )
        self.registry.touch(str(thread_id))
        self.wake_delivery_worker()
        # In tests and during early startup the worker may not be running yet.
        if not self._delivery_task or self._delivery_task.done():
            await self.deliver_pending_once(client)
        return message_id

    async def shutdown_session(self, thread_id: int) -> None:
        sess = self.sessions.pop(thread_id, None)
        if sess:
            await sess.stop()
        self.registry.fail_pending_for_thread(str(thread_id), "session ended")
        self.registry.update_status(str(thread_id), "dead")
        # Clean up any per-thread image uploads. macOS would clean /tmp
        # eventually but we want explicit cleanup so /kill is deterministic.
        _delete_upload_dir(thread_id)
        await self.refresh_sleep_guard()


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
        # Channel check must happen before defer so we can use the
        # synchronous send_message path for the early return.
        if not isinstance(inter.channel, discord.TextChannel):
            await inter.response.send_message(
                "텍스트 채널에서만 가능해요.", ephemeral=True)
            return

        # workspace.create (if "__new__") + create_thread are both Discord/cmux
        # round-trips; defer first to stay under the 3s interaction deadline.
        await inter.response.defer(ephemeral=True)

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

        thread = await inter.channel.create_thread(
            name=f"kimi-{ws_name or ws_id}-{int(time.time())%10000}",
            type=discord.ChannelType.public_thread,
            auto_archive_duration=1440,
        )
        await inter.followup.send(
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
                f"메시지를 보내면 kimi에 전달됩니다.\n"
                f"이 스레드에서: `/stop` 응답 멈춤 · `/kill` 세션 종료")
        except Exception as e:
            log.exception("session start failed")
            await thread.send(f"⚠️ 세션 시작 실패: `{e}`")

    select.callback = on_select
    view = discord.ui.View(timeout=120)
    view.add_item(select)
    await interaction.response.send_message("워크스페이스 선택:", view=view, ephemeral=True)


# ── Additional slash commands ─────────────────────────────────────────────────

ZOMBIE_GRACE_SEC = 30  # protect freshly-created sessions from cmux propagation lag


def _short_tab_title(workspace_name: str | None, thread_name: str) -> str:
    """Compact cmux tab title for /new sessions.

    Format: '{workspace_name[:3]}-{trailing 4-digit suffix from thread}'.

    The 4-digit suffix is `int(time.time()) % 10000` baked into the thread
    name at creation time (effectively a random-looking identifier — the
    user just needs to visually match a tab to a thread, exact uniqueness
    isn't required).

    Example:
        ws='kimi-hub', thread='kimi-kimi-hub-3590' → 'kim-3590'
        ws=None,       thread='kimi-foo-9999'      → 'ws-9999'
    """
    prefix = (workspace_name or "ws")[:3] or "ws"
    suffix = thread_name.rsplit("-", 1)[-1] if "-" in thread_name else ""
    return f"{prefix}-{suffix}" if suffix else prefix


async def _classify_threads_for_cleanup(
    channel_threads: list,
    active_by_thread: dict,
    *,
    cmux_ok: bool,
    surface_probe,
    now: int,
    grace_sec: int = ZOMBIE_GRACE_SEC,
) -> tuple[set[str], set[str]]:
    """Classify channel threads into (zombie_ids, unregistered_ids).

    - unregistered: thread is in the channel but has no active registry row
    - zombie: registry says 'active' but cmux surface ping fails

    Skips zombie classification entirely when cmux is unreachable so a single
    cmux outage doesn't cause mass-delete of the whole fleet. Threads with
    no monitor_surface_id, or those created within `grace_sec` (cmux state
    may not have propagated yet), are also kept off the zombie list.

    Extracted from cleanup_cmd so it can be unit-tested without spinning up
    Discord interactions.
    """
    zombie_ids: set[str] = set()
    unregistered_ids: set[str] = set()

    async def _classify(t) -> None:
        tid = str(t.id)
        row = active_by_thread.get(tid)
        if not row:
            unregistered_ids.add(tid)
            return
        if not cmux_ok or not row.monitor_surface_id:
            return
        if now - (row.created_at or 0) < grace_sec:
            return
        try:
            await surface_probe(row.monitor_surface_id)
        except CmuxError:
            zombie_ids.add(tid)

    await asyncio.gather(*(_classify(t) for t in channel_threads))
    return zombie_ids, unregistered_ids


async def _kill_session_and_thread(thread: discord.Thread,
                                    surface_id: str | None) -> tuple[str, str]:
    """Shutdown the session, verify cmux surface closed, then delete the
    Discord thread. Returns (status_line, thread_name_for_log).

    - surface verification: a successful surface.read_text after close means
      the surface is still alive (close failed). CmuxError = surface gone =
      expected.
    - thread.delete failure is logged but doesn't abort — we still want the
      parent-channel log to fire so the user sees something happened.
    """
    thread_name = thread.name
    # shutdown_session is idempotent: pops from in-memory sessions if present,
    # marks registry row 'dead' if exists, otherwise no-op. Safe for orphan
    # threads with no active session.
    await router.shutdown_session(thread.id)

    parts: list[str] = []
    if surface_id:
        try:
            await surface_read_text(surface_id)
            parts.append("surface ⚠️ 닫기 실패")
        except CmuxError:
            parts.append("surface ✓")

    thread_ok = True
    try:
        await thread.delete()
    except Exception as e:
        thread_ok = False
        log.warning("thread.delete failed for %s: %s", thread.id, e)
    parts.append("thread 삭제 ✓" if thread_ok else "thread 삭제 ⚠️ 실패")

    return " · ".join(parts), thread_name


@tree.command(name="kill", description="세션을 종료합니다 (thread 내/외 모두 사용 가능)")
async def kill_cmd(interaction: discord.Interaction):
    # Case 1: used inside a thread → kill this thread's session OR clean up
    # an orphan/dead thread.
    if isinstance(interaction.channel, discord.Thread):
        thread = interaction.channel
        row = router.registry.get_by_thread(str(thread.id))
        is_active = bool(row and row.status == "active")
        # Owner check: enforced if registry row exists. True orphans (no row)
        # have no recorded owner — anyone in the thread can clean them up.
        if row and str(interaction.user.id) != row.owner_user_id:
            verb = "종료" if is_active else "정리"
            await interaction.response.send_message(
                f"세션 소유자만 {verb}할 수 있어요.", ephemeral=True)
            return
        # Defer so we have headroom for cmux close + verify + thread.delete.
        await interaction.response.defer(ephemeral=True)
        parent = thread.parent
        # surface_id only set for active sessions — orphan/dead threads
        # don't need surface verification (already gone).
        surface_id = row.monitor_surface_id if is_active else None
        status, name = await _kill_session_and_thread(thread, surface_id)
        # Ephemeral feedback: this lands in the thread but the thread is now
        # gone — the caller sees it for a brief moment in their client cache.
        try:
            await interaction.followup.send(f"🛑 종료 완료 — {status}", ephemeral=True)
        except Exception:
            pass
        # Persistent log in the parent channel.
        if isinstance(parent, discord.TextChannel):
            try:
                await parent.send(f"🛑 `{name}` 종료 — {status}")
            except Exception as e:
                log.warning("parent log send failed: %s", e)
        return

    # Case 2: used outside a thread → show dropdown of active sessions
    active = router.registry.list_active()
    owner_sessions = [r for r in active if r.owner_user_id == str(interaction.user.id)]
    if not owner_sessions:
        await interaction.response.send_message("종료할 활성 세션이 없어요.", ephemeral=True)
        return

    options = []
    _surface_map: dict[str, str | None] = {}
    for r in owner_sessions[:25]:
        thread = client.get_channel(int(r.thread_id))
        label = (thread.name if thread else f"thread:{r.thread_id}")[:100]
        _surface_map[r.thread_id] = r.monitor_surface_id
        options.append(discord.SelectOption(
            label=label,
            value=r.thread_id,
            description=f"{r.cwd[:50]} · {r.acp_session_id[:8]}…"))

    select = discord.ui.Select(placeholder="종료할 세션 선택", options=options)

    async def on_select(inter: discord.Interaction):
        # shutdown_session + verify + thread.delete: ~2-4s total. Defer.
        await inter.response.defer(ephemeral=True)
        thread_id_str = select.values[0]
        thread = client.get_channel(int(thread_id_str))
        if not isinstance(thread, discord.Thread):
            await inter.followup.send("thread 객체를 찾을 수 없어요.", ephemeral=True)
            return
        parent = thread.parent
        status, name = await _kill_session_and_thread(
            thread, _surface_map.get(thread_id_str))
        await inter.followup.send(f"🛑 `{name}` 종료 — {status}", ephemeral=True)
        if isinstance(parent, discord.TextChannel):
            try:
                await parent.send(f"🛑 `{name}` 종료 — {status}")
            except Exception as e:
                log.warning("parent log send failed: %s", e)

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
    guard_status = router.sleep_guard.status()
    pending_count = router.registry.count_pending_messages(str(interaction.channel.id))
    last_error = router.registry.last_delivery_error(str(interaction.channel.id)) or "-"
    worker_status = (
        "running"
        if router._delivery_task and not router._delivery_task.done()
        else "stopped"
    )
    cmux_status = "unknown"
    if row.monitor_surface_id:
        try:
            await surface_read_text(row.monitor_surface_id)
            cmux_status = "ok"
        except CmuxError as e:
            cmux_status = f"error: {e}"
    msg = (
        f"**세션 상태**\n"
        f"```\n"
        f"session_id: {row.acp_session_id}\n"
        f"surface_id: {row.monitor_surface_id}\n"
        f"cwd:        {row.cwd}\n"
        f"workspace:  {row.workspace_name or row.workspace_id}\n"
        f"status:     {row.status}\n"
        f"tail:       {tail_status}\n"
        f"cmux:       {cmux_status}\n"
        f"queue:      pending={pending_count} worker={worker_status}\n"
        f"sleep:      enabled={guard_status.enabled} active={guard_status.active} pid={guard_status.pid or '-'}\n"
        f"last_error: {last_error}\n"
        f"```"
    )
    await interaction.response.send_message(msg, ephemeral=True)


RENAME_THREAD_TIMEOUT_S = 10  # Discord rate-limit guard


async def _do_rename(thread: discord.Thread, new_name: str, row) -> str:
    """Rename a Discord thread and (if active) its bound cmux surface.

    Pre-condition: caller has already validated permissions and that
    new_name is non-empty after strip. Returns a one-line status message
    suitable for ephemeral followup.

    Discord rate-limits thread renames to 2 per 10 minutes per channel.
    When the client-side limiter would block, discord.py silently sleeps
    for the full retry-after window (can be 7+ minutes). We cap that with
    asyncio.wait_for so /rename fails fast and tells the user, instead of
    leaving them staring at "thinking…".
    """
    truncated = new_name[:100]  # Discord thread.name limit
    truncated_note = " (100자 초과로 자름)" if len(new_name) > 100 else ""

    parts: list[str] = []
    try:
        await asyncio.wait_for(thread.edit(name=truncated),
                                timeout=RENAME_THREAD_TIMEOUT_S)
        parts.append(f"thread ✓{truncated_note}")
    except asyncio.TimeoutError:
        log.warning("thread.edit hit Discord rate limit (>%ss)",
                    RENAME_THREAD_TIMEOUT_S)
        parts.append(
            "thread ⚠️ Discord 제한 (스레드 이름 변경은 10분에 2회까지). "
            "잠시 후 다시 시도하세요")
    except Exception as e:
        log.warning("thread.edit failed: %s", e)
        parts.append(f"thread ⚠️ 실패 ({e})")

    has_surface = bool(row and row.monitor_surface_id and row.status == "active")
    if has_surface:
        try:
            await rename_tab(row.monitor_surface_id, truncated)
            parts.append("surface ✓")
        except Exception as e:
            log.warning("rename_tab failed: %s", e)
            parts.append(f"surface ⚠️ 실패 ({e})")
    else:
        parts.append("surface 없음(thread만 변경)")

    return "✏️ 이름 변경: " + " · ".join(parts)


@tree.command(name="rename", description="현재 thread와 연결된 cmux surface 이름을 변경합니다")
@app_commands.describe(new_name="새 이름 (Discord 100자 한도 초과 시 잘림)")
async def rename_cmd(interaction: discord.Interaction, new_name: str):
    if not isinstance(interaction.channel, discord.Thread):
        await interaction.response.send_message("thread에서만 사용 가능해요.", ephemeral=True)
        return
    name = new_name.strip()
    if not name:
        await interaction.response.send_message(
            "⚠️ 빈 이름은 사용할 수 없어요.", ephemeral=True)
        return

    thread = interaction.channel
    row = router.registry.get_by_thread(str(thread.id))
    # Owner check: enforced only when a registry row exists. True orphans
    # (no row) can be renamed by anyone in the thread, matching /kill's
    # cleanup policy.
    if row and str(interaction.user.id) != row.owner_user_id:
        await interaction.response.send_message(
            "세션 소유자만 사용할 수 있어요.", ephemeral=True)
        return

    # thread.edit / rename_tab are both round-trips → defer.
    await interaction.response.defer(ephemeral=True)
    status = await _do_rename(thread, name, row)
    await interaction.followup.send(status, ephemeral=True)


@tree.command(name="stop", description="진행 중인 kimi 응답을 멈춥니다 (ESC 전송)")
async def stop_cmd(interaction: discord.Interaction):
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
    # Send raw ESC (no newline) directly to the surface. Bypassing
    # send_to_surface keeps us from appending '\n' (which would commit a
    # blank line instead of cancelling) and from triggering the tail-start
    # path (already started by the time user can /stop).
    await interaction.response.defer(ephemeral=True)
    try:
        await surface_send_text(sess.surface_id, "\x1b")
        await interaction.followup.send("⏹ ESC 전송 완료", ephemeral=True)
    except CmuxError as e:
        await interaction.followup.send(f"⚠️ ESC 전송 실패: {e}", ephemeral=True)


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
    # send_to_surface may call ensure_tail which can wait up to 30s.
    # Defer first so we never trip Discord's 3-second interaction deadline.
    await interaction.response.defer(ephemeral=True)
    await sess.send_to_surface("/clear", client)
    await interaction.followup.send("🧹 `/clear` 전송 완료", ephemeral=True)


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
    await interaction.response.defer(ephemeral=True)
    await sess.send_to_surface("/yolo", client)
    await interaction.followup.send("⚡ `/yolo` 전송 완료", ephemeral=True)


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
    await interaction.response.defer(ephemeral=True)
    await sess.send_to_surface(f"/model {name}", client)
    await interaction.followup.send(f"🤖 `/model {name}` 전송 완료", ephemeral=True)


@tree.command(name="attach", description="로컬에서 실행 중인 kimi-cli를 Discord thread에 연결합니다")
async def attach_cmd(interaction: discord.Interaction):
    """Attach an already-running local kimi surface to a Discord thread.

    Called from a TextChannel: creates a new thread.
    Called from a Thread: attaches to that same thread (requires no active
    session already bound to it and the caller to own any pre-existing row).
    """
    ch = interaction.channel
    is_thread = isinstance(ch, discord.Thread)
    is_textch = isinstance(ch, discord.TextChannel)
    if not (is_thread or is_textch):
        await interaction.response.send_message(
            "텍스트 채널 또는 thread에서만 사용 가능해요.", ephemeral=True)
        return

    if is_thread:
        row = router.registry.get_by_thread(str(ch.id))
        if row and row.owner_user_id != str(interaction.user.id):
            await interaction.response.send_message(
                "이 thread는 다른 사용자의 세션입니다.", ephemeral=True)
            return
        # Only block if BOTH registry says active AND we have it in-memory.
        # A bare 'active' row with no in-memory session is a zombie (bot
        # restart leaves status='active' on purpose to preserve cmux surfaces
        # — see commit f9fdfd2) and re-attaching is the supported recovery.
        if row and row.status == "active" and ch.id in router.sessions:
            await interaction.response.send_message(
                "이 thread에 이미 활성 세션이 있어요. `/kill` 후 다시 시도하세요.",
                ephemeral=True)
            return

    await interaction.response.defer(ephemeral=True)

    # 1. Collect all workspaces
    try:
        wss = await list_workspaces()
    except CmuxError as e:
        await interaction.followup.send(f"cmux 호출 실패: {e}", ephemeral=True)
        return

    # 2. Gather candidate surfaces (with session UUID)
    candidates: list[dict] = []
    registered_surface_ids = {
        r.monitor_surface_id for r in router.registry.list_active()
        if r.monitor_surface_id
    }
    # Process-level ground truth: a surface is only a real kimi candidate
    # if its tty is currently owned by a kimi-cli process. We get this by
    # joining cmux's system.tree (surface → tty) with `ps` output (kimi-cli
    # → tty). Anything else (closed kimi, Claude scrollback containing a
    # UUID-shaped string, stale banner left by a crashed session) is rejected.
    live_kimi_surfaces = await find_live_kimi_surface_ids()
    log.info("/attach: %d surface(s) currently running kimi-cli: %s",
             len(live_kimi_surfaces), live_kimi_surfaces)

    log.info("/attach: scanning %d workspace(s), registered=%s",
             len(wss), registered_surface_ids)
    for ws in wss:
        ws_id = ws.get("id") or ws.get("ref")
        if not ws_id:
            continue
        try:
            surfaces = await list_surfaces(ws_id)
        except CmuxError as e:
            log.info("/attach: list_surfaces(%s) failed: %s", ws_id, e)
            continue
        log.info("/attach: ws=%s has %d surface(s)", ws.get("title") or ws_id, len(surfaces))
        for surf in surfaces:
            surf_id = surf.get("id") or surf.get("surface_id") or surf.get("ref")
            log.info("/attach:   surf id=%s title=%r", surf_id, surf.get("title"))
            if not surf_id:
                continue
            if surf_id in registered_surface_ids:
                log.info("/attach:     skip (already registered)")
                continue  # already attached
            if surf_id not in live_kimi_surfaces:
                log.info("/attach:     skip (not in live_kimi_surfaces)")
                continue  # tty not owned by a kimi-cli process — skip
            # Read with scrollback so the kimi banner ("Session: <uuid>",
            # "Directory: ...") is still findable on surfaces that have been
            # running long enough for the welcome screen to scroll off the
            # visible viewport. False-positives are impossible here because
            # the tty/proc check above already proved kimi-cli is alive.
            try:
                text = await surface_read_text(surf_id, scrollback=True)
            except CmuxError as e:
                log.info("/attach:     skip (read_text err: %s)", e)
                continue
            if not text:
                log.info("/attach:     skip (empty text)")
                continue
            # Take the *last* UUID match — if the same surface restarted
            # kimi, the most recent banner is what's currently running.
            matches = SESSION_RE.findall(text)
            log.info("/attach:     text len=%d session matches=%s", len(text), matches)
            if not matches:
                continue
            session_uuid = matches[-1]

            # Banner's "Directory:" line is the authoritative cwd source.
            # cmux's requested_working_directory is often None for surfaces
            # started without an explicit cwd, which leads to a wrong
            # wire.jsonl hash and "not found" failures.
            dir_m = DIRECTORY_RE.search(text)
            cwd = (dir_m.group(1) if dir_m
                   else surf.get("requested_working_directory") or DEFAULT_WORK_DIR)
            title = surf.get("title") or f"surface:{surf_id}"
            candidates.append({
                "surface_id": surf_id,
                "workspace_id": ws_id,
                "workspace_name": ws.get("title") or ws.get("name") or ws_id,
                "session_uuid": session_uuid,
                "cwd": cwd,
                "title": title,
            })

    if not candidates:
        await interaction.followup.send(
            "연결할 수 있는 kimi surface를 찾지 못했어요. (이미 등록되었거나, session banner가 보이지 않는 surface)",
            ephemeral=True)
        return

    # 3. Build select options (Discord limit: 25)
    options = []
    _candidate_map: dict[str, dict] = {}
    for c in candidates[:25]:
        label = c["title"][:100]
        desc = f"{c['workspace_name'][:40]} · {c['session_uuid'][:8]}…"
        val = c["surface_id"]
        _candidate_map[val] = c
        options.append(discord.SelectOption(label=label, value=val, description=desc))

    select = discord.ui.Select(placeholder="연결할 surface 선택", options=options)

    async def on_select(inter: discord.Interaction):
        await inter.response.defer(ephemeral=True)
        val = select.values[0]
        cand = _candidate_map[val]
        surf_id = cand["surface_id"]
        ws_id = cand["workspace_id"]
        ws_name = cand["workspace_name"]
        session_uuid = cand["session_uuid"]
        cwd = cand["cwd"]

        # Race re-check: candidate list was built before user picked. Another
        # /attach in the same window could have grabbed this surface meanwhile.
        if any(r.monitor_surface_id == surf_id
               for r in router.registry.list_active()):
            await inter.followup.send(
                "이 surface는 방금 다른 thread에 연결됐어요.", ephemeral=True)
            return

        if is_thread:
            thread = ch
            await inter.followup.send(
                f"✓ 이 thread에 연결합니다.", ephemeral=True)
        else:
            thread = await inter.channel.create_thread(
                name=f"kimi-attach-{session_uuid[:8]}-{int(time.time())%10000}",
                type=discord.ChannelType.public_thread,
                auto_archive_duration=1440,
            )
            await inter.followup.send(
                f"✓ Thread 생성: {thread.mention}", ephemeral=True)
        await thread.send(
            f"🔗 로컬 surface를 연결 중...\n"
            f"session=`{session_uuid[:8]}…` · surface `{surf_id}`"
        )

        # Match cmux tab title to the new thread (same rule as /new).
        try:
            await rename_tab(surf_id, _short_tab_title(ws_name, thread.name))
        except Exception as e:
            log.warning("rename_tab failed for attach: %s", e)

        # Build WireSession without creating a new surface
        sess = WireSession(
            thread_id=thread.id,
            surface_id=surf_id,
            session_uuid=session_uuid,
            cwd=cwd,
            owner_id=inter.user.id,
        )
        await sess.start_relay(thread, client)
        router.sessions[thread.id] = sess

        # wire.jsonl is created lazily on the first conversation turn. If the
        # attached surface already has history, tail it now with
        # from_beginning=False so past events don't get replayed into Discord.
        # If not, fall through — send_to_surface → ensure_tail handles it on
        # the first user message (same pattern as /new) and surfaces a
        # "wire.jsonl not found" warning after 60s if it truly fails.
        wire_path = await wait_for_wire_jsonl(session_uuid, cwd, max_wait=2.0)
        if wire_path and sess.relay:
            await sess.relay.start_tail(wire_path, client, from_beginning=False)

        # Register in DB
        router.registry.insert(SessionRow(
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
        await router.refresh_sleep_guard()

        await thread.send(
            f"준비 완료. 메시지를 보내면 kimi에 전달됩니다.\n"
            f"session=`{session_uuid[:8]}…` · surface `{surf_id}`\n"
            f"이 스레드에서: `/stop` 응답 멈춤 · `/kill` 세션 종료"
        )
        log.info("attach: session %s attached to thread %s", session_uuid, thread.id)

    select.callback = on_select
    view = discord.ui.View(timeout=120)
    view.add_item(select)
    await interaction.followup.send("연결할 surface를 선택하세요:", view=view, ephemeral=True)


@tree.command(name="cleanup", description="고아 thread를 삭제합니다 (registry에 없는 thread, 복구 불가)")
async def cleanup_cmd(interaction: discord.Interaction):
    """Find active Discord threads that are NOT registered in the registry and clean them up."""
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("텍스트 채널에서만 사용 가능해요.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    # 1. Fetch all active threads in the guild
    try:
        active_threads = await interaction.guild.active_threads()
    except Exception as e:
        await interaction.followup.send(f"thread 목록 조회 실패: {e}", ephemeral=True)
        return

    # 2. Filter threads that belong to this channel
    channel_threads = [
        t for t in active_threads
        if t.parent_id == interaction.channel.id
    ]

    if not channel_threads:
        await interaction.followup.send("이 채널에 활성 thread가 없어요.", ephemeral=True)
        return

    # 3. Classify each channel thread.
    #    - unregistered: no active registry row → orphan (legacy definition)
    #    - zombie: active registry row but cmux surface dead → orphan (new)
    #    - live: registered + surface responsive (or within grace period)
    active_by_thread = {r.thread_id: r for r in router.registry.list_active()}

    # cmux reachability preflight: if cmux daemon itself is unreachable, every
    # ping would fail and we'd wrongly flag the entire fleet as zombies.
    cmux_ok = True
    try:
        await list_workspaces()
    except CmuxError as e:
        log.warning("cmux preflight failed: %s; zombie detection disabled", e)
        cmux_ok = False

    zombie_ids, unregistered_ids = await _classify_threads_for_cleanup(
        channel_threads,
        active_by_thread,
        cmux_ok=cmux_ok,
        surface_probe=surface_read_text,
        now=int(time.time()),
    )

    orphan_ids = unregistered_ids | zombie_ids
    orphan_threads = [t for t in channel_threads if str(t.id) in orphan_ids]

    if not orphan_threads:
        msg = "🧹 정리할 고아 thread가 없어요. 모든 활성 thread가 살아있는 세션을 가지고 있습니다."
        if not cmux_ok:
            msg += "\n⚠️ cmux 응답 없음 — 좀비 탐지는 건너뜀 (registry 미등록 thread만 검사함)."
        await interaction.followup.send(msg, ephemeral=True)
        return

    # 4. Present options to delete them
    options = []
    _thread_map: dict[str, discord.Thread] = {}
    for t in orphan_threads[:25]:
        label = t.name[:100]
        tid = str(t.id)
        _thread_map[tid] = t
        desc = ("⚠️ 좀비: registry=active이지만 cmux surface 죽음"
                if tid in zombie_ids else "registry에 없는 thread")
        options.append(discord.SelectOption(
            label=label, value=tid, description=desc[:100]))

    # Add "all" option if within limit
    if len(options) < 25:
        z = len(zombie_ids)
        u = len(unregistered_ids)
        options.insert(0, discord.SelectOption(
            label="🗑️ 모두 삭제",
            value="__all__",
            description=f"{len(orphan_threads)}개 삭제 (좀비 {z} + 미등록 {u}, 복구 불가)"[:100]))

    select = discord.ui.Select(placeholder="삭제할 고아 thread 선택", options=options)

    async def on_select(inter: discord.Interaction):
        # Deleting N threads = N Discord API calls; can exceed the 3s
        # interaction deadline. Defer first.
        await inter.response.defer(ephemeral=True)

        val = select.values[0]
        if val == "__all__":
            targets = list(_thread_map.values())
        else:
            targets = [_thread_map.get(val)]
            targets = [t for t in targets if t]

        deleted = 0
        zombies_cleaned = 0
        errors = []
        for t in targets:
            tid = str(t.id)
            try:
                # Zombies still have stale registry rows + in-memory sessions.
                # Tear those down before deleting the thread to keep state
                # consistent. shutdown_session is idempotent and tolerates a
                # dead surface (close_surface is wrapped in try/except).
                if tid in zombie_ids:
                    try:
                        await router.shutdown_session(t.id)
                        zombies_cleaned += 1
                    except Exception as e:
                        log.warning("zombie shutdown failed for thread %s: %s", t.id, e)
                await t.delete()
                deleted += 1
            except Exception as e:
                errors.append(f"{t.name}: {e}")
                log.warning("cleanup delete failed for thread %s: %s", t.id, e)

        msg_parts = [f"🗑️ {deleted}개의 고아 thread를 삭제했습니다."]
        if zombies_cleaned:
            msg_parts.append(f"🧟 좀비 세션 {zombies_cleaned}개의 registry 상태를 정리했습니다.")
        if errors:
            msg_parts.append(f"⚠️ 실패 {len(errors)}개: " + ", ".join(errors[:3]))
        await inter.followup.send("\n".join(msg_parts), ephemeral=True)

    select.callback = on_select
    view = discord.ui.View(timeout=120)
    view.add_item(select)
    summary = f"**{len(orphan_threads)}개의 고아 thread** 발견 (좀비 {len(zombie_ids)} + 미등록 {len(unregistered_ids)})."
    if not cmux_ok:
        summary += "\n⚠️ cmux 응답 없음 — 좀비 탐지는 건너뜀."
    await interaction.followup.send(summary + " 삭제할 thread를 선택하세요:",
                                     view=view, ephemeral=True)


@tree.command(name="cmux-run", description="cmux 데몬이 꺼져 있으면 실행시킵니다")
async def cmux_run_cmd(interaction: discord.Interaction):
    # ensure_cmux_running can take up to 15s; defer first.
    await interaction.response.defer(ephemeral=True)
    try:
        await list_workspaces()
        await interaction.followup.send(
            "✓ cmux는 이미 실행 중입니다.", ephemeral=True)
        return
    except CmuxError:
        pass
    ok = await ensure_cmux_running()
    if ok:
        await interaction.followup.send(
            "🚀 cmux를 실행했습니다.", ephemeral=True)
    else:
        await interaction.followup.send(
            "⚠️ cmux 실행 실패. `/Applications/cmux.app` 설치 여부 확인 "
            "(macOS 전용). 자세한 사유는 봇 로그 참조.",
            ephemeral=True)


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

    # Match cmux tab title to the new thread (same rule as /new).
    try:
        await rename_tab(old_sess.surface_id,
                          _short_tab_title(row.workspace_name, new_thread.name))
    except Exception as e:
        log.warning("rename_tab failed for rebind: %s", e)

    # Move the session: update in-memory dict
    router.sessions.pop(old_thread_id, None)
    old_sess.thread_id = new_thread.id
    router.sessions[new_thread.id] = old_sess

    # Update registry — wrap to roll back in-memory state on failure.
    try:
        router.registry.conn.execute(
            "UPDATE sessions SET thread_id=?, channel_id=? WHERE thread_id=?",
            (str(new_thread.id), str(new_thread.parent_id), str(old_thread_id)),
        )
        router.registry.conn.commit()
    except sqlite3.IntegrityError as e:
        # PK conflict — restore in-memory dict, surface error.
        router.sessions.pop(new_thread.id, None)
        old_sess.thread_id = old_thread_id
        router.sessions[old_thread_id] = old_sess
        log.error("rebind UPDATE failed: %s", e)
        await interaction.followup.send(
            f"⚠️ rebind 실패 (DB): {e}", ephemeral=True)
        return

    # Update relay's thread reference
    if old_sess.relay:
        old_sess.relay.thread = new_thread

    # Farewell old thread, welcome new thread
    await interaction.channel.send(
        f"✓ 세션이 {new_thread.mention} 로 이동되었습니다.")
    await new_thread.send(
        f"🔄 세션이 이곳으로 이동되었습니다.\n"
        f"session=`{old_sess.session_uuid[:8]}…` · surface `{old_sess.surface_id[:8]}…`\n"
        f"메시지를 보내면 kimi에 전달됩니다.")

    log.info("rebind: session %s moved from thread %s to %s",
             old_sess.session_uuid, old_thread_id, new_thread.id)


# ── Image attachment relay helpers ───────────────────────────────────────

def _sanitize_filename(name: str) -> str:
    """Strip path separators, traversal segments, NULL bytes, and leading
    dots that would create a hidden file. Falls back to 'file' if the
    result is empty. Length-capped at 80 chars to keep paths sane."""
    import re as _re
    if not name:
        return "file"
    # Take the basename component only — drop any directory parts the
    # client may have included.
    base = name.replace("\\", "/").split("/")[-1]
    # Remove NULL bytes and ASCII control characters.
    base = _re.sub(r"[\x00-\x1f]", "", base)
    # Strip leading dots so we never write a hidden file.
    base = base.lstrip(".")
    # Whitelist: alnum + ._- ; replace everything else with _.
    base = _re.sub(r"[^A-Za-z0-9._-]", "_", base)
    if not base:
        base = "file"
    return base[:80]


def _is_allowed_image(filename: str) -> bool:
    """Extension-based image gate. Allows png/jpg/jpeg/webp/gif."""
    if not filename:
        return False
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in ALLOWED_IMAGE_EXTS)


def _upload_dir(thread_id: int) -> Path:
    return UPLOADS_ROOT / str(thread_id)


def _delete_upload_dir(thread_id: int) -> None:
    """Best-effort recursive deletion of a thread's upload directory."""
    import shutil
    target = _upload_dir(thread_id)
    if target.exists():
        try:
            shutil.rmtree(target)
        except Exception as e:
            log.warning("upload dir cleanup failed for thread %s: %s",
                        thread_id, e)


async def _save_attachment(thread_id: int,
                           attachment: discord.Attachment) -> tuple[Path | None, str | None]:
    """Validate + save one Discord attachment.

    Returns (saved_path, None) on success, or (None, reason) on rejection.
    """
    fname = attachment.filename or ""
    if not _is_allowed_image(fname):
        return None, f"`{fname}`: 이미지만 허용 (png/jpg/jpeg/webp/gif)"
    if attachment.size and attachment.size > MAX_UPLOAD_BYTES:
        mb = attachment.size / (1024 * 1024)
        return None, f"`{fname}`: {mb:.1f}MB > 10MB 한도 초과"
    safe_name = _sanitize_filename(fname)
    dest_dir = _upload_dir(thread_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Prefix with timestamp-ms so concurrent same-name uploads don't collide.
    dest = dest_dir / f"{int(time.time() * 1000)}-{safe_name}"
    try:
        data = await attachment.read()
    except Exception as e:
        log.warning("attachment download failed: %s", e)
        return None, f"`{fname}`: 다운로드 실패 ({e})"
    if len(data) > MAX_UPLOAD_BYTES:
        # attachment.size can be unreliable; cap on actual bytes too.
        return None, f"`{fname}`: {len(data)/(1024*1024):.1f}MB > 10MB 한도 초과"
    try:
        dest.write_bytes(data)
    except Exception as e:
        log.warning("attachment save failed: %s", e)
        return None, f"`{fname}`: 디스크 저장 실패 ({e})"
    return dest, None


def _compose_message_with_attachments(text: str, paths: list[Path]) -> str:
    """Build the final kimi-cli payload: one `@<abspath>` line per image,
    followed by the user's text. kimi-cli's TUI recognises `@` as a file
    mention (status bar literally shows `@: mention files`)."""
    lines = [f"@{p}" for p in paths]
    if text:
        lines.append(text)
    return "\n".join(lines)


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

    # Handle image attachments (Discord → kimi via @path mention).
    saved_paths: list[Path] = []
    rejections: list[str] = []
    if msg.attachments:
        for att in msg.attachments:
            path, reason = await _save_attachment(msg.channel.id, att)
            if path:
                saved_paths.append(path)
            elif reason:
                rejections.append(reason)
        if rejections:
            try:
                await msg.channel.send(
                    "⚠️ 일부 첨부를 건너뛰었어요:\n" + "\n".join(f"• {r}" for r in rejections))
            except Exception:
                log.exception("rejection notice send failed")
        # If user sent ONLY rejected attachments (no text, no accepted images)
        # there's nothing meaningful to forward — skip the relay.
        if not saved_paths and not msg.content:
            return

    payload = _compose_message_with_attachments(msg.content, saved_paths)
    await router.relay_user_message(
        msg.channel.id, payload, client, author_user_id=msg.author.id)


@client.event
async def on_thread_update(before: discord.Thread, after: discord.Thread):
    if not before.archived and after.archived:
        await router.shutdown_session(after.id)
    elif before.archived and not after.archived:
        # Thread un-archived (manually) — surface was closed on archive,
        # so this thread is just a corpse. Tell the user how to recover.
        row = router.registry.get_by_thread(str(after.id))
        if row and row.status == "dead":
            try:
                await after.send(
                    "⚠️ 이 thread는 자동 보관 중 세션이 종료됐습니다.\n"
                    "`/attach` 로 살아있는 surface에 연결하거나, "
                    "`/new` 로 새 세션을 시작하세요."
                )
            except Exception:
                log.exception("un-archive notice failed")


@client.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        tree.copy_global_to(guild=guild)
        try:
            synced = await tree.sync(guild=guild)
            log.info("synced %d commands to guild %s: %s",
                     len(synced), GUILD_ID,
                     [c.name for c in synced])
        except Exception as e:
            log.error("command sync failed: %s", e)
    else:
        try:
            synced = await tree.sync()
            log.info("synced %d global commands: %s",
                     len(synced), [c.name for c in synced])
        except Exception as e:
            log.error("command sync failed: %s", e)
    log.info("bot online: %s", client.user)
    router.start_delivery_worker(client)
    await router.refresh_sleep_guard()


async def _shutdown() -> None:
    """Best-effort cleanup on bot process exit (SIGINT/SIGTERM).

    Intentionally does NOT close cmux surfaces or mark sessions dead. The
    bot may be restarting while the user wants their kimi-cli sessions to
    keep running in cmux — they can `/attach` (or `/rebind`) once the bot
    is back online to re-link Discord threads to the surviving surfaces.

    Per-session destruction happens only through explicit user actions
    (`/kill`, `/cleanup`, thread archive) which call `shutdown_session`
    directly.
    """
    if router.sessions:
        log.info("shutdown: leaving %d cmux session(s) alive for later /attach",
                 len(router.sessions))
    await router.stop_delivery_worker()
    await router.sleep_guard.stop()
    # Stop in-process relays (cancels debounce flush tasks) without touching
    # the cmux surface itself.
    for sess in list(router.sessions.values()):
        try:
            if sess.relay:
                await sess.relay.stop()
        except Exception:
            log.exception("relay stop failed for thread %s", sess.thread_id)
    try:
        router.registry.conn.close()
    except Exception:
        log.exception("registry close failed")
    try:
        await client.close()
    except Exception:
        log.exception("client close failed")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    def _handler():
        log.info("signal received, beginning shutdown")
        asyncio.create_task(_shutdown())
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:
            # Windows or restricted env — fall back to default ^C behaviour.
            pass


def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set (check .env)")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_signal_handlers(loop)
    try:
        loop.run_until_complete(client.start(token))
    except KeyboardInterrupt:
        loop.run_until_complete(_shutdown())
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()


if __name__ == "__main__":
    main()
