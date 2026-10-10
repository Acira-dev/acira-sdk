"""PR2 step 4: payload truncation (4 KB cap per string field, after redaction)."""

from __future__ import annotations

from acira_cdp_proxy.audit.truncation import (
    PAYLOAD_FIELD_MAX_BYTES as CAP,
    truncate_event_payload,
)
from acira_cdp_proxy.audit import AuditPipeline
from acira_cdp_proxy.config import AciraGuardConfig


def test_cap_is_4kb():
    assert CAP == 4096


def test_under_cap_untouched():
    event = {"payload": {"text": "small"}, "redactions_applied": []}
    out = truncate_event_payload(event)
    assert out["payload"] == {"text": "small"}
    assert out["redactions_applied"] == []


def test_over_cap_truncated_with_siblings():
    big = "a" * 5000
    out = truncate_event_payload({"payload": {"text": big}, "redactions_applied": []})
    assert len(out["payload"]["text"].encode("utf-8")) == CAP
    assert out["payload"]["text_truncated"] is True
    assert out["payload"]["text_original_size"] == 5000
    assert "payload.text:truncated" in out["redactions_applied"]


def test_truncation_is_byte_safe_for_multibyte():
    blob = "€" * 2000  # 3 bytes each → 6000 bytes
    out = truncate_event_payload({"payload": {"expression": blob}})
    truncated = out["payload"]["expression"]
    assert len(truncated.encode("utf-8")) <= CAP
    assert "�" not in truncated  # no broken replacement char from a split codepoint
    assert out["payload"]["expression_original_size"] == 6000


def test_header_values_truncated():
    out = truncate_event_payload({"payload": {"headers": {"X-Big": "z" * 9000}}})
    assert len(out["payload"]["headers"]["X-Big"].encode("utf-8")) == CAP
    assert out["payload"]["headers"]["X-Big_truncated"] is True
    assert "payload.headers.X-Big:truncated" in out["redactions_applied"]


def test_multiple_fields_no_sibling_collision():
    out = truncate_event_payload({"payload": {"text": "a" * 5000, "expression": "b" * 6000}})
    assert out["payload"]["text_original_size"] == 5000
    assert out["payload"]["expression_original_size"] == 6000
    assert {"payload.text:truncated", "payload.expression:truncated"} <= set(out["redactions_applied"])


def test_non_string_fields_untouched():
    out = truncate_event_payload({"payload": {"method": "m", "count": 5, "flag": True, "blob": None}})
    assert out["payload"] == {"method": "m", "count": 5, "flag": True, "blob": None}


def test_missing_payload_safe():
    assert truncate_event_payload({"event_id": "x"}) == {"event_id": "x"}


# --- through the pipeline (redact then truncate) ----------------------------

def test_pipeline_truncates_after_redaction():
    cfg = AciraGuardConfig.from_dict({"enabled": True, "audit": {"privacy_mode": "medium"}})
    pipe = AuditPipeline.from_config(cfg)
    # CC at the front (redacted) followed by a long blob (truncated).
    pipe.emit({"payload": {"text": "4111 1111 1111 1111 " + "x" * 6000}, "redactions_applied": []})
    buffered = pipe.ring_buffer.snapshot()[0]
    text = buffered["payload"]["text"]
    assert "4111" not in text  # redaction happened first
    assert len(text.encode("utf-8")) <= CAP  # then truncation
    reasons = buffered["redactions_applied"]
    assert any("credit_card_number" in r for r in reasons)
    assert "payload.text:truncated" in reasons
