from __future__ import annotations

import fnmatch
import json
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


VALID_SKILL_DECISIONS = {"ask", "block"}
SKILL_SCHEMA = "acira.specific_skill.v2"
SEMANTIC_PROFILE_SCHEMA = "acira.semantic_profile.v1"
DEFAULT_SEMANTIC_PROFILE_ID = "acira.web_business_controls.v1"
_PROFILE_DIR = Path(__file__).resolve().parent / "resources" / "semantic_profiles"
_MAX_PROFILE_TOKENS = 512
_MAX_PROFILE_ALIAS_GROUPS = 128
HIGH_RISK_MISSING_TARGET_ACTIONS = {
    "click",
    "submit",
    "form_control_activation",
    "meaningful_cdp",
    "unknown_cdp",
    "unknown_js",
    "navigation",
    "keyboard",
    "mouse",
    "touch",
    "dom_mutation",
    "file_upload",
}
SAFE_UNRELATED_FORM_ACTIONS = {"check", "select", "form_input", "focus"}


@dataclass(frozen=True)
class SemanticProfile:
    """Versioned semantic data used by captured-target matching.

    Core treats these terms as data. It knows how to apply token groups, but not
    which business/security words should be considered meaningful.
    """

    id: str
    version: int
    stopwords: frozenset[str] = frozenset()
    high_value_tokens: frozenset[str] = frozenset()
    alias_groups: tuple[frozenset[str], ...] = ()
    action_groups: dict[str, frozenset[str]] = field(default_factory=dict)
    role_groups: dict[str, frozenset[str]] = field(default_factory=dict)
    role_priority: tuple[str, ...] = ()
    source: str | None = None

    @classmethod
    def empty(cls) -> "SemanticProfile":
        return cls(id="acira.empty", version=1)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source: str | None = None) -> "SemanticProfile":
        if not isinstance(data, dict):
            raise TypeError("Semantic profile must be an object")
        _reject_executable_keys(data)
        schema = str(data.get("schema") or "").strip()
        if schema != SEMANTIC_PROFILE_SCHEMA:
            raise ValueError(f"Unsupported semantic profile schema: {schema!r}")
        profile_id = str(data.get("id") or "").strip()
        if not profile_id:
            raise ValueError("Semantic profile is missing id")
        version = _positive_int(data.get("version"), default=1)
        stopwords = frozenset(_profile_tokens(data.get("stopwords")))
        high_value_tokens = frozenset(_profile_tokens(data.get("high_value_tokens")))
        alias_groups = _profile_token_groups(data.get("alias_groups"))
        action_groups = _profile_group_map(data.get("action_groups"))
        role_groups = _profile_group_map(data.get("role_groups"))
        role_priority = tuple(
            name
            for name in _normalize_strings(data.get("role_priority"))
            if name in role_groups
        )
        _validate_profile_size(stopwords, high_value_tokens, alias_groups, action_groups, role_groups)
        return cls(
            id=profile_id,
            version=version,
            stopwords=stopwords,
            high_value_tokens=high_value_tokens,
            alias_groups=alias_groups,
            action_groups=action_groups,
            role_groups=role_groups,
            role_priority=role_priority,
            source=source,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "SemanticProfile":
        file_path = Path(path)
        loaded = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("Semantic profile file must contain a JSON object")
        return cls.from_dict(loaded, source=str(file_path))


_ACTIVE_SEMANTIC_PROFILE: ContextVar[SemanticProfile] = ContextVar(
    "acira_specific_skill_semantic_profile",
    default=SemanticProfile.empty(),
)
_BUILTIN_SEMANTIC_PROFILES: dict[str, SemanticProfile] | None = None


def load_semantic_profiles(items: Any, *, base_path: Path | None = None) -> tuple[SemanticProfile, ...]:
    if items is None:
        return ()
    if isinstance(items, (str, Path, dict)):
        iterable = (items,)
    elif isinstance(items, (list, tuple)):
        iterable = tuple(items)
    else:
        raise TypeError("semantic profiles must be a list, object, id, or path")

    profiles: list[SemanticProfile] = []
    seen: set[str] = set()
    for item in iterable:
        profile = _load_semantic_profile_item(item, base_path=base_path)
        if profile.id in seen:
            raise ValueError(f"Duplicate semantic profile id: {profile.id}")
        seen.add(profile.id)
        profiles.append(profile)
    return tuple(profiles)


def _load_semantic_profile_item(item: Any, *, base_path: Path | None) -> SemanticProfile:
    if isinstance(item, dict):
        if "path" in item and len(item.keys() - {"path"}) == 0:
            return SemanticProfile.from_file(_resolve_path(str(item["path"]), base_path))
        return SemanticProfile.from_dict(item)
    if isinstance(item, (str, Path)):
        value = str(item)
        builtins = _builtin_semantic_profiles()
        if value in builtins:
            return builtins[value]
        return SemanticProfile.from_file(_resolve_path(value, base_path))
    raise TypeError("semantic profile entries must be objects, ids, or paths")


def _builtin_semantic_profiles() -> dict[str, SemanticProfile]:
    global _BUILTIN_SEMANTIC_PROFILES
    if _BUILTIN_SEMANTIC_PROFILES is None:
        profile = SemanticProfile.from_file(_PROFILE_DIR / "web_business_controls.v1.json")
        _BUILTIN_SEMANTIC_PROFILES = {profile.id: profile}
    return dict(_BUILTIN_SEMANTIC_PROFILES)


def _merge_semantic_profiles(profiles: tuple[SemanticProfile, ...]) -> SemanticProfile:
    if not profiles:
        return _builtin_semantic_profiles()[DEFAULT_SEMANTIC_PROFILE_ID]
    stopwords: set[str] = set()
    high_value_tokens: set[str] = set()
    alias_groups: list[frozenset[str]] = []
    action_groups: dict[str, set[str]] = {}
    role_groups: dict[str, set[str]] = {}
    role_priority: list[str] = []
    for profile in profiles:
        stopwords.update(profile.stopwords)
        high_value_tokens.update(profile.high_value_tokens)
        alias_groups.extend(profile.alias_groups)
        for name, tokens in profile.action_groups.items():
            action_groups.setdefault(name, set()).update(tokens)
        for name, tokens in profile.role_groups.items():
            role_groups.setdefault(name, set()).update(tokens)
        for name in profile.role_priority:
            if name not in role_priority:
                role_priority.append(name)
    return SemanticProfile(
        id="+".join(profile.id for profile in profiles),
        version=max(profile.version for profile in profiles),
        stopwords=frozenset(stopwords),
        high_value_tokens=frozenset(high_value_tokens),
        alias_groups=tuple(alias_groups),
        action_groups={name: frozenset(tokens) for name, tokens in action_groups.items()},
        role_groups={name: frozenset(tokens) for name, tokens in role_groups.items()},
        role_priority=tuple(role_priority),
        source=",".join(profile.source or profile.id for profile in profiles),
    )


@dataclass(frozen=True)
class SpecificSkillTarget:
    """Declarative protected target captured from a real page."""

    id: str
    decision: str
    reason: str
    fingerprint: dict[str, Any]
    threshold: int = 70
    capture: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SpecificSkillTarget":
        if not isinstance(data, dict):
            raise TypeError("Specific skill target must be an object")

        target_id = str(data.get("id") or "").strip()
        if not target_id:
            raise ValueError("Specific skill target is missing id")

        decision = str(data.get("decision") or "").strip().lower()
        if decision not in VALID_SKILL_DECISIONS:
            raise ValueError(f"Unsupported specific skill target decision: {decision!r}")

        fingerprint = data.get("fingerprint") or data.get("match") or {}
        if not isinstance(fingerprint, dict):
            raise TypeError("Specific skill target fingerprint must be an object")

        capture = data.get("capture") or data.get("capture_metadata") or {}
        if capture and not isinstance(capture, dict):
            raise TypeError("Specific skill target capture metadata must be an object")

        return cls(
            id=target_id,
            decision=decision,
            reason=str(data.get("reason") or "Specific Acira skill target matched."),
            fingerprint=_clean_fingerprint(fingerprint),
            threshold=_threshold(data.get("threshold", fingerprint.get("threshold", 70))),
            capture=dict(capture),
        )


@dataclass(frozen=True)
class SpecificSkill:
    """Declarative Acira skill. It contains data only, never executable code."""

    id: str
    name: str
    version: int
    url_patterns: tuple[str, ...]
    targets: tuple[SpecificSkillTarget, ...]
    semantic_profile_ids: tuple[str, ...] = ()
    semantic_profile: SemanticProfile = field(default_factory=SemanticProfile.empty, compare=False, repr=False)
    source: str | None = None

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        source: str | None = None,
        base_path: Path | None = None,
    ) -> "SpecificSkill":
        if not isinstance(data, dict):
            raise TypeError("Specific skill must be an object")
        _reject_executable_keys(data)
        schema = str(data.get("schema") or "").strip()
        if schema != SKILL_SCHEMA:
            raise ValueError(f"Unsupported specific skill schema: {schema!r}")

        scope = data.get("scope") or {}
        if scope and not isinstance(scope, dict):
            raise TypeError("Specific skill scope must be an object")

        url_patterns = _normalize_strings(
            scope.get("url_patterns")
            or scope.get("urls")
            or data.get("url_patterns")
            or data.get("urls")
            or ("*",)
        )
        if not url_patterns:
            url_patterns = ("*",)

        raw_targets = data.get("targets")
        if not isinstance(raw_targets, list) or not raw_targets:
            raise ValueError("Specific skill must contain at least one target")

        skill_id = str(data.get("id") or data.get("name") or "").strip()
        if not skill_id:
            raise ValueError("Specific skill is missing id")

        targets = tuple(SpecificSkillTarget.from_dict(item) for item in raw_targets)
        if len({target.id for target in targets}) != len(targets):
            raise ValueError("Specific skill contains duplicate target ids")

        profile_items = data.get("semantic_profiles") or (DEFAULT_SEMANTIC_PROFILE_ID,)
        semantic_profiles = load_semantic_profiles(profile_items, base_path=base_path)

        return cls(
            id=skill_id,
            name=str(data.get("name") or skill_id),
            version=_positive_int(data.get("version"), default=2),
            url_patterns=tuple(url_patterns),
            targets=targets,
            semantic_profile_ids=tuple(profile.id for profile in semantic_profiles),
            semantic_profile=_merge_semantic_profiles(semantic_profiles),
            source=source,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "SpecificSkill":
        file_path = Path(path)
        loaded = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("Specific skill file must contain a JSON object")
        return cls.from_dict(loaded, source=str(file_path), base_path=file_path.parent)

    def matches_url(self, url: str | None) -> bool:
        if not url:
            return "*" in self.url_patterns
        normalized = normalize_scope_url(url)
        return any(
            fnmatch.fnmatchcase(normalized, normalize_scope_url(pattern)) or _local_dev_url_matches(pattern, normalized)
            for pattern in self.url_patterns
        )


@dataclass(frozen=True)
class SpecificSkillMatch:
    skill_id: str
    skill_name: str
    target_id: str
    decision: str
    reason: str
    score: int
    threshold: int
    matched_signals: tuple[str, ...]
    source: str | None = None
    resolution: dict[str, Any] | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "skill_name": self.skill_name,
            "target_id": self.target_id,
            "decision": self.decision,
            "reason": self.reason,
            "score": self.score,
            "threshold": self.threshold,
            "matched_signals": list(self.matched_signals),
            "source": self.source,
            "resolution": self.resolution,
        }


@dataclass(frozen=True)
class MissingSkillTarget:
    skill_id: str
    skill_name: str
    target_id: str
    reason: str
    score: int
    threshold: int
    source: str | None = None
    resolution: dict[str, Any] | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "skill_name": self.skill_name,
            "target_id": self.target_id,
            "reason": self.reason,
            "score": self.score,
            "threshold": self.threshold,
            "source": self.source,
            "resolution": self.resolution,
        }


@dataclass(frozen=True)
class TargetResolution:
    kind: str
    score: int
    identity_score: int
    structure_score: int
    context_score: int
    matched_signals: tuple[str, ...]
    reason: str
    expected_kind: str | None = None
    observed_kind: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "score": self.score,
            "identity_score": self.identity_score,
            "structure_score": self.structure_score,
            "context_score": self.context_score,
            "matched_signals": list(self.matched_signals),
            "reason": self.reason,
            "expected_kind": self.expected_kind,
            "observed_kind": self.observed_kind,
        }


@dataclass(frozen=True)
class SkillEvaluation:
    in_scope: bool
    matches: tuple[SpecificSkillMatch, ...] = ()
    missing_targets: tuple[MissingSkillTarget, ...] = ()

    def to_metadata(self) -> dict[str, Any]:
        return {
            "in_scope": self.in_scope,
            "matches": [match.to_metadata() for match in self.matches],
            "missing_targets": [missing.to_metadata() for missing in self.missing_targets],
        }


def load_specific_skills(items: Any, *, base_path: Path | None = None) -> tuple[SpecificSkill, ...]:
    """Load inline skill objects and skill file references from config data."""

    if items is None:
        return ()
    if isinstance(items, (str, Path, dict)):
        iterable = (items,)
    elif isinstance(items, list):
        iterable = tuple(items)
    else:
        raise TypeError("specific skills must be a list, object, or path")

    skills: list[SpecificSkill] = []
    for item in iterable:
        if isinstance(item, (str, Path)):
            skills.append(SpecificSkill.from_file(_resolve_path(item, base_path)))
            continue
        if not isinstance(item, dict):
            raise TypeError("specific skill entries must be objects or paths")
        if "path" in item and len(item.keys() - {"path"}) == 0:
            skills.append(SpecificSkill.from_file(_resolve_path(str(item["path"]), base_path)))
            continue
        skills.append(SpecificSkill.from_dict(item, base_path=base_path))
    return tuple(skills)


def evaluate_specific_skills(
    skills: tuple[SpecificSkill, ...],
    facts: dict[str, Any],
) -> SkillEvaluation:
    if not skills:
        return SkillEvaluation(in_scope=False)

    url = _string_or_none(facts.get("url"))
    target = facts.get("target") if isinstance(facts.get("target"), dict) else {}
    in_scope_skills = tuple(skill for skill in skills if skill.matches_url(url))
    if not in_scope_skills:
        return SkillEvaluation(in_scope=False)

    # Imported lazily because the mapping helpers also normalize skill URLs.
    from .target_mapping import unchanged_capture_identity, target_mapping_key
    details = facts.get("details") if isinstance(facts.get("details"), dict) else {}
    probe = details.get("page_target_probe") if isinstance(details.get("page_target_probe"), dict) else {}
    observed = details.get("page_targets") if probe.get("status") == "ok" else None
    observed = [item for item in observed if isinstance(item, dict)] if isinstance(observed, list) else []

    matches: list[SpecificSkillMatch] = []
    low_scores: list[tuple[SpecificSkill, SpecificSkillTarget, TargetResolution]] = []
    for skill in in_scope_skills:
        token = _ACTIVE_SEMANTIC_PROFILE.set(skill.semantic_profile)
        try:
            unchanged = {item.id for item in skill.targets
                         if unchanged_capture_identity(item.fingerprint, target, page_url=url)}
            for protected_target in skill.targets:
                # Saturated text scores cannot transfer another unchanged
                # captured control's policy onto this unchanged target. Require
                # positive, unique current-page evidence of the different control;
                # generic, missing, drifted and overlapping captures keep normal
                # Block precedence.
                if unchanged and protected_target.id not in unchanged:
                    elsewhere = [item for item in observed if unchanged_capture_identity(
                        protected_target.fingerprint, item, page_url=url)]
                    if len(elsewhere) == 1 and target_mapping_key(elsewhere[0]) != target_mapping_key(target):
                        continue
                resolution = resolve_target_fingerprint(protected_target.fingerprint, target, facts)
                if resolution.kind in {"strong_match", "structural_slot_match"} and resolution.score >= protected_target.threshold:
                    matches.append(
                        SpecificSkillMatch(
                            skill_id=skill.id,
                            skill_name=skill.name,
                            target_id=protected_target.id,
                            decision=protected_target.decision,
                            reason=protected_target.reason,
                            score=resolution.score,
                            threshold=protected_target.threshold,
                            matched_signals=resolution.matched_signals,
                            source=skill.source,
                            resolution=resolution.to_metadata(),
                        )
                    )
                elif resolution.kind == "ambiguous_match" or _is_missing_target_relevant(
                    protected_target.fingerprint,
                    facts,
                    target,
                    resolution.score,
                    resolution=resolution,
                ):
                    low_scores.append((skill, protected_target, resolution))
        finally:
            _ACTIVE_SEMANTIC_PROFILE.reset(token)

    missing_targets: list[MissingSkillTarget] = []
    if not matches and low_scores and not bool(facts.get("read_only")) and bool(facts.get("meaningful")):
        for skill, protected_target, resolution in low_scores:
            missing_targets.append(
                MissingSkillTarget(
                    skill_id=skill.id,
                    skill_name=skill.name,
                    target_id=protected_target.id,
                    reason=(
                        "A URL-scoped Acira skill expected a protected target on this page, "
                        "but the current page or action target did not match it confidently. "
                        "The page may have changed; re-capture this skill if this is expected."
                    ),
                    score=resolution.score,
                    threshold=protected_target.threshold,
                    source=skill.source,
                    resolution=resolution.to_metadata(),
                )
            )

    return SkillEvaluation(in_scope=True, matches=tuple(matches), missing_targets=tuple(missing_targets))


def score_target_fingerprint(
    fingerprint: dict[str, Any],
    target: dict[str, Any],
    facts: dict[str, Any],
) -> tuple[int, list[str]]:
    """Return a deterministic confidence score in 0..100 for a captured target."""

    resolution = resolve_target_fingerprint(fingerprint, target, facts)
    return resolution.score, list(resolution.matched_signals)


def resolve_target_fingerprint(
    fingerprint: dict[str, Any],
    target: dict[str, Any],
    facts: dict[str, Any] | None = None,
) -> TargetResolution:
    """Resolve whether a current DOM target corresponds to a captured protected target.

    The resolver intentionally separates identity from structural slot evidence.
    A renamed field in the same form slot can still be protected, while a nearby
    unrelated field no longer inherits the decision just because it shares a form.
    """

    if not target:
        return TargetResolution(
            kind="ambiguous_match",
            score=0,
            identity_score=0,
            structure_score=0,
            context_score=0,
            matched_signals=(),
            reason="no current action target",
        )

    facts = facts or {}
    identity_score = 0
    structure_score = 0
    context_score = 0
    signals: list[str] = []
    stable_attrs = _attributes(target)
    expected_attrs = _dict_or_empty(fingerprint.get("stable_attributes") or fingerprint.get("attributes"))
    expected_kind = _control_kind_for_fingerprint(fingerprint)
    observed_kind = _control_kind_for_target(target)
    compatible_kind = not expected_kind or not observed_kind or _compatible_control_kinds(expected_kind, observed_kind)

    expected_id = _string_or_none(fingerprint.get("id"))
    target_id = _string_or_none(target.get("id")) or stable_attrs.get("id")
    if expected_id and target_id and _same(expected_id, target_id):
        identity_score += 35
        signals.append("id")

    selector = _string_or_none(fingerprint.get("selector"))
    if selector and target_id and selector.endswith(f"#{target_id}"):
        identity_score += 10
        signals.append("selector_id")

    for key, weight in (("name", 18),):
        if key == "name" and expected_kind == "radio":
            continue
        expected = _string_or_none(fingerprint.get(key))
        observed = _string_or_none(target.get(key)) or stable_attrs.get(key)
        if expected and observed and _same(expected, observed):
            identity_score += weight
            signals.append(key)

    for key, weight in (("tag", 10), ("type", 8), ("role", 8)):
        if key == "name" and expected_kind == "radio":
            continue
        expected = _string_or_none(fingerprint.get(key))
        observed = _string_or_none(target.get(key)) or stable_attrs.get(key)
        if expected and observed and _same(expected, observed):
            context_score += weight
            signals.append(key)

    expected_texts = [
        _string_or_none(fingerprint.get("text")),
        _string_or_none(fingerprint.get("aria_label") or fingerprint.get("aria-label")),
        _string_or_none(fingerprint.get("title")),
        _string_or_none(fingerprint.get("nearby_text")),
    ]
    observed_texts = [
        _string_or_none(target.get("text")),
        _string_or_none(target.get("aria_label")),
        _string_or_none(target.get("title")),
        _string_or_none(target.get("nearby_text")),
    ]
    text_score, text_signals = _score_texts(expected_texts, observed_texts)
    if text_score and _fingerprint_primary_text_is_value_like(fingerprint):
        text_signals = [f"{signal}_value_like" for signal in text_signals]
    identity_score += text_score
    signals.extend(text_signals)

    expected_classes = set(_normalize_tokens(fingerprint.get("classes")))
    observed_classes = set(_normalize_tokens(target.get("classes")))
    if expected_classes and observed_classes:
        overlap = expected_classes & observed_classes
        if overlap:
            identity_score += min(18, 6 * len(overlap))
            signals.append("classes")

    for key, expected_value in expected_attrs.items():
        if key in {"type", "role"}:
            continue
        if key == "name" and expected_kind == "radio":
            continue
        observed_value = stable_attrs.get(key) or _string_or_none(target.get(key))
        if expected_value and observed_value and _same(str(expected_value), observed_value):
            identity_score += 12
            signals.append(f"attr:{key}")

    if expected_kind and observed_kind and compatible_kind:
        context_score += 16
        signals.append(f"control_kind:{expected_kind}")

    identity_score, identity_signals = _score_identity_tokens(fingerprint, target)
    token_identity_score = identity_score
    identity_score = (
        35 * int("id" in signals)
        + 10 * int("selector_id" in signals)
    )
    # Rebuild the accumulated exact identity subtotal without losing the simpler
    # scoring code above.
    identity_score = 0
    for signal in signals:
        if signal == "id":
            identity_score += 35
        elif signal == "selector_id":
            identity_score += 10
        elif signal == "name":
            identity_score += 18
        elif signal in {"text_exact"}:
            identity_score += 35
        elif signal in {"text_contains"}:
            identity_score += 24
        elif signal in {"text_tokens"}:
            identity_score += 18
        elif signal in {"text_identity_tokens"}:
            identity_score += 12
        elif signal == "text_exact_value_like":
            identity_score += 8
        elif signal == "text_contains_value_like":
            identity_score += 6
        elif signal in {"text_tokens_value_like", "text_identity_tokens_value_like"}:
            identity_score += 4
        elif signal == "classes":
            identity_score += min(18, 6 * len(expected_classes & observed_classes))
        elif signal.startswith("attr:"):
            identity_score += 12
    identity_score += token_identity_score
    signals.extend(identity_signals)

    semantic_score, semantic_signals = _score_semantics(fingerprint, target, facts)
    identity_score += semantic_score
    signals.extend(semantic_signals)

    action_score, action_signals = _score_action_signature(fingerprint, target)
    context_score += action_score
    signals.extend(action_signals)

    form = _dict_or_empty(fingerprint.get("form") or fingerprint.get("form_context"))
    form_conflict = _form_context_conflicts(fingerprint, target)
    if form:
        for key in ("id", "name", "action"):
            expected = _string_or_none(form.get(key))
            observed = _string_or_none(target.get(f"form_{key}"))
            if expected and observed and _same(expected, observed):
                context_score += 8
                signals.append(f"form:{key}")

    structure_score, structure_signals = _score_structural_slot(fingerprint, target)
    signals.extend(structure_signals)

    identity_score = min(100, identity_score)
    context_score = min(100, context_score)
    structure_score = min(100, structure_score)
    field_role_conflict = bool(
        expected_kind == "text_entry"
        and observed_kind == "text_entry"
        and not _required_field_role_overlap(fingerprint, target)
    )
    if field_role_conflict:
        identity_score = max(0, identity_score - 18)
        context_score = max(0, context_score - 8)
        signals.append("field_role_conflict")
    raw_score = min(100, identity_score + context_score + structure_score)
    signal_set = set(signals)
    strong_structural_slot = _has_decisive_structural_slot_signal(signal_set)

    if expected_kind and observed_kind and not compatible_kind:
        signals.append("control_kind_conflict")
        if structure_score >= 82 and "structure:layout_slot" in signal_set and "structure:window_signature" in signal_set:
            return TargetResolution(
                kind="structural_slot_match",
                score=min(100, max(raw_score, 78)),
                identity_score=identity_score,
                structure_score=structure_score,
                context_score=context_score,
                matched_signals=tuple(_dedupe(signals)),
                reason="control kind changed but the structural signature still identifies the protected slot",
                expected_kind=expected_kind,
                observed_kind=observed_kind,
            )
        if structure_score >= 50 or identity_score >= 45:
            return TargetResolution(
                kind="ambiguous_match",
                score=min(69, max(55, raw_score)),
                identity_score=identity_score,
                structure_score=structure_score,
                context_score=context_score,
                matched_signals=tuple(_dedupe(signals)),
                reason="control kind changed for a likely protected slot",
                expected_kind=expected_kind,
                observed_kind=observed_kind,
            )
        return TargetResolution(
            kind="unrelated",
            score=min(54, raw_score),
            identity_score=identity_score,
            structure_score=structure_score,
            context_score=context_score,
            matched_signals=tuple(_dedupe(signals)),
            reason="control kind differs without enough slot evidence",
            expected_kind=expected_kind,
            observed_kind=observed_kind,
        )

    if identity_score >= 70 or (
        identity_score >= 44 and context_score >= 16 and _has_strong_anchor_signal(signals)
    ):
        return TargetResolution(
            kind="strong_match",
            score=min(100, max(raw_score, 82)),
            identity_score=identity_score,
            structure_score=structure_score,
            context_score=context_score,
            matched_signals=tuple(_dedupe(signals)),
            reason="stable identity matched captured target",
            expected_kind=expected_kind,
            observed_kind=observed_kind,
        )

    if structure_score >= 58 and context_score >= 16 and strong_structural_slot:
        return TargetResolution(
            kind="structural_slot_match",
            score=min(100, max(raw_score, 78)),
            identity_score=identity_score,
            structure_score=structure_score,
            context_score=context_score,
            matched_signals=tuple(_dedupe(signals)),
            reason="identity changed but form/neighbor/ordinal structure identifies the protected slot",
            expected_kind=expected_kind,
            observed_kind=observed_kind,
        )

    if structure_score >= 42 and context_score >= 12 and _has_partial_structural_slot_signal(signal_set):
        return TargetResolution(
            kind="ambiguous_match",
            score=min(69, max(raw_score, 58)),
            identity_score=identity_score,
            structure_score=structure_score,
            context_score=context_score,
            matched_signals=tuple(_dedupe(signals)),
            reason="some protected-slot structure matched, but not enough for confident enforcement",
            expected_kind=expected_kind,
            observed_kind=observed_kind,
        )

    if (
        not form_conflict
        and not field_role_conflict
        and (
            (identity_score >= 22 and context_score >= 24 and signal_set & {"semantic_aliases", "identity_token_aliases", "text_semantic_aliases", "action_signature_aliases"})
            or (identity_score >= 18 and context_score >= 32 and signal_set & {"identity_token_alias", "action_signature"})
        )
    ):
        return TargetResolution(
            kind="ambiguous_match",
            score=min(74, max(raw_score, 62)),
            identity_score=identity_score,
            structure_score=structure_score,
            context_score=context_score,
            matched_signals=tuple(_dedupe(signals)),
            reason="semantic/action identity suggests this may be the protected target, but drift requires review",
            expected_kind=expected_kind,
            observed_kind=observed_kind,
        )

    if (
        not field_role_conflict
        and (
            (identity_score >= 42 and context_score >= 16)
            or (identity_score >= 32 and float(target.get("confidence") or 0.0) < 0.65)
            or (identity_score >= 12 and context_score >= 34 and float(target.get("confidence") or 0.0) < 0.65)
        )
    ):
        return TargetResolution(
            kind="ambiguous_match",
            score=min(69, max(raw_score, 55)),
            identity_score=identity_score,
            structure_score=structure_score,
            context_score=context_score,
            matched_signals=tuple(_dedupe(signals)),
            reason="identity partly matches but the target is not stable enough",
            expected_kind=expected_kind,
            observed_kind=observed_kind,
        )

    return TargetResolution(
        kind="unrelated",
        score=min(69, raw_score),
        identity_score=identity_score,
        structure_score=structure_score,
        context_score=context_score,
        matched_signals=tuple(_dedupe(signals)),
        reason="current action target is not the captured protected slot",
        expected_kind=expected_kind,
        observed_kind=observed_kind,
    )


def _clean_fingerprint(fingerprint: dict[str, Any]) -> dict[str, Any]:
    _reject_executable_keys(fingerprint)
    cleaned = dict(fingerprint)
    _reject_captured_raw_values(cleaned)
    if "semanticHints" in cleaned and "semantic_hints" not in cleaned:
        cleaned["semantic_hints"] = cleaned.pop("semanticHints")
    if "ariaLabel" in cleaned and "aria_label" not in cleaned:
        cleaned["aria_label"] = cleaned.pop("ariaLabel")
    if "nearbyText" in cleaned and "nearby_text" not in cleaned:
        cleaned["nearby_text"] = cleaned.pop("nearbyText")
    if "ancestorText" in cleaned and "ancestor_text" not in cleaned:
        cleaned["ancestor_text"] = cleaned.pop("ancestorText")
    if "stableAttributes" in cleaned and "stable_attributes" not in cleaned:
        cleaned["stable_attributes"] = cleaned.pop("stableAttributes")
    if "identityTokens" in cleaned and "identity_tokens" not in cleaned:
        cleaned["identity_tokens"] = cleaned.pop("identityTokens")
    if "controlKind" in cleaned and "control_kind" not in cleaned:
        cleaned["control_kind"] = cleaned.pop("controlKind")
    if "sectionHeading" in cleaned and "section_heading" not in cleaned:
        cleaned["section_heading"] = cleaned.pop("sectionHeading")
    if "parentLabel" in cleaned and "parent_label" not in cleaned:
        cleaned["parent_label"] = cleaned.pop("parentLabel")
    if "controlIndex" in cleaned and "control_index" not in cleaned:
        cleaned["control_index"] = cleaned.pop("controlIndex")
    if "sameKindIndex" in cleaned and "same_kind_index" not in cleaned:
        cleaned["same_kind_index"] = cleaned.pop("sameKindIndex")
    if "previousControlText" in cleaned and "previous_control_text" not in cleaned:
        cleaned["previous_control_text"] = cleaned.pop("previousControlText")
    if "nextControlText" in cleaned and "next_control_text" not in cleaned:
        cleaned["next_control_text"] = cleaned.pop("nextControlText")
    if "domPath" in cleaned and "dom_path" not in cleaned:
        cleaned["dom_path"] = cleaned.pop("domPath")
    if "structureSignature" in cleaned and "structure_signature" not in cleaned:
        cleaned["structure_signature"] = cleaned.pop("structureSignature")
    if "actionSignature" in cleaned and "action_signature" not in cleaned:
        cleaned["action_signature"] = cleaned.pop("actionSignature")
    structural = cleaned.get("structural_context")
    if isinstance(structural, dict):
        for key, value in structural.items():
            normalized_key = re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).lower()
            cleaned.setdefault(normalized_key, value)
    return cleaned


def _reject_executable_keys(data: dict[str, Any]) -> None:
    forbidden = {"code", "script", "js", "javascript", "function", "handler", "eval", "import", "require", "webhook", "url_fetch"}

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            present = forbidden & {str(key).lower() for key in value.keys()}
            if present:
                raise ValueError(
                    f"Specific skills are declarative and cannot contain executable keys at {path}: {sorted(present)}"
                )
            for key, child in value.items():
                visit(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(data, "$")


def _reject_captured_raw_values(data: dict[str, Any]) -> None:
    sensitive_value_keys = {"value", "value_sample", "input_value", "text_value", "raw_value"}

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = str(key).lower()
                if normalized in sensitive_value_keys and isinstance(child, str) and child.strip():
                    raise ValueError(f"Specific skill fingerprints cannot contain raw captured values at {path}.{key}")
                visit(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(data, "$")


def _is_missing_target_relevant(
    fingerprint: dict[str, Any],
    facts: dict[str, Any],
    target: dict[str, Any],
    score: int,
    *,
    resolution: TargetResolution | None = None,
) -> bool:
    action = str(facts.get("action") or "").lower()
    if not action or bool(facts.get("read_only")):
        return False

    if resolution is not None:
        if resolution.kind == "ambiguous_match":
            return True
        if resolution.kind == "unrelated" and action in SAFE_UNRELATED_FORM_ACTIONS:
            return False

    expected_actions = _expected_actions_for_fingerprint(fingerprint)
    if action in SAFE_UNRELATED_FORM_ACTIONS:
        if action in expected_actions:
            return _safe_form_target_looks_related(fingerprint, target, score)
        return _wrong_kind_target_looks_ambiguous(fingerprint, target)

    if action in expected_actions:
        if action in {"click", "form_control_activation", "check", "select", "form_input", "focus"}:
            return _safe_form_target_looks_related(fingerprint, target, score) or not target or float(target.get("confidence") or 0.0) < 0.65
        return True

    if not target or float(target.get("confidence") or 0.0) < 0.65:
        return action in HIGH_RISK_MISSING_TARGET_ACTIONS

    hints = set(_normalize_tokens(fingerprint.get("semantic_hints")))
    target_text = _normalized_text(
        " ".join(
            str(value or "")
            for value in (
                target.get("text"),
                target.get("aria_label"),
                target.get("title"),
                target.get("nearby_text"),
            )
        )
    )
    return bool(hints and any(hint in target_text for hint in hints))


def _wrong_kind_target_looks_ambiguous(fingerprint: dict[str, Any], target: dict[str, Any]) -> bool:
    if not target:
        return False
    expected_kind = _control_kind_for_fingerprint(fingerprint)
    observed_kind = _control_kind_for_target(target)
    if not expected_kind or not observed_kind or _compatible_control_kinds(expected_kind, observed_kind):
        return False
    expected_texts = [
        _string_or_none(fingerprint.get("text")),
        _string_or_none(fingerprint.get("aria_label") or fingerprint.get("aria-label")),
        _string_or_none(fingerprint.get("title")),
    ]
    observed_texts = [
        _string_or_none(target.get("text")),
        _string_or_none(target.get("aria_label")),
        _string_or_none(target.get("title")),
    ]
    for expected in expected_texts:
        for observed in observed_texts:
            expected_normalized = _normalized_text(expected)
            observed_normalized = _normalized_text(observed)
            if not expected_normalized or not observed_normalized:
                continue
            if expected_normalized == observed_normalized:
                return True
            if len(expected_normalized) >= 8 and len(observed_normalized) >= 8 and (
                expected_normalized in observed_normalized or observed_normalized in expected_normalized
            ):
                return True
            if _token_overlap_ratio(expected_normalized, observed_normalized) >= 0.67:
                return True

    expected_tokens = _specific_tokens(
        _tokens_from_values(
            [
                fingerprint.get("id"),
                fingerprint.get("name"),
                fingerprint.get("text"),
                fingerprint.get("aria_label") or fingerprint.get("aria-label"),
                fingerprint.get("title"),
            ]
        )
    )
    observed_tokens = _specific_tokens(
        _tokens_from_values(
            [
                target.get("id"),
                target.get("name"),
                target.get("text"),
                target.get("aria_label"),
                target.get("title"),
            ]
        )
    )
    overlap = expected_tokens & observed_tokens
    return len(overlap) >= 2 or bool(overlap & _profile_high_value_tokens())


def _safe_form_target_looks_related(fingerprint: dict[str, Any], target: dict[str, Any], score: int) -> bool:
    if not target:
        return False

    expected_attrs = _dict_or_empty(fingerprint.get("stable_attributes") or fingerprint.get("attributes"))
    observed_attrs = _attributes(target)

    expected_kind = _control_kind_for_fingerprint(fingerprint)
    observed_kind = _control_kind_for_target(target)

    for key in ("id", "name", "aria-label", "autocomplete", "placeholder"):
        if key == "name" and expected_kind == "radio":
            continue
        expected = _string_or_none(fingerprint.get(key)) or _string_or_none(expected_attrs.get(key))
        observed = _string_or_none(target.get(key)) or _string_or_none(observed_attrs.get(key))
        if expected and observed and _same(expected, observed):
            return True

    if _missing_required_high_value_anchor(fingerprint, target):
        return False

    expected_type = _string_or_none(fingerprint.get("type")) or _string_or_none(expected_attrs.get("type"))
    observed_type = _string_or_none(target.get("type")) or _string_or_none(observed_attrs.get("type"))
    expected_role = _string_or_none(fingerprint.get("role")) or _string_or_none(expected_attrs.get("role"))
    observed_role = _string_or_none(target.get("role")) or _string_or_none(observed_attrs.get("role"))

    same_control_kind = bool(
        (expected_type and observed_type and _same(expected_type, observed_type))
        or (expected_role and observed_role and _same(expected_role, observed_role))
    )
    if same_control_kind and _has_specific_text_or_hint_overlap(fingerprint, target):
        return True

    if _same_form_context(fingerprint, target) and _has_specific_text_or_hint_overlap(fingerprint, target):
        return True

    if (
        expected_kind
        and observed_kind
        and _compatible_control_kinds(expected_kind, observed_kind)
        and float(target.get("confidence") or 0.0) < 0.65
        and score >= 20
    ):
        return True

    return False


def _missing_required_high_value_anchor(fingerprint: dict[str, Any], target: dict[str, Any]) -> bool:
    required = _field_identity_tokens_for_fingerprint(fingerprint) & _profile_high_value_tokens()
    if not required:
        return False
    observed = _identity_tokens_for_target(target)
    return not bool(required & observed)


def _field_identity_tokens_for_fingerprint(fingerprint: dict[str, Any]) -> set[str]:
    attrs = _dict_or_empty(fingerprint.get("stable_attributes") or fingerprint.get("attributes"))
    values: list[Any] = [
        fingerprint.get("id"),
        fingerprint.get("name"),
        fingerprint.get("aria_label") or fingerprint.get("aria-label"),
        fingerprint.get("title"),
        fingerprint.get("nearby_text"),
        fingerprint.get("parent_label"),
        fingerprint.get("section_heading"),
        fingerprint.get("semantic_hints"),
    ]
    values.extend(
        attrs.get(key)
        for key in (
            "id",
            "name",
            "aria-label",
            "title",
            "autocomplete",
            "data-testid",
            "data-test",
            "data-cy",
            "role",
            "type",
        )
    )
    return _specific_tokens(_tokens_from_values(values))


def _has_specific_text_or_hint_overlap(fingerprint: dict[str, Any], target: dict[str, Any]) -> bool:
    expected_text = _normalized_text(
        " ".join(
            str(value or "")
            for value in (
                fingerprint.get("text"),
                fingerprint.get("aria_label") or fingerprint.get("aria-label"),
                fingerprint.get("title"),
                fingerprint.get("nearby_text"),
            )
        )
    )
    observed_text = _normalized_text(
        " ".join(
            str(value or "")
            for value in (
                target.get("text"),
                target.get("aria_label"),
                target.get("title"),
                target.get("nearby_text"),
            )
        )
    )
    if expected_text and observed_text and _token_overlap_ratio(expected_text, observed_text) >= 0.4:
        return True

    observed_tokens = set(_normalize_tokens(observed_text))
    specific_hints = {
        hint
        for hint in _normalize_tokens(fingerprint.get("semantic_hints"))
        if hint and hint not in _profile_stopwords() and len(hint) >= 3
    }
    overlap = specific_hints & observed_tokens if observed_tokens else set()
    return len(overlap) >= 2 or bool(overlap & _profile_high_value_tokens())


def _same_form_context(fingerprint: dict[str, Any], target: dict[str, Any]) -> bool:
    form = _dict_or_empty(fingerprint.get("form") or fingerprint.get("form_context"))
    if not form:
        return False
    for key in ("id", "name", "action"):
        expected = _string_or_none(form.get(key))
        observed = _string_or_none(target.get(f"form_{key}"))
        if expected and observed and _same(expected, observed):
            return True
    return False


def _form_context_conflicts(fingerprint: dict[str, Any], target: dict[str, Any]) -> bool:
    form = _dict_or_empty(fingerprint.get("form") or fingerprint.get("form_context"))
    if not form:
        return False
    compared = 0
    matches = 0
    for key in ("id", "name", "action"):
        expected = _string_or_none(form.get(key))
        observed = _string_or_none(target.get(f"form_{key}"))
        if not expected or not observed:
            continue
        compared += 1
        if _same(expected, observed):
            matches += 1
    return compared > 0 and matches == 0


def _score_structural_slot(fingerprint: dict[str, Any], target: dict[str, Any]) -> tuple[int, list[str]]:
    score = 0
    signals: list[str] = []

    signature_score, signature_signals = _score_structure_signature(fingerprint, target)
    score += signature_score
    signals.extend(signature_signals)

    expected_section = _structural_string(fingerprint, "section_heading")
    observed_section = _target_string(target, "section_heading")
    if expected_section and observed_section and _text_similarity(expected_section, observed_section) >= 0.72:
        score += 12
        signals.append("structure:section_heading")

    expected_parent = _structural_string(fingerprint, "parent_label")
    observed_parent = _target_string(target, "parent_label")
    if expected_parent and observed_parent and _text_similarity(expected_parent, observed_parent) >= 0.78:
        score += 14
        signals.append("structure:parent_label")

    expected_index = _structural_int(fingerprint, "control_index")
    observed_index = _target_int(target, "control_index")
    if expected_index is not None and observed_index is not None and expected_index == observed_index:
        score += 30
        signals.append("structure:control_index")

    expected_kind_index = _structural_int(fingerprint, "same_kind_index")
    observed_kind_index = _target_int(target, "same_kind_index")
    if expected_kind_index is not None and observed_kind_index is not None and expected_kind_index == observed_kind_index:
        score += 20
        signals.append("structure:same_kind_index")

    expected_prev = _structural_string(fingerprint, "previous_control_text")
    observed_prev = _target_string(target, "previous_control_text")
    expected_next = _structural_string(fingerprint, "next_control_text")
    observed_next = _target_string(target, "next_control_text")
    if expected_prev and observed_prev and _text_similarity(expected_prev, observed_prev) >= 0.72:
        score += 24
        signals.append("structure:previous_control")
    if expected_next and observed_next and _text_similarity(expected_next, observed_next) >= 0.72:
        score += 24
        signals.append("structure:next_control")

    ancestor_window_score, ancestor_window_signals = _score_ancestor_neighbor_window(
        fingerprint,
        observed_prev,
        observed_next,
    )
    score += ancestor_window_score
    signals.extend(ancestor_window_signals)

    legacy_score, legacy_signals = _score_legacy_ancestor_slot(fingerprint, target)
    score += legacy_score
    signals.extend(legacy_signals)

    expected_path = _structural_string(fingerprint, "dom_path")
    observed_path = _target_string(target, "dom_path")
    if expected_path and observed_path and _dom_path_similarity(expected_path, observed_path) >= 0.72:
        score += 12
        signals.append("structure:dom_path")

    if {"structure:previous_control", "structure:next_control"} <= set(signals) or {
        "structure:legacy_previous",
        "structure:legacy_next",
    } <= set(signals):
        score += 8
        signals.append("structure:neighbor_pair")

    return min(100, score), signals


def _has_decisive_structural_slot_signal(signals: set[str]) -> bool:
    """Return true only for structural evidence that identifies one slot, not just a region.

    Broad signals such as matching ancestor path, kind sequence, layout band, or
    "all controls in this form are text inputs" are useful context, but they are
    not enough to block a specific neighboring field. A protected target needs
    exact ordinal evidence or matching neighbor anchors to become a confident
    structural slot match.
    """

    local_anchor = bool(
        signals
        & {
            "structure:neighbor_pair",
            "structure:parent_label",
            "structure:anchor_signature",
            "structure:ancestor_neighbor_window",
        }
    )
    exact_ordinal = {"structure:control_index", "structure:same_kind_index"} <= signals
    exact_signature_ordinal = {"structure:signature_control_index", "structure:signature_same_kind_index"} <= signals
    same_region = bool(signals & {"form:id", "form:name", "structure:section_heading"})
    exact_signature_slot = (
        exact_signature_ordinal
        and same_region
        and "structure:signature" in signals
        and bool(signals & {"structure:signature_control_count", "structure:kind_sequence"})
    )
    return (
        "structure:neighbor_pair" in signals
        or {"structure:legacy_previous", "structure:legacy_next"} <= signals
        or {"structure:previous_control", "structure:next_control"} <= signals
        or ((exact_ordinal or exact_signature_ordinal) and local_anchor)
        or exact_signature_slot
    )


def _has_partial_structural_slot_signal(signals: set[str]) -> bool:
    """Return true for weaker-but-still-slot-specific evidence.

    This is intentionally narrower than "the form looks similar." It lets the
    resolver ask on genuinely unclear drift, but avoids asking/blocking every
    sibling input in a repeated grid just because the ancestor and kind sequence
    match.
    """

    if _has_decisive_structural_slot_signal(signals):
        return True
    return bool(
        signals
        & {
            "structure:control_index",
            "structure:same_kind_index",
            "structure:signature_control_index",
            "structure:signature_same_kind_index",
            "structure:previous_control",
            "structure:next_control",
            "structure:legacy_previous",
            "structure:legacy_next",
            "structure:parent_label",
        }
    )


def _score_ancestor_neighbor_window(
    fingerprint: dict[str, Any],
    observed_prev: str | None,
    observed_next: str | None,
) -> tuple[int, list[str]]:
    expected_prev = _structural_string(fingerprint, "previous_control_text")
    expected_next = _structural_string(fingerprint, "next_control_text")
    expected_ancestor = _structural_string(fingerprint, "ancestor_text")
    if not expected_prev or not expected_next or not observed_prev or not observed_next or not expected_ancestor:
        return 0, []

    ancestor = _normalized_text(expected_ancestor)
    prev = _normalized_text(observed_prev)
    next_ = _normalized_text(observed_next)
    if len(prev) < 4 or len(next_) < 4:
        return 0, []

    if _observed_neighbor_is_expected_target(fingerprint, observed_prev, observed_next):
        return 0, []

    prev_index = ancestor.find(prev)
    next_index = ancestor.find(next_)
    if prev_index >= 0 and next_index > prev_index:
        return 22, ["structure:ancestor_neighbor_window"]

    prev_tokens = set(_specific_tokens(_normalize_tokens(prev)))
    next_tokens = set(_specific_tokens(_normalize_tokens(next_)))
    ancestor_tokens = set(_specific_tokens(_normalize_tokens(ancestor)))
    if prev_tokens and next_tokens and prev_tokens <= ancestor_tokens and next_tokens <= ancestor_tokens:
        return 14, ["structure:ancestor_neighbor_window"]
    return 0, []


def _observed_neighbor_is_expected_target(
    fingerprint: dict[str, Any],
    observed_prev: str | None,
    observed_next: str | None,
) -> bool:
    expected_texts = [
        _string_or_none(fingerprint.get("text")),
        _string_or_none(fingerprint.get("nearby_text")),
        _string_or_none(fingerprint.get("parent_label")),
        _string_or_none(fingerprint.get("aria_label") or fingerprint.get("aria-label")),
        _string_or_none(fingerprint.get("title")),
    ]
    for expected in expected_texts:
        if not expected or len(_normalized_text(expected)) < 4:
            continue
        for observed in (observed_prev, observed_next):
            if observed and _text_similarity(expected, observed) >= 0.78:
                return True
    return False


def _score_structure_signature(fingerprint: dict[str, Any], target: dict[str, Any]) -> tuple[int, list[str]]:
    expected = _structure_signature_for_fingerprint(fingerprint)
    observed = _structure_signature_for_target(target)
    if not expected or not observed:
        return 0, []

    score = 0
    signals: list[str] = []

    expected_index = _signature_int(expected, "control_index")
    observed_index = _signature_int(observed, "control_index")
    if expected_index is not None and observed_index is not None:
        if expected_index == observed_index:
            score += 18
            signals.append("structure:signature_control_index")
        elif abs(expected_index - observed_index) == 1:
            score += 6
            signals.append("structure:signature_control_index_near")

    expected_same_kind = _signature_int(expected, "same_kind_index")
    observed_same_kind = _signature_int(observed, "same_kind_index")
    if expected_same_kind is not None and observed_same_kind is not None:
        if expected_same_kind == observed_same_kind:
            score += 10
            signals.append("structure:signature_same_kind_index")
        elif abs(expected_same_kind - observed_same_kind) == 1:
            score += 4
            signals.append("structure:signature_same_kind_index_near")

    expected_count = _signature_int(expected, "control_count")
    observed_count = _signature_int(observed, "control_count")
    if expected_count is not None and observed_count is not None and _close_count(expected_count, observed_count):
        score += 8
        signals.append("structure:signature_control_count")

    expected_target_kind = _string_or_none(expected.get("target_kind"))
    observed_target_kind = _string_or_none(observed.get("target_kind"))
    if expected_target_kind and observed_target_kind and expected_target_kind == observed_target_kind:
        score += 8
        signals.append("structure:signature_target_kind")

    sequence_score, sequence_signals = _score_kind_sequence(expected.get("kind_sequence"), observed.get("kind_sequence"), expected_index, observed_index)
    score += sequence_score
    signals.extend(sequence_signals)

    state_score, state_signals = _score_element_state(expected.get("element_state"), observed.get("element_state"))
    score += state_score
    signals.extend(state_signals)

    layout_score, layout_signals = _score_layout_signature(expected.get("layout"), observed.get("layout"))
    score += layout_score
    signals.extend(layout_signals)

    window_score, window_signals = _score_control_window(expected.get("control_window"), observed.get("control_window"))
    score += window_score
    signals.extend(window_signals)

    path_score, path_signals = _score_ancestor_path(expected.get("ancestor_path"), observed.get("ancestor_path"))
    score += path_score
    signals.extend(path_signals)

    anchor_score, anchor_signals = _score_anchor_signature(expected.get("anchors"), observed.get("anchors"))
    score += anchor_score
    signals.extend(anchor_signals)

    if score >= 54:
        signals.append("structure:signature")
    return min(100, score), signals


def _structure_signature_for_fingerprint(fingerprint: dict[str, Any]) -> dict[str, Any]:
    signature = fingerprint.get("structure_signature") or fingerprint.get("structureSignature")
    if isinstance(signature, dict) and signature:
        return signature

    structural = fingerprint.get("structural_context")
    structural = structural if isinstance(structural, dict) else {}
    expected_kind = _control_kind_for_fingerprint(fingerprint)
    form = _dict_or_empty(fingerprint.get("form") or fingerprint.get("form_context"))
    return {
        "version": 1,
        "control_index": _structural_int(fingerprint, "control_index"),
        "same_kind_index": _structural_int(fingerprint, "same_kind_index"),
        "control_window": [
            {"offset": -1, "kind": None, "text": _structural_string(fingerprint, "previous_control_text")},
            {"offset": 0, "kind": expected_kind, "text": _structural_string(fingerprint, "parent_label") or _structural_string(fingerprint, "nearby_text") or _structural_string(fingerprint, "text")},
            {"offset": 1, "kind": None, "text": _structural_string(fingerprint, "next_control_text")},
        ],
        "ancestor_path": _path_signature(structural.get("ancestor_path") or fingerprint.get("dom_path")),
        "scope": {
            "form_id": form.get("id"),
            "section": _structural_string(fingerprint, "section_heading"),
        },
    }


def _structure_signature_for_target(target: dict[str, Any]) -> dict[str, Any]:
    signature = target.get("structure_signature") or target.get("structureSignature")
    if isinstance(signature, dict) and signature:
        return signature

    observed_kind = _control_kind_for_target(target)
    return {
        "version": 1,
        "control_index": _target_int(target, "control_index"),
        "same_kind_index": _target_int(target, "same_kind_index"),
        "control_window": [
            {"offset": -1, "kind": None, "text": _target_string(target, "previous_control_text")},
            {"offset": 0, "kind": observed_kind, "text": _target_string(target, "parent_label") or _target_string(target, "nearby_text") or _target_string(target, "text")},
            {"offset": 1, "kind": None, "text": _target_string(target, "next_control_text")},
        ],
        "ancestor_path": _path_signature(target.get("dom_path")),
        "scope": {
            "form_id": target.get("form_id"),
            "section": _target_string(target, "section_heading"),
        },
    }


def _signature_int(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        camel = re.sub(r"_([a-z])", lambda match: match.group(1).upper(), key)
        value = data.get(camel)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _close_count(expected: int, observed: int) -> bool:
    if expected == observed:
        return True
    return abs(expected - observed) <= max(1, round(max(expected, observed) * 0.18))


def _window_by_offset(value: Any) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for item in _list_of_dicts(value):
        offset = _signature_int(item, "offset")
        if offset is not None:
            result[offset] = item
    return result


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _path_signature(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_normalized_path_part(item) for item in value if _normalized_path_part(item)][-8:]
    if isinstance(value, str):
        return [_normalized_path_part(item) for item in re.split(r">|/", value) if _normalized_path_part(item)][-8:]
    return []


def _path_parts(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_normalized_path_part(item) for item in value if _normalized_path_part(item)][-8:]
    return _path_signature(value)


def _normalized_path_part(value: Any) -> str:
    if isinstance(value, dict):
        tag = _string_or_none(value.get("tag")) or ""
        role = _string_or_none(value.get("role")) or ""
        kind = _string_or_none(value.get("kind")) or ""
        return ":".join(part for part in (_normalized_text(tag), _normalized_text(role), _normalized_text(kind)) if part)
    text = _normalized_text(str(value or ""))
    text = re.sub(r":nth-of-type\(\d+\)", "", text)
    text = re.sub(r"#[a-z0-9_-]+", "", text)
    text = re.sub(r"\.[a-z0-9_-]+", "", text)
    return text.strip()


def _score_layout_signature(expected: Any, observed: Any) -> tuple[int, list[str]]:
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        return 0, []
    score = 0
    signals: list[str] = []
    for key, weight in (("row", 8), ("col", 8), ("x_band", 6), ("y_band", 6)):
        expected_value = _signature_int(expected, key)
        observed_value = _signature_int(observed, key)
        if expected_value is None or observed_value is None:
            continue
        if expected_value == observed_value:
            score += weight
            signals.append(f"structure:layout_{key}")
        elif abs(expected_value - observed_value) == 1 and key in {"x_band", "y_band"}:
            score += max(2, weight // 2)
            signals.append(f"structure:layout_{key}_near")
    if score >= 14:
        signals.append("structure:layout_slot")
    return score, signals


def _score_kind_sequence(expected: Any, observed: Any, expected_index: int | None, observed_index: int | None) -> tuple[int, list[str]]:
    if not isinstance(expected, list) or not isinstance(observed, list) or expected_index is None or observed_index is None:
        return 0, []
    expected_kinds = [str(item or "") for item in expected]
    observed_kinds = [str(item or "") for item in observed]
    matches = 0
    compared = 0
    for offset in range(-3, 4):
        expected_pos = expected_index + offset
        observed_pos = observed_index + offset
        if expected_pos < 0 or observed_pos < 0:
            continue
        if expected_pos >= len(expected_kinds) or observed_pos >= len(observed_kinds):
            continue
        compared += 1
        if expected_kinds[expected_pos] == observed_kinds[observed_pos]:
            matches += 1
    if compared < 3:
        return 0, []
    ratio = matches / compared
    if ratio >= 0.85:
        return 18, ["structure:kind_sequence"]
    if ratio >= 0.65:
        return 10, ["structure:kind_sequence_partial"]
    if ratio >= 0.45:
        return 4, ["structure:kind_sequence_weak"]
    return 0, []


def _score_element_state(expected: Any, observed: Any) -> tuple[int, list[str]]:
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        return 0, []
    matches = 0
    compared = 0
    for key in ("disabled", "hidden", "required"):
        if key not in expected or key not in observed:
            continue
        compared += 1
        if bool(expected.get(key)) == bool(observed.get(key)):
            matches += 1
    if compared and matches == compared:
        return 6, ["structure:element_state"]
    if matches:
        return 2, ["structure:element_state_partial"]
    return 0, []


def _score_control_window(expected: Any, observed: Any) -> tuple[int, list[str]]:
    expected_items = _window_by_offset(expected)
    observed_items = _window_by_offset(observed)
    if not expected_items or not observed_items:
        return 0, []
    score = 0
    signals: list[str] = []
    kind_matches = 0
    text_matches = 0
    for offset in (-2, -1, 0, 1, 2):
        expected_item = expected_items.get(offset)
        observed_item = observed_items.get(offset)
        if not expected_item or not observed_item:
            continue
        expected_kind = _string_or_none(expected_item.get("kind"))
        observed_kind = _string_or_none(observed_item.get("kind"))
        if expected_kind and observed_kind and expected_kind == observed_kind:
            kind_matches += 1
        expected_text = _string_or_none(expected_item.get("text"))
        observed_text = _string_or_none(observed_item.get("text"))
        if expected_text and observed_text and (
            _text_similarity(expected_text, observed_text) >= 0.62 or _semantic_alias_overlap(expected_text, observed_text)
        ):
            text_matches += 1
    if kind_matches:
        score += min(24, kind_matches * 6)
        signals.append("structure:window_kinds")
    if text_matches:
        score += min(18, text_matches * 6)
        signals.append("structure:window_text")
    if kind_matches >= 3 or (kind_matches >= 2 and text_matches >= 1):
        signals.append("structure:window_signature")
    return score, signals


def _score_ancestor_path(expected: Any, observed: Any) -> tuple[int, list[str]]:
    expected_parts = _path_parts(expected)
    observed_parts = _path_parts(observed)
    if not expected_parts or not observed_parts:
        return 0, []
    suffix = 0
    for expected_item, observed_item in zip(reversed(expected_parts), reversed(observed_parts), strict=False):
        if expected_item != observed_item:
            break
        suffix += 1
    overlap = len(set(expected_parts) & set(observed_parts))
    score = min(14, suffix * 4 + overlap * 2)
    return (score, ["structure:ancestor_path"] if score >= 8 else [])


def _score_anchor_signature(expected: Any, observed: Any) -> tuple[int, list[str]]:
    expected_items = _list_of_dicts(expected)
    observed_items = _list_of_dicts(observed)
    if not expected_items or not observed_items:
        return 0, []
    matches = 0
    for expected_item in expected_items[:6]:
        expected_text = _string_or_none(expected_item.get("text"))
        expected_kind = _string_or_none(expected_item.get("kind"))
        expected_direction = _string_or_none(expected_item.get("direction"))
        for observed_item in observed_items[:8]:
            if expected_direction and observed_item.get("direction") != expected_direction:
                continue
            if expected_kind and observed_item.get("kind") != expected_kind:
                continue
            observed_text = _string_or_none(observed_item.get("text"))
            if expected_text and observed_text and _text_similarity(expected_text, observed_text) >= 0.58:
                matches += 1
                break
    if not matches:
        return 0, []
    return min(18, matches * 6), ["structure:anchor_signature"]


def _score_legacy_ancestor_slot(fingerprint: dict[str, Any], target: dict[str, Any]) -> tuple[int, list[str]]:
    expected_label = _structural_string(fingerprint, "nearby_text") or _structural_string(fingerprint, "text")
    observed_label = _target_string(target, "nearby_text") or _target_string(target, "text")
    expected_ancestor = _structural_string(fingerprint, "ancestor_text")
    observed_ancestor = _target_string(target, "ancestor_text")
    if not expected_label or not observed_label or not expected_ancestor or not observed_ancestor:
        return 0, []

    expected_window = _slot_window(expected_ancestor, expected_label)
    observed_window = _slot_window(observed_ancestor, observed_label)
    if expected_window is None or observed_window is None:
        return 0, []

    score = 0
    signals: list[str] = []
    before_ratio = _suffix_token_ratio(expected_window[0], observed_window[0])
    after_ratio = _prefix_token_ratio(expected_window[1], observed_window[1])
    if before_ratio >= 0.75:
        score += 28
        signals.append("structure:legacy_previous")
    elif before_ratio >= 0.5:
        score += 12
        signals.append("structure:legacy_previous_weak")
    if after_ratio >= 0.75:
        score += 28
        signals.append("structure:legacy_next")
    elif after_ratio >= 0.5:
        score += 12
        signals.append("structure:legacy_next_weak")
    return score, signals


def _slot_window(ancestor: str | None, label: str | None) -> tuple[str, str] | None:
    normalized_ancestor = _normalized_text(ancestor)
    normalized_label = _normalized_text(label)
    if not normalized_ancestor or not normalized_label:
        return None
    index = normalized_ancestor.find(normalized_label)
    if index < 0:
        label_tokens = _normalize_tokens(normalized_label)
        if not label_tokens:
            return None
        pattern = r"\s+".join(re.escape(token) for token in label_tokens)
        match = re.search(pattern, normalized_ancestor)
        if not match:
            return None
        index = match.start()
        end = match.end()
    else:
        end = index + len(normalized_label)
    return normalized_ancestor[:index].strip(), normalized_ancestor[end:].strip()


def _suffix_token_ratio(expected: str, observed: str) -> float:
    expected_tokens = list(_normalize_tokens(expected))
    observed_tokens = list(_normalize_tokens(observed))
    if not expected_tokens or not observed_tokens:
        return 0.0
    size = min(4, len(expected_tokens), len(observed_tokens))
    left = expected_tokens[-size:]
    right = observed_tokens[-size:]
    return sum(1 for expected_token, observed_token in zip(left, right, strict=False) if expected_token == observed_token) / size


def _prefix_token_ratio(expected: str, observed: str) -> float:
    expected_tokens = list(_normalize_tokens(expected))
    observed_tokens = list(_normalize_tokens(observed))
    if not expected_tokens or not observed_tokens:
        return 0.0
    size = min(4, len(expected_tokens), len(observed_tokens))
    left = expected_tokens[:size]
    right = observed_tokens[:size]
    return sum(1 for expected_token, observed_token in zip(left, right, strict=False) if expected_token == observed_token) / size


def _structural_string(fingerprint: dict[str, Any], key: str) -> str | None:
    value = fingerprint.get(key)
    if value is None:
        structural = fingerprint.get("structural_context")
        if isinstance(structural, dict):
            value = structural.get(key)
    if value is None and key in {"section_heading", "parent_label"}:
        value = fingerprint.get("nearby_text")
    return _string_or_none(value)


def _target_string(target: dict[str, Any], key: str) -> str | None:
    value = target.get(key)
    if value is None:
        camel = re.sub(r"_([a-z])", lambda match: match.group(1).upper(), key)
        value = target.get(camel)
    if value is None and key == "parent_label":
        value = target.get("nearby_text") or target.get("nearbyText")
    return _string_or_none(value)


def _structural_int(fingerprint: dict[str, Any], key: str) -> int | None:
    value = fingerprint.get(key)
    if value is None:
        structural = fingerprint.get("structural_context")
        if isinstance(structural, dict):
            value = structural.get(key)
    return _int_or_none(value)


def _target_int(target: dict[str, Any], key: str) -> int | None:
    value = target.get(key)
    if value is None:
        camel = re.sub(r"_([a-z])", lambda match: match.group(1).upper(), key)
        value = target.get(camel)
    return _int_or_none(value)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _text_similarity(left: str | None, right: str | None) -> float:
    left_normalized = _normalized_text(left)
    right_normalized = _normalized_text(right)
    if not left_normalized or not right_normalized:
        return 0.0
    if left_normalized == right_normalized:
        return 1.0
    if left_normalized in right_normalized or right_normalized in left_normalized:
        return 0.82
    return _token_overlap_ratio(left_normalized, right_normalized)


def _dom_path_similarity(left: str, right: str) -> float:
    left_tokens = [token for token in re.split(r"[\s>/]+", left.lower()) if token]
    right_tokens = [token for token in re.split(r"[\s>/]+", right.lower()) if token]
    if not left_tokens or not right_tokens:
        return 0.0
    shared_positions = sum(
        1 for left_token, right_token in zip(left_tokens[-6:], right_tokens[-6:], strict=False) if left_token == right_token
    )
    return shared_positions / max(1, min(6, len(left_tokens), len(right_tokens)))


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _meaningful_action_needs_missing_target_review(facts: dict[str, Any]) -> bool:
    if bool(facts.get("read_only")) or not bool(facts.get("meaningful")):
        return False
    return str(facts.get("action") or "") not in {
        "trusted_helper_js",
        "read_only_js",
        "safe_cdp",
        "read_only_cdp",
        *SAFE_UNRELATED_FORM_ACTIONS,
    }


def _expected_actions_for_fingerprint(fingerprint: dict[str, Any]) -> set[str]:
    tag = str(fingerprint.get("tag") or "").lower()
    input_type = str(fingerprint.get("type") or "").lower()
    role = str(fingerprint.get("role") or "").lower()
    attrs = _dict_or_empty(fingerprint.get("stable_attributes") or fingerprint.get("attributes"))
    text = _normalized_text(
        " ".join(
            str(value or "")
            for value in (
                fingerprint.get("text"),
                fingerprint.get("aria_label"),
                fingerprint.get("title"),
                " ".join(_normalize_tokens(fingerprint.get("semantic_hints"))),
            )
        )
    )
    actions: set[str] = set()
    if tag == "a" or role == "link" or attrs.get("href"):
        actions.add("click")
    if tag == "select" or "select" in text or role in {"listbox", "combobox", "option"}:
        actions.add("select")
    if (tag == "input" and input_type in {"checkbox", "radio"}) or role == "checkbox":
        actions.add("check")
    if tag == "input" and input_type == "file":
        actions.add("file_upload")
        actions.add("focus")
    elif tag in {"input", "textarea"} and input_type not in {"button", "submit", "reset", "checkbox", "radio", "file"}:
        actions.add("form_input")
        actions.add("focus")
    if role in {"textbox", "searchbox"} or attrs.get("contenteditable", "").lower() == "true":
        actions.add("form_input")
        actions.add("focus")
    if tag == "button" or role == "button" or input_type in {"button", "submit", "reset"}:
        actions.add("form_control_activation")
        actions.add("click")
    if input_type == "submit" or any(token in text for token in ("submit", "send", "continue", "confirm", "request", "approve", "authorize", "dispatch", "release", "queue", "proceed", "finalize")):
        actions.add("submit")
    return actions


def expected_actions_for_fingerprint(fingerprint: dict[str, Any]) -> set[str]:
    return set(_expected_actions_for_fingerprint(fingerprint))


def _score_texts(expected_texts: list[str | None], observed_texts: list[str | None]) -> tuple[int, list[str]]:
    score = 0
    signals: list[str] = []
    expected_values = [_normalized_text(value) for value in expected_texts if value]
    observed_values = [_normalized_text(value) for value in observed_texts if value]

    for expected in expected_values:
        for observed in observed_values:
            if not expected or not observed:
                continue
            if expected == observed:
                return 35, ["text_exact"]
            if expected in observed or observed in expected:
                score = max(score, 24)
                signals = ["text_contains"]
            elif _token_overlap_ratio(expected, observed) >= 0.55:
                score = max(score, 18)
                signals = ["text_tokens"]
            elif _specific_token_overlap(expected, observed):
                score = max(score, 12)
                signals = ["text_identity_tokens"]
            elif _semantic_alias_overlap(expected, observed):
                score = max(score, 10)
                signals = ["text_semantic_aliases"]
    return score, signals


def _fingerprint_primary_text_is_value_like(fingerprint: dict[str, Any]) -> bool:
    text = _normalized_text(_string_or_none(fingerprint.get("text")))
    if not text:
        return False

    attrs = _dict_or_empty(fingerprint.get("stable_attributes") or fingerprint.get("attributes"))
    placeholder = _normalized_text(_string_or_none(attrs.get("placeholder")) or _string_or_none(fingerprint.get("placeholder")))
    autocomplete = _normalized_text(_string_or_none(attrs.get("autocomplete")) or _string_or_none(fingerprint.get("autocomplete")))
    labels = fingerprint.get("labels")
    label_values: list[str] = []
    if isinstance(labels, dict):
        for key in ("associated", "accessible", "heading"):
            value = _normalized_text(_string_or_none(labels.get(key)))
            if value:
                label_values.append(value)
    label_values.extend(
        value
        for value in (
            _normalized_text(_string_or_none(fingerprint.get("nearby_text"))),
            _normalized_text(_string_or_none(fingerprint.get("parent_label"))),
            _normalized_text(_string_or_none(fingerprint.get("aria_label") or fingerprint.get("aria-label"))),
            _normalized_text(_string_or_none(fingerprint.get("title"))),
        )
        if value
    )
    if any(value == text for value in label_values):
        return False
    if any(value and value != text for value in label_values):
        return False
    if placeholder and placeholder == text:
        return True
    if autocomplete and autocomplete == text:
        return True
    return bool(re.search(r"(?:^|[\s_-])[a-z]+[\s_-]*\d{2,}|[0-9a-f]{8,}|@|https?://|\+?\d[\d\s().-]{5,}", text, re.IGNORECASE))


def _score_identity_tokens(fingerprint: dict[str, Any], target: dict[str, Any]) -> tuple[int, list[str]]:
    expected = _identity_tokens_for_fingerprint(fingerprint)
    observed = _identity_tokens_for_target(target)
    if not expected or not observed:
        return 0, []

    overlap = expected & observed
    if not overlap:
        expanded_overlap = (_expand_semantic_tokens(expected) & _expand_semantic_tokens(observed)) - _profile_stopwords()
        if len(expanded_overlap) >= 3:
            return 14, ["identity_token_aliases"]
        if len(expanded_overlap) >= 2 or expanded_overlap & _profile_high_value_tokens():
            return 10, ["identity_token_alias"]
        return 0, []

    high_value = overlap & _profile_high_value_tokens()
    if len(overlap) >= 3:
        return 22, ["identity_tokens"]
    if len(overlap) >= 2:
        return 16, ["identity_tokens"]
    if high_value:
        return 14, ["identity_token_high_value"]
    return 6, ["identity_token_weak"]


def _score_semantics(
    fingerprint: dict[str, Any],
    target: dict[str, Any],
    facts: dict[str, Any],
) -> tuple[int, list[str]]:
    score = 0
    signals: list[str] = []
    hints = set(_normalize_tokens(fingerprint.get("semantic_hints")))
    if not hints:
        return score, signals

    haystack = _normalized_text(
        " ".join(
            str(value or "")
            for value in (
                target.get("id"),
                target.get("name"),
                target.get("text"),
                target.get("aria_label"),
                target.get("title"),
                target.get("nearby_text"),
                target.get("parent_label"),
                target.get("section_heading"),
            )
        )
    )
    attrs = _attributes(target)
    haystack_tokens = set(_normalize_tokens([haystack, *attrs.values()]))
    expanded_hints = _expand_semantic_tokens(hints)
    expanded_observed = _expand_semantic_tokens(haystack_tokens)
    matched = [hint for hint in hints if hint and hint in haystack]
    alias_matches = (expanded_hints & expanded_observed) - hints - haystack_tokens - _profile_stopwords()
    if matched:
        specific = [hint for hint in matched if hint not in _profile_stopwords()]
        generic = [hint for hint in matched if hint in _profile_stopwords()]
        score += min(20, 8 * len(specific))
        if generic and not specific:
            score += 4
        signals.append("semantic_hints")
    elif alias_matches:
        score += min(14, 7 * len(alias_matches))
        signals.append("semantic_aliases")
    if bool(target.get("is_submit")) and ("submit" in hints or "request" in hints):
        score += 12
        signals.append("semantic_submit")
    if bool(target.get("is_select")) and "select" in hints:
        score += 12
        signals.append("semantic_select")
    return score, signals


def _score_action_signature(fingerprint: dict[str, Any], target: dict[str, Any]) -> tuple[int, list[str]]:
    expected_kind = _control_kind_for_fingerprint(fingerprint)
    observed_kind = _control_kind_for_target(target)
    action_kinds = {"button", "submit_button", "link"}
    if expected_kind not in action_kinds and observed_kind not in action_kinds:
        return 0, []

    expected = _action_signature_tokens_for_fingerprint(fingerprint)
    observed = _action_signature_tokens_for_target(target)
    if not expected or not observed:
        return 0, []

    overlap = expected & observed
    expanded_overlap = (_expand_semantic_tokens(expected) & _expand_semantic_tokens(observed)) - _profile_stopwords()
    score = 0
    signals: list[str] = []
    if len(overlap) >= 3:
        score += 18
        signals.append("action_signature")
    elif len(overlap) >= 2:
        score += 12
        signals.append("action_signature")
    elif overlap & _profile_high_value_tokens():
        score += 10
        signals.append("action_signature")
    elif len(expanded_overlap) >= 2:
        score += 10
        signals.append("action_signature_aliases")
    elif expanded_overlap & _profile_high_value_tokens():
        score += 8
        signals.append("action_signature_aliases")

    if expected_kind in {"button", "submit_button"} and observed_kind in {"button", "submit_button"}:
        score += 8
        signals.append("action_kind")
    if expected_kind == "submit_button" and observed_kind == "submit_button":
        score += 8
        signals.append("action_submit_kind")
        source_intent = _action_intent_analysis(fingerprint)
        target_intent = _action_intent_analysis(target)
        source_positive = set(source_intent.get("positive") or [])
        target_positive = set(target_intent.get("positive") or [])
        if target_intent.get("negative"):
            score -= 8
            signals.append("action_secondary_penalty")
        elif target_positive:
            score += 12
            signals.append("action_primary_intent")
            if _is_real_submit_control(target):
                score += 8
                signals.append("action_real_submit_control")
            if (source_positive - {"submit"}) and target_positive == {"submit"}:
                score += 6
                signals.append("action_submit_equivalent")
    if {"submit", "request"} & _expand_semantic_tokens(expected) and _profile_action_tokens("primary") & _expand_semantic_tokens(observed):
        score += 8
        signals.append("action_submit_alias")
    return min(44, max(0, score)), signals


def mapping_candidate_eligibility(fingerprint: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic candidate eligibility before page-level assignment.

    The resolver can still rank ineligible candidates for diagnostics. This gate
    decides whether the candidate is safe to turn into a concrete mapping.
    """

    expected_kind = _control_kind_for_fingerprint(fingerprint)
    observed_kind = _control_kind_for_target(target)
    hard: list[str] = []
    soft: list[str] = []
    target_intent = _action_intent_analysis(target)
    source_intent = _action_intent_analysis(fingerprint)
    source_tokens = _specific_tokens(_normalize_tokens(_field_anchor_texts_for_fingerprint(fingerprint)))
    target_tokens = _specific_tokens(_normalize_tokens(_field_anchor_texts_for_target(target)))
    source_roles = _dominant_field_role_groups(source_tokens)
    expects_sensitive_secret = bool(source_tokens & _profile_role_group("token") or _profile_role_group("token") in source_roles)
    expects_final_attestation = bool(source_tokens & (_profile_role_group("processing") | _profile_action_tokens("final_attestation")))

    if _is_hidden_control(target) and not _is_hidden_control(fingerprint):
        hard.append("hidden_implementation_control")

    observed_role = (_string_or_none(target.get("role")) or _attributes(target).get("role") or "").lower()
    if observed_role == "option" and expected_kind != "select":
        hard.append("dropdown_option_cannot_represent_non_dropdown_target")
    if observed_role == "listbox" and expected_kind != "select":
        hard.append("listbox_container_cannot_represent_non_dropdown_target")

    if expected_kind and observed_kind and not _compatible_control_kinds(expected_kind, observed_kind):
        hard.append(f"control_kind_mismatch:{expected_kind}->{observed_kind}")
    elif expected_kind and not observed_kind:
        soft.append("missing_observed_control_kind")

    if expected_kind in {"radio", "checkbox", "range", "select"} and observed_kind != expected_kind:
        hard.append(f"{expected_kind}_requires_same_control_family")

    if expected_kind == "file" and observed_kind != "file" and not _has_upload_intent(target):
        hard.append("file_target_requires_file_or_upload_intent")

    utility_negative = set(target_intent["negative"]) & {"catalog", "directory", "filter", "lookup", "picker", "preview", "roster", "search"}
    if expected_kind == "text_entry" and utility_negative and not (set(source_intent["negative"]) & utility_negative):
        soft.append("utility_text_control:" + ",".join(sorted(utility_negative)[:4]))

    if expected_kind == "text_entry" and expects_sensitive_secret:
        instructional = target_tokens & _profile_action_tokens("instructional")
        source_instructional = source_tokens & _profile_action_tokens("instructional")
        if instructional and not source_instructional:
            hard.append("instructional_helper_cannot_represent_secret_target")

    if expected_kind == "checkbox" and expects_final_attestation:
        preparatory = target_tokens & _profile_action_tokens("preparatory")
        source_preparatory = source_tokens & _profile_action_tokens("preparatory")
        final_tokens = target_tokens & _profile_action_tokens("final_attestation")
        if preparatory and not source_preparatory and not final_tokens:
            hard.append("preparatory_status_cannot_represent_final_attestation")

    if expected_kind == "select":
        classification_tokens = {"category", "class", "kind", "type"}
        shared_raw = (source_tokens & target_tokens) - _profile_stopwords()
        if target_tokens & classification_tokens and not (source_tokens & classification_tokens) and len(shared_raw) < 2:
            soft.append("classification_select_control")

    if _target_state_hidden(target) and not _target_state_hidden(fingerprint):
        hard.append("hidden_state_mismatch")
    if _target_state_disabled(target) and not _target_state_disabled(fingerprint):
        soft.append("disabled_state_changed")

    if expected_kind == "submit_button":
        if target_intent["negative"]:
            hard.append("secondary_action_intent:" + ",".join(sorted(target_intent["negative"])[:4]))
        if not target_intent["positive"]:
            soft.append("missing_primary_action_intent")

    return {
        "eligible": not hard,
        "expected_kind": expected_kind,
        "observed_kind": observed_kind,
        "hard_disqualifiers": hard,
        "soft_penalties": soft,
    }


def mapping_evidence_gate(
    fingerprint: dict[str, Any],
    target: dict[str, Any],
    resolution: TargetResolution,
    eligibility: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return whether a ranked candidate is coherent enough to enforce.

    Scores still matter, but they are no longer sufficient. Each control family
    needs human-readable evidence that matches how a person would identify the
    target on a changed page.
    """

    eligibility = dict(eligibility or mapping_candidate_eligibility(fingerprint, target))
    hard = list(eligibility.get("hard_disqualifiers") or [])
    soft = list(eligibility.get("soft_penalties") or [])
    signals = set(resolution.matched_signals)
    expected_kind = eligibility.get("expected_kind") or _control_kind_for_fingerprint(fingerprint)
    observed_kind = eligibility.get("observed_kind") or _control_kind_for_target(target)
    anchor_profile = _field_anchor_overlap_profile(fingerprint, target)
    textual_anchor = _textual_anchor_overlap(fingerprint, target) or bool(signals & {"id", "name", "selector_id", "text_exact", "text_contains"})
    semantic_anchor = _semantic_anchor_overlap(fingerprint, target)
    option_overlap = _option_set_similarity(fingerprint, target)
    intent = _action_intent_analysis(target)
    concrete = False
    reason = "insufficient coherent evidence"

    if hard:
        reason = "candidate is not eligible for concrete mapping"
    elif expected_kind == "text_entry":
        concrete = observed_kind == "text_entry" and (
            textual_anchor
            or semantic_anchor
            or resolution.identity_score >= 24
        ) and _required_field_role_overlap(fingerprint, target)
        reason = "text identity or semantic label evidence matched" if concrete else reason
    elif expected_kind in {"radio", "checkbox"}:
        concrete = observed_kind == expected_kind and (
            textual_anchor
            or semantic_anchor
            or resolution.identity_score >= 24
            or _fieldset_or_option_anchor_overlap(fingerprint, target)
        )
        reason = f"{expected_kind} option and group evidence matched" if concrete else reason
    elif expected_kind == "select":
        strong_select_anchor = (
            option_overlap >= 0.34
            or anchor_profile["raw"] >= 1
            or anchor_profile["semantic"] >= 2
            or anchor_profile["high_value"] >= 1
            or resolution.identity_score >= 30
        )
        concrete = observed_kind == "select" and (
            strong_select_anchor
        )
        reason = "select label or option-set evidence matched" if concrete else reason
    elif expected_kind == "range":
        concrete = observed_kind == "range" and (
            textual_anchor
            or semantic_anchor
            or resolution.identity_score >= 24
        )
        reason = "range metric label evidence matched" if concrete else reason
    elif expected_kind == "file":
        concrete = (observed_kind == "file" or _has_upload_intent(target)) and (
            textual_anchor
            or semantic_anchor
            or resolution.identity_score >= 20
        )
        reason = "file/upload intent evidence matched" if concrete else reason
    elif expected_kind == "submit_button":
        specific_positive = set(intent["positive"]) - {"submit"}
        submit_anchor = _submit_action_coherent(fingerprint, target, intent, anchor_profile)
        concrete = (
            observed_kind == "submit_button"
            and bool(intent["positive"])
            and not intent["negative"]
            and (bool(specific_positive) or submit_anchor)
        )
        reason = "primary submit/action intent matched" if concrete else reason
    elif expected_kind == "button":
        concrete = observed_kind in {"button", "submit_button"} and (
            textual_anchor
            or semantic_anchor
            or resolution.identity_score >= 24
            or bool(intent["positive"])
        )
        reason = "button identity or action intent matched" if concrete else reason
    else:
        concrete = textual_anchor or semantic_anchor or resolution.identity_score >= 32
        reason = "generic identity evidence matched" if concrete else reason

    if concrete and soft and resolution.score < 82:
        concrete = False
        reason = "candidate has soft eligibility penalties and lacks high confidence"

    return {
        "concrete": bool(concrete and not hard),
        "reason": reason,
        "expected_kind": expected_kind,
        "observed_kind": observed_kind,
        "hard_disqualifiers": hard,
        "soft_penalties": soft,
        "option_similarity": round(option_overlap, 3),
        "action_intent": intent,
        "anchor_overlap": anchor_profile,
        "score_components": {
            "identity": resolution.identity_score,
            "structure": resolution.structure_score,
            "context": resolution.context_score,
        },
    }


def _is_hidden_control(value: dict[str, Any]) -> bool:
    attrs = _dict_or_empty(value.get("stable_attributes") or value.get("attributes"))
    input_type = (_string_or_none(value.get("type")) or attrs.get("type") or "").lower()
    role = (_string_or_none(value.get("role")) or attrs.get("role") or "").lower()
    tag = (_string_or_none(value.get("tag")) or attrs.get("tag") or "").lower()
    if tag == "input" and input_type == "hidden":
        return True
    return bool(value.get("hidden") or attrs.get("hidden") or role == "presentation" and input_type == "hidden")


def _target_state_hidden(value: dict[str, Any]) -> bool:
    signature = value.get("structure_signature") or value.get("structureSignature")
    if isinstance(signature, dict):
        state = signature.get("element_state") or signature.get("elementState")
        if isinstance(state, dict) and "hidden" in state:
            return bool(state.get("hidden"))
    structural = value.get("structural_context")
    if isinstance(structural, dict):
        state = structural.get("element_state") or structural.get("elementState")
        if isinstance(state, dict) and "hidden" in state:
            return bool(state.get("hidden"))
    return bool(value.get("hidden"))


def _target_state_disabled(value: dict[str, Any]) -> bool:
    signature = value.get("structure_signature") or value.get("structureSignature")
    if isinstance(signature, dict):
        state = signature.get("element_state") or signature.get("elementState")
        if isinstance(state, dict) and "disabled" in state:
            return bool(state.get("disabled"))
    structural = value.get("structural_context")
    if isinstance(structural, dict):
        state = structural.get("element_state") or structural.get("elementState")
        if isinstance(state, dict) and "disabled" in state:
            return bool(state.get("disabled"))
    attrs = _dict_or_empty(value.get("stable_attributes") or value.get("attributes"))
    return bool(value.get("disabled") or str(attrs.get("disabled") or "").lower() == "true")


def _has_textual_identity_signal(signals: set[str]) -> bool:
    return bool(
        signals
        & {
            "id",
            "name",
            "selector_id",
            "text_exact",
            "text_contains",
            "text_tokens",
            "text_identity_tokens",
            "identity_tokens",
            "identity_token_high_value",
            "structure:parent_label",
            "structure:section_heading",
        }
    ) or any(signal.startswith("attr:id") or signal.startswith("attr:name") for signal in signals)


def _has_semantic_identity_signal(signals: set[str]) -> bool:
    return bool(
        signals
        & {
            "identity_token_alias",
            "identity_token_aliases",
            "semantic_aliases",
            "semantic_hints",
            "text_semantic_aliases",
            "action_signature_aliases",
            "action_submit_alias",
            "semantic_submit",
            "semantic_select",
        }
    )


def _textual_anchor_overlap(fingerprint: dict[str, Any], target: dict[str, Any]) -> bool:
    expected = _field_anchor_texts_for_fingerprint(fingerprint)
    observed = _field_anchor_texts_for_target(target)
    for left in expected:
        for right in observed:
            left_tokens = _specific_tokens(set(_normalize_tokens(left)))
            right_tokens = _specific_tokens(set(_normalize_tokens(right)))
            overlap = (_expand_semantic_tokens(left_tokens) & _expand_semantic_tokens(right_tokens)) - _profile_stopwords()
            if _same(left, right) or _text_similarity(left, right) >= 0.70 or len(overlap) >= 2:
                return True
    return False


def _semantic_anchor_overlap(fingerprint: dict[str, Any], target: dict[str, Any]) -> bool:
    expected_tokens = _specific_tokens(_normalize_tokens(_field_anchor_texts_for_fingerprint(fingerprint)))
    observed_tokens = _specific_tokens(_normalize_tokens(_field_anchor_texts_for_target(target)))
    if not expected_tokens or not observed_tokens:
        return False
    overlap = (_expand_semantic_tokens(expected_tokens) & _expand_semantic_tokens(observed_tokens)) - _profile_stopwords()
    return len(overlap) >= 2


def _field_anchor_overlap_profile(fingerprint: dict[str, Any], target: dict[str, Any]) -> dict[str, int]:
    expected_tokens = _specific_tokens(_normalize_tokens(_field_anchor_texts_for_fingerprint(fingerprint)))
    observed_tokens = _specific_tokens(_normalize_tokens(_field_anchor_texts_for_target(target)))
    if not expected_tokens or not observed_tokens:
        return {"raw": 0, "semantic": 0, "high_value": 0}
    raw = (expected_tokens & observed_tokens) - _profile_stopwords()
    semantic = (_expand_semantic_tokens(expected_tokens) & _expand_semantic_tokens(observed_tokens)) - _profile_stopwords()
    high_value = raw & _profile_high_value_tokens()
    return {
        "raw": len(raw),
        "semantic": len(semantic),
        "high_value": len(high_value),
    }


def _required_field_role_overlap(fingerprint: dict[str, Any], target: dict[str, Any]) -> bool:
    expected_tokens = _specific_tokens(_normalize_tokens(_field_anchor_texts_for_fingerprint(fingerprint)))
    if not expected_tokens:
        return True
    observed_tokens = _specific_tokens(_normalize_tokens(_field_anchor_texts_for_target(target)))
    role_groups = _dominant_field_role_groups(expected_tokens)
    for group in role_groups:
        if expected_tokens & group and not (observed_tokens & group):
            return False
    return True


def _dominant_field_role_groups(expected_tokens: set[str]) -> tuple[frozenset[str], ...]:
    """Choose the field's primary role family before applying hard gates.

    Compound labels like "sponsor email" describe a contact field, not a
    person-name field. The gate should require the contact role there without
    also forcing the new page to keep "sponsor" in the label.
    """

    for group in _profile_role_priority_groups():
        if expected_tokens & group:
            return (group,)
    return tuple(group for group in _profile_role_groups() if expected_tokens & group)


def _submit_action_coherent(
    fingerprint: dict[str, Any],
    target: dict[str, Any],
    intent: dict[str, list[str]],
    anchor_profile: dict[str, int],
) -> bool:
    if intent.get("negative"):
        return False
    if not intent.get("positive"):
        return False
    target_positive = set(intent.get("positive") or [])
    if target_positive - {"submit"}:
        return True
    if "submit" not in target_positive:
        return False
    if not _is_real_submit_control(target):
        return False
    source_positive = set(_action_intent_analysis(fingerprint).get("positive") or [])
    if source_positive - {"submit"}:
        return True
    return anchor_profile.get("raw", 0) >= 1 or anchor_profile.get("high_value", 0) >= 1


def _is_real_submit_control(target: dict[str, Any]) -> bool:
    attrs = _attributes(target)
    tag = (_string_or_none(target.get("tag")) or "").lower()
    input_type = (_string_or_none(target.get("type")) or attrs.get("type") or "").lower()
    if tag in {"button", "input"} and input_type == "submit":
        return True
    return bool(attrs.get("formaction"))


def _fieldset_or_option_anchor_overlap(fingerprint: dict[str, Any], target: dict[str, Any]) -> bool:
    expected = _human_anchor_texts_for_fingerprint(fingerprint)
    observed = _human_anchor_texts_for_target(target)
    expected_group = _normalized_text(
        " ".join(
            str(value or "")
            for value in (
                fingerprint.get("section_heading"),
                fingerprint.get("fieldset"),
                fingerprint.get("legend"),
                fingerprint.get("parent_label"),
            )
        )
    )
    observed_group = _normalized_text(
        " ".join(
            str(value or "")
            for value in (
                target.get("section_heading"),
                target.get("fieldset"),
                target.get("legend"),
                target.get("parent_label"),
            )
        )
    )
    label_match = any(_text_similarity(left, right) >= 0.68 or _semantic_alias_overlap(left, right) for left in expected for right in observed)
    group_match = bool(expected_group and observed_group and (_text_similarity(expected_group, observed_group) >= 0.62 or _semantic_alias_overlap(expected_group, observed_group)))
    return label_match and (group_match or not expected_group)


def _option_set_similarity(fingerprint: dict[str, Any], target: dict[str, Any]) -> float:
    expected = _option_text_tokens(fingerprint)
    observed = _option_text_tokens(target)
    if not expected or not observed:
        return 0.0
    expanded_expected = _expand_semantic_tokens(expected)
    expanded_observed = _expand_semantic_tokens(observed)
    overlap = (expanded_expected & expanded_observed) - _profile_stopwords()
    denom = max(1, min(len(expanded_expected), len(expanded_observed)))
    return min(1.0, len(overlap) / denom)


def _option_text_tokens(value: dict[str, Any]) -> set[str]:
    options = value.get("options")
    if not isinstance(options, list):
        return set()
    texts: list[Any] = []
    for option in options:
        if isinstance(option, dict):
            texts.extend([option.get("text"), option.get("value"), option.get("label")])
        else:
            texts.append(option)
    return _specific_tokens(_normalize_tokens(texts))


def _human_anchor_texts_for_fingerprint(fingerprint: dict[str, Any]) -> list[str]:
    values: list[Any] = [
        fingerprint.get("text"),
        fingerprint.get("aria_label") or fingerprint.get("aria-label"),
        fingerprint.get("title"),
        fingerprint.get("nearby_text"),
        fingerprint.get("parent_label"),
        fingerprint.get("section_heading"),
        fingerprint.get("semantic_hints"),
    ]
    labels = fingerprint.get("labels")
    if isinstance(labels, dict):
        values.extend(labels.get(key) for key in ("accessible", "associated", "heading", "legend", "description"))
    return [_normalized_text(str(value)) for value in _flatten_signature_values(values) if _normalized_text(str(value))]


def _human_anchor_texts_for_target(target: dict[str, Any]) -> list[str]:
    values: list[Any] = [
        target.get("text"),
        target.get("aria_label"),
        target.get("title"),
        target.get("nearby_text"),
        target.get("parent_label"),
        target.get("section_heading"),
        target.get("semantic_hints"),
    ]
    labels = target.get("labels")
    if isinstance(labels, dict):
        values.extend(labels.get(key) for key in ("accessible", "associated", "heading", "legend", "description"))
    return [_normalized_text(str(value)) for value in _flatten_signature_values(values) if _normalized_text(str(value))]


def _field_anchor_texts_for_fingerprint(fingerprint: dict[str, Any]) -> list[str]:
    values: list[Any] = [
        fingerprint.get("text"),
        fingerprint.get("aria_label") or fingerprint.get("aria-label"),
        fingerprint.get("title"),
        fingerprint.get("nearby_text"),
        fingerprint.get("parent_label"),
        fingerprint.get("id"),
        fingerprint.get("name"),
    ]
    labels = fingerprint.get("labels")
    if isinstance(labels, dict):
        values.extend(labels.get(key) for key in ("accessible", "associated", "placeholder", "description"))
    attrs = _dict_or_empty(fingerprint.get("stable_attributes") or fingerprint.get("attributes"))
    values.extend(attrs.get(key) for key in ("id", "name", "aria-label", "placeholder", "autocomplete", "data-testid", "data-test", "data-cy"))
    return [_normalized_text(str(value)) for value in _flatten_signature_values(values) if _normalized_text(str(value))]


def _field_anchor_texts_for_target(target: dict[str, Any]) -> list[str]:
    values: list[Any] = [
        target.get("text"),
        target.get("aria_label"),
        target.get("title"),
        target.get("nearby_text"),
        target.get("parent_label"),
        target.get("id"),
        target.get("name"),
    ]
    labels = target.get("labels")
    if isinstance(labels, dict):
        values.extend(labels.get(key) for key in ("accessible", "associated", "placeholder", "description"))
    attrs = _attributes(target)
    values.extend(attrs.get(key) for key in ("id", "name", "aria-label", "placeholder", "autocomplete", "data-testid", "data-test", "data-cy"))
    return [_normalized_text(str(value)) for value in _flatten_signature_values(values) if _normalized_text(str(value))]


def _action_intent_analysis(target: dict[str, Any]) -> dict[str, list[str]]:
    attrs = _attributes(target)
    direct_values: list[Any] = [
        target.get("id"),
        target.get("name"),
    ]
    direct_values.extend(attrs.get(key) for key in ("type", "role", "href", "formaction", "formmethod", "data-action", "data-route", "data-testid", "data-test", "data-cy"))
    visible_values: list[Any] = [
        target.get("text"),
        target.get("aria_label"),
        target.get("title"),
    ]
    direct_tokens = _specific_tokens(_tokens_from_values(_flatten_signature_values(direct_values)))
    visible_tokens = _specific_tokens(_tokens_from_values(_flatten_signature_values(visible_values)))
    tokens = direct_tokens | visible_tokens
    positive = sorted(tokens & _profile_action_tokens("primary"))
    negative_tokens = tokens & _profile_action_tokens("secondary")
    if _is_real_submit_control(target) and direct_tokens & _profile_action_tokens("primary"):
        negative_tokens = direct_tokens & _profile_action_tokens("secondary")
    negative = sorted(negative_tokens)
    return {"positive": positive, "negative": negative}


def _has_upload_intent(target: dict[str, Any]) -> bool:
    tokens = _expand_semantic_tokens(_identity_tokens_for_target(target) | _action_signature_tokens_for_target(target))
    return bool(tokens & _profile_action_tokens("upload"))


def _profile() -> SemanticProfile:
    profile = _ACTIVE_SEMANTIC_PROFILE.get()
    if profile.id == "acira.empty":
        return _builtin_semantic_profiles()[DEFAULT_SEMANTIC_PROFILE_ID]
    return profile


def _profile_stopwords() -> frozenset[str]:
    return _profile().stopwords


def _profile_high_value_tokens() -> frozenset[str]:
    return _profile().high_value_tokens


def _profile_alias_groups() -> tuple[frozenset[str], ...]:
    return _profile().alias_groups


def _profile_action_tokens(name: str) -> frozenset[str]:
    return _profile().action_groups.get(name, frozenset())


def _profile_role_group(name: str) -> frozenset[str]:
    return _profile().role_groups.get(name, frozenset())


def _profile_role_groups() -> tuple[frozenset[str], ...]:
    return tuple(group for group in _profile().role_groups.values() if group)


def _profile_role_priority_groups() -> tuple[frozenset[str], ...]:
    profile = _profile()
    priority = tuple(profile.role_groups[name] for name in profile.role_priority if name in profile.role_groups)
    return priority or _profile_role_groups()


def _threshold(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 70
    return min(100, max(1, parsed))


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _profile_tokens(value: Any) -> tuple[str, ...]:
    tokens = tuple(_dedupe(_normalize_tokens(value)))
    if len(tokens) > _MAX_PROFILE_TOKENS:
        raise ValueError("Semantic profile contains too many tokens")
    return tokens


def _profile_token_groups(value: Any) -> tuple[frozenset[str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TypeError("Semantic profile alias_groups must be a list")
    if len(value) > _MAX_PROFILE_ALIAS_GROUPS:
        raise ValueError("Semantic profile contains too many alias groups")
    groups: list[frozenset[str]] = []
    for item in value:
        group = frozenset(_profile_tokens(item))
        if group:
            groups.append(group)
    return tuple(groups)


def _profile_group_map(value: Any) -> dict[str, frozenset[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError("Semantic profile group maps must be objects")
    result: dict[str, frozenset[str]] = {}
    for key, raw_tokens in value.items():
        name = str(key).strip()
        if not name:
            raise ValueError("Semantic profile group name cannot be empty")
        result[name] = frozenset(_profile_tokens(raw_tokens))
    return result


def _validate_profile_size(
    stopwords: frozenset[str],
    high_value_tokens: frozenset[str],
    alias_groups: tuple[frozenset[str], ...],
    action_groups: dict[str, frozenset[str]],
    role_groups: dict[str, frozenset[str]],
) -> None:
    total = len(stopwords) + len(high_value_tokens)
    total += sum(len(group) for group in alias_groups)
    total += sum(len(group) for group in action_groups.values())
    total += sum(len(group) for group in role_groups.values())
    if total > _MAX_PROFILE_TOKENS * 8:
        raise ValueError("Semantic profile is too large")


def _resolve_path(path: str | Path, base_path: Path | None) -> Path:
    file_path = Path(path)
    if not file_path.is_absolute() and base_path is not None:
        file_path = base_path / file_path
    return file_path


def normalize_scope_url(url: str | None) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    raw = url.strip()
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        netloc = parsed.netloc
        if parsed.hostname == "::1" and not netloc.startswith("["):
            netloc = f"[::1]{f':{parsed.port}' if parsed.port else ''}"
        path = parsed.path or "/"
        return f"{parsed.scheme}://{netloc}{path}"
    return raw.split("?", 1)[0].split("#", 1)[0]


def _local_dev_url_matches(pattern: str, url: str) -> bool:
    pattern_url = urlparse(normalize_scope_url(pattern))
    current_url = urlparse(normalize_scope_url(url))
    if pattern_url.scheme not in {"http", "https"} or current_url.scheme != pattern_url.scheme:
        return False
    if not _is_loopback_host(pattern_url.hostname) or not _is_loopback_host(current_url.hostname):
        return False

    pattern_path = pattern_url.path or "/"
    current_path = current_url.path or "/"
    if fnmatch.fnmatchcase(current_path, pattern_path):
        return True

    pattern_name = pattern_path.rsplit("/", 1)[-1]
    current_name = current_path.rsplit("/", 1)[-1]
    return bool(pattern_name and pattern_name == current_name)


def _is_loopback_host(hostname: str | None) -> bool:
    return hostname in {"127.0.0.1", "localhost", "::1"}


def _normalize_strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _normalize_tokens(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw = _split_identifier_tokens(value)
    elif isinstance(value, (list, tuple, set)):
        raw = []
        for item in value:
            raw.extend(_split_identifier_tokens(str(item)))
    else:
        return ()
    return tuple(token for token in (_normalized_text(item) for item in raw) if token)


def _identity_tokens_for_fingerprint(fingerprint: dict[str, Any]) -> set[str]:
    explicit = set(_normalize_tokens(fingerprint.get("identity_tokens")))
    if explicit:
        return _specific_tokens(explicit)

    attrs = _dict_or_empty(fingerprint.get("stable_attributes") or fingerprint.get("attributes"))
    values: list[Any] = [
        fingerprint.get("id"),
        fingerprint.get("name"),
        fingerprint.get("text"),
        fingerprint.get("aria_label") or fingerprint.get("aria-label"),
        fingerprint.get("title"),
        fingerprint.get("href"),
        fingerprint.get("nearby_text"),
        fingerprint.get("parent_label"),
        fingerprint.get("section_heading"),
        fingerprint.get("previous_control_text"),
        fingerprint.get("next_control_text"),
        fingerprint.get("semantic_hints"),
    ]
    labels = fingerprint.get("labels")
    if isinstance(labels, dict):
        values.extend(labels.get(key) for key in ("accessible", "associated", "heading", "placeholder"))
    options = fingerprint.get("options")
    if isinstance(options, list):
        values.extend(option.get("text") or option.get("value") for option in options if isinstance(option, dict))
    values.extend(_flatten_signature_values(fingerprint.get("action_signature") or fingerprint.get("actionSignature")))
    values.extend(attrs.get(key) for key in ("id", "name", "aria-label", "title", "placeholder", "autocomplete", "data-testid", "data-test", "data-cy", "href", "role", "type"))
    return _specific_tokens(_tokens_from_values(values))


def _identity_tokens_for_target(target: dict[str, Any]) -> set[str]:
    attrs = _attributes(target)
    values: list[Any] = [
        target.get("id"),
        target.get("name"),
        target.get("text"),
        target.get("aria_label"),
        target.get("title"),
        target.get("href"),
        target.get("nearby_text"),
        target.get("parent_label"),
        target.get("section_heading"),
        target.get("previous_control_text"),
        target.get("next_control_text"),
        target.get("semantic_hints"),
        target.get("identity_tokens"),
    ]
    labels = target.get("labels")
    if isinstance(labels, dict):
        values.extend(labels.get(key) for key in ("accessible", "associated", "heading", "placeholder"))
    options = target.get("options")
    if isinstance(options, list):
        values.extend(option.get("text") or option.get("value") for option in options if isinstance(option, dict))
    values.extend(_flatten_signature_values(target.get("action_signature") or target.get("actionSignature")))
    values.extend(attrs.get(key) for key in ("id", "name", "aria-label", "title", "placeholder", "autocomplete", "data-testid", "data-test", "data-cy", "href", "role", "type"))
    return _specific_tokens(_tokens_from_values(values))


def _action_signature_tokens_for_fingerprint(fingerprint: dict[str, Any]) -> set[str]:
    attrs = _dict_or_empty(fingerprint.get("stable_attributes") or fingerprint.get("attributes"))
    form = _dict_or_empty(fingerprint.get("form") or fingerprint.get("form_context"))
    values: list[Any] = [
        fingerprint.get("action_signature") or fingerprint.get("actionSignature"),
        fingerprint.get("id"),
        fingerprint.get("name"),
        fingerprint.get("text"),
        fingerprint.get("nearby_text"),
        fingerprint.get("section_heading"),
        fingerprint.get("semantic_hints"),
        form.get("id"),
        form.get("name"),
        form.get("action"),
    ]
    values.extend(attrs.get(key) for key in ("type", "role", "href", "formaction", "formmethod", "data-action", "data-route", "data-testid", "data-test", "data-cy"))
    return _specific_tokens(_tokens_from_values(_flatten_signature_values(values)))


def _action_signature_tokens_for_target(target: dict[str, Any]) -> set[str]:
    attrs = _attributes(target)
    values: list[Any] = [
        target.get("action_signature") or target.get("actionSignature"),
        target.get("id"),
        target.get("name"),
        target.get("text"),
        target.get("nearby_text"),
        target.get("parent_label"),
        target.get("section_heading"),
        target.get("semantic_hints"),
        target.get("form_id"),
        target.get("form_name"),
        target.get("form_action"),
    ]
    values.extend(attrs.get(key) for key in ("type", "role", "href", "formaction", "formmethod", "data-action", "data-route", "data-testid", "data-test", "data-cy"))
    return _specific_tokens(_tokens_from_values(_flatten_signature_values(values)))


def _flatten_signature_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, dict):
        result: list[Any] = []
        for item in value.values():
            result.extend(_flatten_signature_values(item))
        return result
    if isinstance(value, (list, tuple, set)):
        result: list[Any] = []
        for item in value:
            result.extend(_flatten_signature_values(item))
        return result
    return [value]


def _tokens_from_values(values: list[Any]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        tokens.update(_normalize_tokens(value))
    return tokens


def _specific_tokens(tokens: set[str]) -> set[str]:
    return {token for token in tokens if len(token) >= 3 and token not in _profile_stopwords()}


def _expand_semantic_tokens(tokens: set[str]) -> set[str]:
    expanded = set(tokens)
    for group in _profile_alias_groups():
        if expanded & group:
            expanded.update(group)
    return expanded


def _specific_token_overlap(left: str, right: str) -> bool:
    left_tokens = _specific_tokens(set(_normalize_tokens(left)))
    right_tokens = _specific_tokens(set(_normalize_tokens(right)))
    overlap = left_tokens & right_tokens
    return len(overlap) >= 2 or bool(overlap & _profile_high_value_tokens())


def _semantic_alias_overlap(left: str, right: str) -> bool:
    left_tokens = _specific_tokens(set(_normalize_tokens(left)))
    right_tokens = _specific_tokens(set(_normalize_tokens(right)))
    if not left_tokens or not right_tokens:
        return False
    overlap = (_expand_semantic_tokens(left_tokens) & _expand_semantic_tokens(right_tokens)) - _profile_stopwords()
    return len(overlap) >= 2 or bool(overlap & _profile_high_value_tokens())


def _split_identifier_tokens(value: str) -> list[str]:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return re.findall(r"[A-Za-z0-9]+", separated)


def _control_kind_for_fingerprint(fingerprint: dict[str, Any]) -> str | None:
    explicit = _string_or_none(fingerprint.get("control_kind"))
    if explicit:
        return explicit.lower()
    attrs = _dict_or_empty(fingerprint.get("stable_attributes") or fingerprint.get("attributes"))
    tag = (_string_or_none(fingerprint.get("tag")) or "").lower()
    input_type = (_string_or_none(fingerprint.get("type")) or attrs.get("type") or "").lower()
    role = (_string_or_none(fingerprint.get("role")) or attrs.get("role") or "").lower()
    text = _normalized_text(
        " ".join(
            str(value or "")
            for value in (
                fingerprint.get("text"),
                fingerprint.get("aria_label") or fingerprint.get("aria-label"),
                fingerprint.get("title"),
                fingerprint.get("nearby_text"),
                " ".join(_normalize_tokens(fingerprint.get("semantic_hints"))),
            )
        )
    )
    return _control_kind(tag, input_type, role, attrs, text=text, is_submit=False)


def _control_kind_for_target(target: dict[str, Any]) -> str | None:
    attrs = _attributes(target)
    tag = (_string_or_none(target.get("tag")) or "").lower()
    input_type = (_string_or_none(target.get("type")) or attrs.get("type") or "").lower()
    role = (_string_or_none(target.get("role")) or attrs.get("role") or "").lower()
    text = _normalized_text(
        " ".join(
            str(value or "")
            for value in (
                target.get("text"),
                target.get("aria_label"),
                target.get("title"),
                target.get("nearby_text"),
            )
        )
    )
    return _control_kind(tag, input_type, role, attrs, text=text, is_submit=bool(target.get("is_submit")))


def _control_kind(tag: str, input_type: str, role: str, attrs: dict[str, str], *, text: str, is_submit: bool) -> str | None:
    if tag == "a" or role == "link" or attrs.get("href"):
        return "link"
    if tag == "input" and input_type == "file":
        return "file"
    if tag == "input" and input_type == "range" or role == "slider":
        return "range"
    if tag == "select" or role in {"combobox", "listbox", "option"}:
        return "select"
    if tag == "input" and input_type == "radio" or role == "radio":
        return "radio"
    if tag == "input" and input_type == "checkbox" or role in {"checkbox", "switch"}:
        return "checkbox"
    if tag in {"textarea"} or role in {"textbox", "searchbox"} or attrs.get("contenteditable", "").lower() == "true":
        return "text_entry"
    if tag == "input" and input_type not in {"button", "submit", "reset", "checkbox", "radio", "file", "range", "hidden", "image"}:
        return "text_entry"
    if tag == "button" or role == "button" or input_type in {"button", "submit", "reset"}:
        if is_submit or input_type == "submit" or any(token in text for token in ("submit", "send", "continue", "confirm", "approve", "authorize", "dispatch", "release", "queue", "proceed", "finalize")):
            return "submit_button"
        return "button"
    return tag or None


def _compatible_control_kinds(expected: str, observed: str) -> bool:
    if expected == observed:
        return True
    text_kinds = {"text_entry"}
    if expected in text_kinds and observed in text_kinds:
        return True
    select_kinds = {"select"}
    if expected in select_kinds and observed in select_kinds:
        return True
    if expected == "button" and observed == "submit_button":
        return True
    if expected == "submit_button" and observed == "button":
        return False
    return False


def _has_strong_anchor_signal(signals: list[str]) -> bool:
    return any(
        signal in {"id", "selector_id", "text_exact", "text_contains"}
        or signal.startswith("attr:id")
        or signal.startswith("attr:name")
        or signal.startswith("attr:autocomplete")
        or signal.startswith("attr:data-test")
        or signal.startswith("attr:data-cy")
        or signal.startswith("attr:aria-label")
        for signal in signals
    )


def _attributes(target: dict[str, Any]) -> dict[str, str]:
    attrs = target.get("attributes") or target.get("stable_attributes") or {}
    return _dict_or_empty(attrs)


def _dict_or_empty(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key).strip().lower(): str(item).strip() for key, item in value.items() if item is not None}


def _same(left: str, right: str) -> bool:
    return left.strip().lower() == right.strip().lower()


def _normalized_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.strip().lower())


def _token_overlap_ratio(left: str, right: str) -> float:
    left_tokens = set(re.findall(r"[a-z0-9]+", left))
    right_tokens = set(re.findall(r"[a-z0-9]+", right))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None

