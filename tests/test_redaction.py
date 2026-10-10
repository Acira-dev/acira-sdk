"""PR2 step 3: three-tier redaction. One test per default pattern, per mode where it differs."""

from __future__ import annotations

import pytest

from acira_cdp_proxy.audit.redaction import Redactor
from acira_cdp_proxy.audit import AuditPipeline
from acira_cdp_proxy.config import AciraGuardConfig

LIGHT = Redactor("light")
MEDIUM = Redactor("medium")
CONSERVATIVE = Redactor("conservative")
ALL_MODES = [LIGHT, MEDIUM, CONSERVATIVE]


def _redact_text(redactor, text, **payload):
    event = {"payload": {"text": text, **payload}}
    return redactor.redact_event(event)


# --- text secret patterns (all modes catch these) --------------------------

@pytest.mark.parametrize("redactor", ALL_MODES)
def test_credit_card_luhn_valid_redacted(redactor):
    out = _redact_text(redactor, "card 4111 1111 1111 1111 end")
    assert "4111" not in str(out["payload"]["text"])
    assert "payload.text:credit_card_number" in out["redactions_applied"]


@pytest.mark.parametrize("redactor", ALL_MODES)
def test_credit_card_invalid_luhn_left_alone(redactor):
    out = _redact_text(redactor, "order 1234 5678 9012 3456 ok")  # not Luhn-valid
    assert "1234 5678 9012 3456" in out["payload"]["text"]
    assert not any("credit_card" in r for r in out["redactions_applied"])


@pytest.mark.parametrize("redactor", ALL_MODES)
def test_ssn(redactor):
    out = _redact_text(redactor, "ssn 123-45-6789")
    assert "123-45-6789" not in out["payload"]["text"]
    assert "payload.text:ssn" in out["redactions_applied"]


@pytest.mark.parametrize("redactor", ALL_MODES)
def test_jwt(redactor):
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV"
    out = _redact_text(redactor, f"token {jwt}")
    assert jwt not in out["payload"]["text"]
    assert "payload.text:jwt" in out["redactions_applied"]


@pytest.mark.parametrize("redactor", ALL_MODES)
def test_bearer_token(redactor):
    out = _redact_text(redactor, "Authorization: Bearer abc123.def456")
    assert "abc123.def456" not in out["payload"]["text"]
    assert "payload.text:bearer_token" in out["redactions_applied"]


@pytest.mark.parametrize("redactor", ALL_MODES)
@pytest.mark.parametrize("token", ["ya29.a0AfH6SMabcDEF12345", "1//09abcdefghij1234567890klmn"])
def test_oauth_token(redactor, token):
    out = _redact_text(redactor, f"t={token}")
    assert token not in out["payload"]["text"]
    assert "payload.text:oauth_token" in out["redactions_applied"]


@pytest.mark.parametrize("redactor", ALL_MODES)
@pytest.mark.parametrize("key", [
    "sk_live_abcdef123456", "pk_test_abcdef123456", "AKIAIOSFODNN7EXAMPLE",
    "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "xoxb-123456789012", "glpat-AbCdEf123456",
])
def test_api_keys(redactor, key):
    out = _redact_text(redactor, f"key={key}")
    assert key not in out["payload"]["text"]
    assert "payload.text:api_key" in out["redactions_applied"]


# --- field-value masking ---------------------------------------------------

@pytest.mark.parametrize("redactor", ALL_MODES)
@pytest.mark.parametrize("field", [
    {"type": "password"}, {"name": "user_password"}, {"name": "card_cvv"}, {"autocomplete": "ssn"},
])
def test_password_field_masked_all_modes(redactor, field):
    out = redactor.redact_event({"payload": {"text": "hunter2", "field": field}})
    assert out["payload"]["text"] == "[REDACTED:password_field]"
    assert "payload.text:password_field" in out["redactions_applied"]


@pytest.mark.parametrize("redactor", ALL_MODES)
def test_cc_field_masked(redactor):
    out = redactor.redact_event({"payload": {"text": "4111111111111111", "field": {"autocomplete": "cc-number"}}})
    assert out["payload"]["text"] == "[REDACTED:cc_field]"


def test_generic_input_masked_medium_not_light():
    field = {"tag": "input"}
    light = LIGHT.redact_event({"payload": {"text": "Springfield", "field": field}})
    assert light["payload"]["text"] == "Springfield"  # non-secret form value visible in Light
    medium = MEDIUM.redact_event({"payload": {"text": "Springfield", "field": field}})
    assert medium["payload"]["text"] == "[REDACTED:input_value]"


def test_textarea_value_masked_medium():
    out = MEDIUM.redact_event({"payload": {"text": "some note", "field": {"tag": "textarea"}}})
    assert out["payload"]["text"] == "[REDACTED:textarea_value]"
    assert "payload.text:textarea_value" in out["redactions_applied"]


# --- headers ---------------------------------------------------------------

@pytest.mark.parametrize("header", ["Authorization", "Cookie", "Set-Cookie", "X-API-Key", "X-Auth-Token"])
def test_auth_headers_stripped_medium(header):
    out = MEDIUM.redact_event({"payload": {"headers": {header: "secretvalue"}}})
    assert out["payload"]["headers"][header] == "[REDACTED:auth_header]"
    assert f"payload.headers.{header}:auth_header" in out["redactions_applied"]


def test_auth_headers_not_structurally_stripped_in_light():
    out = LIGHT.redact_event({"payload": {"headers": {"Cookie": "session=plainvalue"}}})
    assert out["payload"]["headers"]["Cookie"] == "session=plainvalue"


# --- conservative text nodes -----------------------------------------------

def test_text_nodes_masked_conservative_only():
    payload = {"page_text": "Visible page copy", "text_nodes": ["a", "b"]}
    medium = MEDIUM.redact_event({"payload": dict(payload)})
    assert medium["payload"]["page_text"] == "Visible page copy"
    cons = CONSERVATIVE.redact_event({"payload": dict(payload)})
    assert cons["payload"]["page_text"] == "[REDACTED:text_node]"
    assert cons["payload"]["text_nodes"] == ["[REDACTED:text_node]", "[REDACTED:text_node]"]


# --- known secrets + custom patterns ---------------------------------------

@pytest.mark.parametrize("redactor_mode", ["light", "medium", "conservative"])
def test_known_secret_masked_all_modes(redactor_mode):
    r = Redactor(redactor_mode, known_secrets=["s3cr3t-VALUE-xyz"])
    out = r.redact_event({"payload": {"text": "prefix s3cr3t-VALUE-xyz suffix"}})
    assert "s3cr3t-VALUE-xyz" not in out["payload"]["text"]
    assert "payload.text:known_secret" in out["redactions_applied"]


def test_custom_pattern_value_and_header():
    r = Redactor("medium", custom_patterns=[r"EMP-\d+", r"X-Internal-.*"])
    out = r.redact_event({"payload": {"text": "employee EMP-42", "headers": {"X-Internal-Token": "v"}}})
    assert "EMP-42" not in out["payload"]["text"]
    assert "payload.text:custom_pattern" in out["redactions_applied"]
    assert out["payload"]["headers"]["X-Internal-Token"] == "[REDACTED:auth_header]"


# --- mechanics -------------------------------------------------------------

def test_replacement_format():
    out = _redact_text(MEDIUM, "4111 1111 1111 1111")
    assert out["payload"]["text"] == "[REDACTED:credit_card_number]"


def test_non_string_payload_values_safe():
    out = MEDIUM.redact_event({"payload": {"count": 5, "flag": None, "list": [1, 2], "text": None}})
    assert out["payload"]["count"] == 5 and out["payload"]["flag"] is None


def test_redactions_applied_deduped_and_sorted():
    out = _redact_text(MEDIUM, "123-45-6789 and 987-65-4321")  # two SSNs, one reason
    assert out["redactions_applied"].count("payload.text:ssn") == 1


def test_invalid_mode_raises():
    with pytest.raises(ValueError, match="privacy mode"):
        Redactor("paranoid")


# --- end-to-end through the pipeline ---------------------------------------

def test_pipeline_redacts_before_buffer():
    cfg = AciraGuardConfig.from_dict({"enabled": True, "audit": {"privacy_mode": "medium"}})
    pipe = AuditPipeline.from_config(cfg)
    pipe.emit({"payload": {"text": "pay 4111 1111 1111 1111"}, "redactions_applied": []})
    buffered = pipe.ring_buffer.snapshot()[0]
    assert "4111" not in str(buffered["payload"])
    assert any("credit_card_number" in r for r in buffered["redactions_applied"])


def test_pipeline_default_mode_is_medium():
    cfg = AciraGuardConfig.from_dict({"enabled": True})  # no audit block → defaults
    pipe = AuditPipeline.from_config(cfg)
    # Medium masks generic input values; Light would not.
    pipe.emit({"payload": {"text": "Main Street", "field": {"tag": "input"}}, "redactions_applied": []})
    assert pipe.ring_buffer.snapshot()[0]["payload"]["text"] == "[REDACTED:input_value]"
