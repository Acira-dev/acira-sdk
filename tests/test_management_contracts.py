import json

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
