"""PR2 step 7: overflow buffer + mid-run persistence-failure coordinator."""

from __future__ import annotations

import asyncio
import json

import pytest

from acira_cdp_proxy.audit.failure import PersistenceFailureCoordinator
from acira_cdp_proxy.audit.hitl import OperationalHITLResult
from acira_cdp_proxy.audit.overflow import OverflowBuffer
from acira_cdp_proxy.audit.writer import PersistenceWriter


# --- overflow buffer --------------------------------------------------------

def test_overflow_buffer_bounds_and_levels():
    b = OverflowBuffer(4)
    assert b.add({"i": 0}) and b.level == "normal"
    b.add({"i": 1})
    assert b.level == "warning"  # 2/4 = 50%
    b.add({"i": 2})
    assert b.add({"i": 3}) and b.level == "critical" and b.is_full
    assert b.add({"i": 4}) is False  # full → never silently drops
    assert b.size == 4


def test_overflow_buffer_drain_and_clear():
    b = OverflowBuffer(10)
    for i in range(3):
        b.add({"i": i})
    assert b.drain() == [{"i": 0}, {"i": 1}, {"i": 2}] and b.size == 0


# --- coordinator backend stub ----------------------------------------------

class SeqBackend:
    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    async def ask(self, request):
        self.asked.append(request)
        return OperationalHITLResult(request.request_id, self.answers.pop(0), timed_out=False)


def _coord(backend, **over):
    opts = dict(overflow_cap=2, backend=backend, retry_disk=lambda: True,
               flush_to_disk=lambda evs: True)
    opts.update(over)
    return PersistenceFailureCoordinator(**opts)


async def test_single_hitl_for_multiple_failures():
    backend = SeqBackend(["retry"])
    flushed = []
    coord = _coord(backend, overflow_cap=10, flush_to_disk=lambda evs: (flushed.extend(evs) or True))
    await coord.record_failure({"i": 0}, "disk full")
    await coord.record_failure({"i": 1}, "disk full")
    await coord.record_failure({"i": 2}, "disk full")
    await coord.wait()
    assert len(backend.asked) == 1  # one HITL, not three
    assert coord.resolved and flushed == [{"i": 0}, {"i": 1}, {"i": 2}]


async def test_retry_success_flushes_in_order_and_resumes():
    paused, resumed = [], []
    flushed = []
    backend = SeqBackend(["retry"])
    coord = _coord(backend, overflow_cap=2, flush_to_disk=lambda evs: (flushed.extend(evs) or True),
                   on_pause=lambda: paused.append(1), on_resume=lambda: resumed.append(1))
    await coord.record_failure({"i": 0}, "io error")
    await coord.record_failure({"i": 1}, "io error")  # 2/2 → full → pause
    await coord.wait()
    assert paused == [1] and resumed == [1] and coord.paused is False
    assert flushed == [{"i": 0}, {"i": 1}] and coord.buffer.size == 0


async def test_retry_failure_then_success_reopens_hitl():
    attempts = {"n": 0}

    def retry_disk():
        attempts["n"] += 1
        return attempts["n"] >= 2  # first retry fails, second succeeds

    backend = SeqBackend(["retry", "retry"])
    coord = _coord(backend, retry_disk=retry_disk)
    await coord.record_failure({"i": 0}, "io")
    await coord.wait()
    assert coord.resolved and len(backend.asked) == 2


async def test_drop_with_confirmation_clears_and_downgrades():
    downgrades = []
    backend = SeqBackend(["drop", "confirm"])
    coord = _coord(backend, on_downgrade=lambda reasons: downgrades.append(reasons))
    await coord.record_failure({"i": 0}, "io")
    await coord.wait()
    assert coord.resolved and coord.buffer.size == 0 and len(downgrades) == 1


async def test_drop_cancel_reopens_then_retry():
    backend = SeqBackend(["drop", "cancel", "retry"])
    coord = _coord(backend)
    await coord.record_failure({"i": 0}, "io")
    await coord.wait()
    assert coord.resolved and len(backend.asked) == 3  # drop, confirm-cancel, retry


async def test_export_writes_buffer_to_file(tmp_path):
    export = tmp_path / "export" / "overflow.jsonl"
    backend = SeqBackend(["export"])
    coord = _coord(backend, export_path=export)
    await coord.record_failure({"i": 0}, "io")
    await coord.record_failure({"i": 1}, "io")
    await coord.wait()
    rows = [json.loads(l) for l in export.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert rows == [{"i": 0}, {"i": 1}] and coord.resolved


async def test_status_reports_escalation():
    backend = SeqBackend(["retry"])
    coord = _coord(backend, overflow_cap=4, retry_disk=lambda: False)  # stays failing
    await coord.record_failure({"i": 0}, "io")
    await coord.record_failure({"i": 1}, "io")  # 2/4 = warning
    status = coord.status()
    assert status["buffered"] == 2 and status["level"] == "warning" and status["active"] is True
    coord.active = False  # stop the reopen loop for clean teardown
    await asyncio.sleep(0)


# --- writer failure reporting ----------------------------------------------

async def test_writer_reports_flush_error_without_crashing(tmp_path, monkeypatch):
    captured = []

    def raising_fsync(_fd):
        raise OSError("simulated disk failure")

    monkeypatch.setattr("acira_cdp_proxy.audit.writer.os.fsync", raising_fsync)
    w = PersistenceWriter(
        tmp_path / "s" / "events.jsonl",
        flush_max_events=2,
        flush_interval_seconds=0.05,
        on_flush_error=lambda batch, exc: captured.append((batch, exc)),
    )
    await w.start()
    w.enqueue({"i": 0})
    w.enqueue({"i": 1})
    await asyncio.sleep(0.1)
    assert captured and len(captured[0][0]) == 2
    assert isinstance(captured[0][1], OSError)
    assert w.flush_errors >= 1
    await w.aclose()  # must not raise despite the failing fsync
