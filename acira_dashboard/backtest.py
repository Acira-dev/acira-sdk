from __future__ import annotations

from pathlib import Path
from typing import Any

from acira_cdp_proxy.config import AciraGuardConfig
from acira_cdp_proxy.decision_engine import CedarDecisionEngine
from acira_cdp_proxy.general_skills import load_general_skills
from acira_cdp_proxy.redaction import redact_value
from acira_cdp_proxy.skills import load_specific_skills

from .merged_skills import MergedSkillStore
from .runtime import DashboardRuntimeStore


SAFE_JSON_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


class DashboardBacktestError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def run_backtest(
    *,
    runtime_store: DashboardRuntimeStore,
    production_dir: Path,
    request: dict[str, Any],
) -> dict[str, Any]:
    session_id = str(request.get("session_id") or request.get("run_id") or "").strip()
    if not session_id:
        raise DashboardBacktestError("Backtest requires session_id.")
    session = runtime_store.get_session(session_id)
    selected = _selected_skills(production_dir=production_dir, request=request)
    engine = CedarDecisionEngine()
    rows: list[dict[str, Any]] = []
    for event in session["events"]:
        facts = _facts_from_event(event)
        if not facts:
            if event.get("action"):
                rows.append({"event_id": event.get("event_id"), "action": event.get("action"), "final": "indeterminate", "reason": "Insufficient recorded normalized evidence"})
            continue
        specific = engine.decide_specific_skills(facts=facts, skills=selected["specific_skills"])
        general = engine.decide_general_skills(
            facts=facts,
            skills=selected["general_skills"],
            skill_policies=selected["skill_policies"],
            default_decision="ask",
        )
        rows.append(
            redact_value(
                {
                    "event_id": event.get("event_id"),
                    "created_at": event.get("created_at"),
                    "action": event.get("action") or facts.get("action", {}).get("action"),
                    "summary": event.get("summary"),
                    "specific": _decision_metadata(specific),
                    "general": _decision_metadata(general),
                    "final": _ranked_final(general.decision, specific.decision),
                }
            )
        )
    return {
        "session_id": session_id,
        "skill_selection": selected["selection"],
        "event_count": len(rows),
        "results": rows,
    }


def _selected_skills(*, production_dir: Path, request: dict[str, Any]) -> dict[str, Any]:
    raw_config_name = str(request.get("config") or request.get("config_name") or "").strip()
    config_name = _safe_json_name(raw_config_name, label="config") if raw_config_name else ""
    decisions: dict[str, str] = {}
    skill_policies: dict[str, dict[str, str]] = {}
    general_refs: list[str] = []
    specific_refs: list[str] = []
    merged_refs: list[str] = []
    configs_dir = production_dir / "configs"

    if config_name:
        config = AciraGuardConfig.from_file(configs_dir / config_name)
        general_refs.extend(str(skill.source) for skill in config.general_skills if skill.source)
        specific_refs.extend(str(skill.source) for skill in config.specific_skills if skill.source)
        for skill_id, risks in config.skill_policies.items():
            skill_policies.setdefault(skill_id, {}).update(risks)

    raw_skills = request.get("skills") if isinstance(request.get("skills"), dict) else {}
    general_refs.extend(_paths_from_names(raw_skills.get("general"), "general"))
    specific_refs.extend(_paths_from_names(raw_skills.get("specific"), "specific"))
    merged_refs.extend(_safe_json_name(str(item), label="merged skill") for item in raw_skills.get("merged", []) if str(item))

    merged_store = MergedSkillStore(production_dir=production_dir)
    if merged_refs:
        compiled = merged_store.expand_config_skills(merged_refs)
        for skill_id, block in compiled["general_skills"].items():
            general_refs.append(block["path"])
            skill_policies.setdefault(skill_id, {}).update(block.get("risks", {}))
        specific_refs.extend(item["path"] for item in compiled["specific_skills"])

    if isinstance(request.get("risk_policy"), dict):
        decisions.update({str(key): str(value).strip().lower() for key, value in request["risk_policy"].items()})

    general_refs = _dedupe(general_refs)
    specific_refs = _dedupe(specific_refs)
    general_skills = load_general_skills(general_refs, base_path=configs_dir)
    specific_skills = load_specific_skills(specific_refs, base_path=configs_dir)

    for risk, decision in decisions.items():
        owners = [skill.id for skill in general_skills if any(emit.risk == risk for emit in skill.emits)]
        if len(owners) > 1:
            raise DashboardBacktestError("Ambiguous risk override; supply skill_policies keyed by policy id")
        if owners:
            skill_policies.setdefault(owners[0], {})[risk] = decision
    for skill_id, risks in request.get("skill_policies", {}).items():
        skill_policies.setdefault(skill_id, {}).update(risks)

    return {
        "general_skills": general_skills,
        "specific_skills": specific_skills,
        "skill_policies": skill_policies,
        "selection": {
            "config": config_name or None,
            "general": general_refs,
            "specific": specific_refs,
            "merged": merged_refs,
        },
    }


def _facts_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
    facts = event.get("facts") if isinstance(event.get("facts"), dict) else {}
    target = event.get("target") if isinstance(event.get("target"), dict) else facts.get("target")
    if not facts or not all(key in facts for key in ("confidence", "read_only", "meaningful")):
        return None
    action_name = str(event.get("action") or facts.get("action") or facts.get("method") or "").strip()
    normalized: dict[str, Any] = dict(facts)
    if target and isinstance(target, dict):
        normalized["target"] = target
        normalized.setdefault("url", target.get("url"))
    normalized["action"] = {
        "action": action_name,
        "method": event.get("method") or facts.get("method") or action_name,
        "reason": event.get("reason") or facts.get("reason"),
        "confidence": facts["confidence"],
        "read_only": facts["read_only"],
        "meaningful": facts["meaningful"],
        "tokens": facts.get("tokens", []),
    }
    return normalized


def _decision_metadata(decision: Any) -> dict[str, Any]:
    return {
        "decision": decision.decision,
        "reason": decision.reason,
        "source": decision.source,
        "skill_decision_id": decision.skill_decision_id,
        "metadata": redact_value(decision.metadata or {}),
    }


def _ranked_final(*decisions: str) -> str:
    ranks = {"allow": 0, "ask": 1, "block": 2}
    return max((str(decision) for decision in decisions), key=lambda item: ranks.get(item, 0))


def _paths_from_names(value: Any, skill_type: str) -> list[str]:
    if not isinstance(value, list):
        return []
    return [f"../skills/{skill_type}/{_safe_json_name(item, label=f'{skill_type} skill')}" for item in value if isinstance(item, str)]


def _safe_json_name(name: str, *, label: str) -> str:
    text = str(name or "").strip()
    if "/" in text or "\\" in text or ".." in text:
        raise DashboardBacktestError(f"Path traversal is not allowed in {label} names.")
    if not text or text.startswith(".") or text.endswith("."):
        raise DashboardBacktestError(f"Invalid {label} file name.")
    if any(char not in SAFE_JSON_NAME_CHARS for char in text):
        raise DashboardBacktestError(f"Invalid {label} file name.")
    if not text.endswith(".json"):
        raise DashboardBacktestError(f"{label} must be a .json file.")
    return text


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
