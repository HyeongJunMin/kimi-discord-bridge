"""Shared pytest fixtures.

Importantly: we set env vars that `router.bot` reads at *module import time*
(SESSION_DB_PATH, DISCORD_GUILD_ID) BEFORE any test imports the module.
Otherwise the bot would open a sqlite file in the repo cwd as a side effect.
"""
from __future__ import annotations
import os
import tempfile
from pathlib import Path

# Point the bot's registry at a throwaway file for the whole session.
# Tests that need their own Registry instance create one with a tmp path.
_TMP_DB = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False).name
os.environ.setdefault("SESSION_DB_PATH", _TMP_DB)
# Avoid the optional guild-id env throwing on int() if absent — but the bot
# already guards against this. Nothing to set here.

import pytest


@pytest.fixture
def tmp_sqlite_path(tmp_path: Path) -> str:
    """Return a fresh sqlite path for tests that want their own Registry."""
    return str(tmp_path / "registry.sqlite3")
