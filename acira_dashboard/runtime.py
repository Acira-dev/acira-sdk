from __future__ import annotations

import asyncio
import base64
import binascii
import json
import tempfile
import time
import uuid
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from acira_cdp_proxy.guard import GuardReviewRequest
from acira_cdp_proxy.redaction import redact_value

from .store import DashboardEventStore, DEFAULT_EVENT_FILE, utc_now_iso


PRODUCTION_DIR = Path(__file__).resolve().parents[1]
# Kept only so existing imports/signatures resolve; the dashboard never writes here.
DEFAULT_RUNTIME_DIR = PRODUCTION_DIR / "local-runtime" / "dashboard"
# Ephemeral in-memory caps (LRU; oldest evicted first). Tunable.
MAX_SESSIONS = 200
MAX_REVIEWS = 500
MAX_ARTIFACTS = 100
VALID_REVIEW_DECISIONS = {"allow", "block"}
REVIEW_HISTORY_STATUSES = {"allow", "block", "expired"}
DEFAULT_SESSION_STALE_SECONDS = 15.0
MAX_SCREENSHOT_BASE64_CHARS = 5_000_000
SCREENSHOT_DATA_URL_PREFIX = "data:image/png;base64,"
SENSITIVE_VALUE_KEYS = {
    "api_key",
    "authorization",
    "password",
    "plain_text",
    "plaintext",
    "secret_value",
    "token",
    "value",
    "values",
}


class DashboardRuntimeError(RuntimeError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class DashboardRuntimeStore:
    """Ephemeral in-memory control plane shared by the dashboard and Acira runtime.

    All session/review/decision/artifact/secret state lives in bounded in-memory
    maps (LRU, oldest evicted) and is gone when the process exits. Nothing is
    written to disk. Public method signatures are unchanged.
    """

    def __init__(
        self,
        root: str | Path = DEFAULT_RUNTIME_DIR,
        *,
        event_store: DashboardEventStore | None = None,
    ) -> None:
        self.root = Path(root)  # retained for signature/back-compat; never written
        self.event_store = event_store or DashboardEventStore(DEFAULT_EVENT_FILE)
        self._sessions: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._reviews: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._artifacts: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._decisions: dict[str, dict[str, Any]] = {}

    def register_session(self, run_id: str, *, framework: str = "browser_use", agent_name: str | None = None, metadata: dict[str, Any] | None = None, project_root: str | None = None) -> dict[str, Any]:
        existed = self._read_session(run_id) is not None
        metadata = metadata if isinstance(metadata, dict) else {}
        session = self._read_session(run_id) or {
            "session_id": run_id,
            "run_id": run_id,
            "framework": framework,
            "agent_name": (agent_name or "").strip(),
            "status": "active",
            "paused": False,
            "started_at": utc_now_iso(),
        }
        current_status = str(session.get("status") or "").strip().lower()
        paused = bool(session.get("paused", False))
        if current_status == "ended":
            next_status = "ended"
        elif paused or current_status == "paused":
            next_status = "paused"
        else:
            next_status = "active"
        update: dict[str, Any] = {
            "framework": framework or session.get("framework") or "browser_use",
            "agent_name": (agent_name or session.get("agent_name") or "").strip(),
            "status": next_status,
            "updated_at": utc_now_iso(),
            "last_heartbeat_at": utc_now_iso(),
            "metadata": redact_value(metadata or session.get("metadata") or {}),
        }
        if isinstance(project_root, str) and project_root.strip():
            update["project_root"] = project_root.strip()
        session.update(update)
        self._store_session(run_id, session)
        if not existed:
            self.event_store.append(
                {
                    "type": "session_started",
                    "run_id": run_id,
                    "session_id": run_id,
                    "framework": session["framework"],
                    "summary": f"{session['framework']} session active",
                }
            )
        return dashboard_redact(session)

    def touch_session(self, run_id: str | None) -> None:
        """Refresh liveness for an active session from the agent's own runtime traffic.

        The agent is fire-and-forget (no heartbeat loop), so its real traffic — creating
        or polling a HITL review, emitting events — is the proof it is alive. Without this
        an active session goes stale within DEFAULT_SESSION_STALE_SECONDS and its pending
        reviews get auto-expired to "block" before a human can answer (and the session
        disappears from the live view). No-op for unknown or already-ended sessions.
        """
        run_id_text = str(run_id or "").strip()
        if not run_id_text:
            return
        session = self._read_session(run_id_text)
        if session is None:
            return
        if str(session.get("status") or "").strip().lower() == "ended":
            return
        now = utc_now_iso()
        session["last_heartbeat_at"] = now
        session["updated_at"] = now
        self._store_session(run_id_text, session)

    def session_control(self, run_id: str) -> dict[str, Any]:
        session = self._read_session(run_id)
        if session is None:
            raise DashboardRuntimeError("Session not found.", 404)
        return {
            "session_id": session.get("session_id") or run_id,
            "run_id": session.get("run_id") or run_id,
            "status": session.get("status") or "unknown",
            "paused": bool(session.get("paused", False)),
            "updated_at": session.get("updated_at"),
            "last_heartbeat_at": session.get("last_heartbeat_at"),
        }

    def end_session(self, run_id: str, *, reason: str | None = None) -> dict[str, Any]:
        session = self._read_session(run_id) or {
            "session_id": run_id,
            "run_id": run_id,
            "framework": "unknown",
            "started_at": utc_now_iso(),
        }
        session.update({"status": "ended", "paused": False, "ended_at": utc_now_iso(), "updated_at": utc_now_iso()})
        if reason:
            session["reason"] = redact_value(reason)
        self._store_session(run_id, session)
        self.event_store.append(
            {
                "type": "session_ended",
                "run_id": run_id,
                "session_id": run_id,
                "status": "ended",
                "summary": f"Session ended: {reason or 'complete'}",
            }
        )
        self._expire_pending_reviews_for_run(run_id, reason="Session ended before dashboard decision.")
        return dashboard_redact(session)

    def set_session_paused(self, run_id: str, *, paused: bool, reason: str | None = None) -> dict[str, Any]:
        session = self._read_session(run_id)
        if session is None:
            raise DashboardRuntimeError("Session not found.", 404)
        if session.get("status") == "ended":
            raise DashboardRuntimeError("Ended sessions cannot be paused or resumed.", 409)
        session.update({"paused": paused, "status": "paused" if paused else "active", "updated_at": utc_now_iso()})
        if paused:
            if reason:
                session["pause_reason"] = redact_value(reason)
        else:
            session.pop("pause_reason", None)
        self._store_session(run_id, session)
        self.event_store.append(
            {
                "type": "session_paused" if paused else "session_resumed",
                "run_id": run_id,
                "session_id": run_id,
                "status": session["status"],
                "reason": redact_value(reason),
                "summary": "Session paused" if paused else "Session resumed",
            }
        )
        return dashboard_redact(session)

    async def wait_if_paused(self, run_id: str, *, poll_seconds: float = 0.25) -> None:
        emitted = False
        while True:
            revision = self.event_store.revision
            try:
                session = self._read_session(run_id)
            except Exception:
                session = {"paused": True}
            if isinstance(session, dict) and session.get("status") == "ended":
                return
            if isinstance(session, dict) and not session.get("paused"):
                return
            if not emitted:
                emitted = True
                self.event_store.append(
                    {
                        "type": "session_pause_wait",
                        "run_id": run_id,
                        "session_id": run_id,
                        "status": "paused",
                        "summary": "Runtime waiting for dashboard resume",
                    }
                )
                revision = self.event_store.revision
            await self.event_store.wait_for_change_after(revision, timeout=poll_seconds)

    def list_sessions(self) -> list[dict[str, Any]]:
        sessions = list(self._sessions.values())
        by_id: dict[str, dict[str, Any]] = {
            str(session.get("session_id") or session.get("run_id")): session
            for session in sessions
            if isinstance(session, dict)
        }
        for event in self.event_store.read_recent(limit=100):
            run_id = str(event.get("run_id") or "")
            if not run_id or run_id in by_id:
                continue
            by_id[run_id] = {
                "session_id": run_id,
                "run_id": run_id,
                "framework": event.get("framework") or "unknown",
                "status": "observed",
                "paused": False,
                "started_at": event.get("created_at"),
                "updated_at": event.get("updated_at") or event.get("created_at"),
            }
        return sorted(
            (dashboard_redact(_normalize_session(session)) for session in by_id.values()),
            key=lambda item: str(item.get("updated_at") or item.get("started_at") or ""),
            reverse=True,
        )

    def session_summary(self) -> dict[str, int]:
        summary = {
            "active_count": 0,
            "paused_count": 0,
            "stale_count": 0,
            "ended_count": 0,
            "observed_count": 0,
            "total_count": 0,
            "live_count": 0,
        }
        for session in self.list_sessions():
            summary["total_count"] += 1
            status = str(session.get("status") or "").strip().lower()
            if status == "active":
                summary["active_count"] += 1
                summary["live_count"] += 1
            elif status == "paused":
                summary["paused_count"] += 1
                summary["live_count"] += 1
            elif status == "stale":
                summary["stale_count"] += 1
            elif status == "ended":
                summary["ended_count"] += 1
            elif status == "observed":
                summary["observed_count"] += 1
        return summary

    def review_summary(self) -> dict[str, int]:
        summary = {"pending_count": 0, "history_count": 0, "total_count": 0}
        live_session_ids = self._live_session_ids()
        for review in self.list_reviews(status_filter="all"):
            summary["total_count"] += 1
            status = str(review.get("status") or "").strip().lower()
            if _review_is_actionable_pending(review, live_session_ids):
                summary["pending_count"] += 1
            else:
                summary["history_count"] += 1
        return summary

    def get_session(self, run_id: str) -> dict[str, Any]:
        session = self._read_session(run_id)
        events = [event for event in self.event_store.read_recent(limit=500) if str(event.get("run_id") or "") == run_id]
        if session is None:
            session = _observed_session_from_events(run_id, events)
        if session is None:
            raise DashboardRuntimeError("Session not found.", 404)
        reviews = [review for review in self.list_reviews() if str(review.get("run_id") or "") == run_id]
        return {
            "session": dashboard_redact(_normalize_session(session)),
            "events": events,
            "reviews": reviews,
            "secrets_used": _secret_uses_from_events(events),
        }

    def create_review_from_request(
        self,
        request: GuardReviewRequest,
        *,
        run_id: str | None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        review_id = f"review-{uuid.uuid4().hex[:16]}"
        expires_at = datetime.now(UTC) + timedelta(seconds=max(1, int(timeout_seconds)))
        classification = request.classification.to_metadata()
        artifact = self._create_review_artifact(review_id, classification)
        review = {
            "schema": "acira.dashboard_review.v1",
            "review_id": review_id,
            "run_id": run_id or _run_id_from_request(request),
            "status": "pending",
            "reason": redact_value(request.reason),
            "payload": _payload_summary(request.payload),
            "context": redact_value(request.context.to_metadata() if hasattr(request.context, "to_metadata") else {}),
            "classification": redact_value(classification),
            "artifact_id": artifact.get("artifact_id") if artifact else None,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "expires_at": expires_at.isoformat(),
            "summary": _review_summary(classification, request.reason),
        }
        self._store_review(review_id, review)
        self.touch_session(review.get("run_id"))
        self.event_store.append({"type": "hitl_review_pending", **_review_list_summary(review)})
        return dashboard_redact(review)

    def create_review_from_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        review_id = f"review-{uuid.uuid4().hex[:16]}"
        timeout_seconds = _positive_float(data.get("timeout_seconds"), default=3600.0)
        expires_at = datetime.now(UTC) + timedelta(seconds=max(1, int(timeout_seconds)))
        classification = data.get("classification") if isinstance(data.get("classification"), dict) else {}
        artifact = self._create_review_artifact(review_id, classification, data.get("artifact"))
        run_id = str(data.get("run_id") or _run_id_from_metadata(classification) or "run-dashboard")
        review = {
            "schema": "acira.dashboard_review.v1",
            "review_id": review_id,
            "run_id": run_id,
            "status": "pending",
            "reason": redact_value(data.get("reason")) if isinstance(data.get("reason"), str) else None,
            "payload": redact_value(data.get("payload") if isinstance(data.get("payload"), dict) else {}),
            "context": redact_value(data.get("context") if isinstance(data.get("context"), dict) else {}),
            "classification": redact_value(classification),
            "artifact_id": artifact.get("artifact_id") if artifact else None,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "expires_at": expires_at.isoformat(),
            "summary": _review_summary(classification, data.get("reason") if isinstance(data.get("reason"), str) else None),
        }
        self._store_review(review_id, review)
        self.touch_session(review.get("run_id"))
        self.event_store.append({"type": "hitl_review_pending", **_review_list_summary(review)})
        return dashboard_redact(review)

    def update_review_artifact(self, review_id: str, artifact_payload: Any) -> dict[str, Any]:
        review = self.get_review(review_id)
        classification = review.get("classification") if isinstance(review.get("classification"), dict) else {}
        artifact_id = str(review.get("artifact_id") or "").strip()
        artifact = self._artifacts.get(_safe_id(artifact_id)) if artifact_id else None
        if not isinstance(artifact, dict):
            artifact = self._create_review_artifact(review_id, classification)
            artifact_id = str(artifact.get("artifact_id") or "")
            review["artifact_id"] = artifact_id

        supplied = _normalize_screenshot_artifact_payload(artifact_payload)
        target_box = supplied.get("target_box") or artifact.get("target_box") or _target_box(
            classification.get("target") if isinstance(classification.get("target"), dict) else {}
        )
        updated_at = utc_now_iso()
        artifact.update(
            {
                "capture_status": supplied.get("capture_status") or artifact.get("capture_status") or "unavailable",
                "image_data_url": supplied.get("image_data_url"),
                "image_width": supplied.get("image_width"),
                "image_height": supplied.get("image_height"),
                "target_box": target_box,
                "viewport": supplied.get("viewport") or artifact.get("viewport"),
                "capture_error": supplied.get("capture_error"),
                "capture_attempts": supplied.get("capture_attempts"),
                "capture_errors": supplied.get("capture_errors"),
                "capture_session_id": supplied.get("capture_session_id"),
                "capture_session_candidates": supplied.get("capture_session_candidates"),
                "updated_at": updated_at,
            }
        )
        self._store_artifact(artifact_id, artifact)
        review["updated_at"] = updated_at
        review["artifact_id"] = artifact_id
        self._store_review(review_id, review)
        self.event_store.append(
            {
                "type": "hitl_review_artifact_updated",
                "review_id": review_id,
                "run_id": review.get("run_id"),
                "artifact_id": artifact_id,
                "capture_status": artifact.get("capture_status"),
                "summary": f"Review artifact {artifact.get('capture_status') or 'updated'}",
            }
        )
        return {"review": dashboard_redact(review), "artifact": dashboard_redact(artifact)}

    def decide_review(self, review_id: str, *, status: str, reason: str | None = None) -> dict[str, Any]:
        normalized = str(status or "").strip().lower()
        if normalized not in VALID_REVIEW_DECISIONS:
            raise DashboardRuntimeError("Review decision must be allow or block.", 400)
        review = self.get_review(review_id)
        if review.get("status") != "pending":
            raise DashboardRuntimeError("Review is no longer pending.", 409)
        if _is_expired(review.get("expires_at")):
            self.expire_review(review_id, reason="Decision arrived after review expiration.")
            raise DashboardRuntimeError("Review decision is stale.", 409)
        decision = {
            "schema": "acira.dashboard_review_decision.v1",
            "review_id": review_id,
            "status": normalized,
            "reason": redact_value(reason),
            "decided_at": utc_now_iso(),
        }
        self._store_decision(review_id, decision)
        decided_at = decision["decided_at"]
        review.update(
            {
                "status": normalized,
                "decision_reason": redact_value(reason),
                "decided_at": decided_at,
                "updated_at": decided_at,
            }
        )
        self._store_review(review_id, review)
        self.event_store.append(
            {
                "type": "hitl_review_decision",
                "review_id": review_id,
                "run_id": review.get("run_id"),
                "status": normalized,
                "reason": redact_value(reason),
                "decided_at": decided_at,
                "summary": f"Dashboard review {normalized}",
            }
        )
        return dashboard_redact(review)

    async def wait_for_review_decision(self, review_id: str, *, timeout_seconds: float, poll_seconds: float = 0.05) -> str:
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while time.monotonic() < deadline:
            revision = self.event_store.revision
            try:
                review_state = self.poll_review_decision(review_id)
            except DashboardRuntimeError:
                return "block"
            status = str(review_state.get("status") or "").strip().lower()
            if status in VALID_REVIEW_DECISIONS:
                return status
            decision = self._decisions.get(_safe_id(review_id))
            if isinstance(decision, dict):
                status = str(decision.get("status") or "").strip().lower()
                return status if status in VALID_REVIEW_DECISIONS else "block"
            await self.event_store.wait_for_change_after(revision, timeout=min(poll_seconds, max(0.0, deadline - time.monotonic())))
        self.expire_review(review_id, reason="Dashboard review timed out.")
        return "block"

    def poll_review_decision(self, review_id: str) -> dict[str, Any]:
        review = self.get_review(review_id)
        # The agent is actively polling for this decision — that proves its session is
        # alive, so keep it live instead of expiring the review out from under it.
        self.touch_session(review.get("run_id"))
        status = str(review.get("status") or "").strip().lower()
        if status == "pending" and not _review_is_actionable_pending(review, self._live_session_ids()):
            reason = (
                "Dashboard review expired before decision."
                if _is_expired(review.get("expires_at"))
                else "Dashboard review is no longer attached to a live session."
            )
            self.expire_review(review_id, reason=reason)
            review = self.get_review(review_id)
            return {"status": "block", "review_state": "expired", "review": _review_list_summary(review)}
        if status in VALID_REVIEW_DECISIONS:
            return {"status": status, "review_state": status, "review": _review_list_summary(review)}
        if status == "expired":
            return {"status": "block", "review_state": "expired", "review": _review_list_summary(review)}
        return {"status": "pending", "review_state": status or "pending", "review": _review_list_summary(review)}

    def list_reviews(self, *, status_filter: str = "all", run_id: str | None = None) -> list[dict[str, Any]]:
        reviews = list(self._reviews.values())
        normalized_filter = str(status_filter or "all").strip().lower()
        if normalized_filter not in {"all", "pending", "history", "allow", "block", "expired"}:
            raise DashboardRuntimeError("Unsupported review status filter.", 400)
        run_id_text = str(run_id or "").strip()
        live_session_ids = self._live_session_ids()
        filtered: list[dict[str, Any]] = []
        for review in reviews:
            if not isinstance(review, dict):
                continue
            if run_id_text and str(review.get("run_id") or "") != run_id_text:
                continue
            status = str(review.get("status") or "").strip().lower()
            if status == "pending" and not _review_is_actionable_pending(review, live_session_ids):
                review_id = str(review.get("review_id") or "").strip()
                if not review_id:
                    continue
                reason = (
                    "Dashboard review expired before decision."
                    if _is_expired(review.get("expires_at"))
                    else "Dashboard review is no longer attached to a live session."
                )
                self.expire_review(review_id, reason=reason)
                review = self.get_review(review_id)
                status = str(review.get("status") or "").strip().lower()
            active_pending = _review_is_actionable_pending(review, live_session_ids)
            if normalized_filter == "pending" and not active_pending:
                continue
            if normalized_filter == "history" and active_pending:
                continue
            if normalized_filter in VALID_REVIEW_DECISIONS | {"expired"} and status != normalized_filter:
                continue
            filtered.append(review)
        return sorted(
            (_review_list_summary(dashboard_redact(review)) for review in filtered),
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            reverse=True,
        )

    def get_review(self, review_id: str) -> dict[str, Any]:
        review = self._reviews.get(_safe_id(review_id))
        if not isinstance(review, dict):
            review = _review_from_events(review_id, self.event_store.read_recent(limit=500))
        if not isinstance(review, dict):
            raise DashboardRuntimeError("Review not found.", 404)
        return dashboard_redact(review)

    def expire_review(self, review_id: str, *, reason: str) -> None:
        try:
            review = self.get_review(review_id)
        except DashboardRuntimeError:
            return
        if review.get("status") != "pending":
            return
        expired_at = utc_now_iso()
        review.update(
            {
                "status": "expired",
                "updated_at": expired_at,
                "expired_at": expired_at,
                "decision_reason": redact_value(reason),
            }
        )
        self._store_review(review_id, review)
        self.event_store.append(
            {
                "type": "hitl_review_expired",
                "review_id": review_id,
                "run_id": review.get("run_id"),
                "status": "expired",
                "reason": redact_value(reason),
                "expired_at": expired_at,
                "summary": "Dashboard review expired",
            }
        )

    def _expire_pending_reviews_for_run(self, run_id: str, *, reason: str) -> None:
        run_id_text = str(run_id or "").strip()
        if not run_id_text:
            return
        for review in list(self._reviews.values()):
            if not isinstance(review, dict):
                continue
            if str(review.get("run_id") or "") != run_id_text:
                continue
            if str(review.get("status") or "").strip().lower() != "pending":
                continue
            self.expire_review(str(review.get("review_id") or ""), reason=reason)

    def _live_session_ids(self) -> set[str]:
        live_ids: set[str] = set()
        for session in list(self._sessions.values()):
            if not isinstance(session, dict):
                continue
            session = _normalize_session(session)
            if not bool(session.get("live", False)):
                continue
            if str(session.get("status") or "").strip().lower() not in {"active", "paused"}:
                continue
            for key in ("session_id", "run_id"):
                value = str(session.get(key) or "").strip()
                if value:
                    live_ids.add(value)
        return live_ids

    def get_project_root(self, run_id: str) -> Path | None:
        session = self._read_session(run_id)
        if session is None:
            return None
        pr = session.get("project_root")
        if not isinstance(pr, str) or not pr.strip():
            return None
        return Path(pr.strip())

    def _env_file_path(self, run_id: str | None = None) -> Path:
        if run_id is not None:
            pr = self.get_project_root(run_id)
            if pr is not None:
                return pr / "configs" / "secrets" / "acira.secrets.env"
        return PRODUCTION_DIR / "configs" / "secrets" / "acira.secrets.env"

    @staticmethod
    def _parse_env_file(path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return {}
        result: dict[str, str] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, _, v = stripped.partition("=")
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            if k:
                result[k] = v
        return result

    @staticmethod
    def _write_env_file_atomic(path: Path, env: dict[str, str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{k}={v}" for k, v in env.items()]
        content = "\n".join(lines) + ("\n" if lines else "")
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(path)

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        artifact = self._artifacts.get(_safe_id(artifact_id))
        if not isinstance(artifact, dict):
            raise DashboardRuntimeError("Artifact not found.", 404)
        if artifact.get("kind") != "screenshot":
            raise DashboardRuntimeError("Only screenshot artifacts are supported.", 400)
        return dashboard_redact(artifact)

    def list_secrets(self, run_id: str | None = None) -> list[dict[str, Any]]:
        env = self._parse_env_file(self._env_file_path(run_id))
        return [{"id": k, "label": k, "active": True, "value": "[REDACTED]"} for k in env]

    def get_secret(self, secret_id: str, run_id: str | None = None) -> dict[str, Any]:
        safe_id = _safe_id(secret_id)
        env = self._parse_env_file(self._env_file_path(run_id))
        if safe_id not in env:
            raise DashboardRuntimeError("Secret not found.", 404)
        return {"id": safe_id, "label": safe_id, "active": True, "value": "[REDACTED]"}

    def upsert_secret(self, data: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
        secret_id = _safe_id(str(data.get("id") or ""))
        value = data.get("value")
        if not isinstance(value, str) or not value:
            raise DashboardRuntimeError("Plaintext value is required.", 400)
        path = self._env_file_path(run_id)
        env = self._parse_env_file(path)
        env[secret_id] = value
        self._write_env_file_atomic(path, env)
        self.event_store.append(
            {
                "type": "secret_config_updated",
                "secret_id": secret_id,
                "summary": f"Secret updated: {secret_id}",
            }
        )
        label = str(data.get("label") or secret_id)
        return {"id": secret_id, "label": label, "active": True, "value": "[REDACTED]"}

    def delete_secret(self, secret_id: str, run_id: str | None = None) -> None:
        safe_id = _safe_id(secret_id)
        path = self._env_file_path(run_id)
        env = self._parse_env_file(path)
        if safe_id not in env:
            raise DashboardRuntimeError("Secret not found.", 404)
        del env[safe_id]
        self._write_env_file_atomic(path, env)
        self.event_store.append({"type": "secret_config_deleted", "secret_id": safe_id, "summary": f"Secret deleted: {safe_id}"})

    def acira_secrets_config_path(self) -> Path | None:
        # Guard reads secrets from its own config block; dashboard manages the .env file directly.
        return None

    def _read_session(self, run_id: str) -> dict[str, Any] | None:
        data = self._sessions.get(_safe_id(run_id))
        return data if isinstance(data, dict) else None

    def _create_review_artifact(
        self,
        review_id: str,
        classification: dict[str, Any],
        artifact_payload: Any = None,
    ) -> dict[str, Any]:
        artifact_id = f"artifact-{uuid.uuid4().hex[:16]}"
        supplied = _normalize_screenshot_artifact_payload(artifact_payload)
        target_box = supplied.get("target_box") or _target_box(classification.get("target") if isinstance(classification.get("target"), dict) else {})
        artifact = {
            "schema": "acira.dashboard_artifact.v1",
            "artifact_id": artifact_id,
            "review_id": review_id,
            "kind": "screenshot",
            "capture_status": supplied.get("capture_status") or "unavailable",
            "image_data_url": supplied.get("image_data_url"),
            "image_width": supplied.get("image_width"),
            "image_height": supplied.get("image_height"),
            "target_box": target_box,
            "viewport": supplied.get("viewport"),
            "capture_error": supplied.get("capture_error"),
            "capture_attempts": supplied.get("capture_attempts"),
            "capture_errors": supplied.get("capture_errors"),
            "capture_session_id": supplied.get("capture_session_id"),
            "capture_session_candidates": supplied.get("capture_session_candidates"),
            "created_at": utc_now_iso(),
        }
        self._store_artifact(artifact_id, artifact)
        return artifact

    # ---- in-memory persistence (LRU; oldest evicted) -------------------------

    @staticmethod
    def _put(store: "OrderedDict[str, dict[str, Any]]", key: str, value: dict[str, Any], cap: int) -> None:
        store[key] = value
        store.move_to_end(key)
        while len(store) > cap:
            store.popitem(last=False)

    def _store_session(self, run_id: str, session: dict[str, Any]) -> None:
        self._put(self._sessions, _safe_id(run_id), dashboard_redact(session), MAX_SESSIONS)

    def _store_review(self, review_id: str, review: dict[str, Any]) -> None:
        self._put(self._reviews, _safe_id(review_id), dashboard_redact(review), MAX_REVIEWS)

    def _store_artifact(self, artifact_id: str, artifact: dict[str, Any]) -> None:
        self._put(self._artifacts, _safe_id(artifact_id), dashboard_redact(artifact), MAX_ARTIFACTS)

    def _store_decision(self, review_id: str, decision: dict[str, Any]) -> None:
        self._decisions[_safe_id(review_id)] = dashboard_redact(decision)



class DashboardReviewBackend:
    """ReviewBackend adapter that waits for an authorized dashboard decision."""

    def __init__(self, runtime_store: DashboardRuntimeStore | None = None, *, timeout_seconds: float = 3600.0) -> None:
        self.runtime_store = runtime_store or DashboardRuntimeStore()
        self.timeout_seconds = timeout_seconds
        self.run_id: str | None = None

    def bind_run_id(self, run_id: str) -> None:
        self.run_id = run_id

    async def ask(self, request: GuardReviewRequest) -> str:
        try:
            review = self.runtime_store.create_review_from_request(
                request,
                run_id=self.run_id,
                timeout_seconds=self.timeout_seconds,
            )
            return await self.runtime_store.wait_for_review_decision(
                str(review["review_id"]),
                timeout_seconds=self.timeout_seconds,
            )
        except Exception as exc:
            self.runtime_store.event_store.append(
                {
                    "type": "hitl_review_backend_failed",
                    "run_id": self.run_id,
                    "status": "block",
                    "reason": str(exc),
                    "summary": "Dashboard review backend failed closed",
                }
            )
            return "block"


class DashboardRuntimeControl:
    """Runtime control adapter used by framework adapters to honor dashboard pause state."""

    def __init__(self, runtime_store: DashboardRuntimeStore | None = None) -> None:
        self.runtime_store = runtime_store or DashboardRuntimeStore()
        self.run_id: str | None = None

    def bind_run_id(self, run_id: str) -> None:
        self.run_id = run_id
        self.runtime_store.register_session(run_id, framework="browser_use")

    async def wait_if_paused(self) -> None:
        if self.run_id:
            await self.runtime_store.wait_if_paused(self.run_id)

    def close(self, *, reason: str | None = None) -> None:
        if self.run_id:
            self.runtime_store.end_session(self.run_id, reason=reason)


def merge_dashboard_secrets_config(config_data: dict[str, Any], runtime_store: DashboardRuntimeStore) -> dict[str, Any]:
    secrets_path = runtime_store.acira_secrets_config_path()
    if secrets_path is None:
        return dict(config_data)
    merged = dict(config_data)
    merged["secrets_config_path"] = str(secrets_path)
    return merged


def dashboard_redact(value: Any) -> Any:
    if isinstance(value, list):
        return [dashboard_redact(item) for item in value]
    if isinstance(value, tuple):
        return [dashboard_redact(item) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        if key_text.lower() in SENSITIVE_VALUE_KEYS:
            result[key_text] = "[REDACTED]"
        else:
            result[key_text] = dashboard_redact(item)
    return result


def _normalize_session(session: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(session)
    raw_status = str(normalized.get("status") or "").strip().lower()
    paused = bool(normalized.get("paused", False))
    heartbeat_at = _parse_datetime(normalized.get("last_heartbeat_at") or normalized.get("updated_at"))
    stale_after = _session_stale_after_seconds(normalized)
    heartbeat_age = (datetime.now(UTC) - heartbeat_at).total_seconds() if heartbeat_at is not None else None
    heartbeat_stale = heartbeat_age is None or heartbeat_age > stale_after

    if raw_status == "ended":
        status = "ended"
        live = False
    elif raw_status == "observed":
        status = "observed"
        live = False
    elif raw_status in {"active", "paused"} or paused:
        if heartbeat_stale:
            status = "stale"
            live = False
        else:
            status = "paused" if paused or raw_status == "paused" else "active"
            live = True
    else:
        status = raw_status or "unknown"
        live = False

    normalized["raw_status"] = raw_status or None
    normalized["status"] = status
    normalized["live"] = live
    normalized["heartbeat_stale"] = heartbeat_stale
    normalized["stale_after_seconds"] = stale_after
    if heartbeat_age is not None:
        normalized["heartbeat_age_seconds"] = max(0.0, heartbeat_age)
    return normalized


def _observed_session_from_events(run_id: str, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not events:
        return None
    first = events[0]
    latest = events[-1]
    ended = next((event for event in reversed(events) if str(event.get("type") or "") == "session_ended"), None)
    return {
        "session_id": run_id,
        "run_id": run_id,
        "framework": latest.get("framework") or first.get("framework") or "unknown",
        "status": "ended" if ended else "observed",
        "paused": False,
        "started_at": first.get("created_at"),
        "updated_at": latest.get("updated_at") or latest.get("created_at"),
        "ended_at": ended.get("created_at") if isinstance(ended, dict) else None,
    }


def _review_from_events(review_id: str, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    matching = [event for event in events if str(event.get("review_id") or "") == review_id]
    if not matching:
        return None
    first = matching[0]
    latest = matching[-1]
    status = str(latest.get("status") or "").strip().lower()
    event_type = str(latest.get("type") or "")
    if event_type in {"review_decision", "hitl_review_decision"} and status in VALID_REVIEW_DECISIONS:
        review_status = status
    elif event_type == "hitl_review_expired" or status == "expired":
        review_status = "expired"
    else:
        review_status = "pending"
    return {
        "schema": "acira.dashboard_review_event_projection.v1",
        "review_id": review_id,
        "run_id": latest.get("run_id") or first.get("run_id"),
        "status": review_status,
        "reason": latest.get("reason") or first.get("reason"),
        "summary": latest.get("summary") or first.get("summary") or "Historical review",
        "classification": first.get("classification") if isinstance(first.get("classification"), dict) else {},
        "artifact_id": None,
        "created_at": first.get("created_at"),
        "updated_at": latest.get("updated_at") or latest.get("created_at"),
        "source": "events",
    }


def _review_list_summary(review: dict[str, Any]) -> dict[str, Any]:
    classification = review.get("classification") if isinstance(review.get("classification"), dict) else {}
    target = classification.get("target") if isinstance(classification.get("target"), dict) else {}
    return {
        "schema": review.get("schema") or "acira.dashboard_review.v1",
        "review_id": review.get("review_id"),
        "run_id": review.get("run_id"),
        "status": review.get("status"),
        "reason": review.get("reason"),
        "artifact_id": review.get("artifact_id"),
        "created_at": review.get("created_at"),
        "updated_at": review.get("updated_at"),
        "expires_at": review.get("expires_at"),
        "decided_at": review.get("decided_at"),
        "expired_at": review.get("expired_at"),
        "decision_reason": review.get("decision_reason"),
        "summary": review.get("summary")
        or _review_summary(classification, review.get("reason") if isinstance(review.get("reason"), str) else None),
        "classification": {
            "method": classification.get("method"),
            "action": classification.get("action"),
            "reason": classification.get("reason"),
            "target": {
                "tag": target.get("tag"),
                "type": target.get("type"),
                "text": target.get("text"),
                "id": target.get("id"),
                "name": target.get("name"),
                "url": target.get("url"),
            },
        },
    }


def _review_is_actionable_pending(review: dict[str, Any], live_session_ids: set[str]) -> bool:
    if str(review.get("status") or "").strip().lower() != "pending":
        return False
    if _is_expired(review.get("expires_at")):
        return False
    run_id = str(review.get("run_id") or "").strip()
    return bool(run_id and run_id in live_session_ids)


def _session_stale_after_seconds(session: dict[str, Any]) -> float:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    heartbeat = _positive_float(metadata.get("heartbeat_seconds"), default=DEFAULT_SESSION_STALE_SECONDS / 5)
    return max(DEFAULT_SESSION_STALE_SECONDS, heartbeat * 5)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_id(value: str) -> str:
    text = str(value or "").strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if not text or text.startswith(".") or text.endswith(".") or ".." in text or any(char not in allowed for char in text):
        raise DashboardRuntimeError("Invalid identifier.", 400)
    return text


def _payload_summary(payload: str | bytes) -> dict[str, Any]:
    text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"snippet": redact_value(text[:240])}
    if not isinstance(parsed, dict):
        return {"snippet": redact_value(text[:240])}
    return redact_value(
        {
            "method": parsed.get("method"),
            "id": parsed.get("id"),
            "params": parsed.get("params") if isinstance(parsed.get("params"), dict) else None,
        }
    )


def _run_id_from_request(request: GuardReviewRequest) -> str:
    details = request.classification.details if isinstance(request.classification.details, dict) else {}
    run_id = details.get("run_id")
    if isinstance(run_id, str) and run_id:
        return run_id
    return "run-dashboard"


def _run_id_from_metadata(classification: dict[str, Any]) -> str | None:
    details = classification.get("details") if isinstance(classification.get("details"), dict) else {}
    run_id = details.get("run_id")
    return run_id if isinstance(run_id, str) and run_id else None


def _review_summary(classification: dict[str, Any], reason: str | None) -> str:
    action = str(classification.get("action") or classification.get("method") or "action").replace("_", " ")
    target = classification.get("target") if isinstance(classification.get("target"), dict) else {}
    target_text = str(target.get("text") or target.get("aria_label") or target.get("name") or "").strip()
    suffix = f" on {target_text}" if target_text else ""
    if reason:
        return f"Review {action}{suffix}: {reason}"
    return f"Review {action}{suffix}"


def _target_box(target: dict[str, Any]) -> dict[str, Any] | None:
    signature = target.get("structure_signature") if isinstance(target.get("structure_signature"), dict) else {}
    layout = signature.get("layout") if isinstance(signature.get("layout"), dict) else {}
    for key in ("viewport_rect", "rect", "browser_use_rect", "document_rect"):
        rect = layout.get(key) if isinstance(layout.get(key), dict) else None
        normalized = _normalize_box(rect)
        if normalized is not None:
            normalized["source"] = key
            normalized["coordinate_space"] = _coordinate_space_for_box(key, layout)
            return normalized
    return None


def _normalize_screenshot_artifact_payload(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {
            "capture_status": "unavailable",
            "image_data_url": None,
            "image_width": None,
            "image_height": None,
            "target_box": None,
            "viewport": None,
            "capture_attempts": None,
            "capture_errors": None,
            "capture_session_id": None,
            "capture_session_candidates": None,
        }
    if not isinstance(payload, dict):
        raise DashboardRuntimeError("Review artifact must be an object.", 400)
    if _contains_forbidden_artifact_payload(payload):
        raise DashboardRuntimeError("Review artifacts may only contain screenshot data and redacted metadata.", 400)

    status = str(payload.get("capture_status") or payload.get("status") or "unavailable").strip().lower()
    if status not in {"captured", "failed", "pending", "unavailable"}:
        status = "unavailable"

    image_data_url = _normalize_screenshot_data_url(
        payload.get("image_data_url")
        or payload.get("data_url")
        or payload.get("image_base64")
        or payload.get("screenshot_base64")
        or payload.get("data")
    )
    if image_data_url is not None:
        status = "captured"
    elif status == "captured":
        status = "failed"

    image_dimensions = _png_dimensions_from_data_url(image_data_url)
    image_width = image_dimensions[0] if image_dimensions else _optional_positive_float(payload.get("image_width") or payload.get("width"))
    image_height = image_dimensions[1] if image_dimensions else _optional_positive_float(payload.get("image_height") or payload.get("height"))
    target_box = _normalize_box(payload.get("target_box") if isinstance(payload.get("target_box"), dict) else None)
    viewport = _normalize_viewport(payload.get("viewport") if isinstance(payload.get("viewport"), dict) else None)
    capture_error = payload.get("capture_error") or payload.get("error")
    capture_attempts = _optional_positive_float(payload.get("capture_attempts"))
    capture_errors = _normalize_capture_errors(payload.get("capture_errors"))
    capture_session_id = _optional_short_text(payload.get("capture_session_id"))
    capture_session_candidates = _normalize_capture_errors(payload.get("capture_session_candidates"))
    return {
        "capture_status": status,
        "image_data_url": image_data_url,
        "image_width": image_width,
        "image_height": image_height,
        "target_box": target_box,
        "viewport": viewport,
        "capture_error": redact_value(str(capture_error)[:240]) if capture_error else None,
        "capture_attempts": capture_attempts,
        "capture_errors": capture_errors,
        "capture_session_id": capture_session_id,
        "capture_session_candidates": capture_session_candidates,
    }


def _contains_forbidden_artifact_payload(value: Any) -> bool:
    if isinstance(value, str):
        lowered = value[:512].lower()
        return "<html" in lowered or "<!doctype" in lowered or "<script" in lowered
    if isinstance(value, list):
        return any(_contains_forbidden_artifact_payload(item) for item in value)
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower()
            if key_text in {"html", "raw_html", "dom", "document", "script"}:
                return True
            if _contains_forbidden_artifact_payload(item):
                return True
    return False


def _normalize_screenshot_data_url(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DashboardRuntimeError("Screenshot artifact image must be a base64 string.", 400)
    text = value.strip()
    if not text:
        return None
    if text.startswith(SCREENSHOT_DATA_URL_PREFIX):
        encoded = text[len(SCREENSHOT_DATA_URL_PREFIX) :]
    elif text.startswith("data:"):
        raise DashboardRuntimeError("Only PNG screenshot artifacts are supported.", 400)
    else:
        encoded = text
    if len(encoded) > MAX_SCREENSHOT_BASE64_CHARS:
        raise DashboardRuntimeError("Screenshot artifact is too large.", 413)
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DashboardRuntimeError("Screenshot artifact image is not valid base64.", 400) from exc
    if not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
        raise DashboardRuntimeError("Screenshot artifact image must be PNG data.", 400)
    return f"{SCREENSHOT_DATA_URL_PREFIX}{encoded}"


def _normalize_box(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        box = {key: float(value[key]) for key in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError):
        return None
    if box["width"] <= 0 or box["height"] <= 0:
        return None
    coordinate_space = str(value.get("coordinate_space") or "").strip().lower()
    if coordinate_space in {"viewport", "document", "image"}:
        box["coordinate_space"] = coordinate_space
    source = str(value.get("source") or "").strip()
    if source:
        box["source"] = source[:80]
    return box


def _normalize_viewport(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    width = value.get("width", value.get("clientWidth"))
    height = value.get("height", value.get("clientHeight"))
    try:
        viewport = {"width": float(width), "height": float(height)}
    except (TypeError, ValueError):
        return None
    if viewport["width"] <= 0 or viewport["height"] <= 0:
        return None
    for key in (
        "scale",
        "page_x",
        "page_y",
        "offset_x",
        "offset_y",
        "device_pixel_ratio",
        "screenshot_scale_x",
        "screenshot_scale_y",
        "image_width",
        "image_height",
        "client_width",
        "client_height",
    ):
        if key in value:
            try:
                viewport[key] = float(value[key])
            except (TypeError, ValueError):
                pass
    return viewport


def _normalize_capture_errors(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    errors = [redact_value(str(item)[:240]) for item in value if item]
    return errors[:8] or None


def _optional_short_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return redact_value(text[:120]) if text else None


def _coordinate_space_for_box(source: str, layout: dict[str, Any]) -> str:
    if source == "viewport_rect":
        return "viewport"
    if source == "document_rect":
        return "document"
    explicit = str(layout.get("coordinate_space") or "").strip().lower()
    return explicit if explicit in {"viewport", "document", "image"} else "viewport"


def _png_dimensions_from_data_url(image_data_url: str | None) -> tuple[float, float] | None:
    if not image_data_url or not image_data_url.startswith(SCREENSHOT_DATA_URL_PREFIX):
        return None
    encoded = image_data_url[len(SCREENSHOT_DATA_URL_PREFIX) :]
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(decoded) < 24 or not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    width = int.from_bytes(decoded[16:20], "big")
    height = int.from_bytes(decoded[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return float(width), float(height)


def _optional_positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _is_expired(expires_at: Any) -> bool:
    try:
        parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed < datetime.now(UTC)


def _secret_policy(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"decision": "ask"}
    decision = str(data.get("decision") or "ask").strip().lower()
    if decision not in {"allow", "ask", "block"}:
        decision = "ask"
    policy = {"decision": decision}
    if data.get("reason"):
        policy["reason"] = str(data["reason"])
    return policy


def _positive_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _redact_secret_record(record: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(record)
    if "value" in redacted:
        redacted["value"] = "[REDACTED]"
    return dashboard_redact(redacted)


def _secret_uses_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for event in events:
        text = json.dumps(event, sort_keys=True, ensure_ascii=True)
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        candidates = [metadata.get("secret_use"), details.get("secret_use"), event.get("secret_use")]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            secret_id = str(candidate.get("id") or candidate.get("secret_id") or "")
            if not secret_id or secret_id in seen:
                continue
            seen.add(secret_id)
            result.append(redact_value(candidate))
        if "secret_id" in event and str(event.get("secret_id")) not in seen:
            seen.add(str(event.get("secret_id")))
            result.append({"id": event.get("secret_id"), "label": event.get("secret_label")})
        if "[REDACTED:secret]" in text and "unknown" not in seen:
            seen.add("unknown")
            result.append({"id": "unknown", "label": "Secret used"})
    return result
