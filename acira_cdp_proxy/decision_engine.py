from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .general_skills import GeneralSkill, evaluate_general_skills
from .skills import SpecificSkill, evaluate_specific_skills


@dataclass(frozen=True)
class CedarDecision:
    decision: str
    reason: str
    source: str
    skill_decision_id: str | None = None
    metadata: dict[str, Any] | None = None


class CedarDecisionEngine:
    """Deterministic decision boundary for normalized Acira facts.

    This class is intentionally small and side-effect free. It is the integration
    point for an official Cedar bridge/sidecar; until that bridge is available,
    it evaluates the same Cedar-shaped facts and preserves Cedar-style policy
    precedence in process.
    """

    def decide_specific_skills(
        self,
        *,
        facts: dict[str, Any],
        skills: tuple[SpecificSkill, ...],
    ) -> CedarDecision:
        evaluation = evaluate_specific_skills(skills, facts)
        metadata = {"engine": "cedar", "mode": "deterministic_adapter", "skills": evaluation.to_metadata()}

        block_match = next((match for match in evaluation.matches if match.decision == "block"), None)
        if block_match is not None:
            return CedarDecision(
                decision="block",
                reason=block_match.reason,
                source="specific_skill",
                skill_decision_id=f"{block_match.skill_id}:{block_match.target_id}",
                metadata=metadata,
            )

        ask_match = next((match for match in evaluation.matches if match.decision == "ask"), None)
        if ask_match is not None:
            return CedarDecision(
                decision="ask",
                reason=ask_match.reason,
                source="specific_skill",
                skill_decision_id=f"{ask_match.skill_id}:{ask_match.target_id}",
                metadata=metadata,
            )

        missing = evaluation.missing_targets[0] if evaluation.missing_targets else None
        if missing is not None:
            return CedarDecision(
                decision="ask",
                reason=missing.reason,
                source="specific_skill_missing_target",
                skill_decision_id=f"{missing.skill_id}:{missing.target_id}",
                metadata=metadata,
            )

        return CedarDecision(
            decision="allow",
            reason="No specific Acira skill matched this normalized event.",
            source="specific_skill",
            metadata=metadata,
        )

    def decide_general_skills(
        self,
        *,
        facts: dict[str, Any],
        skills: tuple[GeneralSkill, ...],
        skill_policies: dict[str, dict[str, str]],
        default_decision: str = "ask",
    ) -> CedarDecision:
        evaluation = evaluate_general_skills(skills, facts)
        metadata = {
            "engine": "cedar",
            "mode": "deterministic_adapter",
            "general_skills": evaluation.to_metadata(),
            "facts": _general_fact_summary(facts),
        }
        if not evaluation.detections:
            return CedarDecision(
                decision="allow",
                reason="No general Acira skill risk matched this normalized event.",
                source="general_skill",
                metadata=metadata,
            )

        ranked_decisions = ("block", "ask", "allow")
        for decision in ranked_decisions:
            detection = next(
                (
                    item
                    for item in evaluation.detections
                    # Per-(skill_id, risk) lookup: collisions between skills are impossible by
                    # structure. Unconfigured risks fall back to default_decision — "ask" for
                    # action-level (fail-secure), "allow" for page-level (preflight is opt-in).
                    if skill_policies.get(item.skill_id, {}).get(item.risk, default_decision) == decision
                ),
                None,
            )
            if detection is None:
                continue
            metadata["general_skill_decision"] = {
                "decision": decision,
                "detection": detection.to_metadata(),
                "policy": {"risk": detection.risk, "decision": decision},
            }
            return CedarDecision(
                decision=decision,
                reason=detection.reason,
                source="general_skill",
                skill_decision_id=f"{detection.skill_id}:{detection.risk}",
                metadata=metadata,
            )

        return CedarDecision(
            decision="allow",
            reason="General Acira skill risks matched, but project risk policy allowed them.",
            source="general_skill",
            metadata=metadata,
        )


def _general_fact_summary(facts: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ("target", "page", "form", "action", "value", "network"):
        value = facts.get(key)
        if isinstance(value, dict):
            summary[key] = {
                item_key: item_value
                for item_key, item_value in value.items()
                if item_key
                in {
                    "action",
                    "classes",
                    "confidence",
                    "control_tokens",
                    "identity_tokens",
                    "is_submit",
                    "meaningful",
                    "nearby_tokens",
                    "read_only",
                    "section_tokens",
                    "title_tokens",
                    "tokens",
                    "type",
                    "url_tokens",
                }
            }
    return summary
