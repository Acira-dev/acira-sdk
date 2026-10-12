"""Per-run local enforcement support, durable review, and optional dashboard discovery."""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout

from acira_cdp_proxy.guard import GuardReviewRequest, TerminalReviewBackend

from .journal import Journal
from .local import Registry
from .project import Project, digest


class ProjectRuntime:
    def __init__(self, project: Project, agent: str, *, dashboard: str | bool = "auto", review_backend: Any = None) -> None:
        self.project, self.agent = project, agent
        self.snapshot = project.snapshot(agent)
        self.registration = Registry().register(project.root, project.document()["name"])
        self.journal = Journal(project.root)
        self.run_id = "run-" + uuid4().hex
        self.connect = dashboard is True or (dashboard == "auto" and project.document().get("dashboard", {}).get("connect", True))
        if dashboard not in {True, False, "auto"}:
            raise ValueError("dashboard must be auto, True, or False")
        self.external_review = review_backend
        self.adapter = None
        self.task: asyncio.Task | None = None
        self.connected = False
        self.state = "starting"
        self.error: str | None = None
        self.closed = False
        self.pending: set[str] = set()
        self.last_operation = None
        self._last_retention = 0.0
        self._context_scope = None

    def bind_run_id(self, run_id: str) -> None:
        self.run_id = run_id

    def attach(self, adapter: Any) -> None:
        self.adapter = adapter
        adapter.guard_policy.before_decision = self.before_decision
        adapter.guard_policy.policy_revision = self.snapshot.revision
        adapter.guard_policy.require_bound_review = True
        adapter.guard_policy.durable_event = self.event
        adapter.action_gateway.checkpoint_store = self
        pipeline = getattr(adapter.guard_policy, "_audit_pipeline", None)
        if pipeline is not None:
            def persist_legacy_event(event):
                try:
                    self.event("audit_event", audit=event)
                except Exception as exc:
                    # The legacy bus isolates subscribers; retain the failure so
                    # the common authorization boundary cannot silently continue.
                    self._audit_error = exc
            pipeline.bus.subscribe(persist_legacy_event)
        self.project.remember_revision(self.agent, self.snapshot)

    def event(self, kind: str, **payload: Any) -> str:
        payload.setdefault("run", self.run_id)
        payload.setdefault("agent", self.agent)
        identifier = self.journal.event(kind, **payload)
        states = {"action_authorized":"authorized", "action_dispatched":"dispatched",
                  "browser_call_completed":"browser call completed", "action_outcome_unknown":"outcome unknown"}
        if kind in states or kind == "action_evaluated":
            self.last_operation = {"state":states.get(kind, payload.get("decision")), "operation_id":payload.get("operation_id")}
            self.publish()
        return identifier

    def record_pending(self, record: Any) -> None:
        self.journal.record_pending(record)

    def record_decision(self, review_id: str, **payload: Any) -> None:
        self.event("action_decision", operation_id=review_id, **payload)

    def publish(self) -> None:
        from .capabilities import capabilities
        profile = {"adapter": next(iter(self.snapshot.config.adapters), "unknown")}
        self.journal.update_run(self.run_id, {"agent": self.agent, "project_id": self.registration["id"],
            "revision": self.snapshot.revision,
            "state": "awaiting_review" if self.pending and self.state == "running" else self.state,
            "last_operation": self.last_operation, "pending_reviews": len(self.pending), "connected": self.connected,
            "dashboard_enabled": self.connect, "error": self.error,
            "adapter": profile["adapter"], "capabilities": capabilities(profile["adapter"])})

    def refresh(self) -> None:
        if (self.project.root / ".acira/policy-transaction.json").exists():
            raise ValueError("A policy transaction needs recovery; run acira doctor --recover")
        try:
            snapshot = self.project.snapshot(self.agent)
            if (self.project.root / ".acira/policy-transaction.json").exists():
                raise ValueError("Policy transaction in progress; wait or run acira doctor --recover")
            if self.adapter and snapshot.config.adapters != self.snapshot.config.adapters:
                raise ValueError("Integration changed; restart this agent to use the new adapter")
            if snapshot.revision != self.snapshot.revision:
                self.project.remember_revision(self.agent, snapshot)
                for review_id in tuple(self.pending):
                    self.journal.decide(review_id, "invalidated")
                self.revoke_grants("policy_changed")
                self.snapshot = snapshot
                if self.adapter:
                    policy = self.adapter.guard_policy
                    policy.config = snapshot.config
                    policy.policy_revision = snapshot.revision
                    if hasattr(self.adapter, "secret_broker"):
                        from acira_cdp_proxy.secrets import SecretBroker
                        self.adapter.secret_broker = SecretBroker(snapshot.config.secrets)
                    for name in ("_review_approved_keys", "_review_denied_keys", "_approved_empty_mapping_keys",
                                 "_approved_preflight_urls", "_denied_preflight_urls", "_preflight_snapshots",
                                 "_preflight_snapshots_by_url", "_preflight_quarantine", "_preflight_quarantine_by_url",
                                 "_page_mapping_sessions", "_cached_page_target_probes", "_action_transactions"):
                        value = getattr(policy, name, None)
                        if value is not None:
                            value.clear()
                    for name in ("_page_target_probe_cache", "_page_preflight_cache", "_page_mapping_preflight_cache"):
                        cache = getattr(self.adapter, name, None)
                        if cache is not None:
                            cache.clear()
                self.event("policy_activated", revision=snapshot.revision)
                self.publish()
            if time.monotonic() - self._last_retention > 60:
                from .retention import maintain
                maintain(self.project)
                self._last_retention = time.monotonic()
            self.error = getattr(self, "_integration_error", None)
            if self.error is not None:
                self.state = "failed"
        except (OSError, ValueError, TypeError) as exc:
            self.error = str(exc)
            self.state = "invalid_configuration"
            self.publish()
            raise

    async def before_decision(self) -> None:
        if self.closed:
            raise RuntimeError("This Acira run ended; start a fresh protection context before dispatch")
        if getattr(self, "_integration_error", None) is not None:
            raise RuntimeError(self._integration_error)
        if getattr(self, "_audit_error", None) is not None:
            raise OSError("Required audit persistence failed; repair storage and restart the run") from self._audit_error
        self.refresh()
        if any(profile.get("guard", {}).get("audit", {}).get("mode") == "persistent" for profile in self.project.document()["agents"].values()):
            from .retention import maintain
            maintain(self.project)
        await self.wait_if_paused()

    def fail_integration(self, reason: str) -> None:
        self._integration_error = self.error = reason
        self.state = "failed"
        for review_id in tuple(self.pending):
            self.journal.decide(review_id, "invalidated")
        self.revoke_grants("integration_failed")
        self.event("integration_failed", reason=reason, business_outcome="unknown")
        self.publish()

    async def wait_if_paused(self, **_: Any) -> None:
        while self.journal.control(self.run_id) == "paused":
            if self.state != "paused":
                self.state = "paused"
                self.event("control_acknowledged", desired="paused")
                self.publish()
            await asyncio.sleep(0.1)
            self.refresh()
        if self.state in {"paused", "invalid_configuration"}:
            self.state = "running"
            self.event("control_acknowledged", desired="running")
            self.publish()

    async def start(self) -> None:
        if self.task:
            return
        self.state = "running"
        self.publish()
        self.event("run_started", policy_revision=self.snapshot.revision)
        self.task = asyncio.create_task(self._heartbeat())

    async def _heartbeat(self) -> None:
        while not self.closed:
            try:
                self.refresh()
                if self.connect:
                    from .service import verified_descriptor
                    found = await asyncio.to_thread(verified_descriptor)
                    connected = False
                    if found:
                        async with ClientSession(timeout=ClientTimeout(total=2), trust_env=False) as client:
                            async with client.post(found["url"] + "/api/heartbeat", json={"project_id": self.registration["id"], "run_id": self.run_id},
                                                   headers={"Authorization": "Bearer " + found["sdk_key"]}, allow_redirects=False) as response:
                                connected = response.status == 200
                    if connected != self.connected:
                        self.connected = connected
                        self.event("dashboard_connected" if connected else "dashboard_disconnected")
                self.publish()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.error = str(exc)
                with contextlib.suppress(OSError):
                    self.publish()
            await asyncio.sleep(2 if not self.connected else 5)

    async def close(self, *, reason: str = "Run finished") -> None:
        self.closed = True
        errors = []
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                errors.append(exc)
        for review_id in tuple(self.pending):
            try:
                self.journal.decide(review_id, "invalidated")
            except Exception as exc:
                errors.append(exc)
        self.pending.clear()
        try:
            self.revoke_grants("run_ended")
        except Exception as exc:
            errors.append(exc)
        pipeline = getattr(getattr(self.adapter, "guard_policy", None), "_audit_pipeline", None)
        if pipeline is not None:
            try:
                await asyncio.wait_for(pipeline.aclose(end_reason="completed"), timeout=10)
            except Exception as exc:
                errors.append(exc)
        self.state = "ended"
        self.connected = False
        try:
            self.publish()
            self.event("run_ended", reason=reason)
        except Exception as exc:
            errors.append(exc)
        if errors:
            self._audit_error = errors[0]
            self.state = "failed"
            self.error = "Acira closed its integration, but required shutdown bookkeeping failed; repair storage before starting a fresh run"
            with contextlib.suppress(Exception):
                self.publish()
            raise OSError(self.error) from errors[0]

    def revoke_grants(self, reason: str) -> None:
        with self.journal.db() as db:
            rows = db.execute("SELECT id FROM grants WHERE run=?", (self.run_id,)).fetchall()
            db.execute("DELETE FROM grants WHERE run=?", (self.run_id,))
        for row in rows:
            self.event("grant_revoked", grant_id=row[0], reason=reason)

    def observe_context(self, scope: dict[str, Any] | None) -> None:
        if scope is None:
            return
        key = digest(scope)
        if self._context_scope is not None and key != self._context_scope:
            self.revoke_grants("context_changed")
        self._context_scope = key

    async def ask(self, request: GuardReviewRequest) -> str:
        if self.closed:
            return "block"
        from acira_cdp_proxy.action_gateway import AUTHORIZATION_DEADLINE, PolicyRevisionChanged
        gateway_deadline = AUTHORIZATION_DEADLINE.get()
        deadline = gateway_deadline if gateway_deadline is not None else time.monotonic()+300
        if time.monotonic() >= deadline:
            return "block"
        evidence = (request.classification.details or {}).get("operation_evidence") or {}
        scope = evidence.get("grant_scope")
        cedar = (request.classification.details or {}).get("cedar") or {}
        detection = (cedar.get("general_skill_decision") or {}).get("detection") or {}
        detections = (cedar.get("general_skills") or {}).get("detections") or []
        policies = {document["id"]: document for document in self.snapshot.documents.values()}
        for detected in detections:
            requirements = policies.get(detected.get("skill_id"), {}).get("acira_review", {}).get(detected.get("risk"), {})
            if not requirements:
                continue
            verified = evidence.get("verified_fields", {})
            missing = [field for field in requirements.get("required_fields", []) if verified.get(field) in (None, "", "[REDACTED]")]
            if requirements.get("request_binding") and not evidence.get("request_bound"):
                missing.append("verified request binding")
            if missing:
                self.event("review_evidence_missing", missing=missing, policy_id=detected.get("skill_id"), risk=detected.get("risk"),
                           reason="Use the supported submit() contract to bind the reviewed destination and values")
                if self.snapshot.config.terminal_log:
                    print("[Acira] Review blocked: required operation evidence missing: " + ", ".join(missing) + ". Use guard.submit(...) to bind the actual destination and values; see https://pypi.org/project/acira/#human-review-and-critical-submissions.", file=sys.stderr, flush=True)
                return "block"
        high_risk = detection.get("severity") in {"high", "critical"} or any(item.get("severity") in {"high", "critical"} for item in detections)
        eligible = bool(scope) and not evidence.get("critical", True) and not high_risk
        scope_key = digest({"scope": scope, "run": self.run_id, "revision": self.snapshot.revision}) if eligible else None
        if scope_key:
            with self.journal.db() as db:
                grant = db.execute("SELECT payload FROM grants WHERE id=? AND run=? AND expires>?", (scope_key, self.run_id, time.time())).fetchone()
            if grant and json.loads(grant["payload"]).get("expires_monotonic", 0) > time.monotonic():
                self.event("grant_used", grant_id=scope_key)
                return "allow"
        terminal_available = self.external_review is not None or (self.snapshot.config.terminal_review and sys.stdin.isatty())
        remaining = max(0, math.ceil(deadline - time.monotonic()))
        channel = "your review backend" if self.external_review is not None else "this terminal" if terminal_available else "acira open → Approvals" if self.connect else "no reviewer is available; this action will block"
        notice = f"[Acira] Review required. Respond using {channel}. Blocks after {remaining // 60}:{remaining % 60:02d} without approval."
        if self.connect and not terminal_available and not self.connected:
            notice += " No dashboard is connected yet; open it now, or press Ctrl-C to stop the run."
        if self.snapshot.config.terminal_log:
            print(notice, file=sys.stderr, flush=True)
        from .adapters.playwright_boundary import CAPTURE
        capture = CAPTURE.get()
        if capture is not None:
            evidence = {**evidence, "artifact": self.journal.store_artifact(await capture())}
        elif request.context.send_probe is not None:
            from acira_cdp_proxy.screenshots import capture_masked_cdp
            from acira_dashboard.api_client import _target_box_from_classification
            artifact = await capture_masked_cdp(request.context.send_probe, session_id=request.context.session_id,
                bounding_box=_target_box_from_classification(request.classification.to_metadata()))
            evidence = {**evidence, "artifact": self.journal.store_artifact(artifact)}
        review = self.journal.create_review(self.run_id, {"agent": self.agent, "policy_revision": self.snapshot.revision,
            "reason": request.reason, "classification": request.classification.to_metadata(), "evidence": evidence,
            "grant_eligible": eligible, "artifact": evidence.get("artifact", {"capture_status": "unavailable", "reason": "No masked image was supplied by this integration"})},
            timeout=min(300, deadline-time.monotonic()) if deadline is not None else 300)
        review_id = review["review_id"]
        self.pending.add(review_id)
        self.publish()
        stop_terminal = asyncio.Event()
        terminal_task = None
        if self.external_review is not None:
            terminal_task = asyncio.create_task(self.external_review.ask(request))
        elif self.snapshot.config.terminal_review and sys.stdin.isatty():
            if evidence:
                print("\nAcira operation evidence:", json.dumps(review["evidence"], indent=2), file=sys.stderr, flush=True)
            terminal_task = asyncio.create_task(TerminalReviewBackend(self.snapshot.config).ask_until(request, stop_event=stop_terminal))
        try:
            while True:
                if self.closed:
                    return "block"
                if time.monotonic() >= deadline:
                    self.journal.decide(review_id, "expired")
                    return "block"
                self.refresh()
                if terminal_task and terminal_task.done():
                    answer = await terminal_task
                    allow = answer is True or (isinstance(answer, str) and answer.lower() in {"y", "yes", "allow", "approve", "approved"})
                    self.journal.decide(review_id, "approved" if allow else "rejected")
                    terminal_task = None
                current = self.journal.review(review_id)
                if current["state"] != "pending":
                    if (current["state"] == "invalidated" and gateway_deadline is not None
                            and review["policy_revision"] != self.snapshot.revision):
                        raise PolicyRevisionChanged()
                    if current["state"] == "approved":
                        if scope_key and current.get("grant_requested"):
                            expiry = time.time() + 900
                            with self.journal.db() as db:
                                db.execute("INSERT OR REPLACE INTO grants VALUES(?,?,?,?)", (scope_key, self.run_id, expiry, json.dumps({"scope": scope, "revision": self.snapshot.revision, "expires_monotonic": time.monotonic()+900})))
                            self.event("grant_created", grant_id=scope_key, scope=scope, expires_at=expiry)
                        return "allow"
                    return "block"
                # No dashboard and no interactive channel: never silently approve.
                if not self.connect and terminal_task is None:
                    self.journal.decide(review_id, "rejected")
                    return "block"
                await asyncio.sleep(0.1)
        finally:
            try:
                self.journal.decide(review_id, "invalidated")
            finally:
                self.pending.discard(review_id)
                stop_terminal.set()
                try:
                    if terminal_task and not terminal_task.done():
                        terminal_task.cancel()
                        done, _ = await asyncio.wait((terminal_task,), timeout=1)
                        if done:
                            with contextlib.suppress(asyncio.CancelledError):
                                await terminal_task
                finally:
                    self.publish()


class ProjectContext:
    def __init__(self, adapter: Any, runtime: ProjectRuntime) -> None:
        self.adapter, self.runtime = adapter, runtime
        runtime.attach(adapter)

    async def __aenter__(self) -> Any:
        try:
            return await self.adapter.__aenter__()
        except BaseException:
            await self.runtime.close(reason="Integration failed to start")
            raise

    async def __aexit__(self, *args: Any) -> Any:
        return await self.adapter.__aexit__(*args)
