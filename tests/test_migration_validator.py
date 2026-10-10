"""Migration validator (PR1) — rejects every documented stale form with a clear message."""

from __future__ import annotations

from pathlib import Path

import pytest

from acira_cdp_proxy.config import AciraGuardConfig
from acira_cdp_proxy.general_skills import GENERAL_SKILL_SCHEMA, GeneralSkill
from acira_cdp_proxy.migration_validator import (
    EXPECTED_SKILL_SCHEMA,
    MigrationError,
    check_config,
    check_skill,
)


def test_skill_schema_constants_in_sync():
    # The validator carries its own copy of the expected schema to avoid a circular import;
    # this guards against drift.
    assert GENERAL_SKILL_SCHEMA == EXPECTED_SKILL_SCHEMA


# --- check_config -----------------------------------------------------------

def test_rejects_legacy_risk_policy():
    with pytest.raises(MigrationError) as exc:
        check_config({"risk_policy": {"x": "ask"}}, source="cfg.json")
    msg = str(exc.value)
    assert "cfg.json" in msg and "'risk_policy' is no longer supported" in msg


def test_rejects_legacy_page_risk_policy():
    with pytest.raises(MigrationError) as exc:
        check_config({"page_risk_policy": {"x": "ask"}}, source="cfg.json")
    assert "'page_risk_policy' is no longer supported" in str(exc.value)


def test_rejects_list_form_general_skills():
    with pytest.raises(MigrationError) as exc:
        check_config({"general_skills": ["../skills/general/checkout.json"]}, source="cfg.json")
    assert "must be an object keyed by skill id" in str(exc.value)


def test_rejects_monitor_in_config_risks():
    data = {"general_skills": {"acira.general.checkout": {
        "path": "x", "risks": {"checkout.card_data_entry": "monitor"}}}}
    with pytest.raises(MigrationError) as exc:
        check_config(data, source="cfg.json")
    msg = str(exc.value)
    assert "monitor" in msg and "checkout.card_data_entry" in msg and "Replace with 'allow'" in msg


def test_valid_config_passes():
    check_config({"general_skills": {"acira.general.checkout": {
        "path": "x", "risks": {"checkout.card_data_entry": "ask"}}}}, source="cfg.json")


# --- check_skill ------------------------------------------------------------

def test_rejects_v2_skill_schema():
    with pytest.raises(MigrationError) as exc:
        check_skill({"schema": "acira.general_skill.v2", "id": "x"}, source="sk.json")
    msg = str(exc.value)
    assert "sk.json" in msg and "no longer supported" in msg


def test_rejects_monitor_in_skill_decisions():
    data = {"schema": EXPECTED_SKILL_SCHEMA, "risks": [
        {"id": "x.y", "decisions": ["allow", "monitor", "ask", "block"]}]}
    with pytest.raises(MigrationError) as exc:
        check_skill(data, source="sk.json")
    assert "'monitor'" in str(exc.value)


def test_valid_skill_passes():
    check_skill({"schema": EXPECTED_SKILL_SCHEMA, "risks": [
        {"id": "x.y", "decisions": ["allow", "ask", "block"]}]}, source="sk.json")


# --- end-to-end through the real loaders ------------------------------------

def test_from_dict_rejects_stale_config():
    # from_file delegates to from_dict, so exercising from_dict covers the full load path
    # without touching the filesystem.
    with pytest.raises(MigrationError):
        AciraGuardConfig.from_dict(
            {"schema": "acira.config.v1", "general_skills": {}, "risk_policy": {"x": "ask"}},
            source="stale.json",
        )


def test_general_skill_from_dict_rejects_v2():
    with pytest.raises(MigrationError):
        GeneralSkill.from_dict({"schema": "acira.general_skill.v2", "id": "x"}, source="v2.json")


def test_all_repo_configs_and_skills_load_clean():
    root = Path(__file__).resolve().parents[1]
    configs = list((root / "configs").glob("acira.*.json"))
    skills = list((root / "skills/general").glob("*.json"))
    assert configs and skills, "Configuration fixtures must be present"
    for f in configs:
        if "secret" in f.name.lower():
            continue
        AciraGuardConfig.from_file(f)
    for f in skills:
        GeneralSkill.from_file(f)
