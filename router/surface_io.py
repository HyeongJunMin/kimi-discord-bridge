"""PoC: cmux surface I/O wrappers (send_text, read_text)."""
from __future__ import annotations
import asyncio, json, os, sys
from pathlib import Path

# Re-use existing thin wrapper
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "router"))
from cmux_client import _rpc, CmuxError  # noqa: E402


async def surface_send_text(surface_id: str, text: str) -> dict | None:
    """Send keystrokes to a cmux terminal surface."""
    return await _rpc("surface.send_text", {"surface_id": surface_id, "text": text})


async def surface_read_text(surface_id: str) -> str | None:
    """Read plain-text content of a cmux terminal surface (ANSI stripped)."""
    res = await _rpc("surface.read_text", {"surface_id": surface_id})
    # cmux may return either a plain string or a dict with a text field
    if isinstance(res, str):
        return res
    if isinstance(res, dict):
        return res.get("text") or res.get("content")
    return None


async def create_surface(*, workspace_id: str, command: str | None = None,
                         focus: bool = False) -> dict:
    """Create a terminal surface in a workspace."""
    params = {"workspace_id": workspace_id, "type": "terminal", "focus": focus}
    if command:
        params["command"] = command
    return await _rpc("surface.create", params)


async def create_workspace(*, name: str | None = None, cwd: str | None = None) -> dict:
    params: dict = {}
    if name:
        params["name"] = name
    if cwd:
        params["cwd"] = cwd
    return await _rpc("workspace.create", params)


async def close_surface(surface_id: str) -> None:
    await _rpc("surface.close", {"surface_id": surface_id})
