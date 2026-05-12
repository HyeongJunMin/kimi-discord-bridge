"""WireTail: async file tail + JSON event dispatch."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path

import pytest

from router.wire_tail import WireTail


async def _drain_until(predicate, timeout: float = 2.0, interval: float = 0.05):
    """Poll predicate() until True or timeout. Returns whether it succeeded."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def _wait_briefly():
    """Give the reader/dispatcher loops one tick."""
    await asyncio.sleep(0.5)


async def test_tail_picks_up_newly_appended_lines(tmp_path: Path):
    wire = tmp_path / "wire.jsonl"
    wire.write_text("")  # ensure exists; tail starts at EOF by default
    events: list[dict] = []

    tail = WireTail(wire)
    tail.on_event(lambda ev: _async_append(events, ev))

    await tail.start(from_beginning=False)
    try:
        await _wait_briefly()  # let reader open the file at EOF
        with wire.open("a") as f:
            f.write(json.dumps({"type": "hello"}) + "\n")
            f.flush()
        ok = await _drain_until(lambda: len(events) >= 1, timeout=3.0)
        assert ok, "event was never dispatched"
        assert events[0]["type"] == "hello"
    finally:
        await tail.stop()


async def test_tail_from_beginning_replays_existing_lines(tmp_path: Path):
    wire = tmp_path / "wire.jsonl"
    wire.write_text(json.dumps({"type": "first"}) + "\n"
                    + json.dumps({"type": "second"}) + "\n")
    events: list[dict] = []

    tail = WireTail(wire)
    tail.on_event(lambda ev: _async_append(events, ev))
    await tail.start(from_beginning=True)
    try:
        ok = await _drain_until(lambda: len(events) >= 2, timeout=3.0)
        assert ok, f"expected 2 events, got {len(events)}"
        assert [e["type"] for e in events] == ["first", "second"]
    finally:
        await tail.stop()


async def test_tail_default_starts_at_eof(tmp_path: Path):
    """Pre-existing lines are skipped when from_beginning=False (default)."""
    wire = tmp_path / "wire.jsonl"
    wire.write_text(json.dumps({"type": "old"}) + "\n")
    events: list[dict] = []

    tail = WireTail(wire)
    tail.on_event(lambda ev: _async_append(events, ev))
    await tail.start(from_beginning=False)
    try:
        await _wait_briefly()
        # No new writes — events should remain empty.
        await asyncio.sleep(0.5)
        assert events == []
    finally:
        await tail.stop()


async def test_tail_handles_partial_lines(tmp_path: Path):
    """A line without trailing newline should not be dispatched until completed."""
    wire = tmp_path / "wire.jsonl"
    wire.write_text("")
    events: list[dict] = []

    tail = WireTail(wire)
    tail.on_event(lambda ev: _async_append(events, ev))
    await tail.start(from_beginning=False)
    try:
        await _wait_briefly()
        # Write a line without newline first.
        with wire.open("a") as f:
            f.write(json.dumps({"type": "partial"}))
            f.flush()
        await asyncio.sleep(0.5)
        assert events == [], "partial line should not dispatch yet"
        # Now complete the line.
        with wire.open("a") as f:
            f.write("\n")
            f.flush()
        ok = await _drain_until(lambda: len(events) >= 1, timeout=2.0)
        assert ok
        assert events[0]["type"] == "partial"
    finally:
        await tail.stop()


async def test_tail_skips_malformed_json(tmp_path: Path):
    wire = tmp_path / "wire.jsonl"
    wire.write_text("")
    events: list[dict] = []

    tail = WireTail(wire)
    tail.on_event(lambda ev: _async_append(events, ev))
    await tail.start(from_beginning=False)
    try:
        await _wait_briefly()
        with wire.open("a") as f:
            f.write("not-json\n")
            f.write(json.dumps({"type": "good"}) + "\n")
            f.flush()
        ok = await _drain_until(lambda: len(events) >= 1, timeout=2.0)
        assert ok
        assert len(events) == 1
        assert events[0]["type"] == "good"
    finally:
        await tail.stop()


async def test_stop_is_idempotent(tmp_path: Path):
    wire = tmp_path / "wire.jsonl"
    wire.write_text("")
    tail = WireTail(wire)
    await tail.start(from_beginning=False)
    await tail.stop()
    # Second stop must not raise.
    await tail.stop()


async def _async_append(target: list, ev: dict) -> None:
    target.append(ev)


async def test_tail_reopens_after_truncation(tmp_path: Path):
    """Writer truncates wire.jsonl back to size 0 and writes fresh content
    (rare but kimi-cli has rotated session files this way). Tail should
    reopen and pick up the new content."""
    wire = tmp_path / "wire.jsonl"
    wire.write_text("")
    events: list[dict] = []

    tail = WireTail(wire)
    tail.on_event(lambda ev: _async_append(events, ev))
    await tail.start(from_beginning=False)
    try:
        await _wait_briefly()
        # Append a line.
        with wire.open("a") as f:
            f.write(json.dumps({"type": "before-trunc"}) + "\n")
            f.flush()
        await _drain_until(lambda: len(events) >= 1, timeout=2.0)
        # Truncate the file back to 0 and write fresh content.
        wire.write_text(json.dumps({"type": "after-trunc"}) + "\n")
        ok = await _drain_until(lambda: len(events) >= 2, timeout=3.0)
        assert ok, f"expected 2 events after truncation, got {len(events)}"
        types = [e["type"] for e in events]
        assert "after-trunc" in types
    finally:
        await tail.stop()


async def test_tail_waits_for_file_to_appear(tmp_path: Path):
    """If wire.jsonl doesn't exist when start() is called, tail should wait
    rather than fail. (ensure_tail in production guarantees existence first,
    but the inner reader handles the no-file case defensively.)"""
    wire = tmp_path / "later.jsonl"  # doesn't exist yet
    events: list[dict] = []

    tail = WireTail(wire)
    tail.on_event(lambda ev: _async_append(events, ev))
    await tail.start(from_beginning=False)
    try:
        await asyncio.sleep(0.2)
        # Create file with content while tail is already polling.
        wire.write_text(json.dumps({"type": "appeared"}) + "\n")
        # from_beginning=False but skip_to_eof only applies on FIRST open.
        # When file didn't exist initially, the reader treats next open as
        # a fresh file: skip_to_eof stays True until file exists. So this
        # new line may or may not be replayed depending on timing. The
        # important guarantee: another newly-appended line gets caught.
        await asyncio.sleep(0.5)
        with wire.open("a") as f:
            f.write(json.dumps({"type": "after-open"}) + "\n")
            f.flush()
        ok = await _drain_until(lambda: any(e.get("type") == "after-open"
                                            for e in events), timeout=3.0)
        assert ok, "appended line after file appearance was missed"
    finally:
        await tail.stop()
