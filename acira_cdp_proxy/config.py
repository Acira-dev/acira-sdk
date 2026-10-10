from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audit_config import AuditConfig, parse_audit_config
from .bounded_regex import compile_pattern, MAX_PATTERNS
from .framework_adapters import AdapterRuntimeConfig, parse_adapter_configs
from .general_skills import GeneralSkill, normalize_risk_policy
from .migration_validator import check_config
from .secrets import AciraSecretsConfig, parse_secrets_config
from .skills import SpecificSkill, load_specific_skills


DEFAULT_READ_ONLY_JS_ALLOWLIST = (
    "document.title",
    "document.URL",
    "document.readyState",
    "location.href",
    "window.location.href",
    "navigator.userAgent",
    "document.body.innerText",
    "document.body.textContent",
)


@dataclass(frozen=True)
class AciraGuardRule:
    id: str
    actions: tuple[str, ...]
    decision: str
    reason: str | None = None
    enabled: bool = True


@dataclass(frozen=True)
class BlockedRetryPolicy:
    same_target_limit: int = 2
    on_exceed: str = "compact_block"


@dataclass(frozen=True)
class GeneralSkillPreflightConfig:
    enabled: bool = False
    quiet_ms: int = 750
    max_wait_ms: int = 5000
    on_error: str = "block_mutating_until_action_probe"
    on_deny: str = "read_only_quarantine"


@dataclass(frozen=True)
class AciraGuardConfig:
    """Configuration for framework-agnostic CDP guard decisions."""

    enabled: bool = True
    agent_name: str | None = None
    rules: tuple[AciraGuardRule, ...] = ()
    block_arbitrary_js: bool = False
    read_only_js_allowlist: tuple[str, ...] = DEFAULT_READ_ONLY_JS_ALLOWLIST
    runtime_js_mode: str = "hybrid_strict"
    runtime_js_unknown: str = "ask"
    runtime_js_log_snippet_chars: int = 180
    ask_low_confidence_meaningful_actions: bool = False
    terminal_review: bool = False
    terminal_log: bool = True
    terminal_log_read_only: bool = False
    default_review_decision: str = "block"
    probe_timeout: float = 1.0
    blocked_retry_policy: BlockedRetryPolicy = BlockedRetryPolicy()
    specific_skills: tuple[SpecificSkill, ...] = ()
    general_skills: tuple[GeneralSkill, ...] = ()
    # Per-skill decisions: {skill_id: {risk: decision}}. Collisions between skills are
    # impossible by structure. has_page_policy is True when any configured risk is page-level
    # (used to keep page preflight opt-in).
    skill_policies: dict[str, dict[str, str]] = field(default_factory=dict)
    has_page_policy: bool = False
    general_skill_preflight: GeneralSkillPreflightConfig = GeneralSkillPreflightConfig()
    hitl_freeze_mode: str = "strict"
    adapters: dict[str, AdapterRuntimeConfig] = field(default_factory=dict)
    target_mapping_store_path: Path | None = None
    secrets: AciraSecretsConfig = field(default_factory=AciraSecretsConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any] | "AciraGuardConfig" | None,
        *,
        base_path: str | Path | None = None,
        source: str | None = None,
    ) -> "AciraGuardConfig":
        if isinstance(data, AciraGuardConfig):
            return data
        if data is None:
            return cls(enabled=False)

        if not isinstance(data, dict):
            raise TypeError("AciraGuardConfig.from_dict expects a dict")

        check_config(data, source=source)

        rules: list[AciraGuardRule] = []
        for item in data.get("rules", []):
            rules.append(_rule_from_dict(item))

        blocked_actions = data.get("blocked_actions") or data.get("block_actions")
        if blocked_actions:
            rules.append(
                AciraGuardRule(
                    id="blocked_actions",
                    actions=_normalize_actions(blocked_actions),
                    decision="block",
                    reason="Acira Guard blocked this CDP action.",
                )
            )

        review_actions = data.get("review_actions") or data.get("ask_actions")
        if review_actions:
            rules.append(
                AciraGuardRule(
                    id="review_actions",
                    actions=_normalize_actions(review_actions),
                    decision="ask",
                    reason="Acira Guard requires review for this CDP action.",
                )
            )

        agent_js = data.get("agent_js") or {}
        if agent_js and not isinstance(agent_js, dict):
            raise TypeError("agent_js must be an object")

        runtime_js = data.get("runtime_js") or {}
        if runtime_js and not isinstance(runtime_js, dict):
            raise TypeError("runtime_js must be an object")

        blocked_retry_policy = data.get("blocked_retry_policy") or {}
        if blocked_retry_policy and not isinstance(blocked_retry_policy, dict):
            raise TypeError("blocked_retry_policy must be an object")

        general_skill_preflight = data.get("general_skill_preflight") or {}
        if general_skill_preflight and not isinstance(general_skill_preflight, dict):
            raise TypeError("general_skill_preflight must be an object")

        allowlist = agent_js.get("read_only_allowlist", data.get("read_only_js_allowlist"))
        if allowlist is not None:
            if not isinstance(allowlist, (list, tuple)) or len(allowlist) > 128 or any(not isinstance(item, str) for item in allowlist):
                raise ValueError("Read-only JS allowlist requires at most 128 string entries")
            expressions = [item[6:] for item in allowlist if item.startswith("regex:")]
            if len(expressions) > MAX_PATTERNS:
                raise ValueError("Read-only JS allowlist supports at most 32 regex patterns")
            for expression in expressions:
                compile_pattern(expression)
        low_confidence = data.get("low_confidence_meaningful_action")
        legacy_specific_keys = [key for key in ("skills", "skill_paths") if key in data]
        if legacy_specific_keys:
            raise ValueError(
                "Legacy specific skill config keys are no longer supported; use specific_skills or specific_skill_paths"
            )

        skill_items: list[Any] = []
        for key in ("specific_skills", "specific_skill_paths"):
            value = data.get(key)
            if value is None:
                continue
            if isinstance(value, list):
                skill_items.extend(value)
            else:
                skill_items.append(value)
        resolved_base_path = Path(base_path) if base_path is not None else None
        specific_skills = load_specific_skills(skill_items, base_path=resolved_base_path)
        general_skills, skill_policies, has_page_policy = _load_general_skill_blocks(
            data, base_path=resolved_base_path
        )

        return cls(
            enabled=bool(data.get("enabled", True)),
            agent_name=_clean_optional_str(data.get("agent_name") or data.get("name")),
            rules=tuple(rules),
            block_arbitrary_js=bool(agent_js.get("block_arbitrary", data.get("block_arbitrary_js", False))),
            read_only_js_allowlist=tuple(allowlist or DEFAULT_READ_ONLY_JS_ALLOWLIST),
            runtime_js_mode=_normalize_runtime_js_mode(runtime_js.get("mode", data.get("runtime_js_mode", "hybrid_strict"))),
            runtime_js_unknown=_normalize_runtime_js_unknown(runtime_js.get("unknown", data.get("runtime_js_unknown", "ask"))),
            runtime_js_log_snippet_chars=_positive_int(
                runtime_js.get("log_snippet_chars", data.get("runtime_js_log_snippet_chars", 180)),
                default=180,
            ),
            ask_low_confidence_meaningful_actions=low_confidence == "ask"
            or bool(data.get("ask_low_confidence_meaningful_actions", False)),
            terminal_review=bool(data.get("terminal_review", False)),
            terminal_log=bool(data.get("terminal_log", True)),
            terminal_log_read_only=bool(data.get("terminal_log_read_only", False)),
            default_review_decision=_normalize_decision(data.get("default_review_decision", "block")),
            probe_timeout=float(data.get("probe_timeout", 1.0)),
            blocked_retry_policy=_blocked_retry_policy_from_dict(blocked_retry_policy),
            specific_skills=specific_skills,
            general_skills=general_skills,
            skill_policies=skill_policies,
            has_page_policy=has_page_policy,
            audit=parse_audit_config(data.get("audit")),
            general_skill_preflight=_general_skill_preflight_from_dict(general_skill_preflight),
            hitl_freeze_mode=_normalize_hitl_freeze_mode(data.get("hitl_freeze_mode", "strict")),
            adapters=parse_adapter_configs(data.get("adapters")),
            target_mapping_store_path=_optional_path(
                data.get("target_mapping_store")
                or data.get("target_mapping_store_path")
                or data.get("mapping_store_path"),
                base_path=resolved_base_path,
            ),
            secrets=parse_secrets_config(_load_secrets_config_block(data, base_path=resolved_base_path)),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "AciraGuardConfig":
        file_path = Path(path)
        text = file_path.read_text(encoding="utf-8")

        if file_path.suffix.lower() in {".yaml", ".yml"}:
            try:
                import yaml  # type: ignore[import-not-found]
            except ModuleNotFoundError as exc:
                raise RuntimeError("YAML Acira config files require PyYAML to be installed") from exc
            loaded = yaml.safe_load(text)
        else:
            loaded = json.loads(text)

        if not isinstance(loaded, dict):
            raise ValueError("Acira config file must contain a JSON/YAML object")
        return cls.from_dict(loaded, base_path=file_path.parent, source=str(file_path))

    @classmethod
    def default_config(cls) -> "AciraGuardConfig":
        from importlib.resources import files
        data = json.loads(files("acira").joinpath("resources/default.json").read_text(encoding="utf-8"))
        for block in data["general_skills"].values():
            block["path"] = str(files("acira").joinpath("resources/policies/" + Path(block["path"]).name))
        return cls.from_dict(data)

    def matching_rules(self, action: str) -> tuple[AciraGuardRule, ...]:
        normalized = _normalize_action(action)
        return tuple(
            rule
            for rule in self.rules
            if rule.enabled and (normalized in rule.actions or "*" in rule.actions)
        )


def _rule_from_dict(data: dict[str, Any]) -> AciraGuardRule:
    if not isinstance(data, dict):
        raise TypeError("Each guard rule must be an object")

    actions = data.get("actions", data.get("action"))
    if actions is None:
        raise ValueError("Guard rule is missing action/actions")

    return AciraGuardRule(
        id=str(data.get("id") or ",".join(_normalize_actions(actions))),
        actions=_normalize_actions(actions),
        decision=_normalize_decision(data.get("decision", "block")),
        reason=data.get("reason") if isinstance(data.get("reason"), str) else None,
        enabled=bool(data.get("enabled", True)),
    )


def _normalize_actions(actions: Any) -> tuple[str, ...]:
    if isinstance(actions, str):
        return (_normalize_action(actions),)
    return tuple(_normalize_action(str(action)) for action in actions)


def _normalize_action(action: str) -> str:
    return action.strip().lower().replace("-", "_").replace(" ", "_")


def _clean_optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalize_decision(decision: Any) -> str:
    normalized = str(decision).strip().lower()
    if normalized not in {"allow", "ask", "block"}:
        raise ValueError(f"Unsupported guard decision: {decision!r}")
    return normalized


def _normalize_runtime_js_mode(mode: Any) -> str:
    normalized = str(mode).strip().lower().replace("-", "_").replace(" ", "_")
    if normalized != "hybrid_strict":
        raise ValueError(f"Unsupported runtime_js.mode: {mode!r}")
    return normalized


def _normalize_runtime_js_unknown(decision: Any) -> str:
    normalized = str(decision).strip().lower()
    if normalized not in {"allow", "ask", "block"}:
        raise ValueError(f"Unsupported runtime_js.unknown decision: {decision!r}")
    return normalized


def _normalize_hitl_freeze_mode(value: Any) -> str:
    normalized = str(value or "strict").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized not in {"strict", "timeout_only", "off"}:
        raise ValueError(f"Unsupported hitl_freeze_mode: {value!r}")
    return normalized


def _blocked_retry_policy_from_dict(data: dict[str, Any]) -> BlockedRetryPolicy:
    on_exceed = str(data.get("on_exceed", "compact_block")).strip().lower().replace("-", "_").replace(" ", "_")
    if on_exceed != "compact_block":
        raise ValueError(f"Unsupported blocked_retry_policy.on_exceed: {on_exceed!r}")
    return BlockedRetryPolicy(
        same_target_limit=_positive_int(data.get("same_target_limit", 2), default=2),
        on_exceed=on_exceed,
    )


def _general_skill_preflight_from_dict(data: dict[str, Any]) -> GeneralSkillPreflightConfig:
    on_error = str(data.get("on_error", "block_mutating_until_action_probe")).strip().lower().replace("-", "_").replace(" ", "_")
    if on_error != "block_mutating_until_action_probe":
        raise ValueError(f"Unsupported general_skill_preflight.on_error: {on_error!r}")
    on_deny = str(data.get("on_deny", "read_only_quarantine")).strip().lower().replace("-", "_").replace(" ", "_")
    if on_deny not in {"read_only_quarantine", "close_page"}:
        raise ValueError(f"Unsupported general_skill_preflight.on_deny: {on_deny!r}")
    return GeneralSkillPreflightConfig(
        enabled=bool(data.get("enabled", False)),
        quiet_ms=_positive_int(data.get("quiet_ms", 750), default=750),
        max_wait_ms=_positive_int(data.get("max_wait_ms", 5000), default=5000),
        on_error=on_error,
        on_deny=on_deny,
    )


def _resolve_skill_path(path: str, base_path: Path | None) -> Path:
    candidate = Path(path)
    if candidate.is_absolute() or base_path is None:
        return candidate
    return base_path / candidate


def _load_general_skill_blocks(
    data: dict[str, Any],
    *,
    base_path: Path | None,
) -> tuple[tuple[GeneralSkill, ...], dict[str, dict[str, str]], bool]:
    """Parse the per-skill ``general_skills`` blocks into loaded skills + their policy.

    Each block is keyed by skill id and is fully self-contained::

        "general_skills": {
            "acira.general.checkout": {
                "path": "../skills/general/checkout.json",
                "risks": {"checkout.card_data_entry": "ask", ...}
            }
        }

    Decisions are scoped per skill, so collisions between skills are impossible by structure.
    The old flat ``risk_policy`` / ``page_risk_policy`` maps and the list form of
    ``general_skills`` are rejected outright — there is exactly one supported format.
    """
    # Legacy forms (flat risk_policy/page_risk_policy, list-form general_skills) are rejected
    # earlier by migration_validator.check_config, which runs at the top of from_dict.
    raw = data.get("general_skills")
    if not raw:
        return (), {}, False
    if not isinstance(raw, dict):
        raise TypeError("general_skills must be an object keyed by skill id")

    skills: list[GeneralSkill] = []
    skill_policies: dict[str, dict[str, str]] = {}
    has_page_policy = False
    for raw_id, block in raw.items():
        block_id = str(raw_id).strip()
        if not block_id:
            raise ValueError("general_skills contains an empty skill id")
        if not isinstance(block, dict):
            raise TypeError(f"general skill block {block_id!r} must be an object")
        path_value = block.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            raise ValueError(f"general skill block {block_id!r} must declare a 'path'")
        skill = GeneralSkill.from_file(_resolve_skill_path(path_value.strip(), base_path))
        if skill.id != block_id:
            raise ValueError(
                f"general skill block key {block_id!r} does not match the loaded skill id "
                f"{skill.id!r} (path: {path_value!r})"
            )
        emitted_context = {emit.risk: emit.context for emit in skill.emits}
        risks_raw = block.get("risks")
        if risks_raw is not None and not isinstance(risks_raw, dict):
            raise TypeError(f"general skill block {block_id!r} 'risks' must be an object")
        policy = normalize_risk_policy(risks_raw) if risks_raw else {}
        unknown = sorted(set(policy) - set(emitted_context))
        if unknown:
            raise ValueError(
                f"general skill block {block_id!r} configures risks not emitted by that skill: {unknown}"
            )
        if any(emitted_context[risk] == "page" for risk in policy):
            has_page_policy = True
        skills.append(skill)
        skill_policies[block_id] = policy
    return tuple(skills), skill_policies, has_page_policy


def _load_secrets_config_block(data: dict[str, Any], *, base_path: Path | None) -> Any:
    inline = data.get("secrets")
    file_value = data.get("secrets_config_path") or data.get("secrets_config_file")
    if isinstance(inline, dict):
        file_value = file_value or inline.get("config_file") or inline.get("file") or inline.get("path")

    external = None
    secrets_base = base_path
    if file_value is not None and str(file_value).strip():
        path = _optional_path(file_value, base_path=base_path)
        if path is None:
            raise ValueError("secrets config path is empty")
        external = _read_structured_config_file(path, label="Acira Secrets Manager config")
        secrets_base = path.parent

    if external is None:
        return _resolve_env_file_paths(inline, secrets_base)
    if inline is None:
        return _resolve_env_file_paths(external, secrets_base)
    if not isinstance(inline, dict):
        raise TypeError("Inline secrets config must be an object when secrets_config_path is used")
    if not isinstance(external, dict):
        raise TypeError("Acira Secrets Manager config file must contain an object")

    merged = dict(external)
    inline_without_path = dict(inline)
    for key in ("config_file", "file", "path"):
        inline_without_path.pop(key, None)
    for key, value in inline_without_path.items():
        if key in {"providers", "items", "entries", "secrets"} and isinstance(value, dict) and isinstance(merged.get(key), dict):
            combined = dict(merged[key])
            combined.update(value)
            merged[key] = combined
        else:
            merged[key] = value
    return _resolve_env_file_paths(merged, secrets_base)


def _resolve_env_file_paths(block: Any, base_path: Path | None) -> Any:
    """Resolve ``env_file`` provider ``env_path`` values to absolute paths.

    The env_path in a secrets config is written relative to the secrets config
    file (or the main config when inline); resolve it here so the provider factory
    receives an absolute path regardless of the agent's working directory.
    """
    if not isinstance(block, dict):
        return block
    providers = block.get("providers")
    if not providers:
        return block
    result = dict(block)
    if isinstance(providers, dict):
        result["providers"] = {key: _resolve_provider_env_file_path(value, base_path) for key, value in providers.items()}
    elif isinstance(providers, list):
        result["providers"] = [_resolve_provider_env_file_path(value, base_path) for value in providers]
    return result


def _resolve_provider_env_file_path(item: Any, base_path: Path | None) -> Any:
    if not isinstance(item, dict):
        return item
    if (item.get("kind") or item.get("type")) != "env_file":
        return item
    env_path = item.get("env_path") or item.get("path")
    if not isinstance(env_path, str) or not env_path.strip():
        return item
    resolved = _optional_path(env_path, base_path=base_path)
    if resolved is None:
        return item
    updated = dict(item)
    updated["env_path"] = str(resolved)
    return updated


def _read_structured_config_file(path: Path, *, label: str) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            raise RuntimeError(f"{label} YAML files require PyYAML to be installed") from exc
        return yaml.safe_load(text)
    return json.loads(text)


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _optional_path(value: Any, *, base_path: Path | None) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    path = Path(str(value))
    if not path.is_absolute() and base_path is not None:
        path = base_path / path
    return path
