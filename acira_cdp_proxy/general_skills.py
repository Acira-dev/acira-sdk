from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .facts import SAFE_ATTRIBUTE_KEYS
from .migration_validator import check_skill
from .redaction import redact_value


GENERAL_SKILL_SCHEMA = "acira.general_skill.v3"
VALID_DOMAINS = {"page", "element", "target", "form", "action", "value", "network", "session", "secret"}
VALID_SEVERITIES = {"low", "medium", "high", "critical"}
RISK_DECISION_VALUES = ("allow", "ask", "block")
VALID_RISK_DECISIONS = set(RISK_DECISION_VALUES)
VALID_RISK_CONTEXTS = {"page", "action"}
FORBIDDEN_KEYS = {"code", "script", "js", "javascript", "function", "handler", "eval", "import", "require", "webhook", "url_fetch"}
MAX_SKILL_BYTES = 128 * 1024
MAX_EXPRESSION_DEPTH = 8
MAX_RULES = 64
MAX_EMITS = 64
MAX_TOKEN_LIST = 64
MAX_LEXICONS = 64
MAX_COLLECTION_SCAN = 200

ROOT_FACT_PATHS = {
    "method",
    "url",
    "action.method",
    "action.action",
    "action.reason",
    "action.confidence",
    "action.read_only",
    "action.meaningful",
    "action.tokens",
    "target.tag",
    "target.type",
    "target.role",
    "target.kind",
    "target.text",
    "target.id",
    "target.name",
    "target.aria_label",
    "target.title",
    "target.placeholder",
    "target.url",
    "target.href",
    "target.nearby_text",
    "target.ancestor_text",
    "target.section_heading",
    "target.form_id",
    "target.form_name",
    "target.form_action",
    "target.is_button",
    "target.is_editable",
    "target.is_form_control",
    "target.is_submit",
    "target.tokens",
    "target.identity_tokens",
    "target.nearby_tokens",
    "target.section_tokens",
    "target.form_tokens",
    "element.tag",
    "element.type",
    "element.role",
    "element.kind",
    "element.text",
    "element.id",
    "element.name",
    "element.aria_label",
    "element.title",
    "element.placeholder",
    "element.url",
    "element.href",
    "element.nearby_text",
    "element.ancestor_text",
    "element.section_heading",
    "element.form_id",
    "element.form_name",
    "element.form_action",
    "element.is_button",
    "element.is_editable",
    "element.is_form_control",
    "element.is_submit",
    "element.tokens",
    "element.identity_tokens",
    "element.nearby_tokens",
    "element.section_tokens",
    "element.form_tokens",
    "page.url",
    "page.hostname",
    "page.title",
    "page.tokens",
    "page.url_tokens",
    "page.title_tokens",
    "page.control_tokens",
    "page.target_count",
    "page.controls",
    "form.tokens",
    "value.present",
    "value.length",
    "value.classes",
    "network.tokens",
    "session.recent_actions",
    "secret.present",
    "secret.id",
    "secret.label",
    "secret.provider",
    "secret.ref",
    "secret.classification",
    "secret.tags",
    "secret.policy_decision",
}
ROOT_FACT_PATHS |= {f"target.attributes.{key}" for key in SAFE_ATTRIBUTE_KEYS}
ROOT_FACT_PATHS |= {f"element.attributes.{key}" for key in SAFE_ATTRIBUTE_KEYS}

CONTROL_FACT_PATHS = {
    "tag",
    "type",
    "role",
    "kind",
    "label",
    "id",
    "name",
    "form_id",
    "section",
    "is_button",
    "is_editable",
    "is_form_control",
    "is_submit",
    "is_disabled",
    "is_hidden",
    "tokens",
    "identity_tokens",
    "nearby_tokens",
    "section_tokens",
    "form_tokens",
}
CONTROL_FACT_PATHS |= {f"attributes.{key}" for key in SAFE_ATTRIBUTE_KEYS}

COLLECTION_PATHS = {
    "page.controls": "control",
}


@dataclass(frozen=True)
class GeneralSkillEmit:
    """One risk a general skill can fire. The skill JSON declares these in a top-level
    ``risks`` array; each is the single source of truth for the risk (what it means, the
    context it fires in, valid decisions) plus its detection wiring."""

    risk: str
    context: str
    severity: str
    description: str
    when: tuple[str, ...]
    reason: str
    suppress_when: tuple[str, ...] = ()
    confidence_from: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "risk": self.risk,
            "context": self.context,
            "severity": self.severity,
            "description": self.description,
            "decisions": list(RISK_DECISION_VALUES),
            "when": list(self.when),
            "reason": self.reason,
            "suppress_when": list(self.suppress_when),
            "confidence_from": self.confidence_from,
        }


@dataclass(frozen=True)
class GeneralSkill:
    id: str
    name: str
    version: int
    requires: tuple[str, ...]
    lexicons: dict[str, tuple[str, ...]]
    rules: dict[str, dict[str, Any]]
    emits: tuple[GeneralSkillEmit, ...]
    source: str | None = None
    review_requirements: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source: str | None = None) -> "GeneralSkill":
        if not isinstance(data, dict):
            raise TypeError("General skill must be an object")
        check_skill(data, source=source)
        _reject_executable_keys(data)
        schema = str(data.get("schema") or "").strip()
        if schema != GENERAL_SKILL_SCHEMA:
            raise ValueError(f"Unsupported general skill schema: {schema!r}")

        skill_id = _required_id(data.get("id"), "general skill id")
        lexicons = _normalize_lexicons(data.get("lexicons", {}))
        raw_rules = data.get("rules")
        if not isinstance(raw_rules, dict) or not raw_rules:
            raise ValueError("General skill v2 must define at least one rule")
        if len(raw_rules) > MAX_RULES:
            raise ValueError(f"General skill has too many rules; max={MAX_RULES}")

        rule_names = set(raw_rules)
        rules: dict[str, dict[str, Any]] = {}
        for name, expression in raw_rules.items():
            rule_name = _required_id(name, "rule name")
            if not isinstance(expression, dict):
                raise TypeError(f"General skill rule {rule_name!r} must be an object")
            _validate_expression(expression, depth=0, rule_names=rule_names, lexicons=lexicons, path_context="root")
            rules[rule_name] = dict(expression)

        raw_risks = data.get("risks")
        if not isinstance(raw_risks, list) or not raw_risks:
            raise ValueError("General skill must declare at least one risk in 'risks'")
        if len(raw_risks) > MAX_EMITS:
            raise ValueError(f"General skill has too many risks; max={MAX_EMITS}")

        emits = tuple(_emit_from_dict(item, set(rules)) for item in raw_risks)
        requires = tuple(_normalize_requires(data.get("requires", ())))

        return cls(
            id=skill_id,
            name=str(data.get("name") or skill_id),
            version=int(data.get("version") or 2),
            requires=requires,
            lexicons=lexicons,
            rules=rules,
            emits=emits,
            source=source,
            review_requirements=dict(data.get("acira_review", {})),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "GeneralSkill":
        file_path = Path(path)
        text = file_path.read_text(encoding="utf-8")
        if len(text.encode("utf-8")) > MAX_SKILL_BYTES:
            raise ValueError(f"General skill file is too large: {file_path}")
        loaded = json.loads(text)
        if not isinstance(loaded, dict):
            raise ValueError("General skill file must contain a JSON object")
        return cls.from_dict(loaded, source=str(file_path))


@dataclass(frozen=True)
class RuleResult:
    name: str
    matched: bool
    confidence: float
    matched_facts: tuple[str, ...] = ()

    def to_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "matched": self.matched,
            "confidence": self.confidence,
            "matched_facts": list(self.matched_facts),
        }


@dataclass(frozen=True)
class GeneralSkillDetection:
    skill_id: str
    skill_name: str
    risk: str
    context: str
    severity: str
    reason: str
    confidence: float
    matched_rules: tuple[str, ...]
    source: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "skill_name": self.skill_name,
            "risk": self.risk,
            "context": self.context,
            "severity": self.severity,
            "reason": self.reason,
            "confidence": self.confidence,
            "matched_rules": list(self.matched_rules),
            "source": self.source,
        }


@dataclass(frozen=True)
class GeneralSkillEvaluation:
    detections: tuple[GeneralSkillDetection, ...] = ()
    rules: tuple[RuleResult, ...] = ()

    def to_metadata(self) -> dict[str, Any]:
        rule_metadata = [rule.to_metadata() for rule in self.rules]
        return {
            "detections": [detection.to_metadata() for detection in self.detections],
            "rules": rule_metadata,
        }


def load_general_skills(items: Any, *, base_path: Path | None = None) -> tuple[GeneralSkill, ...]:
    if items is None:
        return ()
    if isinstance(items, (str, Path, dict)):
        iterable = (items,)
    elif isinstance(items, list):
        iterable = tuple(items)
    else:
        raise TypeError("general skills must be a list, object, or path")

    skills: list[GeneralSkill] = []
    for item in iterable:
        if isinstance(item, (str, Path)):
            skills.append(GeneralSkill.from_file(_resolve_path(item, base_path)))
            continue
        if not isinstance(item, dict):
            raise TypeError("general skill entries must be objects or paths")
        if "path" in item and len(item.keys() - {"path"}) == 0:
            skills.append(GeneralSkill.from_file(_resolve_path(str(item["path"]), base_path)))
            continue
        skills.append(GeneralSkill.from_dict(item))
    return tuple(skills)


def evaluate_general_skills(skills: tuple[GeneralSkill, ...], facts: dict[str, Any]) -> GeneralSkillEvaluation:
    detections: list[GeneralSkillDetection] = []
    rule_results: list[RuleResult] = []
    for skill in skills:
        evaluated: dict[str, RuleResult] = {}
        for name, expression in skill.rules.items():
            result = _evaluate_rule(name, expression, facts, evaluated, skill.lexicons, skill.rules, ())
            evaluated[name] = result
            rule_results.append(result)
        for emit in skill.emits:
            suppressions = [evaluated[name] for name in emit.suppress_when if name in evaluated and evaluated[name].matched]
            if suppressions:
                continue
            matches = [evaluated[name] for name in emit.when if name in evaluated and evaluated[name].matched]
            if len(matches) != len(emit.when):
                continue
            if emit.confidence_from and emit.confidence_from in evaluated:
                confidence = evaluated[emit.confidence_from].confidence
            else:
                confidence = min((match.confidence for match in matches), default=0.0)
            detections.append(
                GeneralSkillDetection(
                    skill_id=skill.id,
                    skill_name=skill.name,
                    risk=emit.risk,
                    context=emit.context,
                    severity=emit.severity,
                    reason=emit.reason,
                    confidence=confidence,
                    matched_rules=tuple(match.name for match in matches),
                    source=skill.source,
                )
            )
    return GeneralSkillEvaluation(detections=tuple(detections), rules=tuple(rule_results))


def normalize_risk_policy(data: Any) -> dict[str, str]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise TypeError("risk_policy must be an object")
    normalized: dict[str, str] = {}
    for risk, decision in data.items():
        risk_id = _required_id(risk, "risk policy id")
        normalized_decision = str(decision).strip().lower()
        if normalized_decision not in VALID_RISK_DECISIONS:
            raise ValueError(f"Unsupported risk policy decision for {risk_id!r}: {decision!r}")
        normalized[risk_id] = normalized_decision
    return normalized


def redact_general_skill_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return redact_value(metadata)


def _evaluate_rule(
    name: str,
    expression: dict[str, Any],
    facts: dict[str, Any],
    rule_cache: dict[str, RuleResult],
    lexicons: dict[str, tuple[str, ...]],
    all_rules: dict[str, dict[str, Any]],
    stack: tuple[str, ...],
) -> RuleResult:
    cached = rule_cache.get(name)
    if cached is not None:
        return cached
    if name in stack:
        return RuleResult(name=name, matched=False, confidence=0.0)
    result = _evaluate_expression(expression, facts, rule_cache, lexicons, all_rules, (*stack, name))
    rule = RuleResult(
        name=name,
        matched=result.matched,
        confidence=result.confidence,
        matched_facts=result.matched_facts,
    )
    rule_cache[name] = rule
    return rule


@dataclass(frozen=True)
class _ExpressionResult:
    matched: bool
    confidence: float
    matched_facts: tuple[str, ...] = ()


def _evaluate_expression(
    expression: dict[str, Any],
    facts: dict[str, Any],
    rule_cache: dict[str, RuleResult],
    lexicons: dict[str, tuple[str, ...]],
    all_rules: dict[str, dict[str, Any]],
    stack: tuple[str, ...],
) -> _ExpressionResult:
    if "all" in expression:
        children = [_evaluate_expression(child, facts, rule_cache, lexicons, all_rules, stack) for child in _expression_list(expression["all"])]
        matched = all(child.matched for child in children)
        return _combine(children, matched=matched, confidence=min((child.confidence for child in children), default=0.0))
    if "any" in expression:
        children = [_evaluate_expression(child, facts, rule_cache, lexicons, all_rules, stack) for child in _expression_list(expression["any"])]
        matched_children = [child for child in children if child.matched]
        if not matched_children:
            return _combine(children, matched=False, confidence=0.0)
        return _combine(matched_children, matched=True, confidence=max(child.confidence for child in matched_children))
    if "not" in expression:
        child = _evaluate_expression(_expression_object(expression["not"]), facts, rule_cache, lexicons, all_rules, stack)
        return _ExpressionResult(matched=not child.matched, confidence=1.0 if not child.matched else 0.0)
    if "rule" in expression:
        rule_name = str(expression["rule"])
        rule = rule_cache.get(rule_name)
        if rule is None:
            rule_expression = all_rules.get(rule_name)
            if rule_expression is None:
                return _ExpressionResult(False, 0.0)
            rule = _evaluate_rule(rule_name, rule_expression, facts, rule_cache, lexicons, all_rules, stack)
        return _ExpressionResult(rule.matched, rule.confidence, rule.matched_facts)
    if "exists" in expression:
        path = _path_from_unary(expression["exists"])
        value = _path(facts, path)
        matched = _exists(value)
        return _ExpressionResult(matched, 1.0 if matched else 0.0, (path,) if matched else ())
    if "missing" in expression:
        path = _path_from_unary(expression["missing"])
        value = _path(facts, path)
        matched = not _exists(value)
        return _ExpressionResult(matched, 1.0 if matched else 0.0, (path,) if matched else ())
    if "equals" in expression:
        spec = _expression_object(expression["equals"])
        path = str(spec.get("path") or "")
        expected = spec.get("value")
        matched = _path(facts, path) == expected
        return _ExpressionResult(matched, 1.0 if matched else 0.0, (path,) if matched else ())
    if "not_equals" in expression:
        spec = _expression_object(expression["not_equals"])
        path = str(spec.get("path") or "")
        expected = spec.get("value")
        matched = _path(facts, path) != expected
        return _ExpressionResult(matched, 1.0 if matched else 0.0, (path,) if matched else ())
    if "in" in expression:
        return _evaluate_in(expression["in"], facts, negate=False)
    if "not_in" in expression:
        return _evaluate_in(expression["not_in"], facts, negate=True)
    if "contains_any" in expression:
        return _evaluate_contains(expression["contains_any"], facts, lexicons, require_all=False)
    if "contains_all" in expression:
        return _evaluate_contains(expression["contains_all"], facts, lexicons, require_all=True)
    if "starts_with" in expression:
        return _evaluate_string_affix(expression["starts_with"], facts, mode="starts_with")
    if "ends_with" in expression:
        return _evaluate_string_affix(expression["ends_with"], facts, mode="ends_with")
    if "number_gt" in expression:
        return _evaluate_number(expression["number_gt"], facts, op="gt")
    if "number_gte" in expression:
        return _evaluate_number(expression["number_gte"], facts, op="gte")
    if "number_lt" in expression:
        return _evaluate_number(expression["number_lt"], facts, op="lt")
    if "number_lte" in expression:
        return _evaluate_number(expression["number_lte"], facts, op="lte")
    if "number_between" in expression:
        return _evaluate_number_between(expression["number_between"], facts)
    if "confidence_at_least" in expression:
        return _evaluate_number(expression["confidence_at_least"], facts, op="gte")
    if "token_match" in expression:
        return _evaluate_token_match(expression["token_match"], facts, lexicons)
    if "any_where" in expression:
        return _evaluate_collection(expression["any_where"], facts, lexicons, all_rules, stack, mode="any")
    if "none_where" in expression:
        return _evaluate_collection(expression["none_where"], facts, lexicons, all_rules, stack, mode="none")
    if "count_where" in expression:
        return _evaluate_collection(expression["count_where"], facts, lexicons, all_rules, stack, mode="count")
    if "within_recent_actions" in expression:
        actions = _path(facts, "session.recent_actions")
        wanted = {str(item).lower() for item in _string_list(expression["within_recent_actions"])}
        matched = isinstance(actions, list) and bool(wanted & {str(item).lower() for item in actions})
        return _ExpressionResult(matched, 1.0 if matched else 0.0, tuple(f"session.recent_actions:{item}" for item in sorted(wanted)) if matched else ())
    return _ExpressionResult(False, 0.0)


def _combine(children: list[_ExpressionResult], *, matched: bool, confidence: float) -> _ExpressionResult:
    facts: list[str] = []
    for child in children:
        facts.extend(child.matched_facts)
    return _ExpressionResult(matched, confidence, tuple(_dedupe(facts)))


def _evaluate_in(spec_value: Any, facts: dict[str, Any], *, negate: bool) -> _ExpressionResult:
    spec = _expression_object(spec_value)
    path = str(spec.get("path") or "")
    values = {str(item).lower() for item in _string_list(spec.get("values"))}
    actual = _path(facts, path)
    matched = str(actual or "").lower() in values
    if negate:
        matched = not matched
    return _ExpressionResult(matched, 1.0 if matched else 0.0, (path,) if matched else ())


def _evaluate_contains(spec_value: Any, facts: dict[str, Any], lexicons: dict[str, tuple[str, ...]], *, require_all: bool) -> _ExpressionResult:
    spec = _expression_object(spec_value)
    path = str(spec.get("path") or "")
    wanted = _tokens_from_spec(spec, lexicons)
    observed = _token_values(_path(facts, path))
    if not wanted:
        return _ExpressionResult(False, 0.0)
    matches = observed & wanted
    matched = wanted <= observed if require_all else bool(matches)
    facts_out = tuple(f"{path}:{token}" for token in sorted(matches)) if matched else ()
    confidence = min(1.0, max(_bounded_float(spec.get("confidence"), default=0.0), 0.5 + 0.1 * len(matches)))
    return _ExpressionResult(matched, confidence if matched else 0.0, facts_out)


def _evaluate_string_affix(spec_value: Any, facts: dict[str, Any], *, mode: str) -> _ExpressionResult:
    spec = _expression_object(spec_value)
    path = str(spec.get("path") or "")
    values = tuple(str(item).lower() for item in _string_list(spec.get("values") or spec.get("prefixes") or spec.get("suffixes")))
    actual = str(_path(facts, path) or "").lower()
    if not actual or not values:
        return _ExpressionResult(False, 0.0)
    matched = any(actual.startswith(value) for value in values) if mode == "starts_with" else any(actual.endswith(value) for value in values)
    return _ExpressionResult(matched, 1.0 if matched else 0.0, (path,) if matched else ())


def _evaluate_number(spec_value: Any, facts: dict[str, Any], *, op: str) -> _ExpressionResult:
    spec = _expression_object(spec_value)
    path = str(spec.get("path") or "")
    actual = _number(_path(facts, path))
    expected = _number(spec.get("value"))
    if actual is None or expected is None:
        return _ExpressionResult(False, 0.0)
    matched = {
        "gt": actual > expected,
        "gte": actual >= expected,
        "lt": actual < expected,
        "lte": actual <= expected,
    }[op]
    return _ExpressionResult(matched, 1.0 if matched else 0.0, (path,) if matched else ())


def _evaluate_number_between(spec_value: Any, facts: dict[str, Any]) -> _ExpressionResult:
    spec = _expression_object(spec_value)
    path = str(spec.get("path") or "")
    actual = _number(_path(facts, path))
    minimum = _number(spec.get("min"))
    maximum = _number(spec.get("max"))
    if actual is None:
        return _ExpressionResult(False, 0.0)
    matched = (minimum is None or actual >= minimum) and (maximum is None or actual <= maximum)
    return _ExpressionResult(matched, 1.0 if matched else 0.0, (path,) if matched else ())


def _evaluate_token_match(spec_value: Any, facts: dict[str, Any], lexicons: dict[str, tuple[str, ...]]) -> _ExpressionResult:
    spec = _expression_object(spec_value)
    paths = _string_list(spec.get("paths") or spec.get("path"))
    wanted = _tokens_from_spec(spec, lexicons)
    if not paths or not wanted:
        return _ExpressionResult(False, 0.0)
    observed: set[str] = set()
    matched_facts: list[str] = []
    for path in paths:
        tokens = _token_values(_path(facts, path))
        observed |= tokens
        matched_facts.extend(f"{path}:{token}" for token in sorted(tokens & wanted))
    matches = observed & wanted
    min_matches = _positive_int(spec.get("min_matches"), default=1)
    if len(matches) < min_matches:
        return _ExpressionResult(False, 0.0, tuple(matched_facts))
    base = _bounded_float(spec.get("base_confidence"), default=0.0)
    per_match = _bounded_float(spec.get("per_match_confidence"), default=0.24)
    confidence = min(1.0, max(_bounded_float(spec.get("confidence"), default=0.0), base + per_match * len(matches)))
    return _ExpressionResult(True, confidence, tuple(_dedupe(matched_facts)))


def _evaluate_collection(
    spec_value: Any,
    facts: dict[str, Any],
    lexicons: dict[str, tuple[str, ...]],
    all_rules: dict[str, dict[str, Any]],
    stack: tuple[str, ...],
    *,
    mode: str,
) -> _ExpressionResult:
    spec = _expression_object(spec_value)
    path = str(spec.get("path") or "")
    items = _path(facts, path)
    where = _expression_object(spec.get("where"))
    if not isinstance(items, list):
        return _ExpressionResult(False, 0.0)
    matches: list[_ExpressionResult] = []
    for item in items[:MAX_COLLECTION_SCAN]:
        if not isinstance(item, dict):
            continue
        result = _evaluate_expression(where, item, {}, lexicons, all_rules, stack)
        if result.matched:
            matches.append(result)
    if mode == "any":
        if not matches:
            return _ExpressionResult(False, 0.0)
        best = max(matches, key=lambda item: item.confidence)
        return _ExpressionResult(True, max(_bounded_float(spec.get("confidence"), default=0.0), best.confidence), _prefix_collection_facts(path, best.matched_facts))
    if mode == "none":
        matched = not matches
        return _ExpressionResult(matched, 1.0 if matched else 0.0)
    minimum = _positive_int(spec.get("min"), default=1)
    maximum_raw = spec.get("max")
    maximum = _positive_int(maximum_raw, default=MAX_COLLECTION_SCAN) if maximum_raw is not None else None
    count = len(matches)
    matched = count >= minimum and (maximum is None or count <= maximum)
    facts_out: list[str] = []
    for result in matches:
        facts_out.extend(_prefix_collection_facts(path, result.matched_facts))
    confidence = min((result.confidence for result in matches), default=0.0)
    return _ExpressionResult(matched, confidence if matched else 0.0, tuple(_dedupe(facts_out)))


def _prefix_collection_facts(path: str, facts: tuple[str, ...]) -> tuple[str, ...]:
    if not facts:
        return (path,)
    return tuple(f"{path}.{fact}" if not fact.startswith(path) else fact for fact in facts)


def _infer_risk_context(when: tuple[str, ...]) -> str:
    # Page-level risks fire during page preflight; everything else is an action-level risk.
    return "page" if "page_preflight" in when else "action"


def _emit_from_dict(data: Any, rule_names: set[str]) -> GeneralSkillEmit:
    if not isinstance(data, dict):
        raise TypeError("General skill risk entries must be objects")
    risk = _required_id(data.get("id") or data.get("risk"), "risk id")
    detection = data.get("detection")
    if not isinstance(detection, dict):
        raise ValueError(f"General skill risk {risk!r} must include a 'detection' object")
    when = tuple(_string_list(detection.get("when")))
    if not when:
        raise ValueError(f"General skill risk {risk!r} detection must reference at least one rule")
    unknown = sorted(set(when) - rule_names)
    if unknown:
        raise ValueError(f"General skill risk {risk!r} references unknown rules: {unknown}")
    suppress_when = tuple(_string_list(detection.get("suppress_when")))
    unknown_suppressions = sorted(set(suppress_when) - rule_names)
    if unknown_suppressions:
        raise ValueError(f"General skill risk {risk!r} references unknown suppress_when rules: {unknown_suppressions}")
    confidence_from = str(detection.get("confidence_from") or "").strip() or None
    if confidence_from and confidence_from not in rule_names:
        raise ValueError(f"General skill risk {risk!r} references unknown confidence_from rule: {confidence_from!r}")
    severity = str(data.get("severity") or "medium").lower()
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"Unsupported general skill severity for {risk!r}: {severity!r}")
    context = str(data.get("context") or "").strip().lower() or _infer_risk_context(when)
    if context not in VALID_RISK_CONTEXTS:
        raise ValueError(f"Unsupported general skill risk context for {risk!r}: {context!r} (expected page or action)")
    declared_decisions = data.get("decisions")
    if declared_decisions is not None:
        for value in _string_list(declared_decisions):
            if value.strip().lower() not in VALID_RISK_DECISIONS:
                raise ValueError(f"General skill risk {risk!r} lists an unsupported decision value: {value!r}")
    reason = str(detection.get("reason") or f"General Acira skill risk detected: {risk}")
    description = str(data.get("description") or reason).strip()
    return GeneralSkillEmit(
        risk=risk,
        context=context,
        severity=severity,
        description=description,
        when=when,
        reason=reason,
        suppress_when=suppress_when,
        confidence_from=confidence_from,
    )


def _validate_expression(
    expression: dict[str, Any],
    *,
    depth: int,
    rule_names: set[str],
    lexicons: dict[str, tuple[str, ...]],
    path_context: str,
) -> None:
    if depth > MAX_EXPRESSION_DEPTH:
        raise ValueError(f"General skill expression is too deep; max={MAX_EXPRESSION_DEPTH}")
    _reject_executable_keys(expression)
    allowed = {
        "all",
        "any",
        "not",
        "rule",
        "exists",
        "missing",
        "equals",
        "not_equals",
        "in",
        "not_in",
        "contains_any",
        "contains_all",
        "token_match",
        "starts_with",
        "ends_with",
        "number_gt",
        "number_gte",
        "number_lt",
        "number_lte",
        "number_between",
        "count_where",
        "any_where",
        "none_where",
        "confidence_at_least",
        "within_recent_actions",
    }
    unknown = set(expression) - allowed
    if unknown:
        raise ValueError(f"Unsupported general skill v2 expression keys: {sorted(unknown)}")
    op_count = len(set(expression) & allowed)
    if op_count != 1:
        raise ValueError("General skill expression must contain exactly one operator")

    op = next(iter(set(expression) & allowed))
    if op in {"all", "any"}:
        children = _expression_list(expression[op])
        if not children:
            raise ValueError(f"General skill {op!r} expression must not be empty")
        for child in children:
            _validate_expression(child, depth=depth + 1, rule_names=rule_names, lexicons=lexicons, path_context=path_context)
    elif op == "not":
        _validate_expression(_expression_object(expression[op]), depth=depth + 1, rule_names=rule_names, lexicons=lexicons, path_context=path_context)
    elif op == "rule":
        if str(expression[op]) not in rule_names:
            raise ValueError(f"General skill references unknown rule: {expression[op]!r}")
    elif op in {"exists", "missing"}:
        _validate_fact_path(_path_from_unary(expression[op]), path_context)
    elif op in {
        "equals",
        "not_equals",
        "in",
        "not_in",
        "contains_any",
        "contains_all",
        "starts_with",
        "ends_with",
        "number_gt",
        "number_gte",
        "number_lt",
        "number_lte",
        "number_between",
        "confidence_at_least",
    }:
        spec = _expression_object(expression[op])
        _validate_fact_path(str(spec.get("path") or ""), path_context)
        if op in {"contains_any", "contains_all", "token_match"}:
            _validate_match_spec(spec, lexicons, op)
        if op in {"number_gt", "number_gte", "number_lt", "number_lte", "confidence_at_least"}:
            _bounded_number(spec.get("value"))
        if op == "number_between":
            if "min" in spec:
                _bounded_number(spec.get("min"))
            if "max" in spec:
                _bounded_number(spec.get("max"))
    elif op == "token_match":
        spec = _expression_object(expression[op])
        for path in _string_list(spec.get("paths") or spec.get("path")):
            _validate_fact_path(path, path_context)
        _validate_match_spec(spec, lexicons, op)
    elif op in {"any_where", "none_where", "count_where"}:
        spec = _expression_object(expression[op])
        path = str(spec.get("path") or "")
        child_context = COLLECTION_PATHS.get(path)
        if child_context is None:
            raise ValueError(f"Unsupported collection fact path: {path!r}")
        _validate_expression(_expression_object(spec.get("where")), depth=depth + 1, rule_names=rule_names, lexicons=lexicons, path_context=child_context)
        if op == "count_where":
            if "min" in spec and _positive_int(spec.get("min"), default=1) > MAX_COLLECTION_SCAN:
                raise ValueError(f"count_where.min is too large; max={MAX_COLLECTION_SCAN}")
            if "max" in spec and _positive_int(spec.get("max"), default=MAX_COLLECTION_SCAN) > MAX_COLLECTION_SCAN:
                raise ValueError(f"count_where.max is too large; max={MAX_COLLECTION_SCAN}")
    elif op == "within_recent_actions":
        if len(_string_list(expression[op])) > MAX_TOKEN_LIST:
            raise ValueError(f"within_recent_actions is too large; max={MAX_TOKEN_LIST}")


def _validate_fact_path(path: str, context: str) -> None:
    if not path:
        raise ValueError("General skill fact path must not be empty")
    allowed = CONTROL_FACT_PATHS if context == "control" else ROOT_FACT_PATHS
    if path not in allowed:
        raise ValueError(f"Unsupported general skill fact path: {path!r}")


def _normalize_requires(value: Any) -> tuple[str, ...]:
    items = _string_list(value)
    unknown = [item for item in items if item not in VALID_DOMAINS]
    if unknown:
        raise ValueError(f"Unknown general skill requirements: {unknown}")
    return tuple(items)


def _normalize_lexicons(value: Any) -> dict[str, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError("general skill lexicons must be an object")
    if len(value) > MAX_LEXICONS:
        raise ValueError(f"General skill has too many lexicons; max={MAX_LEXICONS}")
    normalized: dict[str, tuple[str, ...]] = {}
    for name, raw_tokens in value.items():
        lexicon_name = _required_id(name, "lexicon name")
        tokens = tuple(sorted({item.lower() for item in _string_list(raw_tokens)}))
        if not tokens:
            raise ValueError(f"General skill lexicon {lexicon_name!r} must not be empty")
        normalized[lexicon_name] = tokens
    return normalized


def _validate_match_spec(spec: Any, lexicons: dict[str, tuple[str, ...]], label: str) -> None:
    if not isinstance(spec, dict):
        raise TypeError(f"{label} expression must be an object")
    for key in ("lexicon",):
        for ref in _string_list(spec.get(key)):
            if ref not in lexicons:
                raise ValueError(f"{label} references unknown lexicon: {ref!r}")
    if "min_matches" in spec and _positive_int(spec.get("min_matches"), default=1) > MAX_TOKEN_LIST:
        raise ValueError(f"{label}.min_matches is too large")
    for float_key in ("confidence", "base_confidence", "per_match_confidence"):
        if float_key in spec:
            _bounded_float(spec.get(float_key), default=0.0)


def _reject_executable_keys(data: dict[str, Any]) -> None:
    present = FORBIDDEN_KEYS & {str(key).lower() for key in data.keys()}
    if present:
        raise ValueError(f"General skills are declarative and cannot contain executable keys: {sorted(present)}")
    for value in data.values():
        if isinstance(value, dict):
            _reject_executable_keys(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _reject_executable_keys(item)


def _required_id(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"Missing {label}")
    if len(text) > 160:
        raise ValueError(f"{label} is too long")
    return text


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list):
        raise TypeError("Expected a string or list of strings")
    items = [str(item).strip() for item in value if str(item).strip()]
    if len(items) > MAX_TOKEN_LIST:
        raise ValueError(f"List is too large; max={MAX_TOKEN_LIST}")
    return items


def _positive_int(value: Any, *, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, number)


def _bounded_float(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number < 0.0 or number > 1.0:
        raise ValueError("confidence values must be between 0 and 1")
    return number


def _bounded_number(value: Any) -> float:
    number = _number(value)
    if number is None:
        raise ValueError("numeric operator values must be numbers")
    return number


def _expression_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise TypeError("General skill expression branch must be a list")
    if not all(isinstance(item, dict) for item in value):
        raise TypeError("General skill expression branch entries must be objects")
    return list(value)


def _expression_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("General skill expression branch must be an object")
    return value


def _path_from_unary(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("path") or "")
    raise TypeError("Unary fact operator expects a path string or object")


def _exists(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _path(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _token_values(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        return {str(item).lower() for item in value}
    if isinstance(value, str):
        return {value.lower()}
    return set()


def _tokens_from_spec(spec: dict[str, Any], lexicons: dict[str, tuple[str, ...]]) -> set[str]:
    return set(_string_list(spec.get("tokens") or spec.get("values"))) | _tokens_from_lexicon_ref(spec.get("lexicon"), lexicons)


def _tokens_from_lexicon_ref(value: Any, lexicons: dict[str, tuple[str, ...]]) -> set[str]:
    tokens: set[str] = set()
    for name in _string_list(value):
        tokens |= set(lexicons.get(name, ()))
    return tokens


def _dedupe(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


def _resolve_path(path: str | Path, base_path: Path | None) -> Path:
    file_path = Path(path)
    if not file_path.is_absolute() and base_path is not None:
        file_path = base_path / file_path
    return file_path
