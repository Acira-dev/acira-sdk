"""PR2 step 8: session bundle (gzip + zip) + agent-crash handling."""

from __future__ import annotations

import gzip
import json
import zipfile
from types import SimpleNamespace

from acira_cdp_proxy.audit import AuditPipeline
from acira_cdp_proxy.audit.bundle import finalize_bundle, gzip_events, zip_session_folder
from acira_cdp_proxy.audit.hitl import OperationalHITLResult
from acira_cdp_proxy.audit.lifecycle import handle_agent_crash
from acira_cdp_proxy.audit.session import AuditSession
from acira_cdp_proxy.config import AciraGuardConfig


def _decision(metadata):
    return SimpleNamespace(allow=True, reason="r", metadata=metadata)


def _persistent_cfg(tmp_path, **over):
    block = {"mode": "persistent", "directory": str(tmp_path / "audits"),
             "writer_flush_max_events": 1, "writer_flush_interval_seconds": 0.05}
    block.update(over)
    return AciraGuardConfig.from_dict({"enabled": True, "agent_name": "demo", "audit": block})


class AckBackend:
    def __init__(self):
        self.asked = []

    async def ask(self, request):
        self.asked.append(request)
        return OperationalHITLResult(request.request_id, request.default_option, timed_out=False)


# --- bundle primitives ------------------------------------------------------

def test_gzip_events_replaces_plain_file(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text('{"i":0}\n{"i":1}\n', encoding="utf-8")
    gz = gzip_events(events)
    assert gz.name == "events.jsonl.gz"
    assert not events.exists()
    assert gzip.open(gz, "rt", encoding="utf-8").read() == '{"i":0}\n{"i":1}\n'


def test_gzip_events_missing_is_none(tmp_path):
    assert gzip_events(tmp_path / "nope.jsonl") is None


def test_zip_session_folder_contains_files(tmp_path):
    sdir = tmp_path / "sess1"
    sdir.mkdir()
    (sdir / "session.json").write_text("{}", encoding="utf-8")
    (sdir / "events.jsonl.gz").write_bytes(b"gzdata")
    zip_path = zip_session_folder(sdir, tmp_path / "sess1.zip")
    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
    assert names == {"sess1/session.json", "sess1/events.jsonl.gz"}


def test_finalize_bundle_writes_json_gzips_and_zips(tmp_path):
    session = AuditSession(tmp_path, "s", agent_id="a", config_id="c", acira_version="0.1.0",
                           started_at_iso="2026-06-23T10:00:00+00:00")
    session.dir.mkdir(parents=True)
    session.events_path.write_text('{"event_id":"e0"}\n', encoding="utf-8")
    zip_path = finalize_bundle(session, end_reason="completed", graceful_close=True, event_count=1)
    assert zip_path.exists()
    assert (session.dir / "events.jsonl.gz").exists() and not session.events_path.exists()
    assert session.read_session_json()["end_reason"] == "completed"


# --- agent crash ------------------------------------------------------------

async def test_agent_crash_drains_acks_bundles_and_exits(tmp_path):
    pipe = AuditPipeline.from_config(_persistent_cfg(tmp_path))
    await pipe.start()
    for i in range(3):
        pipe.record_decision(method="m", decision=_decision({"decision": "allow"}), latency_ms=0.1)
    session = pipe.session
    backend = AckBackend()
    exits = []

    zip_path = await handle_agent_crash(pipe, cause="Target.targetCrashed", backend=backend,
                                        exit_func=exits.append)

    assert zip_path.exists()
    assert (session.dir / "events.jsonl.gz").exists()
    meta = session.read_session_json()
    assert meta["end_reason"] == "agent_crash" and meta["graceful_close"] is True and meta["event_count"] == 3
    assert len(backend.asked) == 1 and backend.asked[0].kind == "agent_crash"
    assert exits == [1]  # non-zero exit


async def test_agent_crash_volatile_still_acks_and_exits(tmp_path):
    cfg = AciraGuardConfig.from_dict({"enabled": True, "audit": {"mode": "volatile", "directory": str(tmp_path / "a")}})
    pipe = AuditPipeline.from_config(cfg)
    pipe.record_decision(method="m", decision=_decision({"decision": "allow"}), latency_ms=0.1)
    backend = AckBackend()
    exits = []
    zip_path = await handle_agent_crash(pipe, cause="disconnect", backend=backend, exit_func=exits.append)
    assert zip_path is None  # nothing persisted in volatile mode
    assert len(backend.asked) == 1 and exits == [1]
