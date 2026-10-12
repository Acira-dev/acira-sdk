"""Malformed private discovery data must fail safely without echoing credentials."""
import json

import pytest

from acira.diagnostics import doctor
from acira.local import atomic_json, state_dir
from acira.project import initialize
from acira.service import descriptor

SENTINEL = "private-descriptor-sentinel"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    monkeypatch.setattr("acira.diagnostics.chromium_check", lambda: {"status": "ready"})
    return initialize(tmp_path / "project", selected=[], dashboard=False)


def save_descriptor(**overrides):
    value = {"protocol": 1, "url": "http://127.0.0.1:45678",
             "proof_key": SENTINEL, "sdk_key": SENTINEL, "admin_key": SENTINEL}
    value.update(overrides)
    atomic_json(state_dir() / "server.json", value, private=True)


@pytest.mark.parametrize("url", [f"http://127.0.0.1:{SENTINEL}", 12345, {"url": SENTINEL}, None])
def test_malformed_descriptor_url_returns_safe_doctor_failure(project, url):
    save_descriptor(url=url)
    with pytest.raises(ValueError, match="Invalid loopback service descriptor") as error:
        descriptor()
    assert SENTINEL not in str(error.value)
    result = doctor(project, "default")
    assert result["healthy"] is False
    assert result["checks"]["dashboard_descriptor_valid"] is False
    assert SENTINEL not in json.dumps(result)


@pytest.mark.parametrize("key", ["proof_key", "sdk_key", "admin_key"])
@pytest.mark.parametrize("value", [None, 12345, {"credential": SENTINEL}, ""])
def test_malformed_descriptor_credentials_rejected_before_handshake(project, monkeypatch, key, value):
    save_descriptor(**{key: value})
    monkeypatch.setattr("acira.service.urllib.request.build_opener", lambda *args: pytest.fail("Malformed credentials must not start the trusted handshake"))
    with pytest.raises(ValueError, match="Invalid local service credential fields") as error:
        descriptor()
    assert SENTINEL not in str(error.value)
    result = doctor(project, "default")
    assert result["healthy"] is False
    assert SENTINEL not in json.dumps(result)


def test_doctor_does_not_echo_unexpected_discovery_error_contents(project, monkeypatch):
    def malformed():
        raise AttributeError(SENTINEL)
    monkeypatch.setattr("acira.service.verified_descriptor", malformed)
    result = doctor(project, "default")
    assert result["healthy"] is False
    assert result["checks"]["dashboard_descriptor_valid"] is False
    assert SENTINEL not in json.dumps(result)
