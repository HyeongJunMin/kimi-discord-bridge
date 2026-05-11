"""Thin async wrapper around `cmux rpc` for workspace/surface mgmt."""
from __future__ import annotations
import asyncio, json, logging, os
from typing import Any

log = logging.getLogger(__name__)
CMUX_CMD = os.environ.get("CMUX_CMD", "cmux")


class CmuxError(RuntimeError):
    pass


async def _rpc(method: str, params: dict | None = None) -> Any:
    args = [CMUX_CMD, "rpc", method]
    if params is not None:
        args.append(json.dumps(params))
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise CmuxError(f"cmux rpc {method} failed: {err.decode().strip()}")
    out_s = out.decode().strip()
    if not out_s:
        return None
    try:
        return json.loads(out_s)
    except json.JSONDecodeError:
        return out_s  # some methods return plain text


async def list_workspaces() -> list[dict]:
    """Returns list of workspace dicts (subset of fields used)."""
    res = await _rpc("workspace.list")
    if not res:
        return []
    return res.get("workspaces", [])


async def list_surfaces(workspace_id: str) -> list[dict]:
    """Returns list of surface dicts in the given workspace."""
    res = await _rpc("surface.list", {"workspace_id": workspace_id})
    if not res:
        return []
    return res.get("surfaces", [])


async def create_workspace(*, name: str | None = None, cwd: str | None = None) -> dict:
    """Create a workspace; returns the workspace info dict."""
    params: dict = {}
    if name: params["name"] = name
    if cwd: params["cwd"] = cwd
    return await _rpc("workspace.create", params)


async def create_monitor_surface(workspace_id: str, *, command: str | None = None) -> dict:
    """Create a terminal surface in the given workspace, optionally running a command.
    Used as a read-only viewer pane next to the Discord-controlled session."""
    params = {"workspace_id": workspace_id, "type": "terminal", "focus": False}
    if command:
        params["command"] = command
    return await _rpc("surface.create", params)


async def create_surface(workspace_id: str, *, command: str | None = None,
                           focus: bool = False) -> dict:
    """Create a terminal surface in the given workspace."""
    params = {"workspace_id": workspace_id, "type": "terminal", "focus": focus}
    if command:
        params["command"] = command
    return await _rpc("surface.create", params)


async def surface_send_text(surface_id: str, text: str) -> dict | None:
    """Send keystrokes to a cmux terminal surface."""
    return await _rpc("surface.send_text", {"surface_id": surface_id, "text": text})


async def surface_read_text(surface_id: str) -> str | None:
    """Read plain-text content of a cmux terminal surface (ANSI stripped)."""
    res = await _rpc("surface.read_text", {"surface_id": surface_id})
    if isinstance(res, str):
        return res
    if isinstance(res, dict):
        return res.get("text") or res.get("content")
    return None


async def close_surface(surface_id: str) -> None:
    await _rpc("surface.close", {"surface_id": surface_id})
