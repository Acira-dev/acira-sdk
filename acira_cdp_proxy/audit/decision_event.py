"""Build a schema-valid ``guard_decision`` event from a guard decision.

This is where the guard's internal, historically-grown decision representation
(``CDPPolicyDecision.metadata["decision"]`` with values like ``allow_after_review`` or
``block_after_cached_mapping_review``) is normalized to the pinned 3-value contract
(``allow|ask|block``) plus a HITL outcome, and the cedar/rule source is mapped to the canonical
``decision_source`` enum. PR1 pinned the enums; PR2 owns this per-path tagging.
"""

from __future__ import annotations

from typing import Any

from ..audit_schema import SCHEMA_VERSION, new_event_id

# cedar_decision.source -> decision_source enum
_SOURCE_MAP = {
    "specific_skill": "specific_skill",
    "specific_skill_missing_target": "specific_skill",
    "general_skill": "general_skill",
    "general_skill_preflight": "page_preflight",
    "secret_policy": "secret_policy",
}

_REVIEW_APPROVED = {"allow", "allow_cached"}
_REVIEW_DENIED = {"block", "block_cached"}
# resolved-review markers in metadata["decision"] (excludes 'block_during_pending_review',
# which is a block caused by a *different* pending review, not this action's review)
_REVIEW_MARKERS = (
    "after_review",
    "after_current_action_review",
    "after_mapping_review",
    "after_cached_review",
    "after_cached_mapping_review",
    "after_preflight_review",
    "after_cached_preflight_review",
)


def _is_review_decision(internal: str, review_result: Any) -> bool:
    if review_result in _REVIEW_APPROVED or review_result in _REVIEW_DENIED:
        return True
    if internal == "ask":
        return True
    return any(marker in internal for marker in _REVIEW_MARKERS)


def classify_decision(metadata: dict, allow: bool) -> tuple[str, str | None]:
    """Map guard decision metadata to (decision, hitl_outcome).

    decision ∈ {allow, ask, block}. A decision that required human review is recorded as
    ``ask`` with the outcome in ``hitl_outcome`` — the schema invariant ties hitl fields to
    ``decision == 'ask'``, and "this needed a human" is the audit-meaningful fact.
    """
    internal = str(metadata.get("decision") or "")
    review_result = metadata.get("review_result")
    if _is_review_decision(internal, review_result):
        if review_result in _REVIEW_APPROVED or internal.startswith("allow") or internal.startswith("continue"):
            return "ask", "approved"
        if review_result in _REVIEW_DENIED or internal.startswith("block"):
            return "ask", "denied"
        return "ask", None  # unresolved ask (e.g. timeout — caller may override)
    if internal.startswith("block"):
        return "block", None
    if internal.startswith("allow"):
        return "allow", None
    return ("allow" if allow else "block"), None


def resolve_decision_source(
    metadata: dict, classification_meta: dict, decision: str, hitl_outcome: str | None
) -> str:
    if metadata.get("security_lockdown"):
        return "security_lockdown"
    if hitl_outcome is not None or metadata.get("review_result"):
        return "human_review"
    source = metadata.get("source")
    if source in _SOURCE_MAP:
        return _SOURCE_MAP[source]
    if metadata.get("rule_id"):
        return "explicit_rule"
    if decision == "allow":
        return "read_only" if classification_meta.get("read_only") else "default_allow"
    return "explicit_rule"


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _target_from_classification(classification_meta: dict) -> dict:
    raw = classification_meta.get("target")
    target = raw if isinstance(raw, dict) else {}
    attrs = target.get("attributes")
    return {
        "url": str(classification_meta.get("url") or target.get("url") or ""),
        "selector_primary": _opt_str(target.get("selector") or target.get("selector_primary")),
        "selector_xpath": _opt_str(target.get("xpath") or target.get("selector_xpath")),
        "selector_computed_css": _opt_str(target.get("selector_computed_css")),
        "element_attributes": attrs if isinstance(attrs, dict) else {},
        "bounding_box": None,
        "frame_path": [],
        "in_shadow_dom": bool(target.get("in_shadow_dom", False)),
        "page_title": str(classification_meta.get("page_title") or ""),
    }


def build_guard_decision_event(
    *,
    method: str | None,
    allow: bool,
    reason: str | None,
    metadata: dict,
    session_id: str,
    agent_id: str,
    config_id: str,
    acira_version: str,
    latency_ms: float,
    now_iso: str,
    event_id: str | None = None,
) -> dict:
    metadata = metadata or {}
    classification_meta = metadata.get("classification") if isinstance(metadata.get("classification"), dict) else {}

    decision, hitl_outcome = classify_decision(metadata, allow)
    decision_source = resolve_decision_source(metadata, classification_meta, decision, hitl_outcome)

    action_type = _opt_str(classification_meta.get("action")) or _opt_str(method) or "unknown"
    skill_id = _opt_str(metadata.get("skill_id")) or _opt_str(classification_meta.get("skill_id"))
    risk_id = _opt_str(metadata.get("risk_id")) or _opt_str(classification_meta.get("risk_id"))

    hitl_review_id = _opt_str(metadata.get("review_cache_key")) if decision == "ask" else None

    # Step 2 keeps the payload to safe action metadata only (no user-typed content); full
    # payload capture lands with the redaction layer (step 3) so nothing unredacted is buffered.
    payload = {"method": method, "action": action_type}

    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id or new_event_id(),
        "timestamp_iso": now_iso,
        "session_id": session_id,
        "agent_id": agent_id,
        "config_id": config_id,
        "acira_version": acira_version,
        "event_type": "guard_decision",
        "action_type": action_type,
        "target": _target_from_classification(classification_meta),
        "skill_id": skill_id,
        "risk_id": risk_id,
        "risk_severity": _opt_str(metadata.get("risk_severity")),
        "decision": decision,
        "decision_source": decision_source,
        "decision_rationale": _opt_str(reason),
        "payload": payload,
        "redactions_applied": [],
        "latency_ms": float(latency_ms),
        "hitl_review_id": hitl_review_id,
        "hitl_outcome": hitl_outcome,
        "snapshot_ref": None,
        "snapshot_error": None,
    }
