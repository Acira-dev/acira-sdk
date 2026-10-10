"""PR2 step 5: background JSONL persistence writer — batching, fsync, drain, bounds."""

from __future__ import annotations

import asyncio
import json

import pytest

from acira_cdp_proxy.audit.writer import PersistenceQueueFull, PersistenceWriter


def _lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def test_writes_all_events_in_order(tmp_path):
    path = tmp_path / "s" / "events.jsonl"
    w = PersistenceWriter(path, flush_max_events=2, flush_interval_seconds=0.05)
    await w.start()
    for i in range(5):
        w.enqueue({"event_id": f"e{i}", "i": i})
    await w.aclose()
    rows = _lines(path)
    assert [r["i"] for r in rows] == [0, 1, 2, 3, 4]


async def test_flush_on_max_events(tmp_path):
    path = tmp_path / "s" / "events.jsonl"
    w = PersistenceWriter(path, flush_max_events=3, flush_interval_seconds=100)
    await w.start()
    for i in range(3):
        w.enqueue({"i": i})
    await asyncio.sleep(0.1)  # background flush by max-events, well before the 100s interval
    assert len(_lines(path)) == 3
    assert w.flushed_count == 3 and w.fsync_count >= 1
    await w.aclose()


async def test_flush_on_interval(tmp_path):
    path = tmp_path / "s" / "events.jsonl"
    w = PersistenceWriter(path, flush_max_events=1000, flush_interval_seconds=0.05)
    await w.start()
    w.enqueue({"i": 0})
    w.enqueue({"i": 1})
    await asyncio.sleep(0.2)  # interval flush even though max not reached
    assert len(_lines(path)) == 2
    await w.aclose()


async def test_fsync_invoked(tmp_path, monkeypatch):
    calls = {"n": 0}
    real = __import__("os").fsync

    def counting(fd):
        calls["n"] += 1
        return real(fd)

    monkeypatch.setattr("acira_cdp_proxy.audit.writer.os.fsync", counting)
    path = tmp_path / "s" / "events.jsonl"
    w = PersistenceWriter(path, flush_max_events=2, flush_interval_seconds=0.05)
    await w.start()
    for i in range(4):
        w.enqueue({"i": i})
    await w.aclose()
    assert calls["n"] >= 1


async def test_enqueue_before_start_is_buffered(tmp_path):
    path = tmp_path / "s" / "events.jsonl"
    w = PersistenceWriter(path, flush_max_events=10, flush_interval_seconds=0.05)
    w.enqueue({"i": "pre1"})
    w.enqueue({"i": "pre2"})
    await w.start()
    w.enqueue({"i": "post"})
    await w.aclose()
    assert [r["i"] for r in _lines(path)] == ["pre1", "pre2", "post"]


async def test_aclose_without_start_flushes_prestart(tmp_path):
    path = tmp_path / "s" / "events.jsonl"
    w = PersistenceWriter(path, flush_max_events=10, flush_interval_seconds=0.05)
    w.enqueue({"i": 0})
    w.enqueue({"i": 1})
    await w.aclose()  # never started; must still persist
    assert len(_lines(path)) == 2


def test_bounded_prestart_buffer_raises():
    w = PersistenceWriter("unused.jsonl", queue_maxsize=2)
    w.enqueue({"i": 0})
    w.enqueue({"i": 1})
    with pytest.raises(PersistenceQueueFull):
        w.enqueue({"i": 2})


async def test_enqueue_after_close_is_dropped_silently(tmp_path):
    path = tmp_path / "s" / "events.jsonl"
    w = PersistenceWriter(path)
    await w.start()
    await w.aclose()
    w.enqueue({"i": "late"})  # no raise, no write
    assert len(_lines(path)) == 0
