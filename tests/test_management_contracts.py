import json
from pathlib import Path

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from acira import catalog
from acira.journal import Journal
from acira.local import Registry
from acira.project import catalog_policy, digest, initialize
from acira.secret_references import describe, save
from acira.service import Service


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))


def test_secret_editor_accepts_references_and_never_reads_values(tmp_path):
    project = initialize(tmp_path / "project", selected=[])
    (project.root / ".env").write_text("PAYMENT_PASSWORD=private-test-value\n")
    body = {"expected":digest(project.document()), "enabled":True,
        "entries":[{"id":"payment-login","label":"Payment login","ref":"PAYMENT_PASSWORD","active":True,"policy":{"decision":"ask"}}]}
    save(project, "default", body)
    metadata = describe(project, "default")
    assert metadata["enabled"]
    assert "private-test-value" not in json.dumps(metadata)
    assert metadata["secrets"][0]["ref"] == "PAYMENT_PASSWORD"
    body["entries"][0]["value"] = "do not accept raw values"
    with pytest.raises(ValueError, match="metadata"):
        save(project, "default", body)


def test_catalog_checksums_and_local_overrides(monkeypatch):
    value = catalog_policy("credentials")
    manifest = {"schema":"acira.catalog.v1","policies":{"credentials":{"url":"https://example.test/policy.json","sha256":"a"*64,"version":"1"}}}
    def fetch(url):
        return (manifest, "b"*64) if url.endswith("catalog.json") else (dict(value), "a"*64)
    monkeypatch.setattr(catalog,"fetch_json",fetch)
    pinned = catalog.policy("https://example.test/catalog.json","credentials")
    pinned["acira_decisions"] = {risk:"block" for risk in pinned["acira_decisions"]}
    assert catalog.updated(pinned)["acira_decisions"] == pinned["acira_decisions"]
    manifest["policies"]["credentials"]["sha256"] = "c"*64
    with pytest.raises(ValueError, match="checksum"):
        catalog.policy("https://example.test/catalog.json","credentials")


def test_event_retention_is_bounded_and_audits_pruning(tmp_path):
    journal = Journal(tmp_path)
    cap = 1024*1024
    journal.retention(cap=cap)
    for number in range(100):
        journal.event("example", message="example history entry "+("word "*2000), number=number)
    with journal.db() as db:
        assert db.execute("SELECT value FROM journal_usage WHERE name='events'").fetchone()[0] <= cap
    assert journal.path.stat().st_size <= cap
    events = journal.events()
    assert any(event["type"] == "history_pruned" for event in events)
    assert next(event for event in reversed(events) if event["type"] == "example")["number"] == 99


@pytest.mark.asyncio
async def test_history_pages_export_and_extension_revoke(tmp_path):
    project = initialize(tmp_path / "project", selected=[])
    journal = Journal(project.root)
    for number in range(7):
        journal.event("example", run="run-one", number=number)
    project_id = Registry().projects()[0]["id"]
    service = Service()
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization":"Bearer "+service.admin_key}) as client:
            base = str(server.make_url("/api/projects/"+project_id))
            first = await (await client.get(base+"/events?order=newest&limit=3")).json()
            second = await (await client.get(base+"/events?order=newest&limit=3&before="+str(first["next_cursor"]))).json()
            assert [event["number"] for event in first["events"]] == [6,5,4]
            assert [event["number"] for event in second["events"]] == [3,2,1]
            exported = await (await client.get(base+"/events/export?kind=example")).text()
            assert len(exported.splitlines()) == 7
            code = (await (await client.post(server.make_url("/api/pairing"),json={})).json())["code"]
            paired = await (await client.post(server.make_url("/api/pair"),json={"code":code})).json()
            headers = {"Authorization":"Bearer "+paired["credential"]}
            assert (await client.get(server.make_url("/api/destinations"),headers=headers)).status == 200
            assert (await client.get(base+"/events/export",headers=headers)).status == 403
            await client.delete(server.make_url("/api/extensions/"+paired["extension_id"]))
            assert (await client.get(server.make_url("/api/destinations"),headers=headers)).status == 401


def _legacy_catalog_capture(identity_form):
    value = {"schema": "acira.specific_skill.v2", "name": "Legacy named guide",
        "scope": {"url_patterns": ["https://fixture.invalid/*"]},
        "targets": [{"id": "guide", "decision": "ask", "fingerprint": {
            "tag": "a", "id": "guide", "href": "/guide", "text": "Guide"}}]}
    if identity_form != "name-only":
        value["id"] = "  captured-guide  " if identity_form == "padded-id" else "captured-guide"
    return value


def _fake_capture_catalog(monkeypatch, initial):
    """Only local values reach the fetch boundary; checksum real serialized bytes."""
    import hashlib
    state = {"document": initial, "version": "1", "calls": []}
    catalog_url = "https://catalog.invalid/catalog.json"
    policy_url = "https://catalog.invalid/guide.json"

    def fetch(url):
        state["calls"].append(url)
        raw = json.dumps(state["document"], sort_keys=True).encode()
        checksum = hashlib.sha256(raw).hexdigest()
        if url == catalog_url:
            return {"schema": "acira.catalog.v1", "policies": {"guide": {
                "url": policy_url, "sha256": checksum, "version": state["version"]}}}, "b" * 64
        assert url == policy_url, "Unexpected fetch must not reach the network"
        return json.loads(raw), checksum

    monkeypatch.setattr(catalog, "fetch_json", fetch)
    return state, catalog_url


@pytest.mark.parametrize("identity_form", ["name-only", "padded-id", "explicit-id"])
def test_catalog_update_uses_parsed_capture_identity_without_rewriting(monkeypatch, identity_form):
    from acira.project import validate_policy
    original = _legacy_catalog_capture(identity_form)
    state, url = _fake_capture_catalog(monkeypatch, original)
    pinned = catalog.policy(url, "guide")
    saved = json.loads(json.dumps(pinned))
    upstream = json.loads(json.dumps(original))
    if identity_form == "padded-id":
        upstream["id"] = "captured-guide"
    upstream["targets"][0]["decision"] = "block"
    state.update(document=upstream, version="2")
    proposal = catalog.updated(pinned)
    assert validate_policy(proposal) == validate_policy(pinned)
    assert proposal.get("id") == upstream.get("id")
    assert proposal["name"] == upstream["name"]
    assert proposal["targets"][0]["decision"] == "block"
    assert proposal["acira_source"]["catalog"] == url
    assert proposal["acira_source"]["version"] == "2"
    assert proposal["acira_source"]["sha256"] != pinned["acira_source"]["sha256"]
    assert pinned == saved


@pytest.mark.parametrize("original_kind", ["general", "specific"])
def test_catalog_update_rejects_kind_change_even_when_parsed_id_matches(monkeypatch, original_kind):
    general = catalog_policy("credentials")
    general["id"] = "captured-guide"
    specific = _legacy_catalog_capture("explicit-id")
    original, replacement = (general, specific) if original_kind == "general" else (specific, general)
    state, url = _fake_capture_catalog(monkeypatch, original)
    pinned = catalog.policy(url, "guide")
    saved = json.loads(json.dumps(pinned))
    state.update(document=replacement, version="2")
    with pytest.raises(ValueError, match="identity changed"):
        catalog.updated(pinned)
    assert pinned == saved


@pytest.mark.parametrize("identity_form", ["name-only", "padded-id"])
def test_catalog_update_rejects_changed_parsed_capture_identity(monkeypatch, identity_form):
    original = _legacy_catalog_capture(identity_form)
    state, url = _fake_capture_catalog(monkeypatch, original)
    pinned = catalog.policy(url, "guide")
    changed = json.loads(json.dumps(original))
    changed["id"] = "different-capture"
    state.update(document=changed, version="2")
    with pytest.raises(ValueError, match="identity changed"):
        catalog.updated(pinned)


async def _local_management_post(path, body):
    service = Service()
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            async with client.post(server.make_url(path), json=body) as response:
                text = await response.text()
                assert response.status == 200, (response.status, text)
                return json.loads(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_form", ["name-only", "padded-id", "explicit-id"])
async def test_catalog_preview_uses_parsed_capture_filename(monkeypatch, identity_form):
    from acira.project import validate_policy
    document = _legacy_catalog_capture(identity_form)
    _, url = _fake_capture_catalog(monkeypatch, document)
    result = await _local_management_post("/api/catalog/preview", {"url": url, "name": "guide"})
    assert result["document"].get("id") == document.get("id")
    assert result["document"]["name"] == document["name"]
    identity = validate_policy(document)[1]
    assert result["filename"] == "catalog-" + digest({"id": identity})[:16] + ".json"


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_form", ["name-only", "padded-id", "explicit-id"])
async def test_policy_updates_skips_unpinned_terminal_capture_without_fetching_or_writing(
        tmp_path, monkeypatch, identity_form):
    from acira.cli import run
    project = initialize(tmp_path / "project", selected=[])
    capture = tmp_path / "capture.json"
    document = _legacy_catalog_capture(identity_form)
    capture.write_text(json.dumps(document))
    run(["policy", "import", str(capture), "--project", str(project.root)])
    entry, = project.policies("default")
    saved = Path(entry["path"]).read_bytes()
    revision = project.snapshot().revision
    monkeypatch.setattr(catalog, "fetch_json", lambda url: pytest.fail("An unpinned capture must not fetch"))
    project_id = Registry().projects()[0]["id"]
    result = await _local_management_post("/api/projects/" + project_id + "/policy-updates", {"agent": "default"})
    assert result["updates"] == []
    assert result["note"] == "Preview only. Explicit save is required for each policy."
    assert Path(entry["path"]).read_bytes() == saved and project.snapshot().revision == revision


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_form", ["name-only", "padded-id", "explicit-id"])
async def test_policy_updates_previews_pinned_capture_without_applying(tmp_path, monkeypatch, identity_form):
    from acira.project import validate_policy
    project = initialize(tmp_path / "project", selected=[])
    original = _legacy_catalog_capture(identity_form)
    state, url = _fake_capture_catalog(monkeypatch, original)
    pinned = catalog.policy(url, "guide")
    project.save_policy("default", "guide.json", pinned, expected=None)
    entry, = project.policies("default")
    saved = Path(entry["path"]).read_bytes()
    revision = project.snapshot().revision
    upstream = json.loads(json.dumps(original))
    if identity_form == "padded-id":
        upstream["id"] = "captured-guide"
    upstream["targets"][0]["decision"] = "block"
    state.update(document=upstream, version="2")
    project_id = Registry().projects()[0]["id"]
    result = await _local_management_post("/api/projects/" + project_id + "/policy-updates", {"agent": "default"})
    update, = result["updates"]
    assert update["id"] == validate_policy(original)[1]
    assert update["proposed"].get("id") == upstream.get("id")
    assert update["proposed"]["targets"][0]["decision"] == "block"
    assert update["proposed"]["acira_source"]["version"] == "2"
    assert Path(entry["path"]).read_bytes() == saved and project.snapshot().revision == revision


@pytest.mark.asyncio
async def test_policy_updates_normalizes_general_id_and_preserves_local_decisions(tmp_path):
    project = initialize(tmp_path / "project", selected=[])
    document = catalog_policy("credentials")
    identity = document["id"]
    document["id"] = "  " + identity + "  "
    document["acira_decisions"] = {risk: "block" for risk in document["acira_decisions"]}
    project.save_policy("default", "credentials.json", document, expected=None)
    entry, = project.policies("default")
    saved = Path(entry["path"]).read_bytes()
    revision = project.snapshot().revision
    project_id = Registry().projects()[0]["id"]
    result = await _local_management_post("/api/projects/" + project_id + "/policy-updates", {"agent": "default"})
    update, = result["updates"]
    assert update["id"] == identity
    assert update["proposed"]["id"] == identity
    assert update["proposed"]["acira_decisions"] == document["acira_decisions"]
    assert Path(entry["path"]).read_bytes() == saved and project.snapshot().revision == revision


@pytest.mark.asyncio
async def test_policy_updates_does_not_replace_specific_capture_with_general_same_id(tmp_path, monkeypatch):
    project = initialize(tmp_path / "project", selected=[])
    specific = _legacy_catalog_capture("explicit-id")
    specific["id"] = catalog_policy("credentials")["id"]
    project.save_policy("default", "guide.json", specific, expected=None)
    entry, = project.policies("default")
    saved = Path(entry["path"]).read_bytes()
    revision = project.snapshot().revision
    monkeypatch.setattr(catalog, "fetch_json", lambda url: pytest.fail("An unpinned capture must not fetch"))
    project_id = Registry().projects()[0]["id"]
    result = await _local_management_post("/api/projects/" + project_id + "/policy-updates", {"agent": "default"})
    assert result["updates"] == []
    assert Path(entry["path"]).read_bytes() == saved and project.snapshot().revision == revision
