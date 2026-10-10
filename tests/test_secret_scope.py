"""Shared-process secret redaction survives independent agent lifecycles."""
import pytest

from acira_cdp_proxy.redaction import SecretRedactionRegistry


def test_projects_using_the_same_secret_name_keep_both_values_redacted():
    registry = SecretRedactionRegistry()
    first, second = "Cedar River Test Value", "Maple Grove Test Value"
    assert registry.register_secret(secret_id="account", label="Project A", value=first)
    assert registry.register_secret(secret_id="account", label="Project B", value=second)
    text = registry.redact_text(f"{first} and {second}")
    assert first not in text and second not in text


def test_registry_capacity_never_evicts_existing_redaction(monkeypatch):
    from acira_cdp_proxy import redaction
    clock = [100.0]
    monkeypatch.setattr(redaction.time, "monotonic", lambda: clock[0])
    registry = SecretRedactionRegistry(max_secrets=1, max_fields=1, max_bytes=200)
    first, second = "Cedar River Test Value", "Maple Grove Test Value"
    assert registry.register_secret(secret_id="one", label="One", value=first, ttl_seconds=1)
    assert not registry.register_secret(secret_id="two", label="Two", value=second)
    assert registry.contains(first)
    assert registry.register_secret_field(secret_id="one", label="One", target={"id": "first"}, ttl_seconds=1)
    assert not registry.register_secret_field(secret_id="two", label="Two", target={"id": "second"})
    assert registry.secret_field_count() == 1
    clock[0] += 2
    assert registry.register_secret(secret_id="two", label="Two", value=second)
    assert registry.register_secret_field(secret_id="two", label="Two", target={"id": "second"})
    assert registry.contains(second)


def test_registry_byte_budget_and_expiry_are_bounded():
    registry = SecretRedactionRegistry(max_bytes=8)
    assert not registry.register_secret(secret_id="one", label="One", value="more than eight bytes")
    assert not registry.register_secret(secret_id="two", label="Two", value="small", ttl_seconds=float("inf"))


def test_numeric_secret_values_never_reach_journal_or_transport(tmp_path):
    import json
    from acira.journal import Journal
    from acira_cdp_proxy.redaction import SECRET_REDACTION_REGISTRY, redact_payload
    from acira_cdp_proxy.audit.redaction import Redactor

    registry = SECRET_REDACTION_REGISTRY
    registry.clear()
    try:
        assert registry.register_secret(secret_id="otp", label="Fixture code", value="987654")
        journal = Journal(tmp_path / "project")
        redactor = Redactor("light", known_secret_matcher=registry.redact_text)
        for value in (987654, 4111111111111111, 4111111111111111.0):
            journal.event("fixture_observation", observation=value)
            assert isinstance(journal.events(limit=100)[-1]["observation"], str)
            masked = json.loads(redact_payload(json.dumps({"observation": value})))
            assert isinstance(masked["observation"], str)
            assert isinstance(redactor.redact_event({"payload": {"value": value}})["payload"]["value"], str)
        assert json.loads(redact_payload('{"count":10,"enabled":true}')) == {"count": 10, "enabled": True}
    finally:
        registry.clear()


def test_active_run_prevents_mid_workflow_expiry_and_cleanup_is_bounded(monkeypatch):
    from acira_cdp_proxy import redaction
    clock = [100.0]
    monkeypatch.setattr(redaction.time, "monotonic", lambda: clock[0])
    registry = SecretRedactionRegistry()
    first, second = registry.begin_run(), registry.begin_run()
    value = "Cedar River Test Value"
    assert registry.register_secret(secret_id="account", label="Account", value=value, ttl_seconds=1)
    clock[0] += 7200
    assert registry.contains(value)
    registry.end_run(first)
    assert registry.contains(value)
    registry.end_run(second)
    assert registry.contains(value)
    clock[0] += 3601
    assert not registry.contains(value)


@pytest.mark.asyncio
async def test_browser_use_lifecycle_does_not_erase_other_integrations_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_USE_CONFIG_DIR", str(tmp_path / "browser-use"))
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    monkeypatch.setenv("BROWSER_USE_CLOUD_SYNC", "false")
    pytest.importorskip("browser_use")
    from acira.adapters.browser_use import BrowserUseCDPProxy
    from acira.adapters.playwright import PlaywrightCDPProxy
    from acira_cdp_proxy.redaction import SECRET_REDACTION_REGISTRY, redact_value
    import time
    monkeypatch.setattr(SECRET_REDACTION_REGISTRY, "_expiry", lambda ttl: time.monotonic() - 1)

    value = "Cedar River Test Value"
    SECRET_REDACTION_REGISTRY.clear()
    try:
        async with PlaywrightCDPProxy(config={"terminal_log": False}):
            assert SECRET_REDACTION_REGISTRY.register_secret(secret_id="account", label="Other run", value=value)
            async with BrowserUseCDPProxy(config={"terminal_log": False}):
                assert value not in redact_value(f"Log: {value}")
            assert value not in redact_value(f"Log after other run closed: {value}")
    finally:
        SECRET_REDACTION_REGISTRY.clear()
