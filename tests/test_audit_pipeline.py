"""PR2 step 2: event bus, memory ring buffer, decision-event normalization, exactly-once."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from acira_cdp_proxy.audit import (
    AuditBus,
    AuditPipeline,
    MemoryRingBuffer,
    build_guard_decision_event,
    classify_decision,
    resolve_decision_source,
)
from acira_cdp_proxy.audit.decision_event import _target_from_classification
from acira_cdp_proxy.audit_schema import EventValidator
from acira_cdp_proxy.config import AciraGuardConfig

VALIDATOR = EventValidator()


def _decision(allow=True, reason="r", metadata=None):
    return SimpleNamespace(allow=allow, reason=reason, metadata=metadata or {})


# --- ring buffer ------------------------------------------------------------

def test_ring_buffer_byte_cap_and_eviction():
    rb = MemoryRingBuffer(cap_mb=0.01)  # ~10 KB
    for i in range(2000):
        rb.append({"event_id": f"e{i}", "payload": {"i": i, "pad": "x" * 50}})
    assert rb.size > 0
    assert rb.bytes <= rb.cap_bytes  # >1 event ⇒ stays under cap
    assert rb.evicted_count > 0
    last = rb.snapshot()[-1]
    assert last["payload"]["i"] == 1999  # newest always retained


def test_ring_buffer_keeps_newest_even_if_oversized():
    rb = MemoryRingBuffer(cap_mb=0.0001)  # ~100 bytes
    rb.append({"event_id": "big", "blob": "y" * 5000})
    assert rb.size == 1  # never drops the only/newest event


def test_ring_buffer_clear():
    rb = MemoryRingBuffer(cap_mb=1)
    rb.append({"a": 1})
    rb.clear()
    assert rb.size == 0 and rb.bytes == 0


# --- bus --------------------------------------------------------------------

def test_bus_fanout_and_error_isolation():
    rb = MemoryRingBuffer(cap_mb=1)
    seen = []
    bus = AuditBus()
    bus.subscribe(rb.append)

    def boom(_event):
        raise RuntimeError("subscriber blew up")

    bus.subscribe(boom)
    bus.subscribe(seen.append)
    bus.emit({"event_id": "x"})  # must not raise despite boom
    assert rb.size == 1 and len(seen) == 1
    assert bus.subscriber_count == 3


# --- decision normalization (the per-path tagging) --------------------------

@pytest.mark.parametrize("internal,review_result,allow,expected", [
    ("allow", None, True, ("allow", None)),
    ("block", None, False, ("block", None)),
    ("allow_after_review", "allow", True, ("ask", "approved")),
    ("allow_after_current_action_review", "allow", True, ("ask", "approved")),
    ("allow_after_cached_review", "allow_cached", True, ("ask", "approved")),
    ("block_after_review", "block", False, ("ask", "denied")),
    ("block_after_mapping_review", "block", False, ("ask", "denied")),
    ("block_after_cached_review", "block_cached", False, ("ask", "denied")),
    ("block_after_cached_mapping_review", "block_cached", False, ("ask", "denied")),
    ("continue_after_preflight_review", None, True, ("ask", "approved")),
    ("continue_after_cached_preflight_review", None, True, ("ask", "approved")),
    ("block_during_pending_review", "pending_review_blocked", False, ("block", None)),
    ("ask", None, False, ("ask", None)),
    ("", None, True, ("allow", None)),
    ("", None, False, ("block", None)),
])
def test_classify_decision(internal, review_result, allow, expected):
    md = {"decision": internal}
    if review_result is not None:
        md["review_result"] = review_result
    assert classify_decision(md, allow) == expected


@pytest.mark.parametrize("metadata,cls_meta,decision,outcome,expected", [
    ({"security_lockdown": {"x": 1}}, {}, "block", None, "security_lockdown"),
    ({"review_result": "allow"}, {}, "ask", "approved", "human_review"),
    ({"source": "general_skill"}, {}, "block", None, "general_skill"),
    ({"source": "general_skill_preflight"}, {}, "block", None, "page_preflight"),
    ({"source": "specific_skill"}, {}, "block", None, "specific_skill"),
    ({"source": "secret_policy"}, {}, "block", None, "secret_policy"),
    ({"rule_id": "blocked_actions"}, {}, "block", None, "explicit_rule"),
    ({}, {"read_only": True}, "allow", None, "read_only"),
    ({}, {}, "allow", None, "default_allow"),
    ({}, {}, "block", None, "explicit_rule"),
])
def test_resolve_decision_source(metadata, cls_meta, decision, outcome, expected):
    assert resolve_decision_source(metadata, cls_meta, decision, outcome) == expected


def test_every_decision_source_is_in_schema_enum():
    from acira_cdp_proxy.audit_schema import DECISION_SOURCES
    cases = [
        ({"security_lockdown": {}}, {}, "block", None),
        ({"review_result": "block"}, {}, "ask", "denied"),
        ({"source": "general_skill_preflight"}, {}, "block", None),
        ({"rule_id": "x"}, {}, "block", None),
        ({}, {"read_only": True}, "allow", None),
        ({}, {}, "allow", None),
        ({}, {}, "block", None),
    ]
    for md, cls, dec, out in cases:
        assert resolve_decision_source(md, cls, dec, out) in DECISION_SOURCES


# --- event builder produces schema-valid events -----------------------------

def _build(metadata, allow=True, method="Input.dispatchKeyEvent"):
    return build_guard_decision_event(
        method=method, allow=allow, reason="because", metadata=metadata,
        session_id="run-1", agent_id="agent", config_id="acira.default.json",
        acira_version="0.1.0", latency_ms=2.5, now_iso="2026-06-23T10:00:00.000000+00:00",
    )


def test_built_event_is_schema_valid_across_variants():
    variants = [
        {"decision": "allow", "classification": {"action": "click", "read_only": False}},
        {"decision": "block", "rule_id": "blocked_actions", "classification": {"action": "form_input"}},
        {"decision": "allow_after_review", "review_result": "allow", "review_cache_key": "k1",
         "source": "general_skill", "classification": {"action": "submit", "target": {"selector": "#go", "url": "https://x/y"}}},
        {"decision": "block_after_review", "review_result": "block", "review_cache_key": "k2",
         "classification": {"action": "submit"}},
        {},  # bare metadata
    ]
    for md in variants:
        event = _build(md, allow=not str(md.get("decision", "")).startswith("block"))
        assert VALIDATOR.validate(event) == [], (md, VALIDATOR.validate(event))


def test_hitl_review_id_only_present_for_ask():
    ask_event = _build({"decision": "allow_after_review", "review_result": "allow", "review_cache_key": "k"})
    assert ask_event["decision"] == "ask" and ask_event["hitl_review_id"] == "k"
    allow_event = _build({"decision": "allow", "review_cache_key": "leak"})
    assert allow_event["decision"] == "allow" and allow_event["hitl_review_id"] is None


def test_payload_is_safe_metadata_only():
    # Step 2 must not buffer user-typed content (no redaction yet).
    event = _build({"decision": "allow", "classification": {"action": "form_input", "text": "hunter2"}})
    assert "hunter2" not in str(event["payload"])
    assert set(event["payload"]) == {"method", "action"}


def test_target_defaults_are_schema_shaped():
    t = _target_from_classification({})
    assert t["url"] == "" and t["frame_path"] == [] and t["element_attributes"] == {}
    assert t["in_shadow_dom"] is False


# --- pipeline: exactly-once + emits regardless of subscribers ---------------

def test_pipeline_records_exactly_one_event():
    rb = MemoryRingBuffer(cap_mb=1)
    pipe = AuditPipeline(
        session_id="s", agent_id="a", config_id="c", acira_version="0.1.0", ring_buffer=rb,
    )
    pipe.record_decision(method="Input.dispatchKeyEvent",
                         decision=_decision(metadata={"decision": "allow", "classification": {"action": "click"}}),
                         latency_ms=1.0)
    assert rb.size == 1
    assert VALIDATOR.validate(rb.snapshot()[0]) == []


def test_pipeline_emits_with_no_extra_subscribers():
    # Ring buffer is always subscribed; events accrue even with no dashboard attached.
    rb = MemoryRingBuffer(cap_mb=1)
    pipe = AuditPipeline(session_id="s", agent_id="a", config_id="c", acira_version="0.1.0", ring_buffer=rb)
    assert pipe.bus.subscriber_count == 1
    for _ in range(5):
        pipe.record_decision(method="m", decision=_decision(metadata={"decision": "allow"}), latency_ms=0.1)
    assert rb.size == 5


def test_pipeline_redactor_runs_before_emit():
    rb = MemoryRingBuffer(cap_mb=1)
    pipe = AuditPipeline(session_id="s", agent_id="a", config_id="c", acira_version="0.1.0", ring_buffer=rb)
    pipe.set_redactor(lambda e: {**e, "redactions_applied": ["payload.x:test"]})
    pipe.record_decision(method="m", decision=_decision(metadata={"decision": "allow"}), latency_ms=0.1)
    assert rb.snapshot()[0]["redactions_applied"] == ["payload.x:test"]


def test_pipeline_from_config_uses_buffer_cap():
    cfg = AciraGuardConfig.from_dict({"enabled": True, "agent_name": "demo", "audit": {"memory_buffer_cap_mb": 3}})
    pipe = AuditPipeline.from_config(cfg, config_id="acira.test.json")
    assert pipe.ring_buffer.cap_bytes == int(3 * 1024 * 1024)
    assert pipe.agent_id == "demo" and pipe.config_id == "acira.test.json"
    assert len(pipe.session_id) > 0
