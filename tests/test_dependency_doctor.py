"""Adapter API evidence and dated dependency fixes are separate diagnostics."""
import importlib.util
import json
from pathlib import Path
from importlib import metadata

import pytest
from acira.project import initialize

import acira.framework_compatibility as compat
import acira.cli as cli


def install_versions(monkeypatch, **overrides):
    versions = {"acira": "0.1.1", "browser-use": "0.11.13", "playwright": "1.63.0", "anyio": "4.15.1", "pypdf": "6.20.0"}
    versions.update(overrides)
    def installed(name):
        value = versions.get(name)
        if value is None:
            raise metadata.PackageNotFoundError(name)
        return value
    monkeypatch.setattr(metadata, "version", installed)


@pytest.mark.parametrize("anyio,pypdf,status", [
    ("4.12.1", "6.16.2", "needs_attention"),
    ("4.14.2", "6.19.0", "needs_attention"),
    ("4.15.1", "6.20.0", "meets_recorded_minima"),
    ("5.0.0", "7.0.0", "needs_attention"),
    ("4.15.1rc1", "6.20.0", "needs_attention"),
    ("4.15.1+vendor", "6.20.0", "needs_attention"),
    (None, "6.20.0", "needs_attention"),
])
def test_offline_fix_checks_do_not_claim_a_complete_audit(monkeypatch, anyio, pypdf, status):
    install_versions(monkeypatch, anyio=anyio, pypdf=pypdf)
    result = compat.dependency_checks("browser_use")
    assert result["status"] == status
    assert result["advisory_audit"] == "not_performed"
    assert result["recorded_on"] == "2026-10-12"
    assert "not certification" in result["scope"]
    assert "--dry-run" in result["guidance"]
    assert compat.framework_compatibility("browser_use")["status"] == "verified"
    assert "not a dependency advisory audit" in compat.framework_compatibility("browser_use")["verification_scope"]


def test_recorded_fix_floor_is_distinct_from_supported_release_minimum(monkeypatch):
    install_versions(monkeypatch, anyio="4.14.2", pypdf="6.19.0")
    result = compat.dependency_checks("browser_use")
    assert all(item["status"] == "outside_release_dependency_range" for item in result["packages"].values())
    install_versions(monkeypatch, anyio="4.12.1", pypdf="6.16.2")
    result = compat.dependency_checks("browser_use")
    assert all(item["status"] == "below_recorded_fix_floor" for item in result["packages"].values())


def test_playwright_and_base_install_do_not_require_browser_use_dependencies(monkeypatch):
    def absent(name):
        raise AssertionError("Non-browser-use diagnostics must not probe its optional dependencies")
    monkeypatch.setattr(metadata, "version", absent)
    result = compat.dependency_checks("playwright")
    assert result["status"] == "not_applicable"
    assert result["advisory_audit"] == "not_performed"
    assert result["packages"] == {}


@pytest.mark.parametrize("safe", [False, True])
def test_doctor_separates_framework_verification_from_dependency_checks(tmp_path, monkeypatch, capsys, safe):
    import acira.framework_compatibility as runtime
    install_versions(monkeypatch, anyio="4.15.1" if safe else "4.12.1", pypdf="6.20.0" if safe else "6.16.2")
    monkeypatch.setattr(runtime, "dependency_checks", compat.dependency_checks, raising=False)
    monkeypatch.setattr(runtime, "framework_compatibility", compat.framework_compatibility)
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", adapter="browser_use", selected=[], dashboard=False)
    if safe:
        cli.run(["doctor", "--project", str(project.root), "--json"])
    else:
        with pytest.raises(SystemExit) as error:
            cli.run(["doctor", "--project", str(project.root), "--json"])
        assert error.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["checks"]["framework_version_verified"] is True
    assert result["checks"]["recorded_dependency_minima_satisfied"] is safe
    assert result["dependency_checks"]["advisory_audit"] == "not_performed"
    assert ("dependency_repair" in result) is not safe
