"""An advisory tool's successful exit cannot hide unaudited dependencies."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import zipfile

import pytest

spec = importlib.util.spec_from_file_location("dependency_audit", Path(__file__).resolve().parents[1] / "tools/verify_dependency_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

def report():
    return {"dependencies": [{"name": "acira", "version": "0.1.1", "vulns": []},
        {"name": "aiohttp", "version": "3.14.5", "vulns": []}]}

def inventory():
    return [{"name": "acira", "version": "0.1.1"}, {"name": "aiohttp", "version": "3.14.5"}]

def test_complete_audit_is_accepted():
    assert audit.verify_report(report(), "0.1.1", inventory()) == 1

def test_only_exact_unpublished_sdk_lookup_can_be_unavailable():
    data = report()
    data["dependencies"][0] = {"name": "acira", "skip_reason": "Dependency not found on PyPI and could not be audited: acira (0.1.1)"}
    assert audit.verify_report(data, "0.1.1", inventory()) == 1
    with pytest.raises(ValueError):
        audit.verify_report(data, "0.1.2", inventory())

@pytest.mark.parametrize("failure", ["missing", "empty", "malformed", "duplicate", "advisory", "skipped-third-party", "missing-vulns", "wrong-sdk", "missing-sdk", "only-sdk", "invalid-list", "false-sdk-lookup"])
def test_incomplete_or_unsafe_audit_is_rejected(failure):
    data = report()
    if failure == "missing": data.pop("dependencies")
    elif failure == "empty": data["dependencies"] = []
    elif failure == "malformed": data["dependencies"][1] = None
    elif failure == "duplicate": data["dependencies"].append(deepcopy(data["dependencies"][1]))
    elif failure == "advisory": data["dependencies"][1]["vulns"] = [{"id": "GHSA-test"}]
    elif failure == "skipped-third-party": data["dependencies"][1]["skip_reason"] = "unavailable"
    elif failure == "missing-vulns": data["dependencies"][1].pop("vulns")
    elif failure == "wrong-sdk": data["dependencies"][0]["version"] = "0.1.0"
    elif failure == "missing-sdk": data["dependencies"].pop(0)
    elif failure == "only-sdk": data["dependencies"].pop(1)
    elif failure == "invalid-list": data["dependencies"][1]["vulns"] = None
    elif failure == "false-sdk-lookup": data["dependencies"][0]["skip_reason"] = "ignored by reviewer"
    with pytest.raises(ValueError):
        audit.verify_report(data, "0.1.1", inventory())

def test_wheel_identity_is_read_without_importing_sdk(tmp_path):
    wheel = tmp_path / "release.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("acira-0.1.1.dist-info/METADATA", "Name: acira\nVersion: 0.1.1\n\nDescription")
        archive.writestr("acira/__init__.py", "raise RuntimeError('must never execute')")
    assert audit.wheel_version(wheel) == "0.1.1"


def test_omitted_installed_dependency_is_rejected():
    installed = inventory() + [{"name": "httpx", "version": "0.28.1"}]
    with pytest.raises(ValueError, match="omitted installed packages: httpx"):
        audit.verify_report(report(), "0.1.1", installed)


def test_changed_installed_dependency_version_is_rejected():
    installed = inventory()
    installed[1]["version"] = "3.14.6"
    with pytest.raises(ValueError, match="different installed version: aiohttp"):
        audit.verify_report(report(), "0.1.1", installed)


def test_extra_report_dependency_is_rejected():
    data = report()
    data["dependencies"].append({"name": "httpx", "version": "0.28.1", "vulns": []})
    with pytest.raises(ValueError, match="absent from the installed environment: httpx"):
        audit.verify_report(data, "0.1.1", inventory())


def test_duplicate_installed_dependency_is_rejected():
    installed = inventory() + [{"name": "Aiohttp", "version": "3.14.5"}]
    with pytest.raises(ValueError, match="duplicate identities"):
        audit.verify_report(report(), "0.1.1", installed)


def test_unpublished_sdk_exception_cannot_hide_wrong_installed_version():
    data = report()
    data["dependencies"][0] = {"name": "acira", "skip_reason": "Dependency not found on PyPI and could not be audited: acira (0.1.1)"}
    installed = inventory()
    installed[0]["version"] = "0.1.0"
    with pytest.raises(ValueError, match="Installed SDK differs"):
        audit.verify_report(data, "0.1.1", installed)


def test_skipped_sdk_record_with_wrong_explicit_version_is_rejected():
    data = report()
    data["dependencies"][0] = {"name": "acira", "version": "0.1.0", "skip_reason": "Dependency not found on PyPI and could not be audited: acira (0.1.1)"}
    with pytest.raises(ValueError, match="Unexpected missing Acira"):
        audit.verify_report(data, "0.1.1", inventory())


def test_package_name_normalization_matches_environment():
    installed = inventory() + [{"name": "browser_use", "version": "0.11.13"}]
    data = report()
    data["dependencies"].append({"name": "Browser-Use", "version": "0.11.13", "vulns": []})
    assert audit.verify_report(data, "0.1.1", installed) == 2


@pytest.mark.parametrize("installed", [None, [], [{"name": None, "version": "1"}], [{"name": "aiohttp", "version": None}]])
def test_malformed_inventory_is_rejected(installed):
    with pytest.raises(ValueError):
        audit.verify_report(report(), "0.1.1", installed)


def test_inventory_collects_exact_distribution_metadata(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(audit.metadata, "distributions", lambda: [SimpleNamespace(metadata={"Name": "aiohttp"}, version="3.14.5"), SimpleNamespace(metadata={"Name": "acira"}, version="0.1.1")])
    assert audit.installed_inventory() == inventory()


def test_cli_persists_inventory_and_verified_coverage(tmp_path, monkeypatch):
    import json
    import sys
    wheel = tmp_path / "release.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("acira-0.1.1.dist-info/METADATA", "Name: acira\nVersion: 0.1.1\n\n")
    source = tmp_path / "audit.json"
    source.write_text(json.dumps(report()))
    installed = tmp_path / "inventory.json"
    coverage = tmp_path / "coverage.json"
    monkeypatch.setattr(audit, "installed_inventory", inventory)
    monkeypatch.setattr(sys, "argv", ["audit", str(source), "--wheel", str(wheel), "--inventory-output", str(installed), "--coverage-output", str(coverage)])
    audit.main()
    assert json.loads(installed.read_text()) == inventory()
    result = json.loads(coverage.read_text())
    assert result["status"] == "verified"
    assert result["installed_package_count"] == 2
    assert result["audited_third_party_count"] == 1


def test_cli_persists_inventory_and_rejection_on_missing_package(tmp_path, monkeypatch):
    import json
    import sys
    wheel = tmp_path / "release.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("acira-0.1.1.dist-info/METADATA", "Name: acira\nVersion: 0.1.1\n\n")
    source = tmp_path / "audit.json"
    source.write_text(json.dumps(report()))
    installed = tmp_path / "inventory.json"
    coverage = tmp_path / "coverage.json"
    all_installed = inventory() + [{"name": "httpx", "version": "0.28.1"}]
    monkeypatch.setattr(audit, "installed_inventory", lambda: all_installed)
    monkeypatch.setattr(sys, "argv", ["audit", str(source), "--wheel", str(wheel), "--inventory-output", str(installed), "--coverage-output", str(coverage)])
    with pytest.raises(SystemExit) as rejected:
        audit.main()
    assert rejected.value.code == 1
    assert json.loads(installed.read_text()) == all_installed
    result = json.loads(coverage.read_text())
    assert result["status"] == "rejected"
    assert "omitted installed packages: httpx" in result["error"]
