from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections import deque
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from acira_cdp_proxy.checkpoint import CheckpointStore, PendingReviewRecord
from acira_cdp_proxy.redaction import redact_value


PRODUCTION_DIR = Path(__file__).resolve().parents[1]
# Kept only so existing imports/signatures resolve; the dashboard never writes here.
DEFAULT_EVENT_FILE = PRODUCTION_DIR / "local-runtime" / "dashboard" / "events.jsonl"
# Ephemeral in-memory event log cap (ring buffer; oldest evicted first).
MAX_EVENTS = 2000
EVENT_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "card",
    "card_number",
    "code",
    "credential",
    "cvv",
    "cvc",
    "otp",
    "pass",
    "password",
    "plain_text",
    "plaintext",
    "secret",
    "secret_value",
    "token",
    "value",
    "values",
}


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class DashboardEventStore:
    """Append-only redacted event log for the local dashboard.

    Ephemeral: events live in a bounded in-memory ring buffer (``MAX_EVENTS``)
    and are gone when the process exits. Nothing is written to disk. The file
    "offset" exposed by the streaming API is replaced by a monotonic sequence
    index so existing handlers keep working unchanged.
    """

    def __init__(self, path: str | Path = DEFAULT_EVENT_FILE) -> None:
        self.path = Path(path)  # retained for signature/back-compat; never written
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._seq = 0
        self._revision = 0
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    @property
    def revision(self) -> int:
        return self._revision

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        item = _dashboard_event_redact(
            _compact_dashboard_event(
                {
                    "event_id": str(event.get("event_id") or f"evt-{uuid.uuid4().hex}"),
                    "created_at": str(event.get("created_at") or utc_now_iso()),
                    **event,
                }
            )
        )
        self._seq += 1
        stored = dict(item)
        stored["_seq"] = self._seq
        self._events.append(stored)
        self._revision += 1
        self._notify_waiters()
        return item

    def read_recent(self, *, limit: int = 100) -> list[dict[str, Any]]:
        events, _, _ = self.read_recent_with_position(limit=limit)
        return events

    def read_recent_with_position(self, *, limit: int = 100) -> tuple[list[dict[str, Any]], int, int]:
        limit = _bounded_limit(limit)
        recent = list(self._events)[-limit:]
        return [_strip_seq(event) for event in recent], self._seq, self._revision

    def read_since_offset(self, offset: int) -> tuple[list[dict[str, Any]], int]:
        safe_offset = max(0, int(offset or 0))
        if safe_offset > self._seq:
            safe_offset = 0
        events = [_strip_seq(event) for event in self._events if int(event.get("_seq", 0)) > safe_offset]
        return events, self._seq

    def current_offset(self) -> int:
        return self._seq

    async def wait_for_change_after(self, revision: int, *, timeout: float | None = None) -> None:
        if self._revision != revision:
            return
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        waiter = (loop, event)
        self._waiters.add(waiter)
        try:
            if self._revision != revision:
                return
            if timeout is None:
                await event.wait()
            else:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(event.wait(), timeout=max(0.0, timeout))
        finally:
            self._waiters.discard(waiter)

    def summary(self) -> dict[str, Any]:
        events = self.read_recent(limit=100)
        pending: dict[str, dict[str, Any]] = {}
        runs: set[str] = set()
        paused: set[str] = set()
        for event in events:
            run_id = str(event.get("run_id") or "")
            if run_id:
                runs.add(run_id)
            event_type = str(event.get("type") or "")
            review_id = str(event.get("review_id") or "")
            if event_type in {"pending_review", "hitl_review_pending"} and review_id:
                pending[review_id] = event
            elif event_type in {"review_decision", "hitl_review_decision", "hitl_review_expired"} and review_id:
                pending.pop(review_id, None)
            if event_type == "session_paused" and run_id:
                paused.add(run_id)
            elif event_type in {"session_resumed", "session_ended"} and run_id:
                paused.discard(run_id)
        return {
            "event_count": len(events),
            "active_review_count": len(pending),
            "run_count": len(runs),
            "paused_session_count": len(paused),
            "latest_events": events[-20:],
        }

    def _read_all(self) -> list[dict[str, Any]]:
        return [_strip_seq(event) for event in self._events]

    def _notify_waiters(self) -> None:
        for loop, event in tuple(self._waiters):
            if loop.is_closed():
                self._waiters.discard((loop, event))
                continue
            loop.call_soon_threadsafe(event.set)


class DashboardCheckpointStore:
    """CheckpointStore adapter that mirrors Acira review events to the dashboard."""

    def __init__(
        self,
        event_store: DashboardEventStore | None = None,
        *,
        inner: CheckpointStore | None = None,
    ) -> None:
        self.event_store = event_store or DashboardEventStore()
        self.inner = inner

    @classmethod
    def default(cls) -> "DashboardCheckpointStore":
        return cls(DashboardEventStore(DEFAULT_EVENT_FILE))

    def record_pending(self, record: PendingReviewRecord) -> None:
        if self.inner is not None:
            self.inner.record_pending(record)
        data = redact_value(asdict(record))
        self.event_store.append(
            {
                "type": "pending_review",
                **data,
                "summary": _pending_summary(data),
            }
        )

    def record_decision(self, review_id: str, *, status: str, reason: str | None, metadata: dict[str, Any] | None) -> None:
        if self.inner is not None:
            self.inner.record_decision(review_id, status=status, reason=reason, metadata=metadata)
        self.event_store.append(
            {
                "type": "review_decision",
                "review_id": review_id,
                "status": status,
                "reason": redact_value(reason),
                "metadata": redact_value(metadata or {}),
                "updated_at": utc_now_iso(),
                "summary": _decision_summary(status, reason),
            }
        )


def _pending_summary(record: dict[str, Any]) -> str:
    action = str(record.get("action") or "action").replace("_", " ")
    framework = str(record.get("framework") or "agent")
    reason = str(record.get("reason") or "").strip()
    if reason:
        return f"{framework} requested {action}: {reason}"
    return f"{framework} requested {action}"


def _decision_summary(status: str, reason: str | None) -> str:
    clean_status = str(status or "decision").replace("_", " ")
    clean_reason = str(reason or "").strip()
    if clean_reason:
        return f"{clean_status}: {clean_reason}"
    return clean_status


def _strip_seq(event: dict[str, Any]) -> dict[str, Any]:
    if "_seq" not in event:
        return event
    clean = dict(event)
    clean.pop("_seq", None)
    return clean


def _parse_event_line(line: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return _dashboard_event_redact(_compact_dashboard_event(parsed))


def _tail_text_lines(path: Path, limit: int) -> list[str]:
    if limit <= 0 or not path.exists():
        return []
    chunk_size = 64 * 1024
    data = b""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        while position > 0 and data.count(b"\n") <= limit:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            data = handle.read(read_size) + data
    return [line.decode("utf-8", errors="replace") for line in data.splitlines()[-limit:]]


def _compact_dashboard_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or "")
    if event_type == "hitl_review_pending":
        return _compact_review_event(event)
    if event_type == "pending_review":
        return _compact_if_large(event)
    if event_type in {"hitl_review_decision", "review_decision", "hitl_review_expired"}:
        compact = _base_event_fields(event)
        for key in ("status", "reason", "decided_at", "updated_at", "expired_at"):
            if key in event:
                compact[key] = event.get(key)
        metadata = event.get("metadata")
        if isinstance(metadata, dict):
            compact["metadata"] = _compact_event_metadata(metadata)
        return compact
    return _compact_if_large(event)


def _compact_review_event(event: dict[str, Any]) -> dict[str, Any]:
    compact = _base_event_fields(event)
    for key in ("status", "reason", "summary", "artifact_id", "created_at", "updated_at", "expires_at"):
        if key in event:
            compact[key] = event.get(key)
    classification = event.get("classification")
    if isinstance(classification, dict):
        compact["classification"] = _compact_classification(classification)
    return compact


def _base_event_fields(event: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in ("event_id", "created_at", "type", "review_id", "run_id", "session_id", "framework", "action", "summary"):
        if key in event:
            compact[key] = event.get(key)
    return compact


def _compact_if_large(event: dict[str, Any]) -> dict[str, Any]:
    try:
        if len(json.dumps(event, ensure_ascii=True, separators=(",", ":"))) <= 16000:
            return event
    except (TypeError, ValueError):
        pass
    compact = _base_event_fields(event)
    for key in ("status", "reason", "updated_at", "decided_at", "artifact_id", "capture_status"):
        if key in event:
            compact[key] = event.get(key)
    classification = event.get("classification")
    if isinstance(classification, dict):
        compact["classification"] = _compact_classification(classification)
    metadata = event.get("metadata")
    if isinstance(metadata, dict):
        compact["metadata"] = _compact_event_metadata(metadata)
    compact["truncated"] = True
    return compact


def _compact_event_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    try:
        if len(json.dumps(metadata, ensure_ascii=True, separators=(",", ":"))) <= 8000:
            return metadata
    except (TypeError, ValueError):
        pass
    compact: dict[str, Any] = {}
    for key in ("guard", "decision", "review_result", "review_cache_key", "review_reusable", "skill_decision_id", "rule_id"):
        if key in metadata:
            compact[key] = metadata.get(key)
    classification = metadata.get("classification")
    if isinstance(classification, dict):
        compact["classification"] = _compact_classification(classification)
    cedar = metadata.get("cedar")
    if isinstance(cedar, dict):
        compact["cedar"] = {
            key: cedar.get(key)
            for key in ("source", "decision", "reason", "skill_decision_id")
            if key in cedar
        }
    compact["truncated"] = True
    return compact


def _compact_classification(classification: dict[str, Any]) -> dict[str, Any]:
    compact = {
        key: classification.get(key)
        for key in ("method", "action", "reason", "confidence", "meaningful", "read_only")
        if key in classification
    }
    target = classification.get("target")
    if isinstance(target, dict):
        compact["target"] = {
            key: target.get(key)
            for key in (
                "tag",
                "type",
                "role",
                "text",
                "id",
                "name",
                "url",
                "form_id",
                "is_editable",
                "is_form_control",
                "is_select",
                "is_submit",
                "confidence",
            )
            if key in target
        }
    details = classification.get("details")
    if isinstance(details, dict):
        compact_details: dict[str, Any] = {}
        for key in ("event_type", "text_length", "is_sensitive", "source", "target_resolution", "target_resolution_status"):
            if key in details:
                compact_details[key] = details.get(key)
        if "page_target_probe" in details and isinstance(details["page_target_probe"], dict):
            probe = details["page_target_probe"]
            compact_details["page_target_probe"] = {
                key: probe.get(key)
                for key in ("status", "candidate_count", "url", "title", "ready_state", "cached")
                if key in probe
            }
        if compact_details:
            compact["details"] = compact_details
    return compact


def _dashboard_event_redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_value(value)
    if isinstance(value, list):
        return [_dashboard_event_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_dashboard_event_redact(item) for item in value]
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in EVENT_SENSITIVE_KEYS:
                redacted[key_text] = "[REDACTED]"
            else:
                redacted[key_text] = _dashboard_event_redact(item)
        return redacted
    return value


def _bounded_limit(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 100
    return max(1, min(500, parsed))
