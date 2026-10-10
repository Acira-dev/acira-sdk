"""Canonical Acira audit event schema — the single source of truth.

Every emitter, reader, and validator imports from this module.

Version policy
--------------
``SCHEMA_VERSION = "1.0"`` is the contract for v1 audit files — NOT a freeze on future
evolution. Reading dispatches on each event's ``schema_version`` (see ``_VERSION_READERS``).

ADDITIVE changes do **not** require a version bump (every shipped reader already tolerates
them):
  * new OPTIONAL fields
  * new enum values (``decision_source``, ``event_type``, mutation types, ...)
  * new event types

BREAKING changes **do** require bumping ``SCHEMA_VERSION`` AND adding a reader for the new
version while keeping every prior reader intact:
  * renaming or removing a field
  * changing a field's type
  * changing the meaning of an existing field

Acira commits to reading every schema version it has ever shipped. Add a new reader to
``_VERSION_READERS``; never replace or delete an existing one.

Event shape
-----------
Every event carries a COMMON layer (identity + routing) and a TYPE-SPECIFIC layer selected by
``event_type``. The validator enforces both. PR1/PR2 emit only ``guard_decision``;
``snapshot_captured`` / ``dom_mutation`` / ``dom_mutations_throttled`` are fully specified here
so PR3 adds emitters without changing the pinned contract.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Callable

SCHEMA_VERSION = "1.0"

# --- Frozen enums (additive: appending a value is not a version bump) --------------------

EVENT_TYPES = frozenset({
    "guard_decision",
    "snapshot_captured",
    "dom_mutation",
    "dom_mutations_throttled",
})

#: Collapsed decision model (PR1). ``monitor`` was removed — ``allow`` means permitted + recorded.
DECISION_VALUES = frozenset({"allow", "ask", "block"})

#: Canonical mechanism that produced a verdict, traced from guard.py's decision sites.
DECISION_SOURCES = frozenset({
    "security_lockdown",
    "non_agent_direction",
    "guard_disabled",
    "active_transaction",
    "known_secret_payload",
    "blocked_input_transaction",
    "explicit_rule",
    "specific_skill",
    "general_skill",
    "secret_policy",
    "page_preflight",
    "page_preflight_review",
    "dangerous_js",
    "arbitrary_js_block",
    "runtime_js_unknown",
    "read_only",
    "helper_inspection",
    "low_confidence_meaningful_action",
    "misdirected_text_block",
    "human_review",
    "default_allow",
})

HITL_OUTCOMES = frozenset({"approved", "denied", "timeout"})
CAPTURE_FORMATS = frozenset({"mhtml", "png"})

#: CDP DOM mutation kinds (snake_case), one per subscribed CDP event.
MUTATION_TYPES = frozenset({
    "attribute_modified",
    "attribute_removed",
    "character_data_modified",
    "child_node_inserted",
    "child_node_removed",
    "document_updated",
})

_ISO_TZ_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)$")


class AuditSchemaError(ValueError):
    """Raised when an audit event does not conform to the pinned schema."""


def new_event_id() -> str:
    return str(uuid.uuid4())


# --- Field validators (each returns an error string or None) -----------------------------

def _is_str(v: Any) -> str | None:
    return None if isinstance(v, str) else "must be a string"


def _nonempty_str(v: Any) -> str | None:
    if not isinstance(v, str):
        return "must be a string"
    return None if v else "must be non-empty"


def _opt_str(v: Any) -> str | None:
    return None if v is None or isinstance(v, str) else "must be a string or null"


def _is_number(v: Any) -> str | None:
    return None if isinstance(v, (int, float)) and not isinstance(v, bool) else "must be a number"


def _opt_number(v: Any) -> str | None:
    return None if v is None else _is_number(v)


def _is_bool(v: Any) -> str | None:
    return None if isinstance(v, bool) else "must be a boolean"


def _is_object(v: Any) -> str | None:
    return None if isinstance(v, dict) else "must be an object"


def _is_str_array(v: Any) -> str | None:
    if not isinstance(v, list):
        return "must be an array (never null)"
    return None if all(isinstance(x, str) for x in v) else "must contain only strings"


def _iso_ts(v: Any) -> str | None:
    if not isinstance(v, str) or not _ISO_TZ_RE.match(v):
        return "must be ISO-8601 with timezone (e.g. 2026-06-20T12:00:00.000000+00:00)"
    return None


def _enum(values: frozenset[str], *, allow_null: bool = False) -> Callable[[Any], str | None]:
    def check(v: Any) -> str | None:
        if v is None and allow_null:
            return None
        return None if v in values else f"must be {'null or ' if allow_null else ''}one of {sorted(values)}"
    return check


# --- Sub-object validators ---------------------------------------------------------------

def _validate_bounding_box(value: Any, errors: list[str], path: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append(f"{path}: must be an object with x,y,w,h or null")
        return
    for k in ("x", "y", "w", "h"):
        if k not in value:
            errors.append(f"{path}.{k}: missing")
        elif _is_number(value[k]):
            errors.append(f"{path}.{k}: must be a number")


_TARGET_FIELDS = (
    ("url", _is_str),
    ("selector_primary", _opt_str),
    ("selector_xpath", _opt_str),
    ("selector_computed_css", _opt_str),
    ("element_attributes", _is_object),
    ("bounding_box", None),  # handled separately
    ("frame_path", _is_str_array),
    ("in_shadow_dom", _is_bool),
    ("page_title", _is_str),
)


def _validate_target(value: Any, errors: list[str]) -> None:
    if not isinstance(value, dict):
        errors.append("target: must be an object")
        return
    for key, validator in _TARGET_FIELDS:
        if key not in value:
            errors.append(f"target.{key}: missing")
            continue
        if key == "bounding_box":
            _validate_bounding_box(value[key], errors, "target.bounding_box")
            continue
        err = validator(value[key])
        if err:
            errors.append(f"target.{key}: {err}")


_SNAPSHOT_REF_FIELDS = (
    ("snapshot_id", _nonempty_str),
    ("path", _is_str),
    ("captured_at", _is_str),
    ("action_offset_ms", _is_number),
    ("capture_format", _enum(CAPTURE_FORMATS)),
)


def _validate_snapshot_ref(value: Any, errors: list[str], *, path: str = "snapshot_ref") -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        errors.append(f"{path}: must be an object or null")
        return
    for key, validator in _SNAPSHOT_REF_FIELDS:
        if key not in value:
            errors.append(f"{path}.{key}: missing")
        else:
            err = validator(value[key])
            if err:
                errors.append(f"{path}.{key}: {err}")


# --- Field layers ------------------------------------------------------------------------
# COMMON is required on EVERY event regardless of type.

_COMMON_FIELDS = (
    # schema_version is owned by the dispatcher (validate); not re-checked here.
    ("event_id", _nonempty_str),
    ("timestamp_iso", _iso_ts),
    ("session_id", _nonempty_str),
    ("agent_id", _is_str),
    ("config_id", _is_str),
    ("acira_version", _is_str),
    ("event_type", _enum(EVENT_TYPES)),
)

# Type-specific layers. Sentinel validators (None) are handled inline in the reader.

_GUARD_DECISION_FIELDS = (
    ("action_type", _nonempty_str),
    ("target", None),
    ("skill_id", _opt_str),
    ("risk_id", _opt_str),
    ("risk_severity", _opt_str),
    ("decision", _enum(DECISION_VALUES)),
    ("decision_source", _enum(DECISION_SOURCES)),
    ("decision_rationale", _opt_str),
    ("payload", _is_object),
    ("redactions_applied", _is_str_array),
    ("latency_ms", _is_number),
    ("hitl_review_id", _opt_str),
    ("hitl_outcome", _enum(HITL_OUTCOMES, allow_null=True)),
    ("snapshot_ref", None),
    ("snapshot_error", _opt_str),
)

_DOM_MUTATION_FIELDS = (
    ("mutation_type", _enum(MUTATION_TYPES)),
    ("target_node_id", _is_number),
    ("payload", _is_object),
    ("redactions_applied", _is_str_array),
    ("snapshot_ref", None),
)

_DOM_MUTATIONS_THROTTLED_FIELDS = (
    ("count", _is_number),
    ("period_ms", _is_number),
)

_SNAPSHOT_CAPTURED_FIELDS = (
    ("snapshot_id", _nonempty_str),
    ("path", _is_str),
    ("captured_at", _iso_ts),
    ("capture_format", _enum(CAPTURE_FORMATS)),
    ("trigger", _nonempty_str),
    ("action_offset_ms", _is_number),
    ("size_bytes", _opt_number),
    ("error", _opt_str),
)

_TYPE_FIELDS: dict[str, tuple] = {
    "guard_decision": _GUARD_DECISION_FIELDS,
    "dom_mutation": _DOM_MUTATION_FIELDS,
    "dom_mutations_throttled": _DOM_MUTATIONS_THROTTLED_FIELDS,
    "snapshot_captured": _SNAPSHOT_CAPTURED_FIELDS,
}


def _check_fields(event: dict, fields: tuple, errors: list[str]) -> None:
    for key, validator in fields:
        if key not in event:
            errors.append(f"{key}: missing (required)")
            continue
        if key == "target":
            _validate_target(event[key], errors)
            continue
        if key == "snapshot_ref":
            _validate_snapshot_ref(event[key], errors)
            continue
        err = validator(event[key]) if validator else None
        if err:
            errors.append(f"{key}: {err}")


def _validate_v1_0(event: dict) -> list[str]:
    errors: list[str] = []
    _check_fields(event, _COMMON_FIELDS, errors)

    event_type = event.get("event_type")
    type_fields = _TYPE_FIELDS.get(event_type)
    if type_fields is None:
        # event_type already reported by the common layer if invalid; nothing more to do.
        return errors
    _check_fields(event, type_fields, errors)

    # Cross-field invariants for guard_decision.
    if event_type == "guard_decision":
        if event.get("snapshot_error") and event.get("snapshot_ref") is not None:
            errors.append("snapshot_ref: must be null when snapshot_error is set")
        if event.get("decision") != "ask" and event.get("hitl_review_id") is not None:
            errors.append("hitl_review_id: must be null unless decision == 'ask'")
    return errors


#: Reader dispatch by schema_version. Add new versions; never remove a reader.
_VERSION_READERS: dict[str, Callable[[dict], list[str]]] = {
    "1.0": _validate_v1_0,
}


class EventValidator:
    """Validates a dict against the audit event schema, dispatching on ``schema_version``.

    ``validate`` returns a list of error strings (empty == valid). ``validate_or_raise``
    raises :class:`AuditSchemaError`.
    """

    def validate(self, event: Any) -> list[str]:
        if not isinstance(event, dict):
            return ["event must be an object"]
        if "schema_version" not in event:
            return ["schema_version: missing (required)"]
        version = event["schema_version"]
        reader = _VERSION_READERS.get(version)
        if reader is None:
            return [f"schema_version {version!r}: unsupported (known: {sorted(_VERSION_READERS)})"]
        return reader(event)

    def validate_or_raise(self, event: Any) -> None:
        errors = self.validate(event)
        if errors:
            raise AuditSchemaError("; ".join(errors))

    def is_valid(self, event: Any) -> bool:
        return not self.validate(event)
