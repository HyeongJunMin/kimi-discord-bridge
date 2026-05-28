"""Shared pytest fixtures.

⚠️  RUNNING TESTS — READ THIS (applies to Claude Code AND kimi):
    Plain `pytest` is safe: it excludes `@pytest.mark.slow` tests and caps each
    test at 60s (see pytest.ini). Do NOT override `-m "not slow"` to run the
    whole suite at once — the slow tests spawn real OS processes and the
    combined run has SIGKILLed the sandbox (exit 137), killing the session.
    To exercise slow tests, run ONE FILE at a time:
        pytest -m slow tests/test_lock_restore_flow.py
    Or cover everything file-by-file via:  bash tests/run-safe.sh

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
