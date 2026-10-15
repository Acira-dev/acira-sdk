"""Real loopback/filesystem privacy boundaries; no browser or account credentials."""
import base64
import hashlib
import html
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest
from aiohttp import ClientSession, CookieJar
from aiohttp.test_utils import TestServer

from acira.journal import Journal, journal_redact
from acira.local import atomic_json
from acira.project import initialize
from acira.runtime import ProjectRuntime
from acira.service import Service
from acira_cdp_proxy import redaction
from acira_cdp_proxy.audit.redaction import Redactor
from acira_cdp_proxy.checkpoint import PendingReviewRecord
from acira_cdp_proxy.logger import CDPLogger
from acira_cdp_proxy.secrets import SecretBroker
from acira_dashboard import server as legacy


@pytest.fixture(autouse=True)
def private_fixture_state(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    monkeypatch.setattr(redaction, "SECRET_REDACTION_REGISTRY", redaction.SecretRedactionRegistry())


@pytest.mark.parametrize("url,secret,public", [
    ("https://fixture-user:fixture-password@example.invalid/path#section", "fixture-password", "/path#section"),
    ("wss://fixture%2Duser:fixture%2Dpassword@[::1]:4173/path", "fixture%2Dpassword", "[::1]:4173/path"),
    ("https://example.invalid/callback#access_token=fixture-private&view=summary", "fixture-private", "view=summary"),
    ("https://example.invalid/#/callback?token=fixture-private&view=summary", "fixture-private", "view=summary"),
    ("https://example.invalid/#%70assword=fixture-private", "fixture-private", "example.invalid"),
    ("https://example.invalid/#accessToken=fixture-private&view=summary", "fixture-private", "view=summary"),
    ("https://u:fixture-private@example.invalid/?password=query-private#code=fragment-private", "fixture-private", "example.invalid"),
])
def test_url_credentials_are_masked_in_structured_prose_and_payload_views(url, secret, public):
    for value in (url, "Observed " + url + " at the fixture"):
        result = redaction.redact_value(value)
        assert secret not in result and "query-private" not in result and "fragment-private" not in result
        assert public in result
        payload = json.loads(redaction.redact_payload(json.dumps({"url": value})))
        assert secret not in payload["url"] and public in payload["url"]
    # Browser-wire projection remains private and retains routing semantics.
    assert json.loads(redaction.redact_payload(json.dumps({"url": url}), redact_generic=False))["url"] == url


@pytest.mark.parametrize("mode", ["light", "medium", "conservative"])
def test_persistent_audit_modes_mask_userinfo_and_fragment_without_known_secret_registry(mode):
    url = "https://fixture-user:fixture-password@example.invalid/#access_token=fixture-fragment&view=summary"
    event = Redactor(mode).redact_event({"payload": {"url": url}})
    output = json.dumps(event)
    assert "fixture-password" not in output and "fixture-fragment" not in output
    assert "example.invalid" in output and "view=summary" in output
    assert "payload.url:url_credentials" in event["redactions_applied"]


def test_public_fragment_and_query_are_preserved_and_malformed_authority_is_not_echoed():
    public = "https://example.invalid/docs?view=summary#installation"
    assert redaction.redact_value(public) == public
    broken = "Observed https://fixture-user:fixture-password@[invalid]/path"
    assert "fixture-password" not in redaction.redact_value(broken)


def test_registered_secret_masking_does_not_bypass_other_url_credential_masking():
    assert redaction.SECRET_REDACTION_REGISTRY.register_secret(secret_id="fixture", label="Fixture", value="known-fixture-password")
    url = "https://unregistered-user:known-fixture-password@example.invalid/#token=unregistered-fragment"
    assert "unregistered-user" not in redaction.redact_value(url)
    assert "unregistered-fragment" not in redaction.redact_value(url)
    assert "unregistered-user" not in redaction.redact_payload(json.dumps({"url": url}))
    assert "unregistered-fragment" not in redaction.redact_payload(json.dumps({"url": url}))


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["API key", "api  key", "client API key", "X API Key"])
async def test_space_separated_api_key_aliases_mask_persistence_without_changing_browser_wire(tmp_path, alias):
    secret = "fixture-space-key"
    url = "https://example.invalid/send?" + urlencode({alias: secret, "key": "navigation"})
    payload = json.dumps({"id": 7, "method": "Fixture.describe", "params": {alias: secret, "url": url}})
    for wire in (payload, payload.encode()):
        masked = json.loads(redaction.redact_payload(wire))
        assert masked["params"][alias] == "[REDACTED]"
        assert dict(parse_qsl(urlsplit(masked["params"]["url"]).query)) == {alias: "[REDACTED]", "key": "navigation"}
        assert redaction.redact_payload(wire, redact_generic=False) == wire
        assert type(redaction.redact_payload(wire, redact_generic=False)) is type(wire)
    original = {alias: secret, "url": url, "key": "navigation"}
    assert secret not in json.dumps(redaction.redact_value(original))
    assert original[alias] == secret and original["url"] == url
    journal = Journal(tmp_path / "journal")
    event_id = journal.event("action_proposed", facts=original)
    recorded = next(event for event in journal.events() if event["event_id"] == event_id)
    assert recorded["facts"][alias] == "[REDACTED]" and recorded["facts"]["key"] == "navigation"
    with journal.db() as database:
        stored = database.execute("SELECT payload FROM events WHERE id=?", (event_id,)).fetchone()[0]
    assert secret not in stored
    logger = CDPLogger(tmp_path / "diagnostics.log")
    await logger.log_decision(connection_id="fixture", message_number=1, websocket_path="/devtools/browser/fixture",
                              payload=payload, metadata=original)
    assert secret not in logger.path.read_text()


@pytest.mark.parametrize("key", ["key", "public key", "api keychain", "api keyboard", "monkey"])
def test_space_alias_detection_keeps_ordinary_key_fields_readable(key):
    value = {key: "navigation", "url": "https://example.invalid/docs?" + urlencode({key: "navigation"})}
    assert redaction.redact_value(value) == value
    payload = json.dumps(value)
    assert redaction.redact_payload(payload) == payload


@pytest.mark.parametrize("field,width", [("review_id", 32), ("event_id", 32), ("operation_id", 32),
                                         ("project_id", 32), ("policy_revision", 64), ("grant_id", 64)])
def test_nested_hex_shaped_registered_secrets_are_never_restored_by_field_name(field, width):
    secret = "abcde0123456789f" * 5
    secret = secret[:width]
    assert redaction.SECRET_REDACTION_REGISTRY.register_secret(secret_id="fixture", label="Fixture", value=secret)
    original = {"target": {"attributes": {field: secret}}, "list": [{field: secret}]}
    assert secret not in json.dumps(journal_redact(original))
    assert original["target"]["attributes"][field] == secret


@pytest.mark.asyncio
async def test_actual_browser_use_node_pending_record_and_authenticated_history_export_mask_nested_ids(tmp_path):
    from acira.adapters.browser_use import guard_target_from_browser_use_node
    secret = ("abcde0123456789f" * 3)[:32]
    assert redaction.SECRET_REDACTION_REGISTRY.register_secret(secret_id="fixture", label="Fixture", value=secret)
    project = initialize(tmp_path / "project", selected=[], dashboard=False)
    target = guard_target_from_browser_use_node(SimpleNamespace(tag_name="input", attributes={
        "type": "text", "name": "fixture", "review_id": secret}),
        url="https://fixture-user:fixture-password@example.invalid/#access_token=fixture-fragment")
    assert target.attributes["review_id"] == secret
    journal = Journal(project.root)
    operation_id = hashlib.sha256(b"fixture-operation").hexdigest()[:32]
    run = "run-" + hashlib.sha256(b"fixture-run").hexdigest()[:32]
    journal.record_pending(PendingReviewRecord(operation_id, run, "form_input", "browser_use", "pending", {}, target.to_metadata()))
    event = journal.events()[0]
    assert event["operation_id"] == operation_id and event["run_id"] == run
    assert secret not in json.dumps(event)
    assert all(value not in json.dumps(event) for value in ("fixture-user", "fixture-password", "fixture-fragment"))
    assert secret.encode() not in journal.path.read_bytes()
    reopened = Journal(project.root)
    assert secret not in json.dumps(reopened.events())
    service = Service()
    project_id = service.registry.projects()[0]["id"]
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            response = await client.get(server.make_url(f"/api/projects/{project_id}/events/export"))
            assert response.status == 200
            exported = await response.text()
            assert secret not in exported and operation_id in exported
            assert all(value not in exported for value in ("fixture-user", "fixture-password", "fixture-fragment"))
            assert json.loads(exported)["target"]["attributes"]["review_id"].startswith("[REDACTED")


def test_only_generated_review_artifact_routing_survives_redaction(tmp_path):
    journal = Journal(tmp_path)
    artifact = journal.store_artifact({"capture_status": "ready", "image": base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()})
    # Producer provenance survives a new Journal object and ordinary image expiry.
    review = Journal(tmp_path).create_review("run-fixture", {"artifact": artifact})
    assert review["artifact"]["id"] == artifact["id"]
    forged = ("abcde0123456789f" * 3)[:32] + ".png"
    assert redaction.SECRET_REDACTION_REGISTRY.register_secret(secret_id="fixture", label="Fixture", value=forged)
    second = journal.create_review("run-fixture", {"artifact": {"capture_status": "ready", "id": forged},
                                                "target": {"artifact": {"capture_status": "ready", "id": forged}}})
    assert forged not in json.dumps(second)


@pytest.mark.asyncio
async def test_external_provider_options_change_revision_swap_broker_and_invalidate_review(tmp_path):
    project = initialize(tmp_path / "project", selected=[], dashboard=False)
    for name, value in (("first.env", "fixture-first-value"), ("second.env", "fixture-second-value")):
        (project.root / name).write_text("FIXTURE=" + value + "\n")
    external = project.root / "references.json"
    config = {"enabled": True, "providers": {"fixture": {"kind": "env_file", "env_path": "first.env"}},
              "items": [{"id": "fixture", "provider": "fixture", "ref": "FIXTURE", "active": True}]}
    atomic_json(external, config)
    document = project.document()
    document["agents"]["default"]["guard"] = {"secrets_config_path": "references.json"}
    atomic_json(project.path, document)
    runtime = ProjectRuntime(project, "default", dashboard=False)
    adapter = SimpleNamespace(guard_policy=SimpleNamespace(config=runtime.snapshot.config, _audit_pipeline=None),
                              action_gateway=SimpleNamespace(checkpoint_store=None),
                              secret_broker=SecretBroker(runtime.snapshot.config.secrets))
    runtime.attach(adapter)
    runtime.publish()
    previous = runtime.snapshot.revision
    metadata = runtime.snapshot.config.secrets.to_metadata()
    review = runtime.journal.create_review(runtime.run_id, {"policy_revision": previous})
    runtime.pending.add(review["review_id"])
    config["providers"]["fixture"]["env_path"] = "second.env"
    atomic_json(external, config)
    assert project.snapshot().config.secrets.to_metadata() == metadata
    runtime.refresh()
    assert runtime.snapshot.revision != previous
    assert adapter.guard_policy.policy_revision == runtime.snapshot.revision
    assert runtime.journal.review(review["review_id"])["state"] == "invalidated"
    provider = adapter.secret_broker.provider_registry.get("fixture")
    assert provider.resolve(adapter.secret_broker.config.record("fixture")) == "fixture-second-value"
    assert "fixture-second-value" not in json.dumps(runtime.journal.events())
    assert "second.env" not in json.dumps(metadata)
    active_revision = runtime.snapshot.revision
    active_broker = adapter.secret_broker
    del config["providers"]["fixture"]["env_path"]
    atomic_json(external, config)
    for _ in range(2):
        with pytest.raises(ValueError, match="env_path"):
            await runtime.before_decision()
        assert runtime.snapshot.revision == active_revision
        assert adapter.secret_broker is active_broker and runtime.state == "invalid_configuration"
    config["providers"]["fixture"]["env_path"] = "first.env"
    atomic_json(external, config)
    await runtime.before_decision()
    assert runtime.snapshot.revision == previous and adapter.secret_broker is not active_broker
    await runtime.close()


@pytest.mark.asyncio
async def test_provider_factory_failure_never_partially_activates_or_reuses_old_broker_as_active(tmp_path, monkeypatch):
    import acira_cdp_proxy.secrets as secret_module
    project = initialize(tmp_path / "project", selected=[], dashboard=False)
    external = project.root / "references.json"
    config = {"enabled": True, "providers": {"fixture": {"kind": "env_file", "env_path": "first.env"}},
              "items": [{"id": "fixture", "provider": "fixture", "ref": "FIXTURE", "active": True}]}
    atomic_json(external, config)
    document = project.document()
    document["agents"]["default"]["guard"] = {"secrets_config_path": "references.json"}
    atomic_json(project.path, document)
    runtime = ProjectRuntime(project, "default", dashboard=False)
    policy = SimpleNamespace(config=runtime.snapshot.config, _audit_pipeline=None)
    adapter = SimpleNamespace(guard_policy=policy, action_gateway=SimpleNamespace(checkpoint_store=None),
                              secret_broker=SecretBroker(runtime.snapshot.config.secrets))
    runtime.attach(adapter)
    runtime.publish()
    previous, broker, active_config = runtime.snapshot, adapter.secret_broker, policy.config
    review = runtime.journal.create_review(runtime.run_id, {"policy_revision": previous.revision})
    runtime.pending.add(review["review_id"])
    config["providers"]["fixture"]["env_path"] = "second.env"
    atomic_json(external, config)
    assert project.snapshot().revision != previous.revision

    def failed_factory(*args, **kwargs):
        raise RuntimeError("fixture-factory-private-error")

    with monkeypatch.context() as patch:
        patch.setattr(secret_module, "SecretBroker", failed_factory)
        for _ in range(2):
            with pytest.raises(ValueError, match="Secret provider setup failed"):
                await runtime.before_decision()
            assert runtime.snapshot is previous and adapter.secret_broker is broker and policy.config is active_config
            assert policy.policy_revision == previous.revision and runtime.state == "invalid_configuration"
            assert "fixture-factory-private-error" not in runtime.error
            assert runtime.journal.review(review["review_id"])["state"] == "pending"
            assert not any(row["type"] == "policy_activated" for row in runtime.journal.events())
    await runtime.before_decision()
    assert runtime.snapshot.revision != previous.revision and adapter.secret_broker is not broker
    assert runtime.state == "running" and runtime.error is None
    assert runtime.journal.review(review["review_id"])["state"] == "invalidated"
    assert sum(row["type"] == "policy_activated" for row in runtime.journal.events()) == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_legacy_dashboard_form_cookie_authenticates_ui_assets_and_read_apis(tmp_path):
    key = "fixture-only-access-key"
    app = legacy.create_app(paths=legacy.DashboardPaths(production_dir=tmp_path), token=key)
    async with TestServer(app, host="127.0.0.1") as server:
        origin = str(server.make_url("")).rstrip("/")
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
            initial = await client.get(server.make_url("/"))
            page = await initial.text()
            assert initial.status == 200 and key not in page and 'type="password"' in page
            assert 'autocomplete="off"' in page
            assert initial.headers["Referrer-Policy"] == "same-origin"
            denied = await client.get(server.make_url("/api/bootstrap"))
            assert denied.status == 401 and key not in await denied.text()
            wrong = await client.post(server.make_url("/api/session"), data={"token": "wrong"}, headers={"Origin": origin})
            assert wrong.status == 401 and key not in await wrong.text()
            signed_in = await client.post(server.make_url("/api/session"), data={"token": key}, headers={"Origin": origin}, allow_redirects=False)
            assert signed_in.status == 303 and signed_in.headers["Location"] == "/"
            cookie = signed_in.cookies[app[legacy.COOKIE_KEY]]
            assert cookie["httponly"] and cookie["samesite"] == "Strict" and int(cookie["max-age"]) == 43200
            page = await (await client.get(server.make_url("/"))).text()
            assert html.escape(key, quote=True) in page and "__ACIRA_DASHBOARD_TOKEN__" not in page
            for asset in re.findall(r'(?:src|href)="(/static/[^\"]+)"', page):
                response = await client.get(server.make_url(asset))
                assert response.status == 200, asset
            assert (await client.get(server.make_url("/api/bootstrap"))).status == 200
            assert (await client.get(server.make_url("/api/events"))).status == 200
            # A cookie cannot bypass the existing explicit write confirmation.
            assert (await client.post(server.make_url("/api/configs"), json={})).status == 401
        async with ClientSession() as independent:
            response = await independent.get(server.make_url("/api/bootstrap"))
            assert response.status == 401 and key not in await response.text()
            headers = {"X-Acira-Dashboard-Token": key}
            assert (await independent.get(server.make_url("/api/bootstrap"), headers=headers)).status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [None, "null", "https://example.invalid", "http://127.0.0.1:1"])
async def test_legacy_signin_rejects_missing_or_foreign_origin_without_cookie(tmp_path, origin):
    app = legacy.create_app(paths=legacy.DashboardPaths(production_dir=tmp_path), token="fixture-only-key")
    async with TestServer(app, host="127.0.0.1") as server, ClientSession() as client:
        response = await client.post(server.make_url("/api/session"), json={"token": "fixture-only-key"}, headers={"Origin": origin} if origin else {})
        assert response.status == 403 and not response.cookies and "fixture-only-key" not in await response.text()


@pytest.mark.asyncio
async def test_legacy_sessions_expire_and_refuse_unbounded_creation(tmp_path):
    app = legacy.create_app(paths=legacy.DashboardPaths(production_dir=tmp_path), token="fixture-key")
    app[legacy.SESSIONS_KEY]["expired-cookie"] = time.time() - 1
    async with TestServer(app, host="127.0.0.1") as server, ClientSession() as client:
        response = await client.get(server.make_url("/api/bootstrap"), headers={"Cookie": app[legacy.COOKIE_KEY] + "=expired-cookie"})
        assert response.status == 401 and not app[legacy.SESSIONS_KEY]
        app[legacy.SESSIONS_KEY].update({str(i): time.time() + 60 for i in range(256)})
        response = await client.post(server.make_url("/api/session"), json={"token": "fixture-key"}, headers={"Origin": str(server.make_url("")).rstrip("/")})
        assert response.status == 429 and len(app[legacy.SESSIONS_KEY]) == 256 and not response.cookies


def test_legacy_secret_storage_is_private_atomic_and_ignores_predictable_temp_file(tmp_path):
    path = tmp_path / "configs/secrets/acira.secrets.env"
    path.parent.mkdir(parents=True)
    path.write_text("OLD=fixture-old\n")
    path.chmod(0o644)
    obsolete = path.with_suffix(".tmp")
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    obsolete.symlink_to(outside)
    old_umask = os.umask(0o022)
    try:
        legacy._write_env_file(path, {"FIXTURE": "fixture-private-value"})
    finally:
        os.umask(old_umask)
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    assert path.read_text() == "FIXTURE=fixture-private-value\n" and outside.read_text() == "unchanged"
    assert obsolete.is_symlink() and not list(path.parent.glob(".acira-secret-*"))


@pytest.mark.parametrize("where", ["configs", "secrets", "file"])
def test_legacy_secret_storage_rejects_symlink_files_and_write_roots(tmp_path, where):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("unchanged")
    path = tmp_path / "configs/secrets/acira.secrets.env"
    if where == "configs":
        (tmp_path / "configs").symlink_to(outside, target_is_directory=True)
    elif where == "secrets":
        path.parent.parent.mkdir()
        path.parent.symlink_to(outside, target_is_directory=True)
    else:
        path.parent.mkdir(parents=True)
        path.symlink_to(sentinel)
    with pytest.raises(legacy.DashboardInputError):
        legacy._write_env_file(path, {"FIXTURE": "fixture-private-value"})
    assert sentinel.read_text() == "unchanged" and not (outside / "acira.secrets.env").exists()


@pytest.mark.asyncio
async def test_invalid_secret_configuration_and_line_injection_never_mutate_storage(tmp_path):
    paths = legacy.DashboardPaths(production_dir=tmp_path)
    env = legacy._env_file_path(paths)
    legacy._write_env_file(env, {"EXISTING": "fixture-original"})
    before = env.read_bytes()
    app = legacy.create_app(paths=paths, token="fixture-key")
    async with TestServer(app, host="127.0.0.1") as server, ClientSession(headers={"X-Acira-Dashboard-Token": "fixture-key"}) as client:
        for value, decision in (("fixture-new", "invalid"), ("first\nINJECTED=second", "ask")):
            response = await client.put(server.make_url("/api/secrets/configs/fixture.secrets.local.json"), json={
                "enabled": True, "default_decision": decision, "items": [{"id": "fixture", "value": value, "active": True}]})
            assert response.status == 400
            assert env.read_bytes() == before
            assert not (paths.configs_dir / "fixture.secrets.local.json").exists()
        response = await client.put(server.make_url("/api/secrets/configs/fixture.secrets.local.json"), json={
            "enabled": True, "default_decision": "ask", "items": [{"id": "fixture", "value": "fixture-secret-value", "active": True}]})
        assert response.status == 200 and "fixture-secret-value" not in await response.text()
        assert env.stat().st_mode & 0o777 == 0o600 and env.parent.stat().st_mode & 0o777 == 0o700
        assert legacy._read_env_file(env) == {"EXISTING": "fixture-original", "FIXTURE": "fixture-secret-value"}
        assert "fixture-secret-value" not in (paths.configs_dir / "fixture.secrets.local.json").read_text()
