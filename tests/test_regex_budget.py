"""Configured expressions cannot indefinitely stall processing browser input."""
import time

import pytest

from acira_cdp_proxy.audit.redaction import Redactor
from acira_cdp_proxy.audit_config import AuditConfigError, parse_audit_config
from acira_cdp_proxy.config import AciraGuardConfig
from acira_cdp_proxy.guard import AciraGuardPolicy


def test_pathological_redaction_masks_value_and_rest_of_exhausted_record():
    value = "a"*200+"!"
    redactor = Redactor(custom_patterns=[r"(a|aa)+$"])
    start = time.monotonic()
    result = redactor.redact_event({"payload": {"text": value, "value": value, "url": "https://example.test"}})
    assert time.monotonic()-start < 0.5
    assert result["payload"]["text"] == "[REDACTED:custom_pattern_timeout]"
    assert result["payload"]["value"] == "[REDACTED:custom_pattern_timeout]"
    assert value not in str(result)
    assert "payload.text:custom_pattern_timeout" in result["redactions_applied"]


def test_pathological_header_name_masks_value_with_same_record_budget():
    name = "a"*200+"!"
    start = time.monotonic()
    result = Redactor(custom_patterns=[r"(a|aa)+$"]).redact_event({"payload": {"headers": {name: "private-header-value"}}})
    assert time.monotonic()-start < 0.5
    assert result["payload"]["headers"][name] == "[REDACTED:auth_header]"
    assert "private-header-value" not in str(result)


def test_timed_out_read_only_regex_cannot_fall_through_to_another_allow_rule():
    value = "a"*200+"!"
    policy = AciraGuardPolicy({"read_only_js_allowlist": [r"regex:(a|aa)+$", value]})
    start = time.monotonic()
    assert policy._is_read_only_js(value) is False
    assert time.monotonic()-start < 0.5


def test_ordinary_python_redaction_and_allowlist_patterns_keep_working():
    result = Redactor(custom_patterns=[r"secret-\w+"]).redact_event({"payload": {"text": "A secret-example value"}})
    assert result["payload"]["text"] == "A [REDACTED:custom_pattern] value"
    policy = AciraGuardPolicy({"read_only_js_allowlist": [r"regex:document\.title"]})
    assert policy._is_read_only_js("document.title")


@pytest.mark.parametrize("patterns", [["x"]*33, ["x"*2049], ["("*700+"a"+")"*700]])
def test_oversized_custom_pattern_configuration_is_rejected(patterns):
    with pytest.raises(AuditConfigError):
        parse_audit_config({"redaction_custom_patterns": patterns})
    with pytest.raises(ValueError):
        AciraGuardConfig.from_dict({"read_only_js_allowlist": ["regex:"+item for item in patterns]})
