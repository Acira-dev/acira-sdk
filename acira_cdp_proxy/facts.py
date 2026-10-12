from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from .redaction import redact_value, value_classes


TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
SAFE_ATTRIBUTE_KEYS = {
    "aria-label",
    "autocomplete",
    "href",
    "id",
    "name",
    "placeholder",
    "role",
    "title",
    "type",
}


@dataclass(frozen=True)
class BrowserAction:
    method: str
    action: str
    reason: str
    confidence: float
    read_only: bool
    meaningful: bool
    tokens: tuple[str, ...] = ()

    def to_metadata(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "action": self.action,
            "reason": self.reason,
            "confidence": self.confidence,
            "read_only": self.read_only,
            "meaningful": self.meaningful,
            "tokens": list(self.tokens),
        }


@dataclass(frozen=True)
class TargetFacts:
    tag: str | None = None
    type: str | None = None
    role: str | None = None
    kind: str | None = None
    text: str | None = None
    id: str | None = None
    name: str | None = None
    aria_label: str | None = None
    title: str | None = None
    placeholder: str | None = None
    url: str | None = None
    href: str | None = None
    nearby_text: str | None = None
    ancestor_text: str | None = None
    section_heading: str | None = None
    form_id: str | None = None
    form_name: str | None = None
    form_action: str | None = None
    is_button: bool = False
    is_editable: bool = False
    is_form_control: bool = False
    is_submit: bool = False
    is_native_submit: bool | None = None
    tokens: tuple[str, ...] = ()
    identity_tokens: tuple[str, ...] = ()
    nearby_tokens: tuple[str, ...] = ()
    section_tokens: tuple[str, ...] = ()
    form_tokens: tuple[str, ...] = ()
    attributes: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "tag": self.tag,
            "type": self.type,
            "role": self.role,
            "kind": self.kind,
            "text": self.text,
            "id": self.id,
            "name": self.name,
            "aria_label": self.aria_label,
            "title": self.title,
            "placeholder": self.placeholder,
            "url": self.url,
            "href": self.href,
            "nearby_text": self.nearby_text,
            "ancestor_text": self.ancestor_text,
            "section_heading": self.section_heading,
            "form_id": self.form_id,
            "form_name": self.form_name,
            "form_action": self.form_action,
            "is_button": self.is_button,
            "is_editable": self.is_editable,
            "is_form_control": self.is_form_control,
            "is_submit": self.is_submit,
            "is_native_submit": self.is_native_submit,
            "tokens": list(self.tokens),
            "identity_tokens": list(self.identity_tokens),
            "nearby_tokens": list(self.nearby_tokens),
            "section_tokens": list(self.section_tokens),
            "form_tokens": list(self.form_tokens),
            "attributes": dict(self.attributes),
            "metadata": redact_value(self.metadata),
        }


@dataclass(frozen=True)
class PageFacts:
    url: str | None = None
    hostname: str | None = None
    title: str | None = None
    tokens: tuple[str, ...] = ()
    url_tokens: tuple[str, ...] = ()
    title_tokens: tuple[str, ...] = ()
    control_tokens: tuple[str, ...] = ()
    target_count: int = 0
    controls: tuple[dict[str, Any], ...] = ()

    def to_metadata(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "hostname": self.hostname,
            "title": self.title,
            "tokens": list(self.tokens),
            "url_tokens": list(self.url_tokens),
            "title_tokens": list(self.title_tokens),
            "control_tokens": list(self.control_tokens),
            "target_count": self.target_count,
            "controls": [redact_value(item) for item in self.controls],
        }


@dataclass(frozen=True)
class FormFacts:
    tokens: tuple[str, ...] = ()

    def to_metadata(self) -> dict[str, Any]:
        return {"tokens": list(self.tokens)}


@dataclass(frozen=True)
class ValueFacts:
    present: bool = False
    length: int = 0
    classes: tuple[str, ...] = ()

    def to_metadata(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "length": self.length,
            "classes": list(self.classes),
        }


@dataclass(frozen=True)
class NetworkFacts:
    tokens: tuple[str, ...] = ()

    def to_metadata(self) -> dict[str, Any]:
        return {"tokens": list(self.tokens)}


@dataclass(frozen=True)
class SessionFacts:
    recent_actions: tuple[str, ...] = ()

    def to_metadata(self) -> dict[str, Any]:
        return {"recent_actions": list(self.recent_actions)}


@dataclass(frozen=True)
class SecretFacts:
    present: bool = False
    id: str | None = None
    label: str | None = None
    provider: str | None = None
    ref: str | None = None
    classification: str | None = None
    tags: tuple[str, ...] = ()
    policy_decision: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "id": self.id,
            "label": self.label,
            "provider": self.provider,
            "ref": self.ref,
            "classification": self.classification,
            "tags": list(self.tags),
            "policy_decision": self.policy_decision,
        }


@dataclass(frozen=True)
class BrowserEventFacts:
    action: BrowserAction
    target: TargetFacts
    page: PageFacts
    form: FormFacts
    value: ValueFacts
    network: NetworkFacts = NetworkFacts()
    session: SessionFacts = SessionFacts()
    secret: SecretFacts = SecretFacts()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.to_metadata(),
            "target": self.target.to_metadata(),
            "element": self.target.to_metadata(),
            "page": self.page.to_metadata(),
            "form": self.form.to_metadata(),
            "value": self.value.to_metadata(),
            "network": self.network.to_metadata(),
            "session": self.session.to_metadata(),
            "secret": self.secret.to_metadata(),
            "details": redact_value(self.details),
            "url": self.page.url,
            "method": self.action.method,
        }


def build_browser_event_facts(
    classification: Any,
    parsed_message: dict[str, Any] | None = None,
) -> BrowserEventFacts:
    target_data = _target_metadata(classification)
    target = _target_facts(target_data)
    page = _page_facts(target, classification, target_data)
    form = _form_facts(target, target_data, page)
    value = _value_facts(parsed_message)
    details = getattr(classification, "details", None) if isinstance(getattr(classification, "details", None), dict) else {}
    secret = _secret_facts(details)
    action = BrowserAction(
        method=str(getattr(classification, "method", "") or ""),
        action=str(getattr(classification, "action", "") or ""),
        reason=str(getattr(classification, "reason", "") or ""),
        confidence=float(getattr(classification, "confidence", 0.0) or 0.0),
        read_only=bool(getattr(classification, "read_only", False)),
        meaningful=bool(getattr(classification, "meaningful", False)),
        tokens=_tokens(getattr(classification, "action", ""), getattr(classification, "method", ""), getattr(classification, "reason", "")),
    )
    return BrowserEventFacts(
        action=action,
        target=target,
        page=page,
        form=form,
        value=value,
        secret=secret,
        details=details,
    )


def build_page_preflight_facts(
    page_targets: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build normalized facts for page-level general skill preflight.

    This uses the same target normalizer as action-time checks, but aggregates
    all current-page controls into neutral, redacted summaries. Installed
    general skills own any domain-specific interpretation of those controls.
    """

    probe = dict(metadata or {})
    targets = [dict(item) for item in page_targets if isinstance(item, dict)]
    target_facts = [_target_facts(item) for item in targets]
    url = _string_or_none(probe.get("url")) or _url_from_targets(targets)
    title = _string_or_none(probe.get("title"))
    hostname = urlparse(url).hostname if isinstance(url, str) and url else None
    text_parts: list[Any] = [url, title]
    control_text_parts: list[Any] = [title]
    for item in targets[:160]:
        attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        parts = [
            item.get("text"),
            item.get("id"),
            item.get("name"),
            item.get("aria_label"),
            item.get("ariaLabel"),
            item.get("nearby_text"),
            item.get("nearbyText"),
            item.get("section_heading"),
            item.get("sectionHeading"),
            item.get("ancestor_text"),
            item.get("ancestorText"),
            item.get("form_id"),
            item.get("formId"),
            item.get("form_name"),
            item.get("formName"),
            item.get("form_action"),
            item.get("formAction"),
            attrs.get("autocomplete"),
            attrs.get("placeholder"),
        ]
        text_parts.extend(parts)
        control_text_parts.extend(parts)
    control_inventory = tuple(_control_inventory_item(raw, target) for raw, target in zip(targets, target_facts, strict=False))
    all_form_tokens = tuple(sorted({token for target in target_facts for token in target.form_tokens + target.section_tokens + target.nearby_tokens}))
    page = PageFacts(
        url=url,
        hostname=hostname,
        title=title,
        tokens=_tokens(*text_parts),
        url_tokens=_tokens(url, hostname),
        title_tokens=_tokens(title),
        control_tokens=_tokens(*control_text_parts),
        target_count=len(targets),
        controls=control_inventory,
    )
    return {
        "action": BrowserAction(
            method="Acira.preflight",
            action="page_preflight",
            reason=str(probe.get("status") or "page preflight"),
            confidence=1.0,
            read_only=True,
            meaningful=False,
            tokens=("page", "preflight"),
        ).to_metadata(),
        "target": TargetFacts().to_metadata(),
        "element": TargetFacts().to_metadata(),
        "page": page.to_metadata(),
        "form": FormFacts(tokens=all_form_tokens).to_metadata(),
        "value": ValueFacts().to_metadata(),
        "network": NetworkFacts().to_metadata(),
        "session": SessionFacts().to_metadata(),
        "secret": SecretFacts().to_metadata(),
        "details": redact_value(
            {
                "page_target_probe": probe,
                "target_count": len(targets),
            }
        ),
        "url": url,
        "method": "Acira.preflight",
    }


def _target_metadata(classification: Any) -> dict[str, Any]:
    target = getattr(classification, "target", None)
    if target is not None and hasattr(target, "to_metadata"):
        metadata = target.to_metadata()
        return metadata if isinstance(metadata, dict) else {}
    return {}


def _secret_facts(details: dict[str, Any]) -> SecretFacts:
    secret_use = details.get("secret_use") if isinstance(details.get("secret_use"), dict) else None
    if secret_use is None:
        return SecretFacts()
    tags = secret_use.get("tags")
    if not isinstance(tags, list):
        tags = []
    return SecretFacts(
        present=True,
        id=_string_or_none(secret_use.get("id")),
        label=_string_or_none(secret_use.get("label")),
        provider=_string_or_none(secret_use.get("provider")),
        ref=_string_or_none(secret_use.get("ref")),
        classification=_string_or_none(secret_use.get("classification")),
        tags=tuple(str(item) for item in tags if str(item).strip()),
        policy_decision=_string_or_none(secret_use.get("policy_decision")),
    )


def _target_facts(target: dict[str, Any]) -> TargetFacts:
    attributes = target.get("attributes") if isinstance(target.get("attributes"), dict) else {}
    safe_attributes = _safe_attributes(attributes)
    kind = _target_kind(target, attributes)
    identity_parts = [
        target.get("text"),
        target.get("id"),
        target.get("name"),
        target.get("aria_label"),
        target.get("title"),
        attributes.get("placeholder"),
        attributes.get("autocomplete"),
        target.get("href"),
    ]
    nearby_parts = [
        target.get("nearby_text"),
        target.get("parent_label"),
        target.get("previous_control_text"),
        target.get("next_control_text"),
    ]
    section_parts = [
        target.get("ancestor_text"),
        target.get("section_heading"),
    ]
    form_parts = [
        target.get("form_id"),
        target.get("form_name"),
        target.get("form_action"),
    ]
    identity_tokens = _tokens(*identity_parts)
    nearby_tokens = _tokens(*nearby_parts)
    section_tokens = _tokens(*section_parts)
    form_tokens = _tokens(*form_parts)
    all_tokens = tuple(sorted(set(identity_tokens + nearby_tokens + section_tokens + form_tokens)))
    return TargetFacts(
        tag=_string_or_none(target.get("tag")),
        type=_string_or_none(target.get("type")),
        role=_string_or_none(target.get("role")),
        kind=kind,
        text=_string_or_none(target.get("text")),
        id=_string_or_none(target.get("id")),
        name=_string_or_none(target.get("name")),
        aria_label=_string_or_none(target.get("aria_label")),
        title=_string_or_none(target.get("title")),
        placeholder=_string_or_none(attributes.get("placeholder")),
        url=_string_or_none(target.get("url")),
        href=_string_or_none(target.get("href")),
        nearby_text=_string_or_none(target.get("nearby_text")),
        ancestor_text=_string_or_none(target.get("ancestor_text")),
        section_heading=_string_or_none(target.get("section_heading")),
        form_id=_string_or_none(target.get("form_id")),
        form_name=_string_or_none(target.get("form_name")),
        form_action=_string_or_none(target.get("form_action")),
        is_button=bool(target.get("is_button")),
        is_editable=bool(target.get("is_editable")),
        is_form_control=bool(target.get("is_form_control")),
        is_submit=bool(target.get("is_submit")),
        is_native_submit=target.get("is_native_submit") if isinstance(target.get("is_native_submit"), bool) else None,
        tokens=all_tokens,
        identity_tokens=identity_tokens,
        nearby_tokens=nearby_tokens,
        section_tokens=section_tokens,
        form_tokens=form_tokens,
        attributes=safe_attributes,
        metadata=target,
    )


def _page_facts(target: TargetFacts, classification: Any, target_data: dict[str, Any]) -> PageFacts:
    url = target.url or _string_or_none(target_data.get("url"))
    details = getattr(classification, "details", None) if isinstance(getattr(classification, "details", None), dict) else {}
    page_targets = details.get("page_targets") if isinstance(details.get("page_targets"), list) else []
    normalized_targets = [item for item in page_targets if isinstance(item, dict)]
    target_facts = [_target_facts(item) for item in normalized_targets]
    text_parts: list[Any] = [url]
    control_text_parts: list[Any] = []
    for item in normalized_targets[:80]:
        parts = [item.get("text"), item.get("id"), item.get("name"), item.get("aria_label")]
        text_parts.extend([*parts, item.get("url")])
        control_text_parts.extend(parts)
    hostname = None
    if isinstance(url, str) and url:
        hostname = urlparse(url).hostname
    controls = tuple(_control_inventory_item(raw, facts) for raw, facts in zip(normalized_targets, target_facts, strict=False))
    return PageFacts(
        url=url,
        hostname=hostname,
        tokens=_tokens(*text_parts),
        url_tokens=_tokens(url, hostname),
        control_tokens=_tokens(*control_text_parts),
        target_count=len(normalized_targets),
        controls=controls,
    )


def _form_facts(target: TargetFacts, target_data: dict[str, Any], page: PageFacts) -> FormFacts:
    form_tokens = _tokens(target.form_id, target.form_name, target.form_action, target.section_heading, target.ancestor_text)
    all_tokens = tuple(sorted(set(form_tokens + target.form_tokens + target.section_tokens + target.nearby_tokens + target.identity_tokens)))
    return FormFacts(tokens=all_tokens)


def _control_inventory_item(raw: dict[str, Any], target: TargetFacts) -> dict[str, Any]:
    attrs = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
    structure = raw.get("structure_signature") or raw.get("structureSignature") or {}
    kind = structure.get("target_kind") if isinstance(structure, dict) else None
    element_state = structure.get("element_state") if isinstance(structure, dict) and isinstance(structure.get("element_state"), dict) else {}
    return {
        "tag": target.tag,
        "type": target.type,
        "role": target.role,
        "kind": target.kind,
        "label": target.text or target.aria_label or target.nearby_text or target.name or target.id,
        "id": target.id or attrs.get("id"),
        "name": target.name or attrs.get("name"),
        "form_id": target.form_id,
        "section": target.section_heading,
        "kind": kind,
        "is_button": target.is_button,
        "is_editable": target.is_editable,
        "is_form_control": target.is_form_control,
        "is_submit": target.is_submit,
        "is_native_submit": target.is_native_submit,
        "is_disabled": bool(element_state.get("disabled")),
        "is_hidden": bool(element_state.get("hidden")),
        "attributes": _safe_attributes(attrs),
        "tokens": list(target.tokens),
        "identity_tokens": list(target.identity_tokens),
        "nearby_tokens": list(target.nearby_tokens),
        "section_tokens": list(target.section_tokens),
        "form_tokens": list(target.form_tokens),
    }


def _url_from_targets(targets: list[dict[str, Any]]) -> str | None:
    for target in targets:
        url = _string_or_none(target.get("url"))
        if url:
            return url
    return None


def _value_facts(parsed_message: dict[str, Any] | None) -> ValueFacts:
    params = parsed_message.get("params") if isinstance(parsed_message, dict) and isinstance(parsed_message.get("params"), dict) else {}
    value: Any = None
    for key in ("text", "value"):
        if isinstance(params.get(key), str):
            value = params[key]
            break
    if value is None:
        return ValueFacts()
    text = str(value)
    return ValueFacts(present=True, length=len(text), classes=value_classes(text))


def _safe_attributes(attributes: dict[str, Any]) -> dict[str, str]:
    return {
        str(key).lower(): str(value).strip()
        for key, value in attributes.items()
        if str(key).lower() in SAFE_ATTRIBUTE_KEYS and value is not None and str(value).strip()
    }


def _target_kind(target: dict[str, Any], attributes: dict[str, Any]) -> str | None:
    structure = target.get("structure_signature") or target.get("structureSignature") or {}
    if isinstance(structure, dict):
        kind = _string_or_none(structure.get("target_kind"))
        if kind:
            return kind
    tag = str(target.get("tag") or "").lower()
    input_type = str(target.get("type") or attributes.get("type") or "").lower()
    role = str(target.get("role") or attributes.get("role") or "").lower()
    if tag == "a" or role == "link" or attributes.get("href"):
        return "link"
    if tag == "input" and input_type == "file":
        return "file"
    if tag == "select" or role in {"combobox", "listbox", "option"}:
        return "select"
    if tag == "input" and input_type in {"checkbox", "radio"}:
        return input_type
    if tag == "textarea" or role in {"textbox", "searchbox"} or bool(target.get("is_editable")):
        return "text_entry"
    if bool(target.get("is_button")) or tag == "button" or role == "button" or input_type in {"button", "submit", "reset"}:
        return "button"
    return tag or None


def _tokens(*values: Any) -> tuple[str, ...]:
    tokens: set[str] = set()
    for value in values:
        if value is None:
            continue
        for token in TOKEN_RE.findall(str(value).lower()):
            if token:
                tokens.add(token)
    return tuple(sorted(tokens))


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
