"""Discord → kimi image attachment relay."""
from __future__ import annotations
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from router import bot


# ── Pure helpers ─────────────────────────────────────────────────────────

def test_is_allowed_image_accepts_common_extensions():
    for name in ["a.png", "b.JPG", "c.jpeg", "d.webp", "e.gif"]:
        assert bot._is_allowed_image(name), name


def test_is_allowed_image_rejects_non_images():
    for name in ["a.exe", "b.sh", "c.py", "d.pdf", "e.zip", "f.txt", ""]:
        assert not bot._is_allowed_image(name), name


def test_sanitize_filename_strips_path_separators():
    assert bot._sanitize_filename("../../etc/passwd.png") == "passwd.png"
    assert bot._sanitize_filename("/abs/photo.jpg") == "photo.jpg"
    assert bot._sanitize_filename("a\\b\\c.png") == "c.png"


def test_sanitize_filename_strips_null_and_controls():
    assert bot._sanitize_filename("ph\x00oto.png") == "photo.png"
    assert bot._sanitize_filename("a\nb\tc.png") == "abc.png"


def test_sanitize_filename_strips_leading_dots():
    assert bot._sanitize_filename(".env.png") == "env.png"
    assert bot._sanitize_filename("...hidden.png") == "hidden.png"


def test_sanitize_filename_replaces_unsafe_chars():
    out = bot._sanitize_filename("my photo $name.png")
    assert " " not in out
    assert "$" not in out
    assert out.endswith(".png")


def test_sanitize_filename_fallback_for_empty():
    assert bot._sanitize_filename("") == "file"
    assert bot._sanitize_filename("///") == "file"


def test_sanitize_filename_caps_length():
    out = bot._sanitize_filename("a" * 500 + ".png")
    assert len(out) <= 80


# ── Message composition ──────────────────────────────────────────────────

def test_compose_message_inlines_at_path_per_image():
    out = bot._compose_message_with_attachments(
        "이거 봐줘", [Path("/tmp/a.png"), Path("/tmp/b.jpg")])
    lines = out.splitlines()
    assert lines[0] == "@/tmp/a.png"
    assert lines[1] == "@/tmp/b.jpg"
    assert lines[2] == "이거 봐줘"


def test_compose_message_handles_image_only_no_text():
    out = bot._compose_message_with_attachments("", [Path("/tmp/a.png")])
    assert out == "@/tmp/a.png"


def test_compose_message_text_only_no_attachments():
    out = bot._compose_message_with_attachments("just text", [])
    assert out == "just text"


# ── _save_attachment ─────────────────────────────────────────────────────

def _mock_attachment(filename: str, size: int, data: bytes) -> MagicMock:
    a = MagicMock()
    a.filename = filename
    a.size = size
    a.read = AsyncMock(return_value=data)
    return a


async def test_save_attachment_happy_path(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "UPLOADS_ROOT", tmp_path / "kimi-uploads")
    payload = b"\x89PNG\r\n\x1a\n" + b"x" * 100
    att = _mock_attachment("photo.png", len(payload), payload)
    path, reason = await bot._save_attachment(12345, att)
    assert reason is None
    assert path is not None
    assert path.exists()
    assert path.read_bytes() == payload
    assert path.parent.name == "12345"
    assert path.name.endswith("-photo.png")


async def test_save_attachment_rejects_non_image(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "UPLOADS_ROOT", tmp_path / "kimi-uploads")
    att = _mock_attachment("malware.exe", 100, b"x" * 100)
    path, reason = await bot._save_attachment(1, att)
    assert path is None
    assert reason and "이미지만" in reason
    att.read.assert_not_awaited()  # never downloaded


async def test_save_attachment_rejects_oversize(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "UPLOADS_ROOT", tmp_path / "kimi-uploads")
    monkeypatch.setattr(bot, "MAX_UPLOAD_BYTES", 1000)
    att = _mock_attachment("big.png", 5000, b"x" * 5000)
    path, reason = await bot._save_attachment(1, att)
    assert path is None
    assert reason and "한도 초과" in reason
    att.read.assert_not_awaited()


async def test_save_attachment_caps_on_actual_bytes_when_size_lies(monkeypatch, tmp_path):
    """attachment.size from Discord can be unreliable — verify actual byte length too."""
    monkeypatch.setattr(bot, "UPLOADS_ROOT", tmp_path / "kimi-uploads")
    monkeypatch.setattr(bot, "MAX_UPLOAD_BYTES", 1000)
    # Lie about size in metadata, but deliver oversize bytes.
    att = _mock_attachment("sneaky.png", 500, b"x" * 5000)
    path, reason = await bot._save_attachment(1, att)
    assert path is None
    assert reason and "한도 초과" in reason


async def test_save_attachment_sanitizes_destination_filename(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "UPLOADS_ROOT", tmp_path / "kimi-uploads")
    payload = b"data"
    att = _mock_attachment("../../etc/passwd.png", len(payload), payload)
    path, reason = await bot._save_attachment(7, att)
    assert reason is None
    # Saved name keeps the basename only — no traversal segments.
    assert ".." not in str(path)
    assert path.name.endswith("-passwd.png")
    assert path.parent == tmp_path / "kimi-uploads" / "7"


async def test_save_attachment_propagates_download_error(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "UPLOADS_ROOT", tmp_path / "kimi-uploads")
    att = _mock_attachment("photo.png", 100, b"x")
    att.read = AsyncMock(side_effect=RuntimeError("CDN 5xx"))
    path, reason = await bot._save_attachment(1, att)
    assert path is None
    assert reason and "다운로드 실패" in reason


# ── shutdown_session cleans up upload dir ────────────────────────────────

async def test_delete_upload_dir_removes_existing(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "UPLOADS_ROOT", tmp_path / "uploads")
    target = tmp_path / "uploads" / "999"
    target.mkdir(parents=True)
    (target / "a.png").write_bytes(b"x")

    bot._delete_upload_dir(999)
    assert not target.exists()


def test_delete_upload_dir_silent_on_missing(monkeypatch, tmp_path):
    """Calling on a thread that never uploaded anything is a no-op (not an error)."""
    monkeypatch.setattr(bot, "UPLOADS_ROOT", tmp_path / "uploads")
    # Should not raise.
    bot._delete_upload_dir(404)


async def test_shutdown_session_invokes_upload_cleanup(monkeypatch, tmp_path):
    """Router.shutdown_session must wipe the per-thread uploads dir."""
    monkeypatch.setattr(bot, "UPLOADS_ROOT", tmp_path / "uploads")
    target = tmp_path / "uploads" / "55"
    target.mkdir(parents=True)
    (target / "a.png").write_bytes(b"x")

    # We don't want to touch the real registry/sessions singleton; patch the
    # registry update to a noop and ensure the dir-deletion still fires.
    with patch.object(bot.router.registry, "update_status"):
        await bot.router.shutdown_session(55)

    assert not target.exists()
