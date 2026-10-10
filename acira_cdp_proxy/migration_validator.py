"""Migration safety net (PR1).

Hard-errors on stale config/skill files so a developer carrying files from before the
``monitor`` removal / flat-policy era gets a precise, actionable message at load time instead
of a confusing downstream failure. Runs on every config and skill load.

Keep in v1; remove in v0.2 once no external stale files are expected.

Note: the design brief's legacy-policy message reads "use 'skill_policies'", but the
user-facing config key is ``general_skills.<skill-id>.risks`` (``skill_policies`` is the
internal dataclass field, not a config key). The message points at the real key.
"""

from __future__ import annotations

from typing import Any

EXPECTED_SKILL_SCHEMA = "acira.general_skill.v3"
_LEGACY_POLICY_KEYS = ("risk_policy", "page_risk_policy")


class MigrationError(ValueError):
    """Raised when a config or skill file uses a removed or legacy form."""


def _has_monitor(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() == "monitor"


def check_config(data: Any, *, source: str | None = None) -> None:
    """Reject stale config forms. Called at the top of ``AciraGuardConfig.from_dict``."""
    if not isinstance(data, dict):
        return
    label = source or "<config>"

    for key in _LEGACY_POLICY_KEYS:
        if key in data:
            raise MigrationError(
                f"{label}: '{key}' is no longer supported. Configure decisions per skill "
                f"under general_skills.<skill-id>.risks instead."
            )

    general = data.get("general_skills")
    if isinstance(general, list):
        raise MigrationError(
            f"{label}: 'general_skills' must be an object keyed by skill id "
            '({"<skill-id>": {"path": ..., "risks": {...}}}), not a list. '
            "The flat list + risk_policy format has been removed."
        )
    if isinstance(general, dict):
        for block in general.values():
            risks = block.get("risks") if isinstance(block, dict) else None
            if isinstance(risks, dict):
                for risk, decision in risks.items():
                    if _has_monitor(decision):
                        raise MigrationError(
                            f"{label}: risk '{risk}' uses 'monitor', which is no longer a "
                            f"valid decision. Replace with 'allow'."
                        )


def check_skill(data: Any, *, source: str | None = None) -> None:
    """Reject stale skill forms. Called at the top of ``GeneralSkill.from_dict``."""
    if not isinstance(data, dict):
        return
    label = source or "<skill>"

    schema = str(data.get("schema") or "").strip()
    if schema and schema != EXPECTED_SKILL_SCHEMA:
        raise MigrationError(
            f"{label}: skill schema {schema!r} is no longer supported. "
            f"Use {EXPECTED_SKILL_SCHEMA!r} (see docs/audit migration notes)."
        )

    risks = data.get("risks")
    if isinstance(risks, list):
        for risk in risks:
            if not isinstance(risk, dict):
                continue
            decisions = risk.get("decisions")
            if isinstance(decisions, list) and any(_has_monitor(d) for d in decisions):
                raise MigrationError(
                    f"{label}: risk {risk.get('id')!r} lists 'monitor', which is no longer a "
                    f"valid decision. Replace with 'allow'."
                )
