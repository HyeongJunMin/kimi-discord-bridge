"""Registry: sqlite-backed session table CRUD."""
from __future__ import annotations
import time

import pytest

from router.registry import Registry, SessionRow


def _row(thread_id: str = "t1", status: str = "active", **overrides) -> SessionRow:
    base = dict(
        thread_id=thread_id,
        guild_id="g1",
        channel_id="c1",
        owner_user_id="u1",
        workspace_id="ws1",
        workspace_name="ws1-name",
        cwd="/tmp/work",
        monitor_surface_id="surf-1",
        acp_session_id="sess-1",
        status=status,
        created_at=int(time.time()),
        last_active_at=int(time.time()),
    )
    base.update(overrides)
    return SessionRow(**base)


def test_insert_and_get_roundtrip(tmp_sqlite_path):
    reg = Registry(tmp_sqlite_path)
    row = _row()
    reg.insert(row)

    got = reg.get_by_thread("t1")
    assert got is not None
    assert got.thread_id == "t1"
    assert got.owner_user_id == "u1"
    assert got.status == "active"


def test_get_by_thread_missing_returns_none(tmp_sqlite_path):
    reg = Registry(tmp_sqlite_path)
    assert reg.get_by_thread("does-not-exist") is None


def test_insert_or_replace_overwrites(tmp_sqlite_path):
    """Re-inserting the same thread_id replaces (no UNIQUE violation)."""
    reg = Registry(tmp_sqlite_path)
    reg.insert(_row(owner_user_id="u1", status="dead"))
    reg.insert(_row(owner_user_id="u2", status="active"))
    got = reg.get_by_thread("t1")
    assert got.owner_user_id == "u2"
    assert got.status == "active"


def test_list_active_filters_status(tmp_sqlite_path):
    reg = Registry(tmp_sqlite_path)
    reg.insert(_row(thread_id="t-active", status="active"))
    reg.insert(_row(thread_id="t-dead", status="dead"))
    reg.insert(_row(thread_id="t-starting", status="starting"))

    actives = reg.list_active()
    ids = {r.thread_id for r in actives}
    assert ids == {"t-active"}


def test_update_status_changes_row_and_touches_last_active(tmp_sqlite_path):
    reg = Registry(tmp_sqlite_path)
    reg.insert(_row(thread_id="t1", status="active", last_active_at=0))

    reg.update_status("t1", "dead")
    got = reg.get_by_thread("t1")
    assert got.status == "dead"
    assert got.last_active_at > 0


def test_update_status_on_missing_thread_is_noop(tmp_sqlite_path):
    """UPDATE on a non-existent thread_id affects 0 rows but doesn't raise."""
    reg = Registry(tmp_sqlite_path)
    # Should not raise.
    reg.update_status("never-existed", "dead")
    assert reg.get_by_thread("never-existed") is None


def test_touch_updates_only_last_active(tmp_sqlite_path):
    reg = Registry(tmp_sqlite_path)
    reg.insert(_row(thread_id="t1", status="active", last_active_at=0))

    reg.touch("t1")
    got = reg.get_by_thread("t1")
    assert got.last_active_at > 0
    assert got.status == "active"  # untouched


def test_set_acp_session_sets_id_and_marks_active(tmp_sqlite_path):
    reg = Registry(tmp_sqlite_path)
    reg.insert(_row(thread_id="t1", status="starting", acp_session_id=None))

    reg.set_acp_session("t1", "new-acp-uuid")
    got = reg.get_by_thread("t1")
    assert got.acp_session_id == "new-acp-uuid"
    assert got.status == "active"


def test_persistence_across_reopens(tmp_sqlite_path):
    """A new Registry on the same path should see prior writes."""
    Registry(tmp_sqlite_path).insert(_row(thread_id="t-persist"))
    got = Registry(tmp_sqlite_path).get_by_thread("t-persist")
    assert got is not None


def test_enqueue_and_list_pending_messages(tmp_sqlite_path):
    reg = Registry(tmp_sqlite_path)

    msg_id = reg.enqueue_message(
        thread_id="t1", author_user_id="u1", content="hello")

    pending = reg.list_pending_messages(limit=10)
    assert [m.id for m in pending] == [msg_id]
    assert pending[0].content == "hello"
    assert pending[0].status == "pending"


def test_mark_message_delivered_removes_from_pending(tmp_sqlite_path):
    reg = Registry(tmp_sqlite_path)
    msg_id = reg.enqueue_message(
        thread_id="t1", author_user_id="u1", content="hello")

    reg.mark_message_delivered(msg_id)

    assert reg.list_pending_messages(limit=10) == []
    assert reg.count_pending_messages("t1") == 0


def test_mark_message_failed_keeps_transient_failures_pending(tmp_sqlite_path):
    reg = Registry(tmp_sqlite_path)
    msg_id = reg.enqueue_message(
        thread_id="t1", author_user_id="u1", content="hello")

    reg.mark_message_failed(msg_id, "cmux timeout")

    assert reg.count_pending_messages("t1") == 1
    assert reg.last_delivery_error("t1") == "cmux timeout"
    # Backoff hides the row until next_attempt_at.
    assert reg.list_pending_messages(limit=10) == []


def test_fail_pending_for_thread_terminally_fails_messages(tmp_sqlite_path):
    reg = Registry(tmp_sqlite_path)
    reg.enqueue_message(thread_id="t1", author_user_id="u1", content="a")
    reg.enqueue_message(thread_id="t2", author_user_id="u1", content="b")

    reg.fail_pending_for_thread("t1", "session ended")

    assert reg.count_pending_messages("t1") == 0
    assert reg.count_pending_messages("t2") == 1
    assert reg.last_delivery_error("t1") == "session ended"
