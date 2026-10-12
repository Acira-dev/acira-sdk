"""Doctor distinguishes setup failures from optional services and write access."""
import json
from pathlib import Path

import pytest

from acira.cli import run
from acira.project import initialize


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    from importlib import metadata
    versions = {"acira": "0.1.1", "playwright": "1.63.0", "browser-use": "0.11.13"}
    def version(name):
        if name not in versions:
            raise metadata.PackageNotFoundError(name)
        return versions[name]
    monkeypatch.setattr(metadata, "version", version)
    monkeypatch.setattr("acira.diagnostics.chromium_check", lambda: {"status": "ready", "path": "/verified/chromium", "fix": "python -m playwright install chromium"})
    monkeypatch.setattr("acira.service.verified_descriptor", lambda: None)
    return initialize(tmp_path / "project", selected=[], dashboard=False)


def test_missing_browser_reports_exact_repair_and_failure_exit(project, monkeypatch, capsys):
    monkeypatch.setattr("acira.diagnostics.chromium_check", lambda: {"status": "missing", "path": "/missing/chromium", "fix": "python -m playwright install chromium"})
    with pytest.raises(SystemExit) as error:
        run(["doctor", "--json", "--project", str(project.root)])
    result = json.loads(capsys.readouterr().out)
    assert error.value.code == 1 and result["healthy"] is False
    assert result["checks"]["bundled_chromium_available"] is False
    assert "python -m playwright install chromium" in result["fixes"]
    assert result["framework_compatibility"]["status"] == "verified"
    assert result["dependency_checks"]["advisory_audit"] == "not_performed"


def test_optional_dashboard_and_read_only_files_are_warnings(project, monkeypatch, capsys):
    monkeypatch.setattr("acira.diagnostics.os.access", lambda *args: False)
    run(["doctor", "--json", "--project", str(project.root)])
    result = json.loads(capsys.readouterr().out)
    assert result["healthy"] is True and not result["failures"]
    assert result["checks"]["configuration_writable"] is False
    assert any("read-only" in warning for warning in result["warnings"])
    assert any("Optional dashboard" in warning for warning in result["warnings"])


def test_broken_descriptor_is_reported_without_crash_or_credentials(project, monkeypatch, capsys):
    def broken():
        raise PermissionError("Unsafe local server descriptor permissions; run acira doctor")
    monkeypatch.setattr("acira.service.verified_descriptor", broken)
    with pytest.raises(SystemExit) as error:
        run(["doctor", "--json", "--project", str(project.root)])
    result = json.loads(capsys.readouterr().out)
    assert error.value.code == 1
    assert result["checks"]["dashboard_descriptor_valid"] is False
    assert any("ownership/permissions" in fix for fix in result["fixes"])
    assert not any(key in result for key in ("proof_key", "sdk_key", "admin_key"))


def test_invalid_configuration_is_a_structured_diagnostic(project, capsys):
    project.path.write_text("{")
    with pytest.raises(SystemExit) as error:
        run(["doctor", "--json", "--project", str(project.root)])
    result = json.loads(capsys.readouterr().out)
    assert error.value.code == 1 and result["checks"]["configuration_valid"] is False
    assert str(project.path) in result["failures"][0]
    assert "line 1, column 2" in result["failures"][0]


def test_interactive_doctor_is_readable_but_redirected_output_remains_json(project, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    run(["doctor", "--project", str(project.root)])
    assert "Ready for integration" in capsys.readouterr().out
    run(["doctor", "--json", "--project", str(project.root)])
    assert json.loads(capsys.readouterr().out)["healthy"] is True
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    run(["doctor", "--project", str(project.root)])
    assert json.loads(capsys.readouterr().out)["healthy"] is True


def test_chromium_check_uses_same_interpreter_without_browser_launch(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    import acira.diagnostics as diagnostics
    executable = tmp_path / "chrome"
    executable.write_bytes(b"fake executable")
    executable.chmod(0o700)
    calls = []
    def probe(command, **kwargs):
        calls.append(command)
        assert "chromium.launch" not in command[-1]
        return subprocess.CompletedProcess(command, 0, json.dumps({"path": str(executable)}), "")
    monkeypatch.setattr(diagnostics.subprocess, "run", probe)
    result = diagnostics.chromium_check()
    assert result["status"] == "ready" and calls[0][0] == sys.executable
    executable.unlink()
    assert diagnostics.chromium_check()["status"] == "missing"


def test_custom_browser_skip_is_explicit_unvalidated_and_never_probes(project, monkeypatch, capsys):
    monkeypatch.setattr("acira.diagnostics.chromium_check", lambda: pytest.fail("Explicit custom-browser diagnostics must not probe a bundled build"))
    run(["doctor", "--json", "--skip", "chromium", "--project", str(project.root)])
    result = json.loads(capsys.readouterr().out)
    assert result["healthy"] is True
    assert result["browser"]["status"] == "skipped"
    assert result["checks"]["bundled_chromium_available"] is None
    assert "not validated" in result["browser"]["scope"]
    assert result["checks"]["framework_version_verified"] is True
