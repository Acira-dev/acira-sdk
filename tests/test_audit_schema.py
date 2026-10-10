"""Full coverage for the pinned v1.0 audit event schema: common + type-specific layers,
all four event types, and the version dispatcher (PR1 + PR2 step 1)."""

from __future__ import annotations

import json

import pytest

from acira_cdp_proxy.audit_schema import (
    CAPTURE_FORMATS,
    DECISION_SOURCES,
    DECISION_VALUES,
    EVENT_TYPES,
    MUTATION_TYPES,
    SCHEMA_VERSION,
    AuditSchemaError,
    EventValidator,
    new_event_id,
)

_COMMON = {
    "schema_version": "1.0",
    "event_id": "id-1",
    "timestamp_iso": "2026-06-20T12:00:00.000000+00:00",
    "session_id": "run-abc",
    "agent_id": "checkout-agent",
    "config_id": "acira.default.json",
    "acira_version": "0.1.0",
}


def valid_guard_decision() -> dict:
    return {
        **_COMMON,
        "event_id": new_event_id(),
        "event_type": "guard_decision",
        "action_type": "click",
        "target": {
            "url": "https://shop.example/checkout",
            "selector_primary": "#place-order",
            "selector_xpath": None,
            "selector_computed_css": None,
            "element_attributes": {"tag": "button", "id": "place-order"},
            "bounding_box": {"x": 10, "y": 20, "w": 100, "h": 40},
            "frame_path": ["main"],
            "in_shadow_dom": False,
            "page_title": "Checkout",
        },
        "skill_id": "acira.general.checkout",
        "risk_id": "checkout.payment_submit_attempt",
        "risk_severity": "high",
        "decision": "ask",
        "decision_source": "general_skill",
        "decision_rationale": "payment submit on a checkout page",
        "payload": {"selector": "#place-order"},
        "redactions_applied": [],
        "latency_ms": 3.4,
        "hitl_review_id": "review-1",
        "hitl_outcome": "approved",
        "snapshot_ref": None,
        "snapshot_error": None,
    }


def valid_dom_mutation() -> dict:
    return {
        **_COMMON, "event_id": new_event_id(), "event_type": "dom_mutation",
        "mutation_type": "attribute_modified",
        "target_node_id": 42,
        "payload": {"name": "class", "value": "active"},
        "redactions_applied": [],
        "snapshot_ref": {
            "snapshot_id": "snap-1", "path": "snapshots/snap-1.mhtml",
            "captured_at": "2026-06-20T12:00:00+00:00", "action_offset_ms": 5,
            "capture_format": "mhtml",
        },
    }


def valid_dom_mutations_throttled() -> dict:
    return {
        **_COMMON, "event_id": new_event_id(), "event_type": "dom_mutations_throttled",
        "count": 314, "period_ms": 1000,
    }


def valid_snapshot_captured() -> dict:
    return {
        **_COMMON, "event_id": new_event_id(), "event_type": "snapshot_captured",
        "snapshot_id": "snap-2", "path": "snapshots/snap-2.mhtml",
        "captured_at": "2026-06-20T12:00:01.000000+00:00", "capture_format": "mhtml",
        "trigger": "navigation", "action_offset_ms": 0, "size_bytes": 812345, "error": None,
    }


ALL_FACTORIES = {
    "guard_decision": valid_guard_decision,
    "dom_mutation": valid_dom_mutation,
    "dom_mutations_throttled": valid_dom_mutations_throttled,
    "snapshot_captured": valid_snapshot_captured,
}


@pytest.fixture
def validator() -> EventValidator:
    return EventValidator()


# --- every event type validates + round-trips -------------------------------

@pytest.mark.parametrize("name", sorted(ALL_FACTORIES))
def test_valid_event_passes_and_round_trips(validator, name):
    event = ALL_FACTORIES[name]()
    assert validator.validate(event) == [], (name, validator.validate(event))
    assert validator.validate(json.loads(json.dumps(event))) == []


def test_event_types_set_matches_factories():
    assert set(EVENT_TYPES) == set(ALL_FACTORIES)


# --- common layer enforced on EVERY event type ------------------------------

@pytest.mark.parametrize("name", sorted(ALL_FACTORIES))
@pytest.mark.parametrize("common", ["event_id", "timestamp_iso", "session_id", "agent_id", "config_id", "acira_version", "event_type"])
def test_common_field_required_on_every_type(validator, name, common):
    event = ALL_FACTORIES[name]()
    del event[common]
    assert any(common in e and "missing" in e for e in validator.validate(event))


def test_schema_version_missing_and_unknown(validator):
    event = valid_guard_decision()
    del event["schema_version"]
    assert any("schema_version" in e and "missing" in e for e in validator.validate(event))
    event = valid_guard_decision()
    event["schema_version"] = "0.9"
    assert any("schema_version" in e and "unsupported" in e for e in validator.validate(event))


# --- guard_decision type-specific -------------------------------------------

@pytest.mark.parametrize("field", [
    "action_type", "target", "skill_id", "risk_id", "risk_severity", "decision",
    "decision_source", "decision_rationale", "payload", "redactions_applied", "latency_ms",
    "hitl_review_id", "hitl_outcome", "snapshot_ref", "snapshot_error",
])
def test_guard_decision_type_field_required(validator, field):
    event = valid_guard_decision()
    del event[field]
    assert any(field in e and "missing" in e for e in validator.validate(event))


def test_decision_rejects_monitor_and_unknown(validator):
    for bad in ("monitor", "permit", "", None):
        event = valid_guard_decision()
        event["decision"], event["hitl_review_id"] = bad, None
        assert any(e.startswith("decision:") for e in validator.validate(event)), bad
    assert "monitor" not in DECISION_VALUES


@pytest.mark.parametrize("source", sorted(DECISION_SOURCES))
def test_every_decision_source_accepted(validator, source):
    event = valid_guard_decision()
    event["decision_source"] = source
    assert validator.validate(event) == []


def test_unknown_decision_source_rejected(validator):
    event = valid_guard_decision()
    event["decision_source"] = "made_up"
    assert any("decision_source" in e for e in validator.validate(event))


def test_redactions_applied_array_never_null(validator):
    event = valid_guard_decision()
    event["redactions_applied"] = None
    assert any("redactions_applied" in e for e in validator.validate(event))
    event["redactions_applied"] = []
    assert validator.validate(event) == []


def test_snapshot_ref_error_mutually_exclusive(validator):
    event = valid_guard_decision()
    event["snapshot_error"] = "capture timeout"
    event["snapshot_ref"] = valid_dom_mutation()["snapshot_ref"]
    assert any("snapshot_ref" in e for e in validator.validate(event))


def test_hitl_review_id_only_for_ask(validator):
    event = valid_guard_decision()
    event["decision"], event["decision_source"] = "allow", "default_allow"
    event["hitl_review_id"] = "review-x"
    assert any("hitl_review_id" in e for e in validator.validate(event))


@pytest.mark.parametrize("ts", ["2026-06-20", "not-a-date", "2026-06-20 12:00:00+00:00"])
def test_timestamp_must_be_iso_with_tz(validator, ts):
    event = valid_guard_decision()
    event["timestamp_iso"] = ts
    assert any("timestamp_iso" in e for e in validator.validate(event))


def test_target_and_bounding_box(validator):
    event = valid_guard_decision()
    del event["target"]["url"]
    assert any("target.url" in e for e in validator.validate(event))
    event = valid_guard_decision()
    event["target"]["bounding_box"] = None
    assert validator.validate(event) == []
    event["target"]["bounding_box"] = {"x": 1, "y": 2}
    assert any("bounding_box.w" in e for e in validator.validate(event))


# --- dom_mutation / throttled / snapshot type-specific ----------------------

def test_dom_mutation_required_fields_and_enum(validator):
    for field in ("mutation_type", "target_node_id", "payload", "redactions_applied", "snapshot_ref"):
        event = valid_dom_mutation()
        del event[field]
        assert any(field in e and "missing" in e for e in validator.validate(event)), field
    event = valid_dom_mutation()
    event["mutation_type"] = "exploded"
    assert any("mutation_type" in e for e in validator.validate(event))
    assert "attribute_modified" in MUTATION_TYPES


def test_dom_mutations_throttled_fields(validator):
    for field in ("count", "period_ms"):
        event = valid_dom_mutations_throttled()
        del event[field]
        assert any(field in e and "missing" in e for e in validator.validate(event))


def test_snapshot_captured_fields_and_format_enum(validator):
    for field in ("snapshot_id", "path", "captured_at", "capture_format", "trigger", "action_offset_ms"):
        event = valid_snapshot_captured()
        del event[field]
        assert any(field in e and "missing" in e for e in validator.validate(event)), field
    event = valid_snapshot_captured()
    event["capture_format"] = "jpeg"
    assert any("capture_format" in e for e in validator.validate(event))
    assert set(CAPTURE_FORMATS) == {"mhtml", "png"}
    # size_bytes / error are nullable
    event = valid_snapshot_captured()
    event["size_bytes"], event["error"] = None, "page unreachable"
    assert validator.validate(event) == []


# --- misc -------------------------------------------------------------------

def test_validate_or_raise(validator):
    validator.validate_or_raise(valid_guard_decision())
    bad = valid_guard_decision()
    bad["decision"] = "monitor"
    with pytest.raises(AuditSchemaError):
        validator.validate_or_raise(bad)


def test_constants_pinned():
    assert SCHEMA_VERSION == "1.0"
    assert DECISION_VALUES == frozenset({"allow", "ask", "block"})
