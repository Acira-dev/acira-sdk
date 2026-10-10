"""Hard-kill recovery: at startup, finalize sessions left orphaned by SIGKILL / power loss / OOM.

An orphan is a session folder with events.jsonl but no <session_id>.zip and no
graceful_close:true in session.json. Recovery validates the JSONL line by line, drops a
truncated final line (a kill mid-write), rewrites the clean events, marks the session
graceful_close=false / end_reason=hard_kill_recovered, and gzips + zips it into a normal bundle.
Anything that was fsynced to disk survives — only an incomplete trailing line is dropped.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .bundle import finalize_bundle
from .identity import acira_version, utc_now_iso
from .session import AuditSession


@dataclass(frozen=True)
class RecoveryResult:
    session_id: str
    recovered_events: int
    dropped_lines: int
    zip_path: Path | None


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_orphan(session_dir: str | os.PathLike) -> bool:
    session_dir = Path(session_dir)
    if not (session_dir / "events.jsonl").exists():
        return False  # already finalized (gzipped) or never wrote events
    if (session_dir.parent / f"{session_dir.name}.zip").exists():
        return False  # bundle exists → clean close
    meta = _read_json(session_dir / "session.json")
    if meta and meta.get("graceful_close") is True:
        return False
    return True


def scan_orphans(root: str | os.PathLike) -> list[Path]:
    root = Path(root)
    if not root.exists():
        return []
    return [child for child in sorted(root.iterdir()) if child.is_dir() and is_orphan(child)]


def _validate_jsonl(path: Path) -> tuple[list[dict], int]:
    valid: list[dict] = []
    dropped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            valid.append(json.loads(stripped))
        except ValueError:
            dropped += 1  # truncated/corrupt line (typically the final one after a hard kill)
    return valid, dropped


def recover_orphan(session_dir: str | os.PathLike, *, clock: Callable[[], str] = utc_now_iso) -> RecoveryResult:
    session_dir = Path(session_dir)
    events_path = session_dir / "events.jsonl"
    valid, dropped = _validate_jsonl(events_path)

    # Rewrite the cleaned events (drops the truncated trailing line), fsync'd.
    with open(events_path, "w", encoding="utf-8", newline="\n") as handle:
        for event in valid:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    meta = _read_json(session_dir / "session.json") or {}
    session = AuditSession(
        session_dir.parent,
        session_dir.name,
        agent_id=str(meta.get("agent_id") or ""),
        config_id=str(meta.get("config_id") or ""),
        acira_version=str(meta.get("acira_version") or acira_version()),
        started_at_iso=str(meta.get("started_at") or clock()),
    )
    zip_path = finalize_bundle(
        session,
        end_reason="hard_kill_recovered",
        graceful_close=False,
        event_count=len(valid),
        ended_at_iso=clock(),
    )
    return RecoveryResult(session_dir.name, len(valid), dropped, zip_path)


def recover_all(root: str | os.PathLike, *, clock: Callable[[], str] = utc_now_iso) -> list[RecoveryResult]:
    """Scan the audit root at startup and recover every orphaned session."""
    return [recover_orphan(orphan, clock=clock) for orphan in scan_orphans(root)]
