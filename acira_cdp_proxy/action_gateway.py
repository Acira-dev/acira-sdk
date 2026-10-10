from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from .checkpoint import CheckpointStore, InMemoryCheckpointStore, JsonlCheckpointStore, PendingReviewRecord
from .config import AciraGuardConfig
from .framework_adapters import FrameworkAdapterSpec
from .guard import AciraGuardPolicy, ActionClassification, GuardTarget
from .policy import AGENT_TO_BROWSER, CDPMessageContext, CDPPolicyDecision, CDPProbe


DEFAULT_CHECKPOINT_FILE = Path("runtime") / "acira_checkpoints.jsonl"
AUTHORIZATION_DEADLINE: ContextVar[float | None] = ContextVar("acira_authorization_deadline", default=None)


@dataclass(frozen=True)
class ActionRequest:
    action: str
    framework: str
    target: GuardTarget | None = None
    details: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    confidence: float = 0.82
    read_only: bool = False
    meaningful: bool = True
    run_id: str | None = None
    payload: str | bytes | None = None
    source: str | None = None
    display_name: str | None = None
    opens_cdp_followup_transaction: bool = False
    release_pause_on_review_exit: bool = False
    supervised_runtime: bool = False
    target_resolved: bool | None = None
    target_resolution_source: str | None = None
    target_resolution_confidence: float | None = None
    # When True, the adapter performs its own page-level preflight (e.g. the
    # Playwright adapter via _ensure_page_general_preflight) and the core CDP
    # preflight gate must NOT re-run / re-probe for this action.
    adapter_handles_page_preflight: bool = False
    send_probe: CDPProbe | None = field(default=None, repr=False, compare=False)
    revalidate: Callable[[], Awaitable[bool]] | None = field(default=None, repr=False, compare=False)
    operation_evidence: dict[str, Any] = field(default_factory=dict)
    require_review: bool = False


class AciraActionBlocked(RuntimeError):
    def __init__(self, reason: str, *, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.metadata = metadata or {}


class PolicyRevisionChanged(RuntimeError):
    """A pending review was invalidated before any browser dispatch."""


class ActionGateway:
    """Framework-neutral guarded action executor.

    Framework adapters normalize their action into ActionRequest and delegate
    the execution boundary here. CDP policy enforcement remains underneath as a
    browser-level backstop.
    """

    def __init__(
        self,
        config: AciraGuardConfig | dict[str, Any] | None = None,
        *,
        policy: AciraGuardPolicy | None = None,
        checkpoint_store: CheckpointStore | None = None,
        run_id: str | None = None,
        adapters: tuple[FrameworkAdapterSpec, ...] = (),
    ) -> None:
        self.policy = policy or AciraGuardPolicy(config, adapters=adapters)
        self.checkpoint_store = checkpoint_store or InMemoryCheckpointStore()
        self.run_id = run_id or f"run-{uuid4().hex}"

    async def guard_execute(
        self,
        request: ActionRequest,
        execute: Callable[[], Any | Awaitable[Any]],
        *,
        blocked_result: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> Any:
        authorization_deadline = time.monotonic() + 300
        token = AUTHORIZATION_DEADLINE.set(authorization_deadline)
        try:
            for attempt in range(3):
                review_id = f"review-{uuid4().hex[:16]}"
                try:
                    return await self._guard_execute_once(request, execute, blocked_result=blocked_result,
                        authorization_deadline=authorization_deadline, review_id=review_id)
                except PolicyRevisionChanged:
                    self.checkpoint_store.record_decision(review_id, status="block",
                        reason="Policy changed before dispatch; this evaluation was superseded",
                        metadata={"decision":"policy_invalidated"})
                    if (attempt == 2 or time.monotonic() >= authorization_deadline or request.revalidate is None
                            or not await request.revalidate()):
                        reason = "Operation changed or policy kept changing; request a fresh operation"
                        if blocked_result is not None:
                            return blocked_result(reason, {"decision":"approval_invalidated"})
                        raise AciraActionBlocked(reason)
                    event = getattr(self.checkpoint_store, "event", None)
                    if event is not None:
                        event("operation_reevaluated", operation_id=review_id, policy_revision=self.policy.policy_revision)
        finally:
            AUTHORIZATION_DEADLINE.reset(token)

    async def _guard_execute_once(self, request: ActionRequest, execute: Callable[[], Any | Awaitable[Any]], *,
                                  blocked_result: Callable[[str, dict[str, Any]], Any] | None,
                                  authorization_deadline: float, review_id: str) -> Any:
        observe_context = getattr(self.checkpoint_store, "observe_context", None)
        if observe_context is not None:
            observe_context(request.operation_evidence.get("context_scope"))
        classification = self._classification_from_request(request)
        payload = request.payload if request.payload is not None else self._payload_for_request(request)
        parsed_message = _parse_payload(payload)
        self.checkpoint_store.record_pending(
            PendingReviewRecord(
                review_id=review_id,
                run_id=request.run_id or self.run_id,
                action=request.action,
                framework=request.framework,
                status="pending",
                facts={
                    "action": request.action,
                    "framework": request.framework,
                    "details": dict(request.details),
                    "read_only": request.read_only,
                    "meaningful": request.meaningful,
                },
                target=request.target.to_metadata() if request.target else None,
                reason=request.reason,
            )
        )

        target_quality = _target_resolution_quality(request)
        if _requires_specific_transaction_target(request) and not target_quality["safe_to_authorize"]:
            reason = _unsafe_transaction_target_reason(request, target_quality)
            metadata = {
                "guard": "acira",
                "decision": "block_unsafe_target_resolution",
                "classification": classification.to_metadata(),
                "target_resolution": target_quality,
            }
            self.checkpoint_store.record_decision(
                review_id,
                status="block",
                reason=reason,
                metadata=metadata,
            )
            if hasattr(self.policy, "record_framework_boundary_block"):
                self.policy.record_framework_boundary_block(  # type: ignore[attr-defined]
                    payload,
                    CDPMessageContext(
                        direction=AGENT_TO_BROWSER,
                        connection_id=f"{request.framework}-action",
                        message_number=0,
                        websocket_path=f"/acira/action/{request.framework}",
                        parsed_message=parsed_message,
                        send_probe=request.send_probe,
                    ),
                    classification,
                    reason,
                    metadata,
                )
            if blocked_result is not None:
                return blocked_result(reason, metadata)
            raise AciraActionBlocked(reason, metadata=metadata)

        if self.policy.before_decision is not None:
            await self.policy.before_decision()
        authorized_revision = self.policy.policy_revision
        decision = await self.policy.decide_classification(
            payload,
            CDPMessageContext(
                direction=AGENT_TO_BROWSER,
                connection_id=f"{request.framework}-action",
                message_number=0,
                websocket_path=f"/acira/action/{request.framework}",
                parsed_message=parsed_message,
                send_probe=request.send_probe,
            ),
            classification,
        )

        if request.require_review and decision.allow and (decision.metadata or {}).get("review_result") != "allow":
            context = CDPMessageContext(direction=AGENT_TO_BROWSER, connection_id=f"{request.framework}-action",
                message_number=0, websocket_path=f"/acira/action/{request.framework}", parsed_message=parsed_message, send_probe=request.send_probe)
            approved = await self.policy._ask(payload, context, classification, "Review the exact submission destination and values. This permits one request attempt.")
            decision = CDPPolicyDecision(allow=approved, reason=None if approved else "Submission rejected",
                metadata={**(decision.metadata or {}), "policy_decision": "ask", "review_result": "allow" if approved else "block"})

        self.checkpoint_store.record_decision(
            review_id,
            status="allow" if decision.allow else "block",
            reason=decision.reason,
            metadata=decision.metadata,
        )

        from .facts import build_browser_event_facts
        from .guard import _event_facts_from_classification
        event = getattr(self.checkpoint_store, "event", None)
        if event is not None:
            event("action_evaluated", run=request.run_id or self.run_id, operation_id=review_id,
                  evaluation_contract="acira.action_facts.v1", policy_revision=authorized_revision,
                  facts=build_browser_event_facts(classification, parsed_message).to_dict(),
                  specific_facts=_event_facts_from_classification(classification),
                  classification=classification.to_metadata(), decision="allow" if decision.allow else "block",
                  policy_decision=(decision.metadata or {}).get("policy_decision"), metadata=decision.metadata)

        if not decision.allow:
            reason = decision.reason or "Blocked by Acira policy"
            if hasattr(self.policy, "begin_action_transaction"):
                self.policy.begin_action_transaction(  # type: ignore[attr-defined]
                    framework=request.framework,
                    action=request.action,
                    target=request.target,
                    allow=False,
                    reason=reason,
                    metadata=decision.metadata or {},
                    duration=20.0,
                )
            if blocked_result is not None:
                return blocked_result(reason, decision.metadata or {})
            raise AciraActionBlocked(reason, metadata=decision.metadata)

        if self.policy.before_decision is not None:
            await self.policy.before_decision()
        if time.monotonic() >= authorization_deadline or self.policy.policy_revision != authorized_revision or (request.revalidate is not None and not await request.revalidate()):
            reason = "Reviewed operation or policy changed before dispatch; a fresh review is required."
            self.checkpoint_store.record_decision(review_id, status="block", reason=reason, metadata={"decision": "approval_invalidated"})
            if blocked_result is not None:
                return blocked_result(reason, {"decision": "approval_invalidated"})
            raise AciraActionBlocked(reason)

        from .facts import build_browser_event_facts
        event = getattr(self.checkpoint_store, "event", None)
        if event is not None:
            event("action_authorized", run=request.run_id or self.run_id, operation_id=review_id,
                  policy_revision=authorized_revision, facts=build_browser_event_facts(classification, parsed_message).to_dict(),
                  decision="allow", metadata=decision.metadata)
        transaction_id: str | None = None
        if _should_begin_allowed_transaction(request, decision):
            transaction_id = self.policy.begin_action_transaction(
                framework=request.framework,
                action=request.action,
                target=request.target,
                allow=True,
                reason="approved by human review for this framework action",
                metadata=decision.metadata or {},
                duration=45.0,
            )
        try:
            if event is not None:
                event("action_dispatched", run=request.run_id or self.run_id, operation_id=review_id)
            result = execute()
            if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                result = await result
            if event is not None:
                event("browser_call_completed", run=request.run_id or self.run_id, operation_id=review_id,
                      business_outcome="unverified")
            return result
        except BaseException as exc:
            if event is not None:
                event("action_outcome_unknown", run=request.run_id or self.run_id, operation_id=review_id,
                      reason="Dispatch did not produce a confirmed result; do not automatically retry.")
            if isinstance(exc, PolicyRevisionChanged):
                raise AciraActionBlocked("Outcome unknown after dispatch; automatic retry is prohibited") from exc
            raise
        finally:
            if transaction_id is not None:
                self.policy.end_action_transaction(transaction_id)

    def _classification_from_request(self, request: ActionRequest) -> ActionClassification:
        details = dict(request.details)
        details["operation_evidence"] = request.operation_evidence
        details.setdefault("framework", request.framework)
        details.setdefault("source", request.source or f"{request.framework}.action")
        details["action_capabilities"] = {
            "opens_cdp_followup_transaction": request.opens_cdp_followup_transaction,
            "release_pause_on_review_exit": request.release_pause_on_review_exit,
            "supervised_runtime": request.supervised_runtime,
        }
        if request.adapter_handles_page_preflight:
            details["_acira_adapter_page_preflight"] = True
        details["target_resolution_status"] = {
            "resolved": request.target_resolved,
            "source": request.target_resolution_source,
            "confidence": request.target_resolution_confidence,
            "safe_to_authorize": _target_resolution_quality(request)["safe_to_authorize"],
        }
        if request.display_name:
            details["display_name"] = request.display_name
        return ActionClassification(
            method=f"{request.framework}.{request.action}",
            action=request.action,
            reason=request.reason or _default_reason_for_action(request.action),
            confidence=request.confidence,
            read_only=request.read_only,
            meaningful=request.meaningful,
            target=request.target,
            details=details,
        )

    @staticmethod
    def _payload_for_request(request: ActionRequest) -> str:
        return json.dumps(
            {
                "id": 0,
                "method": f"{request.framework}.{request.action}",
                "params": {
                    "action": request.action,
                    "framework": request.framework,
                    "target": request.target.to_metadata() if request.target else None,
                    "details": request.details,
                },
            },
            separators=(",", ":"),
            ensure_ascii=True,
        )


def _default_reason_for_action(action: str) -> str:
    return {
        "click": "framework click action",
        "form_input": "framework text input action",
        "select": "framework select/dropdown action",
        "navigation": "framework navigation action",
        "keyboard": "framework keyboard action",
    }.get(action, f"framework {action} action")


def _parse_payload(payload: str | bytes) -> dict[str, Any] | None:
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _should_begin_allowed_transaction(request: ActionRequest, decision: CDPPolicyDecision) -> bool:
    if not decision.allow or request.target is None:
        return False
    if _requires_specific_transaction_target(request) and not _target_resolution_quality(request)["safe_to_authorize"]:
        return False
    metadata = decision.metadata or {}
    if _metadata_has_unresolved_skill_mapping(metadata):
        return False
    if request.opens_cdp_followup_transaction:
        return True
    return metadata.get("review_result") in {"allow", "allow_cached"}


def _keeps_allowed_transaction_until_expiry(request: ActionRequest) -> bool:
    return bool(request.opens_cdp_followup_transaction and request.supervised_runtime)


def _metadata_has_unresolved_skill_mapping(metadata: dict[str, Any]) -> bool:
    if metadata.get("review_result") in {"allow", "allow_cached"}:
        return False
    cedar = metadata.get("cedar") if isinstance(metadata.get("cedar"), dict) else {}
    skills = cedar.get("skills") if isinstance(cedar.get("skills"), dict) else {}
    if skills.get("page_mapping_safe_to_forward") is True:
        return False
    missing = skills.get("missing_targets") if isinstance(skills.get("missing_targets"), list) else []
    if missing:
        return True
    mappings = skills.get("page_mapping") if isinstance(skills.get("page_mapping"), list) else []
    return any(
        isinstance(item, dict)
        and item.get("status") in {"ambiguous", "unmapped_no_candidate", "probe_unavailable", "rejected"}
        for item in mappings
    )


TARGET_BEARING_SIDE_EFFECT_ACTIONS = {
    "check",
    "click",
    "file_upload",
    "form_control_activation",
    "form_input",
    "select",
    "submit",
}

GENERIC_TARGET_LABELS = {
    "button",
    "checkbox",
    "combobox",
    "control",
    "element",
    "field",
    "form",
    "input",
    "item",
    "link",
    "option",
    "radio",
    "select",
    "submit",
    "target",
    "text",
    "textarea",
}


def _requires_specific_transaction_target(request: ActionRequest) -> bool:
    return bool(
        request.opens_cdp_followup_transaction
        and request.action in TARGET_BEARING_SIDE_EFFECT_ACTIONS
    )


def _target_resolution_quality(request: ActionRequest) -> dict[str, Any]:
    target = request.target
    label = _target_label_for_quality(target)
    stable_label = _stable_label_for_quality(target)
    context_label = _context_label_for_quality(target)
    resolved = request.target_resolved
    confidence = request.target_resolution_confidence
    if confidence is None and target is not None:
        confidence = target.confidence

    safe = bool(target is not None and stable_label)
    if resolved is False:
        safe = False
    if confidence is not None and confidence < 0.55 and not stable_label:
        safe = False

    return {
        "safe_to_authorize": safe,
        "resolved": resolved,
        "source": request.target_resolution_source,
        "confidence": confidence,
        "label": label,
        "stable_label": stable_label,
        "context_label": context_label,
        "reason": "specific_target" if safe else "generic_or_unresolved_target",
    }


def _unsafe_transaction_target_reason(request: ActionRequest, quality: dict[str, Any]) -> str:
    label = str(quality.get("label") or "").strip()
    target_text = f' target "{label}"' if label else " target"
    action = request.action.replace("_", " ")
    return (
        f"Acira could not safely identify the {action}{target_text}; "
        "the action was not sent. The framework supplied a generic or unresolved target, "
        "so Acira refused to authorize follow-up browser commands."
    )


def _target_label_for_quality(target: GuardTarget | None) -> str:
    if target is None:
        return ""
    for value in (
        target.text,
        target.aria_label,
        target.title,
        _humanize_identifier(target.id),
        _humanize_identifier(target.name),
        target.nearby_text,
        target.type,
        target.role,
        target.tag,
    ):
        label = _clean_quality_label(value)
        if label:
            return label
    return ""


def _stable_label_for_quality(target: GuardTarget | None) -> str:
    if target is None:
        return ""
    for value in (
        target.text,
        target.aria_label,
        target.title,
        _humanize_identifier(target.id),
        _humanize_identifier(target.name),
        target.nearby_text,
        target.section_heading,
        target.parent_label,
    ):
        label = _clean_quality_label(value)
        if label and not _is_generic_target_label(label):
            return label
    return ""


def _context_label_for_quality(target: GuardTarget | None) -> str:
    if target is None:
        return ""
    for value in (target.section_heading, target.nearby_text, target.parent_label):
        label = _clean_quality_label(value)
        if label and not _is_generic_target_label(label):
            return label
    return ""


def _clean_quality_label(value: str | None) -> str:
    text = " ".join(str(value or "").strip().split())
    return _truncate(text, 120) if text else ""


def _is_generic_target_label(value: str) -> bool:
    normalized = " ".join(str(value or "").strip().lower().split())
    return normalized in GENERIC_TARGET_LABELS


def _humanize_identifier(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    spaced = raw.replace("_", " ").replace("-", " ")
    return spaced[:1].upper() + spaced[1:]


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: max(0, limit - 1)]}..."
