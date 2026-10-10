"""Per-session audit folder + session.json metadata.

Layout (one folder per run):
    <audit.directory>/<session_id>/
        events.jsonl     # one redacted, truncated event per line
        session.json     # run metadata, finalized on close (or recovered after a hard kill)

session.json carries end_reason / graceful_close so a reader (and the hard-kill recovery scan,
step 10) can tell a clean close from a crash.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..audit_schema import SCHEMA_VERSION

VALID_END_REASONS = frozenset({
    "completed",
    "signal",
    "agent_crash",
    "hard_kill_recovered",
})


class AuditSession:
    def __init__(
        self,
        root: str | os.PathLike,
        session_id: str,
        *,
        agent_id: str,
        config_id: str,
        acira_version: str,
        started_at_iso: str,
    ) -> None:
        self.root = Path(root)
        self.session_id = session_id
        self.dir = self.root / session_id
        self.agent_id = agent_id
        self.config_id = config_id
        self.acira_version = acira_version
        self.started_at_iso = started_at_iso

    def ensure_dir(self) -> "AuditSession":
        self.dir.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def events_path(self) -> Path:
        return self.dir / "events.jsonl"

    @property
    def session_json_path(self) -> Path:
        return self.dir / "session.json"

    @property
    def zip_path(self) -> Path:
        return self.root / f"{self.session_id}.zip"

    def write_session_json(
        self,
        *,
        ended_at_iso: str | None = None,
        end_reason: str | None = None,
        graceful_close: bool | None = None,
        event_count: int = 0,
    ) -> None:
        if end_reason is not None and end_reason not in VALID_END_REASONS:
            raise ValueError(f"end_reason must be one of {sorted(VALID_END_REASONS)} or None")
        data = {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "config_id": self.config_id,
            "acira_version": self.acira_version,
            "started_at": self.started_at_iso,
            "ended_at": ended_at_iso,
            "end_reason": end_reason,
            "graceful_close": graceful_close,
            "event_count": event_count,
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        # Atomic replace so a crash mid-write never leaves a half-written session.json.
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".session.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.session_json_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def read_session_json(self) -> dict | None:
        try:
            return json.loads(self.session_json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
