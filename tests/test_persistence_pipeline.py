"""PR2 step 5: session folder layout + AuditPipeline persistent end-to-end."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from acira_cdp_proxy.audit import AuditPipeline, AuditSession
from acira_cdp_proxy.audit_schema import EventValidator
from acira_cdp_proxy.config import AciraGuardConfig

VALIDATOR = EventValidator()


def _decision(metadata):
    return SimpleNamespace(allow=True, reason="r", metadata=metadata)


def _events(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# --- AuditSession -----------------------------------------------------------

def test_session_folder_layout(tmp_path):
    s = AuditSession(tmp_path, "sess1", agent_id="a", config_id="c", acira_version="0.1.0",
                     started_at_iso="2026-06-23T10:00:00+00:00").ensure_dir()
    assert s.dir == tmp_path / "sess1"
    assert s.events_path == tmp_path / "sess1" / "events.jsonl"
    s.write_session_json(ended_at_iso="2026-06-23T10:05:00+00:00", end_reason="completed",
                         graceful_close=True, event_count=3)
    meta = s.read_session_json()
    assert meta["session_id"] == "sess1" and meta["event_count"] == 3
    assert meta["end_reason"] == "completed" and meta["graceful_close"] is True


def test_session_rejects_bad_end_reason(tmp_path):
    s = AuditSession(tmp_path, "s", agent_id="a", config_id="c", acira_version="0.1.0",
                     started_at_iso="2026-06-23T10:00:00+00:00")
    with pytest.raises(ValueError, match="end_reason"):
        s.write_session_json(end_reason="exploded")


# --- pipeline persistent ----------------------------------------------------

def _persistent_cfg(tmp_path, **audit):
    block = {"mode": "persistent", "directory": str(tmp_path / "audits"), "writer_flush_max_events": 2,
             "writer_flush_interval_seconds": 0.05}
    block.update(audit)
    return AciraGuardConfig.from_dict({"enabled": True, "agent_name": "demo", "audit": block})


async def test_persistent_pipeline_writes_jsonl_and_session(tmp_path):
    pipe = AuditPipeline.from_config(_persistent_cfg(tmp_path), config_id="acira.default.json")
    assert pipe.persistent
    await pipe.start()
    for i in range(5):
        pipe.record_decision(method="Input.dispatchKeyEvent",
                             decision=_decision({"decision": "allow", "classification": {"action": "click"}}),
                             latency_ms=1.0)
    await pipe.aclose(end_reason="completed")

    rows = _events(pipe.session.events_path)
    assert len(rows) == 5
    assert all(VALIDATOR.validate(r) == [] for r in rows)
    meta = pipe.session.read_session_json()
    assert meta["event_count"] == 5 and meta["graceful_close"] is True and meta["end_reason"] == "completed"


async def test_volatile_pipeline_has_no_persistence(tmp_path):
    cfg = AciraGuardConfig.from_dict({"enabled": True, "audit": {"mode": "volatile", "directory": str(tmp_path / "audits")}})
    pipe = AuditPipeline.from_config(cfg)
    assert not pipe.persistent
    await pipe.start()  # no-op
    pipe.record_decision(method="m", decision=_decision({"decision": "allow"}), latency_ms=0.1)
    await pipe.aclose()
    assert not (tmp_path / "audits").exists()
    assert pipe.ring_buffer.size == 1  # still buffered in memory


async def test_lazy_writer_start_inside_running_loop(tmp_path):
    # No explicit start(): emitting inside a running loop must auto-start the writer.
    pipe = AuditPipeline.from_config(_persistent_cfg(tmp_path))
    pipe.record_decision(method="m", decision=_decision({"decision": "allow"}), latency_ms=0.1)
    await asyncio.sleep(0.15)  # let the lazily-scheduled writer start + flush
    assert pipe.writer.started
    await pipe.aclose(end_reason="completed")
    assert len(_events(pipe.session.events_path)) == 1


# --- pre-flight integration -------------------------------------------------

class _FixedBackend:
    def __init__(self, answer):
        self.answer = answer

    async def ask(self, request):
        from acira_cdp_proxy.audit.hitl import OperationalHITLResult
        return OperationalHITLResult(request.request_id, self.answer, timed_out=False)


async def test_preflight_healthy_stays_persistent(tmp_path):
    pipe = AuditPipeline.from_config(_persistent_cfg(tmp_path))
    assert await pipe.run_preflight(_FixedBackend("stop")) == "persistent"  # backend unused when healthy
    assert pipe.persistent
    await pipe.aclose(end_reason="completed")


async def test_preflight_continue_downgrades_and_writes_nothing(tmp_path):
    pipe = AuditPipeline.from_config(_persistent_cfg(tmp_path, preflight_min_free_mb=10**12))
    assert pipe.persistent
    assert await pipe.run_preflight(_FixedBackend("continue")) == "volatile"
    assert not pipe.persistent
    pipe.record_decision(method="m", decision=_decision({"decision": "allow"}), latency_ms=0.1)
    await pipe.aclose()
    audits = tmp_path / "audits"
    assert list(audits.rglob("events.jsonl")) == [] if audits.exists() else True
    assert pipe.ring_buffer.size == 1  # still recorded in memory


async def test_preflight_stop_returns_stop(tmp_path):
    pipe = AuditPipeline.from_config(_persistent_cfg(tmp_path, preflight_min_free_mb=10**12))
    assert await pipe.run_preflight(_FixedBackend("stop")) == "stop"


async def test_preflight_volatile_pipeline_is_noop(tmp_path):
    cfg = AciraGuardConfig.from_dict({"enabled": True, "audit": {"mode": "volatile", "directory": str(tmp_path / "a")}})
    pipe = AuditPipeline.from_config(cfg)
    assert await pipe.run_preflight(_FixedBackend("stop")) == "persistent"  # nothing to check
