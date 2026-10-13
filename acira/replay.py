"""Replay recorded evidence through the same configured guard decisions as live runs.

Browser/session-dependent decisions are indeterminate without that evidence. No
default confidence or fabricated DOM state is supplied to make a replay succeed.
"""
from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from acira_cdp_proxy.config import AciraGuardConfig
from acira_cdp_proxy.guard import AciraGuardPolicy, ActionClassification, GuardTarget
from acira_cdp_proxy.general_skills import GeneralSkill
from acira_cdp_proxy.skills import SpecificSkill

from .journal import Journal
from .project import Project, SNAPSHOT_JSON_LIMIT, read_json, validate_policy


def revision_config(project: Project, agent: str, revision: str | None) -> AciraGuardConfig:
    project.folders(agent)
    if not revision or revision == "current":
        return project.snapshot(agent).config
    import re
    if not re.fullmatch(r"[a-f0-9]{64}", revision):
        raise ValueError("Invalid policy revision")
    saved = read_json(project.root / ".acira/revisions" / agent / f"{revision}.json", max_bytes=SNAPSHOT_JSON_LIMIT)
    from .project import bundled_default
    data = bundled_default()
    data.update(saved["profile"].get("guard", {}))
    data["adapters"] = {}
    data["general_skills"] = {}
    data["specific_skills"] = []
    data.pop("target_mapping_store", None)
    config = AciraGuardConfig.from_dict(data, base_path=project.root)
    general, specific, decisions = [], [], {}
    disabled = saved["profile"].get("disabled_policies", [])
    for source, document in saved["documents"].items():
        kind, policy_id = validate_policy(document)
        if policy_id in disabled:
            continue
        if kind == "general":
            skill = GeneralSkill.from_dict(document, source=source)
            general.append(skill)
            decisions[skill.id] = {**document.get("acira_decisions", {}), **saved["profile"].get("decisions", {}).get(skill.id, {})}
        else:
            specific.append(SpecificSkill.from_dict(document, source=source))
    return replace(config, general_skills=tuple(general), specific_skills=tuple(specific), skill_policies=decisions)


def _references_native_association(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_references_native_association(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_references_native_association(item) for item in value)
    return value in ("target.is_native_submit", "element.is_native_submit")


def evaluate(config: AciraGuardConfig, event: dict[str, Any]) -> dict[str, Any]:
    facts = event.get("facts")
    classification = event.get("classification")
    metadata = event.get("metadata") or {}
    unavailable = {"specific_skill_mapping_review", "specific_skill_mapping", "specific_skill_mapped", "general_skill_preflight", "specific_skill_missing_target"}
    if (event.get("evaluation_contract") != "acira.action_facts.v1" or not isinstance(facts, dict)
            or not isinstance(classification, dict) or metadata.get("source") in (unavailable - {"specific_skill_mapping"})
            or metadata.get("security_lockdown") or not isinstance(event.get("specific_facts"), dict)):
        return {"decision": "indeterminate", "reason": "Insufficient recorded evidence for this operation or its browser/session-dependent decision"}
    if (classification.get("details") or {}).get("secret_use"):
        return {"decision": "indeterminate", "reason": "Secret provider state was intentionally not recorded; this operation cannot be replayed as a value-bound secret use"}
    action = facts.get("action")
    if not isinstance(action, dict) or not all(key in action for key in ("confidence", "read_only", "meaningful")):
        return {"decision": "indeterminate", "reason": "Missing normalized action facts"}
    target_data = classification.get("target")
    target = GuardTarget(**{key: value for key, value in target_data.items() if key in {f.name for f in fields(GuardTarget)}}) if isinstance(target_data, dict) else None
    selected = {key: value for key, value in classification.items() if key in {f.name for f in fields(ActionClassification)} and key != "target"}
    selected["target"] = target
    item = ActionClassification(**selected)
    # One evaluation implementation; replay supplies frozen specific/general facts.
    guard = AciraGuardPolicy(replace(config, adapters={}, terminal_log=False, terminal_review=False))
    decision, reason, rule, cedar = guard._configured_decision(item, recorded_specific_facts=event["specific_facts"], recorded_general_facts=facts)
    # A final recorded general-policy source can hide the page-wide mapping
    # that resolved another captured control during live authorization. Frozen
    # selected-action facts cannot recreate that browser/session evidence for a
    # candidate revision. Preserve independently proven denials; never present a
    # missing-target review as if it had been the live general-policy decision.
    if decision != "block" and (metadata.get("source") == "specific_skill_mapping"
            or (cedar is not None and cedar.source in unavailable)):
        return {"decision": "indeterminate",
                "reason": "Insufficient recorded mapping or browser/session evidence for this candidate policy decision"}
    # A historical heuristic is_submit flag does not establish native form
    # association. Never manufacture that newer evidence for candidate policies.
    target_facts = facts.get("target")
    if (decision != "block" and action.get("meaningful") and not action.get("read_only")
            and action.get("action") in {"click", "submit", "form_control_activation", "dom_mutation"}
            and any(_references_native_association(skill.rules) for skill in config.general_skills)
            and (not isinstance(target_facts, dict)
                 or not isinstance(target_facts.get("is_native_submit"), bool))):
        return {"decision": "indeterminate", "reason": "Missing recorded native form-association evidence"}
    # Explicit submit contracts always require a fresh review, even when a general
    # configured policy would otherwise allow the click.
    evidence = (item.details or {}).get("operation_evidence") or {}
    if decision == "ask" and cedar is not None:
        detections = ((cedar.metadata or {}).get("general_skills") or {}).get("detections", [])
        policies = {skill.id: skill for skill in config.general_skills}
        for detection in detections:
            skill = policies.get(detection.get("skill_id"))
            requirement = skill.review_requirements.get(detection.get("risk"), {}) if skill else {}
            missing = [name for name in requirement.get("required_fields", [])
                       if (evidence.get("verified_fields") or {}).get(name) in (None, "", "[REDACTED]")]
            if requirement.get("request_binding") and evidence.get("request_bound") is not True:
                missing.append("verified request binding")
            if missing:
                return {"decision":"indeterminate",
                    "reason":"Insufficient recorded approval evidence: "+", ".join(missing),
                    "runtime_behavior":"Critical dispatch remains blocked until evidence is established"}
    if evidence.get("request_bound") is True and decision == "allow":
        decision, reason = "ask", "Explicit one-attempt submission contract requires fresh review"
    return {"decision": decision, "reason": reason, "rule_id": rule.id if rule else None,
            "policy_id": cedar.skill_decision_id if cedar else None, "source": cedar.source if cedar else "configured_guard"}


def compare(project: Project, request: dict[str, Any]) -> dict[str, Any]:
    agent = request.get("agent", "default")
    baseline = revision_config(project, agent, request.get("baseline"))
    candidate = revision_config(project, agent, request.get("candidate"))
    rows = []
    cursor = max(0, int(request.get("after", 0)))
    limit = min(max(int(request.get("limit", 500)), 1), 1000)
    journal = Journal(project.root)
    through = int(request.get("through", journal.latest_sequence()))
    events = journal.events(run=request.get("run"), agent=agent, after=cursor, through=through,
                            kind="action_evaluated", limit=limit+1)
    more = len(events) > limit
    for event in events[:limit]:
        cursor = event["sequence"]
        first, second = evaluate(baseline, event), evaluate(candidate, event)
        rows.append({"event_id": event["event_id"], "run_id": event["run_id"], "created_at": event["created_at"],
                     "recorded_policy_decision": event.get("policy_decision"), "observed_authorization": event.get("decision"),
                     "action": event.get("classification", {}).get("action"), "baseline": first, "candidate": second,
                     "changed": first["decision"] != second["decision"]})
    return {"results": rows, "count": len(rows), "changed": sum(row["changed"] for row in rows),
            "indeterminate": sum(row["candidate"]["decision"] == "indeterminate" for row in rows),
            "next_cursor": cursor if more else None, "through": through,
            "note": "Evaluation of recorded actions; later agent behavior is not predicted."}
