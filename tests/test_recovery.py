"""PR2 step 10: hard-kill recovery (orphan scan + bundle finalize)."""

from __future__ import annotations

import gzip
import json

from acira_cdp_proxy.audit.recovery import (
    is_orphan,
    recover_all,
    recover_orphan,
    scan_orphans,
)


def _make_session(root, name, *, events_lines=None, session_meta=None, zipped=False):
    d = root / name
    d.mkdir(parents=True)
    if events_lines is not None:
        (d / "events.jsonl").write_text("".join(events_lines), encoding="utf-8")
    if session_meta is not None:
        (d / "session.json").write_text(json.dumps(session_meta), encoding="utf-8")
    if zipped:
        (root / f"{name}.zip").write_bytes(b"PK\x05\x06")
    return d


# --- orphan detection -------------------------------------------------------

def test_is_orphan_true_for_unfinalized(tmp_path):
    d = _make_session(tmp_path, "s1", events_lines=['{"i":0}\n'])
    assert is_orphan(d)


def test_not_orphan_when_zipped(tmp_path):
    d = _make_session(tmp_path, "s2", events_lines=['{"i":0}\n'], zipped=True)
    assert not is_orphan(d)


def test_not_orphan_when_graceful_close_true(tmp_path):
    d = _make_session(tmp_path, "s3", events_lines=['{"i":0}\n'], session_meta={"graceful_close": True})
    assert not is_orphan(d)


def test_not_orphan_without_events(tmp_path):
    d = tmp_path / "s4"
    d.mkdir()
    (d / "session.json").write_text("{}", encoding="utf-8")
    assert not is_orphan(d)


def test_scan_orphans_filters(tmp_path):
    _make_session(tmp_path, "orphan1", events_lines=['{"i":0}\n'])
    _make_session(tmp_path, "clean", events_lines=['{"i":0}\n'], zipped=True)
    _make_session(tmp_path, "orphan2", events_lines=['{"i":0}\n'], session_meta={"graceful_close": False})
    names = {p.name for p in scan_orphans(tmp_path)}
    assert names == {"orphan1", "orphan2"}


def test_scan_missing_root_is_empty(tmp_path):
    assert scan_orphans(tmp_path / "nope") == []


# --- recovery ---------------------------------------------------------------

def test_recover_drops_truncated_final_line(tmp_path):
    lines = [
        '{"event_id":"e0","x":1}\n',
        '{"event_id":"e1","x":2}\n',
        '{"event_id":"e2","x":3}\n',
        '{"event_id":"e3","x',  # truncated by a hard kill — no newline, invalid JSON
    ]
    d = _make_session(tmp_path, "killed", events_lines=lines,
                      session_meta={"agent_id": "a", "config_id": "c", "acira_version": "0.1.0",
                                    "started_at": "2026-06-23T10:00:00+00:00"})
    result = recover_orphan(d)
    assert result.recovered_events == 3 and result.dropped_lines == 1
    assert result.zip_path.exists()
    # cleaned events were gzipped; the 3 fsynced events survive
    gz = d / "events.jsonl.gz"
    recovered = [json.loads(l) for l in gzip.open(gz, "rt", encoding="utf-8").read().splitlines()]
    assert [e["event_id"] for e in recovered] == ["e0", "e1", "e2"]
    meta = json.loads((d / "session.json").read_text(encoding="utf-8"))
    assert meta["end_reason"] == "hard_kill_recovered" and meta["graceful_close"] is False
    assert meta["event_count"] == 3 and meta["agent_id"] == "a"
    assert not (d / "events.jsonl").exists()  # plain file gzipped away


def test_recover_all_handles_multiple(tmp_path):
    _make_session(tmp_path, "k1", events_lines=['{"event_id":"a"}\n'])
    _make_session(tmp_path, "k2", events_lines=['{"event_id":"b"}\n', '{"event_id":"c"}\n'])
    _make_session(tmp_path, "done", events_lines=['{"event_id":"d"}\n'], zipped=True)
    results = recover_all(tmp_path)
    by_id = {r.session_id: r for r in results}
    assert set(by_id) == {"k1", "k2"}  # the zipped one is skipped
    assert by_id["k2"].recovered_events == 2
    assert (tmp_path / "k1" / "events.jsonl.gz").exists()


def test_recover_with_no_session_json_uses_defaults(tmp_path):
    d = _make_session(tmp_path, "bare", events_lines=['{"event_id":"e0"}\n'])
    result = recover_orphan(d)
    assert result.recovered_events == 1 and result.zip_path.exists()
    meta = json.loads((d / "session.json").read_text(encoding="utf-8"))
    assert meta["end_reason"] == "hard_kill_recovered" and meta["session_id"] == "bare"
