"""cmux_client: _rpc subprocess invocation + ensure_cmux_running scenarios."""
from __future__ import annotations
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router import cmux_client
from router.cmux_client import CmuxError, _rpc, ensure_cmux_running


def _fake_proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    """Build a mock of asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


async def test_rpc_passes_method_and_params(monkeypatch):
    """`cmux rpc <method> <json-params>` is the expected command shape."""
    seen_args: list = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        seen_args.extend(args)
        return _fake_proc(stdout=b'{"ok": true}\n')

    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        fake_create_subprocess_exec)

    result = await _rpc("surface.create", {"workspace_id": "ws-1"})
    assert result == {"ok": True}
    assert seen_args[0] == cmux_client.CMUX_CMD
    assert seen_args[1] == "rpc"
    assert seen_args[2] == "surface.create"
    assert json.loads(seen_args[3]) == {"workspace_id": "ws-1"}


async def test_rpc_without_params_omits_json_arg(monkeypatch):
    seen_args: list = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        seen_args.extend(args)
        return _fake_proc(stdout=b"{}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        fake_create_subprocess_exec)

    await _rpc("workspace.list")
    assert len(seen_args) == 3
    assert seen_args[-1] == "workspace.list"


async def test_rpc_nonzero_returncode_raises_cmux_error(monkeypatch):
    async def fake_create_subprocess_exec(*args, **kwargs):
        return _fake_proc(stderr=b"no such surface", returncode=2)

    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        fake_create_subprocess_exec)

    with pytest.raises(CmuxError) as exc:
        await _rpc("surface.read_text", {"surface_id": "gone"})
    assert "no such surface" in str(exc.value)


async def test_rpc_returns_raw_string_when_not_json(monkeypatch):
    async def fake_create_subprocess_exec(*args, **kwargs):
        return _fake_proc(stdout=b"plain text output\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        fake_create_subprocess_exec)

    out = await _rpc("some.text_method")
    assert out == "plain text output"


async def test_rpc_returns_none_on_empty_output(monkeypatch):
    async def fake_create_subprocess_exec(*args, **kwargs):
        return _fake_proc(stdout=b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec",
                        fake_create_subprocess_exec)

    out = await _rpc("noop")
    assert out is None


# ── ensure_cmux_running ──────────────────────────────────────────────────

async def test_ensure_cmux_running_returns_true_when_already_up(monkeypatch):
    monkeypatch.setattr(cmux_client, "_rpc", AsyncMock(return_value={}))
    launched: list = []

    async def fake_open(*args, **kwargs):
        launched.append(args)
        return _fake_proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_open)

    ok = await ensure_cmux_running(timeout=1.0)
    assert ok is True
    assert launched == []


async def test_ensure_cmux_running_launches_when_down_then_comes_up(monkeypatch):
    call_count = {"n": 0}

    async def flaky_rpc(method):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise CmuxError("not up")
        return {}

    monkeypatch.setattr(cmux_client, "_rpc", flaky_rpc)

    async def fake_open(*args, **kwargs):
        return _fake_proc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_open)

    ok = await ensure_cmux_running(timeout=3.0)
    assert ok is True
    assert call_count["n"] >= 2


async def test_ensure_cmux_running_returns_false_if_open_fails(monkeypatch):
    monkeypatch.setattr(cmux_client, "_rpc",
                        AsyncMock(side_effect=CmuxError("down")))

    async def failing_open(*args, **kwargs):
        return _fake_proc(stderr=b"app not found", returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failing_open)

    ok = await ensure_cmux_running(timeout=1.0)
    assert ok is False


async def test_ensure_cmux_running_times_out_when_socket_never_comes_up(monkeypatch):
    monkeypatch.setattr(cmux_client, "_rpc",
                        AsyncMock(side_effect=CmuxError("still down")))

    async def fake_open(*args, **kwargs):
        return _fake_proc(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_open)

    ok = await ensure_cmux_running(timeout=2.5)
    assert ok is False


# ── Shallow wrappers ─────────────────────────────────────────────────────

async def test_list_workspaces_unwraps_payload(monkeypatch):
    monkeypatch.setattr(cmux_client, "_rpc",
                        AsyncMock(return_value={"workspaces": [{"id": "w1"},
                                                                {"id": "w2"}]}))
    out = await cmux_client.list_workspaces()
    assert out == [{"id": "w1"}, {"id": "w2"}]


async def test_list_workspaces_empty_returns_empty_list(monkeypatch):
    monkeypatch.setattr(cmux_client, "_rpc", AsyncMock(return_value=None))
    out = await cmux_client.list_workspaces()
    assert out == []


async def test_list_surfaces_passes_workspace_id(monkeypatch):
    mock_rpc = AsyncMock(return_value={"surfaces": [{"id": "s1"}]})
    monkeypatch.setattr(cmux_client, "_rpc", mock_rpc)
    out = await cmux_client.list_surfaces("ws-1")
    assert out == [{"id": "s1"}]
    mock_rpc.assert_awaited_once_with("surface.list", {"workspace_id": "ws-1"})


async def test_surface_send_text_includes_both_params(monkeypatch):
    mock_rpc = AsyncMock(return_value=None)
    monkeypatch.setattr(cmux_client, "_rpc", mock_rpc)
    await cmux_client.surface_send_text("surf-9", "hello\n")
    mock_rpc.assert_awaited_once_with(
        "surface.send_text", {"surface_id": "surf-9", "text": "hello\n"})


async def test_surface_read_text_handles_string_response(monkeypatch):
    monkeypatch.setattr(cmux_client, "_rpc",
                        AsyncMock(return_value="ANSI-stripped screen"))
    out = await cmux_client.surface_read_text("surf-1")
    assert out == "ANSI-stripped screen"


async def test_surface_read_text_handles_dict_text_key(monkeypatch):
    monkeypatch.setattr(cmux_client, "_rpc",
                        AsyncMock(return_value={"text": "from-key"}))
    out = await cmux_client.surface_read_text("surf-1")
    assert out == "from-key"


async def test_close_surface_passes_id(monkeypatch):
    mock_rpc = AsyncMock(return_value=None)
    monkeypatch.setattr(cmux_client, "_rpc", mock_rpc)
    await cmux_client.close_surface("surf-1")
    mock_rpc.assert_awaited_once_with("surface.close", {"surface_id": "surf-1"})


async def test_create_workspace_omits_empty_optional_params(monkeypatch):
    mock_rpc = AsyncMock(return_value={"id": "new"})
    monkeypatch.setattr(cmux_client, "_rpc", mock_rpc)
    await cmux_client.create_workspace()
    mock_rpc.assert_awaited_once_with("workspace.create", {})


async def test_create_surface_sets_workspace_and_type(monkeypatch):
    mock_rpc = AsyncMock(return_value={"surface_id": "s1"})
    monkeypatch.setattr(cmux_client, "_rpc", mock_rpc)
    await cmux_client.create_surface("ws-1", focus=True)
    args = mock_rpc.await_args.args
    assert args[0] == "surface.create"
    params = args[1]
    assert params["workspace_id"] == "ws-1"
    assert params["type"] == "terminal"
    assert params["focus"] is True
