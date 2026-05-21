"""Lock/restore race regressions.

These tests pin down user-visible bugs that the existing unit tests missed:

  A) _restore_one_wire_session marks restore as success without verifying
     that `kimi --session <uuid>` actually attached inside the cmux surface.
  B) cmux start_relay starts before the wire helper is closed, opening a
     window where both backends emit the same session's events.
  C) Pending delivery routes to the wire helper while backend == "restoring",
     racing the in-progress cmux resume on the same session_id.
  D) KimiWireClient resolves "node" via PATH only; in a stock-PATH bot
     environment this raises FileNotFoundError instead of falling back to
     an absolute path.

Tests A–C are written against the *desired* behavior; they are marked
xfail until the corresponding fix lands. Test D is a straight verification
that gains a positive assertion once the fix is in place.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router import bot
from router.kimi_wire import KimiWireClient


def _row(thread_id: str = "123", **overrides) -> bot.SessionRow:
    data = dict(
        thread_id=thread_id,
        guild_id="g",
        channel_id="c",
        owner_user_id="7",
        workspace_id="ws",
        workspace_name="ws",
        cwd="/tmp",
        monitor_surface_id=None,
        acp_session_id="sess-1",
        status="active",
        created_at=0,
        last_active_at=0,
        backend="wire",
    )
    data.update(overrides)
    return bot.SessionRow(**data)


# --------------------------------------------------------------------------- #
# A) restore must verify kimi-cli actually attached                           #
# --------------------------------------------------------------------------- #

async def test_restore_fails_when_kimi_session_does_not_attach(
    tmp_sqlite_path, monkeypatch
):
    """If kimi --session <uuid> never produces a matching banner inside the
    new cmux surface, restore must NOT report success.

    Today _restore_one_wire_session fires surface_send_text and immediately
    flips backend to "cmux" without checking that the banner shows the
    expected session UUID. That silently produces a dead surface."""
    monkeypatch.setattr(bot, "REGISTRY_PATH", tmp_sqlite_path)
    # Keep the verification timeout tiny so the test fails fast.
    monkeypatch.setattr(bot, "RESTORE_VERIFY_TIMEOUT_SEC", 0)
    router = bot.Router()
    router.registry.insert(_row())

    wire_sess = MagicMock()
    wire_sess.active_turn = False
    wire_sess.close = AsyncMock()
    router.wire_sessions[123] = wire_sess

    thread = MagicMock()
    thread.id = 123
    thread.name = "kimi-test"
    thread.send = AsyncMock()
    client = MagicMock()
    client.get_channel.return_value = thread

    # surface_read_text returns a shell prompt but never the banner with
    # the expected acp_session_id — simulating kimi-cli that never attached.
    with patch.object(
        bot, "list_workspaces",
        new=AsyncMock(return_value=[{"id": "ws"}]),
    ), patch.object(
        bot, "create_surface",
        new=AsyncMock(return_value={"surface_id": "surf-new"}),
    ), patch.object(
        bot, "surface_read_text", new=AsyncMock(return_value="$ ")
    ), patch.object(
        bot, "surface_send_text", new=AsyncMock()
    ), patch.object(
        bot, "rename_tab", new=AsyncMock()
    ):
        restored = await router.restore_wire_sessions_once(client)

    # Desired: restore reports 0 successes; backend stays "wire" (or "restoring");
    # the user sees no false "복구 완료" notice.
    assert restored == 0, "restore reported success without banner verification"
    row = router.registry.get_by_thread("123")
    assert row.backend != "cmux", (
        f"backend prematurely flipped to cmux without verifying kimi attach; "
        f"got backend={row.backend!r}"
    )


# --------------------------------------------------------------------------- #
# B) wire helper must close BEFORE cmux relay starts                          #
# --------------------------------------------------------------------------- #

async def test_restore_closes_wire_helper_before_starting_cmux_relay(
    tmp_sqlite_path, monkeypatch
):
    """Closing the wire helper *after* starting the cmux ThreadRelay opens a
    window where both emit events from the same Kimi session_id, causing
    duplicate responses in Discord. Order must be: close wire → resume cmux
    → start relay."""
    monkeypatch.setattr(bot, "REGISTRY_PATH", tmp_sqlite_path)
    router = bot.Router()
    router.registry.insert(_row())

    call_order: list[str] = []

    wire_sess = MagicMock()
    wire_sess.active_turn = False

    async def record_wire_close():
        call_order.append("wire_close")
    wire_sess.close = record_wire_close
    router.wire_sessions[123] = wire_sess

    thread = MagicMock()
    thread.id = 123
    thread.name = "kimi-test"
    thread.send = AsyncMock()
    client = MagicMock()
    client.get_channel.return_value = thread

    original_start_relay = bot.WireSession.start_relay

    async def record_start_relay(self, *args, **kwargs):
        call_order.append("start_relay")
        # Don't actually start anything real.
        return None

    with patch.object(
        bot, "list_workspaces",
        new=AsyncMock(return_value=[{"id": "ws"}]),
    ), patch.object(
        bot, "create_surface",
        new=AsyncMock(return_value={"surface_id": "surf-new"}),
    ), patch.object(
        bot, "surface_read_text", new=AsyncMock(return_value="Session: sess-1")
    ), patch.object(
        bot, "surface_send_text", new=AsyncMock()
    ), patch.object(
        bot, "rename_tab", new=AsyncMock()
    ), patch.object(
        bot.WireSession, "start_relay", new=record_start_relay
    ):
        await router.restore_wire_sessions_once(client)

    assert "wire_close" in call_order, "wire helper was never closed"
    assert "start_relay" in call_order, "cmux relay never started"
    assert call_order.index("wire_close") < call_order.index("start_relay"), (
        f"wire close must precede start_relay to avoid duplicate emit; "
        f"actual order: {call_order}"
    )


# --------------------------------------------------------------------------- #
# C) restoring state must hold pending delivery, not route to wire            #
# --------------------------------------------------------------------------- #

async def test_pending_delivery_holds_while_backend_is_restoring(
    tmp_sqlite_path, monkeypatch
):
    """While backend == 'restoring' the cmux resume is in progress. New
    messages must stay pending; routing them to the wire helper at this
    moment races the freshly-resumed cmux kimi-cli on the same session_id
    and produces duplicate Discord responses."""
    monkeypatch.setattr(bot, "REGISTRY_PATH", tmp_sqlite_path)
    router = bot.Router()
    router.registry.insert(_row(backend="restoring"))
    msg_id = router.registry.enqueue_message(
        thread_id="123", author_user_id="7",
        content="hold me during restore",
        discord_message_id="m-1")

    thread = MagicMock()
    thread.send = AsyncMock()
    client = MagicMock()
    client.get_channel.return_value = thread

    wire_prompt = AsyncMock()
    wire_sess = MagicMock()
    wire_sess.session_id = "sess-1"
    wire_sess.prompt = wire_prompt
    with patch.object(
        router.wire_client, "create_session",
        new=AsyncMock(return_value=wire_sess),
    ):
        await router.deliver_pending_once(client)

    wire_prompt.assert_not_awaited()
    assert router.registry.count_pending_messages("123") == 1, (
        f"message id={msg_id} must remain pending while backend is restoring; "
        f"pending count={router.registry.count_pending_messages('123')}"
    )


# --------------------------------------------------------------------------- #
# D) KimiWireClient must locate node even when PATH lacks brew/nvm            #
# --------------------------------------------------------------------------- #

async def test_kimi_wire_client_raises_clearly_when_no_node_anywhere(
    monkeypatch
):
    """When PATH has no node and all fallback absolute paths are missing,
    spawning the helper must raise FileNotFoundError (not hang or fail
    silently). This pins the diagnostic behavior."""
    from router import kimi_wire

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("KIMI_WIRE_BRIDGE_CMD", raising=False)
    monkeypatch.delenv("KIMI_NODE_CMD", raising=False)
    monkeypatch.setattr(kimi_wire, "_NODE_FALLBACK_PATHS", ())
    client = KimiWireClient()
    with pytest.raises(FileNotFoundError):
        await client.start()


async def test_restore_closes_new_surface_when_verify_fails(
    tmp_sqlite_path, monkeypatch
):
    """Discovered in E2E: a stuck restore loop created 2000+ ghost cmux
    surfaces. When _restore_one_wire_session fails after create_surface
    (e.g. _wait_for_surface_ready or banner verification times out), the
    surface we just made must be closed — otherwise the next worker tick
    creates yet another one."""
    monkeypatch.setattr(bot, "REGISTRY_PATH", tmp_sqlite_path)
    monkeypatch.setattr(bot, "RESTORE_VERIFY_TIMEOUT_SEC", 0)
    router = bot.Router()
    router.registry.insert(_row())

    thread = MagicMock()
    thread.id = 123
    thread.name = "kimi-test"
    thread.send = AsyncMock()
    client = MagicMock()
    client.get_channel.return_value = thread

    close_mock = AsyncMock()
    with patch.object(
        bot, "list_workspaces",
        new=AsyncMock(return_value=[{"id": "ws"}]),
    ), patch.object(
        bot, "create_surface",
        new=AsyncMock(return_value={"surface_id": "leaky-surf"}),
    ), patch.object(
        bot, "surface_read_text", new=AsyncMock(return_value="$ ")
    ), patch.object(
        bot, "surface_send_text", new=AsyncMock()
    ), patch.object(
        bot, "rename_tab", new=AsyncMock()
    ), patch.object(
        bot, "close_surface", new=close_mock
    ):
        await router.restore_wire_sessions_once(client)

    assert close_mock.await_args_list, (
        "failed restore must close the surface it created; "
        "otherwise cmux leaks one surface per failed tick"
    )
    assert close_mock.await_args.args[0] == "leaky-surf"


async def test_restore_skips_create_when_workspace_is_missing(
    tmp_sqlite_path, monkeypatch
):
    """A stale workspace_id must not be fed into surface.create. That path
    was producing Workspace not found errors and repeated create attempts."""
    monkeypatch.setattr(bot, "REGISTRY_PATH", tmp_sqlite_path)
    router = bot.Router()
    router.registry.insert(_row(workspace_id="missing-ws"))

    wire_sess = MagicMock()
    wire_sess.active_turn = False
    wire_sess.close = AsyncMock()
    router.wire_sessions[123] = wire_sess

    thread = MagicMock()
    thread.id = 123
    thread.name = "kimi-test"
    client = MagicMock()
    client.get_channel.return_value = thread

    create_mock = AsyncMock()
    with patch.object(
        bot, "list_workspaces",
        new=AsyncMock(return_value=[{"id": "other-ws"}]),
    ), patch.object(bot, "create_surface", new=create_mock):
        restored = await router.restore_wire_sessions_once(client)

    assert restored == 0
    create_mock.assert_not_awaited()
    wire_sess.close.assert_not_awaited()
    row = router.registry.get_by_thread("123")
    assert row.backend == "wire"
    assert row.last_backend_error.startswith("cmux_unhealthy:")


async def test_cmux_not_found_restore_error_opens_global_circuit_breaker(
    tmp_sqlite_path, monkeypatch
):
    """Once cmux reports an inconsistent surface state, pause all restore
    attempts instead of creating a new surface for each active wire row."""
    monkeypatch.setattr(bot, "REGISTRY_PATH", tmp_sqlite_path)
    monkeypatch.setattr(bot, "RESTORE_CMUX_UNHEALTHY_BACKOFF_SEC", 600)
    router = bot.Router()
    router.registry.insert(_row("123"))
    router.registry.insert(_row("456"))

    thread = MagicMock()
    thread.id = 123
    thread.name = "kimi-test"
    client = MagicMock()
    client.get_channel.return_value = thread

    create_mock = AsyncMock(return_value={"surface_id": "surf-new"})
    with patch.object(
        bot, "list_workspaces",
        new=AsyncMock(return_value=[{"id": "ws"}]),
    ), patch.object(
        bot, "create_surface", new=create_mock
    ), patch.object(
        bot, "surface_read_text",
        new=AsyncMock(side_effect=bot.CmuxError(
            "cmux rpc surface.read_text failed: Error: not_found: "
            "Terminal surface not found"
        )),
    ), patch.object(
        bot, "close_surface", new=AsyncMock()
    ):
        restored = await router.restore_wire_sessions_once(client)
        restored_again = await router.restore_wire_sessions_once(client)

    assert restored == 0
    assert restored_again == 0
    assert create_mock.await_count == 1


async def test_restore_worker_honors_backoff_after_failure(
    tmp_sqlite_path, monkeypatch
):
    """After a failed attempt, restore_attempt_at is set and the next
    worker tick must skip this row until RESTORE_RETRY_BACKOFF_SEC has
    elapsed. Without this, restore_worker hammers cmux every 5 seconds
    and (combined with the leak above) drowns the daemon."""
    import time as _time
    monkeypatch.setattr(bot, "REGISTRY_PATH", tmp_sqlite_path)
    monkeypatch.setattr(bot, "RESTORE_RETRY_BACKOFF_SEC", 300)
    router = bot.Router()
    router.registry.insert(_row())
    router.registry.set_backend(
        "123", "wire",
        surface_id=None, abandoned_surface_id=None,
        restore_attempt_at=int(_time.time()) - 5,
    )

    create_mock = AsyncMock()
    with patch.object(bot, "create_surface", new=create_mock):
        restored = await router.restore_wire_sessions_once(MagicMock())

    assert restored == 0
    create_mock.assert_not_awaited()


def test_singleton_lock_rejects_second_instance(tmp_path):
    """Two bot processes sharing one sqlite caused the runaway surface
    leak. Lock layer #2 (below the shell pgrep guard) is an fcntl flock on
    a pid file — directly invoking `python -m router.bot` must still be
    blocked when another instance holds the lock."""
    pid_path = str(tmp_path / "router.bot.pid")
    fh = bot._acquire_singleton_lock(pid_path)
    try:
        with pytest.raises(SystemExit):
            bot._acquire_singleton_lock(pid_path)
    finally:
        fh.close()


def test_singleton_lock_reusable_after_release(tmp_path):
    """After the first holder closes the lock fd, a new instance may take
    it. Ensures the lock is process-lifetime, not file-lifetime."""
    pid_path = str(tmp_path / "router.bot.pid")
    fh1 = bot._acquire_singleton_lock(pid_path)
    fh1.close()
    fh2 = bot._acquire_singleton_lock(pid_path)
    fh2.close()


def test_run_bot_sh_refuses_when_router_bot_is_running(tmp_path):
    """Shell-level guard: when pgrep reports a running router.bot, run-bot.sh
    exits non-zero with 'already running' before exec'ing python. Uses a fake
    pgrep on PATH so the test doesn't depend on macOS's real pgrep (which can
    fail with 'sysmond service not found' under TCC restrictions)."""
    import subprocess
    import os
    import stat
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_pgrep = fake_bin / "pgrep"
    # Mimic real pgrep: rc=0 with a matching pid printed when -f given.
    fake_pgrep.write_text(
        "#!/bin/sh\n"
        "echo 99999 python -m router.bot\n"
        "exit 0\n"
    )
    fake_pgrep.chmod(fake_pgrep.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    result = subprocess.run(
        ["bash", "run-bot.sh"],
        cwd=str(__import__("pathlib").Path(__file__).parent.parent),
        capture_output=True, text=True, timeout=5, env=env,
    )
    assert result.returncode != 0, (
        f"run-bot.sh must refuse to start when pgrep reports router.bot is alive; "
        f"got rc={result.returncode}, stderr={result.stderr!r}"
    )
    assert "already running" in result.stderr


def test_find_orphan_wire_pids_detects_helper_and_orphan_kimi():
    """Reaper input is a snapshot of `ps -axo pid,ppid,tty,command`. From
    the production incident:
      - 56432 PPID=1 ??  node .../kimi_wire_bridge.mjs   ← orphan helper
      - 56549 PPID=56432 ??  Kimi Code                   ← wire kimi child
      - 57435 PPID=56859 ttys006  Kimi Code              ← cmux live kimi
    Only the helper itself (PPID=1) should be killed by the *PID-level*
    reaper. The wire kimi grandchild dies with its parent's pgrp. The cmux
    kimi has a real tty and a non-1 PPID so it is never touched.
    """
    ps_output = (
        "  PID  PPID TTY      COMMAND\n"
        "56432     1 ??       /opt/homebrew/bin/node /Users/me/router/kimi_wire_bridge.mjs\n"
        "56549 56432 ??       Kimi Code       \n"
        "57435 56859 ttys006  Kimi Code\n"
        "12345     1 ??       Kimi Code       \n"  # detached orphan kimi
        "99999     1 ??       /opt/homebrew/bin/python -m unrelated\n"
    )
    pids = bot._find_orphan_wire_pids(ps_output)
    assert 56432 in pids, "orphan node helper must be reaped"
    assert 12345 in pids, "orphan kimi-cli with no tty must be reaped"
    assert 56549 not in pids, "non-orphan wire kimi child (PPID=helper) skipped"
    assert 57435 not in pids, "cmux live kimi has tty — must be preserved"
    assert 99999 not in pids, "unrelated orphan process must be left alone"


def test_find_orphan_wire_pids_ignores_malformed_lines():
    """Defensive: header lines and short rows must not crash the reaper."""
    pids = bot._find_orphan_wire_pids(
        "  PID  PPID TTY      COMMAND\n"
        "garbage\n"
        "1 2\n"
    )
    assert pids == []


async def test_wire_client_stop_signals_helper_process_group(monkeypatch):
    """Unit-level regression for the orphan-helper bug.

    The old test spawned real processes and then exercised os.killpg, which
    can kill the pytest/Claude process group if process-group isolation ever
    regresses. Keep this as a pure unit test: verify that stop() targets the
    stored helper pgid, without sending a real signal.
    """
    import signal as _signal

    class FakeProc:
        pid = 12345
        returncode = None

        def terminate(self):
            raise AssertionError("terminate fallback should not be used")

        def kill(self):
            raise AssertionError("kill fallback should not be used")

        async def wait(self):
            self.returncode = -int(_signal.SIGTERM)
            return self.returncode

    calls: list[tuple[int, _signal.Signals]] = []

    def fake_killpg(pgid: int, sig: _signal.Signals):
        calls.append((pgid, sig))

    monkeypatch.setattr("router.kimi_wire.os.killpg", fake_killpg)
    monkeypatch.setattr("router.kimi_wire.os.getpgrp", lambda: 11111)

    client = KimiWireClient(command=["unused"])
    client.proc = FakeProc()
    client._process_group_id = 54321

    await client.stop()

    assert calls == [(54321, _signal.SIGTERM)]


async def test_wire_client_stop_refuses_to_signal_own_process_group(monkeypatch):
    """Safety guard: if process-group isolation fails, stop() must not call
    killpg() on the current pytest/Claude process group."""
    import signal as _signal

    class FakeProc:
        pid = 12345
        returncode = None
        terminated = False

        def terminate(self):
            self.terminated = True
            self.returncode = -int(_signal.SIGTERM)

        def kill(self):
            raise AssertionError("kill fallback should not be used")

        async def wait(self):
            return self.returncode

    def fake_killpg(*_args):
        raise AssertionError("must not signal current process group")

    monkeypatch.setattr("router.kimi_wire.os.killpg", fake_killpg)
    monkeypatch.setattr("router.kimi_wire.os.getpgrp", lambda: 54321)

    proc = FakeProc()
    client = KimiWireClient(command=["unused"])
    client.proc = proc
    client._process_group_id = 54321

    await client.stop()

    assert proc.terminated


async def test_kimi_wire_client_uses_absolute_node_when_path_is_bare(
    monkeypatch, tmp_path
):
    """After the fix, KimiWireClient should honor KIMI_NODE_CMD (or
    shutil.which / well-known absolute paths) so the helper spawns even
    when the bot's PATH lacks /opt/homebrew/bin."""
    fake_node = tmp_path / "node"
    fake_node.write_text("#!/usr/bin/env bash\nexec cat\n")
    fake_node.chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("KIMI_NODE_CMD", str(fake_node))
    monkeypatch.delenv("KIMI_WIRE_BRIDGE_CMD", raising=False)
    client = KimiWireClient()
    try:
        await client.start()
    finally:
        await client.stop()
    # If we got here without FileNotFoundError, the fallback worked.
