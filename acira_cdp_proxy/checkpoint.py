from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .redaction import redact_value


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class PendingReviewRecord:
    review_id: str
    run_id: str
    action: str
    framework: str
    status: str
    facts: dict[str, Any]
    target: dict[str, Any] | None
    reason: str | None = None
    decision_cache_key: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)


class CheckpointStore(Protocol):
    def record_pending(self, record: PendingReviewRecord) -> None:
        """Persist a pending review or guarded action snapshot."""

    def record_decision(self, review_id: str, *, status: str, reason: str | None, metadata: dict[str, Any] | None) -> None:
        """Persist the outcome for a pending review."""


class InMemoryCheckpointStore:
    def __init__(self) -> None:
        self.records: dict[str, PendingReviewRecord] = {}
        self.decisions: list[dict[str, Any]] = []

    def record_pending(self, record: PendingReviewRecord) -> None:
        self.records[record.review_id] = replace(
            record,
            facts=redact_value(record.facts),
            target=redact_value(record.target),
            reason=redact_value(record.reason),
        )

    def record_decision(self, review_id: str, *, status: str, reason: str | None, metadata: dict[str, Any] | None) -> None:
        self.decisions.append(
            {
                "review_id": review_id,
                "status": status,
                "reason": redact_value(reason),
                "metadata": redact_value(metadata or {}),
                "updated_at": utc_now_iso(),
            }
        )


class JsonlCheckpointStore:
    """Append-only local checkpoint store for durable HITL audit/recovery records."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record_pending(self, record: PendingReviewRecord) -> None:
        self._append({"type": "pending_review", **asdict(record)})

    def record_decision(self, review_id: str, *, status: str, reason: str | None, metadata: dict[str, Any] | None) -> None:
        self._append(
            {
                "type": "review_decision",
                "review_id": review_id,
                "status": status,
                "reason": reason,
                "metadata": metadata or {},
                "updated_at": utc_now_iso(),
            }
        )

    def _append(self, item: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(redact_value(item), separators=(",", ":"), ensure_ascii=True))
            handle.write("\n")
