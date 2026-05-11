"""Backwards-compat shim — surface I/O lives in cmux_client now.

Production code should import directly from `cmux_client`. This file
exists only so the `poc/*.py` scripts (which add `router/` to sys.path
and do `from surface_io import ...`) keep working without modification.
"""
from __future__ import annotations

# Support both `router.surface_io` (package import) and direct
# `surface_io` (poc/ scripts that put router/ on sys.path).
try:
    from .cmux_client import (  # type: ignore
        create_workspace,
        create_surface,
        surface_send_text,
        surface_read_text,
        close_surface,
        CmuxError,
    )
except ImportError:
    from cmux_client import (  # type: ignore  # noqa: F401
        create_workspace,
        create_surface,
        surface_send_text,
        surface_read_text,
        close_surface,
        CmuxError,
    )
