"""Validation coverage for the `audit` config block + wiring into AciraGuardConfig."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acira_cdp_proxy.audit_config import (
    AuditConfig,
    AuditConfigError,
    parse_audit_config,
)
from acira_cdp_proxy.config import AciraGuardConfig

CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"


def test_defaults_when_absent():
    cfg = parse_audit_config(None)
    assert cfg.mode == "volatile"
    assert cfg.persistent is False
    assert cfg.privacy_mode == "medium"
    assert cfg.directory == ".acira/audits"
    assert cfg.hitl_timeout_seconds == 7200
    assert cfg.hitl_timeout_ceiling_seconds == 86400
    assert cfg.redaction_custom_patterns == ()


def test_empty_block_uses_defaults():
    assert parse_audit_config({}) == AuditConfig()


@pytest.mark.parametrize("mode,persistent", [("volatile", False), ("persistent", True), ("PERSISTENT", True)])
def test_mode_parsing(mode, persistent):
    cfg = parse_audit_config({"mode": mode})
    assert cfg.persistent is persistent


def test_invalid_mode_rejected():
    with pytest.raises(AuditConfigError, match="audit.mode"):
        parse_audit_config({"mode": "archive"})


@pytest.mark.parametrize("pm", ["light", "medium", "conservative"])
def test_privacy_modes_accepted(pm):
    assert parse_audit_config({"privacy_mode": pm}).privacy_mode == pm


def test_invalid_privacy_mode_rejected():
    with pytest.raises(AuditConfigError, match="audit.privacy_mode"):
        parse_audit_config({"privacy_mode": "paranoid"})


def test_hitl_timeout_cannot_exceed_ceiling():
    with pytest.raises(AuditConfigError, match="cannot exceed"):
        parse_audit_config({"hitl_timeout_seconds": 100000, "hitl_timeout_ceiling_seconds": 86400})
    # equal is allowed
    cfg = parse_audit_config({"hitl_timeout_seconds": 86400, "hitl_timeout_ceiling_seconds": 86400})
    assert cfg.hitl_timeout_seconds == 86400


@pytest.mark.parametrize("key", [
    "memory_buffer_cap_mb", "preflight_min_free_mb", "preflight_hitl_timeout_seconds",
    "hitl_timeout_seconds", "writer_flush_interval_seconds",
])
def test_positive_number_fields(key):
    with pytest.raises(AuditConfigError, match=key):
        parse_audit_config({key: 0})
    with pytest.raises(AuditConfigError, match=key):
        parse_audit_config({key: -5})


@pytest.mark.parametrize("key", ["persistent_overflow_cap_events", "writer_flush_max_events"])
def test_positive_int_fields(key):
    with pytest.raises(AuditConfigError, match=key):
        parse_audit_config({key: 0})
    with pytest.raises(AuditConfigError, match=key):
        parse_audit_config({key: 1.5})  # float not allowed for *_events


def test_directory_must_be_nonempty():
    with pytest.raises(AuditConfigError, match="directory"):
        parse_audit_config({"directory": "   "})


def test_redaction_custom_patterns_validated():
    cfg = parse_audit_config({"redaction_custom_patterns": [r"\d{16}", r"secret-\w+"]})
    assert cfg.redaction_custom_patterns == (r"\d{16}", r"secret-\w+")
    with pytest.raises(AuditConfigError, match="invalid regex"):
        parse_audit_config({"redaction_custom_patterns": ["(unclosed"]})
    with pytest.raises(AuditConfigError, match="must be an array"):
        parse_audit_config({"redaction_custom_patterns": "nope"})
    with pytest.raises(AuditConfigError, match="entries must be strings"):
        parse_audit_config({"redaction_custom_patterns": [123]})


def test_not_an_object():
    with pytest.raises(AuditConfigError, match="audit must be an object"):
        parse_audit_config("nope")


# --- wiring into AciraGuardConfig + repo configs ---------------------------

def test_guard_config_exposes_audit_default():
    cfg = AciraGuardConfig.from_dict({"enabled": True})
    assert isinstance(cfg.audit, AuditConfig)
    assert cfg.audit.mode == "volatile"


def test_guard_config_parses_audit_block():
    cfg = AciraGuardConfig.from_dict({"enabled": True, "audit": {"mode": "persistent", "privacy_mode": "conservative"}})
    assert cfg.audit.persistent is True
    assert cfg.audit.privacy_mode == "conservative"


def test_bad_audit_block_fails_config_load():
    with pytest.raises(AuditConfigError):
        AciraGuardConfig.from_dict({"enabled": True, "audit": {"mode": "bogus"}})


GUARD_CONFIGS = [p for p in sorted(CONFIG_DIR.glob("acira.*.json")) if "secrets" not in p.name]


@pytest.mark.parametrize("path", GUARD_CONFIGS)
def test_every_repo_config_has_valid_audit_block(path):
    cfg = AciraGuardConfig.from_file(path)
    assert isinstance(cfg.audit, AuditConfig)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "audit" in raw, f"{path.name} is missing an audit block"
    expected_persistent = path.name == "acira.default.json"
    assert cfg.audit.persistent is expected_persistent, path.name
