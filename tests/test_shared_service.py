import asyncio
import time

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from acira.journal import Journal
from acira.local import Registry, atomic_json
from acira.project import initialize
from acira.runtime import ProjectRuntime
from acira.service import Service, verified_descriptor


@pytest.mark.asyncio
async def test_shared_projects_authentication_and_extension_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    one = initialize(tmp_path / "one", selected=["payment"])
    initialize(tmp_path / "two", selected=["credentials"])
    service = Service()
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        url = str(server.make_url(""))
        admin = {"Authorization": "Bearer " + service.admin_key}
        async with ClientSession() as client:
            response = await client.get(url + "/api/projects")
            assert response.status == 401
            response = await client.get(url + "/api/projects", headers=admin)
            data = await response.json()
            assert len(data["projects"]) == 2
            response = await client.get(url + "/api/projects", headers={"Authorization": "Bearer " + service.sdk_key})
            assert response.status == 403
            response = await client.post(url + "/api/pairing", json={}, headers=admin)
            code = (await response.json())["code"]
            response = await client.post(url + "/api/pair", json={"code": code}, headers={"Origin": "chrome-extension://testing"})
            credential = (await response.json())["credential"]
            extension = {"Authorization": "Bearer " + credential}
            response = await client.get(url + "/api/destinations", headers=extension)
            assert response.status == 200
            response = await client.get(url + "/api/projects", headers=extension)
            assert response.status == 403
            response = await client.post(url + "/api/pair", json={"code": code})
            assert response.status == 403
            response = await client.post(url + "/api/pairing", json={}, headers={**admin,"Origin":"https://untrusted.example"})
            assert response.status == 403


@pytest.mark.asyncio
async def test_agent_started_first_connects_later_and_keeps_local_policy(tmp_path, monkeypatch):
    home = tmp_path / "private"
    monkeypatch.setenv("ACIRA_HOME", str(home))
    project = initialize(tmp_path / "one", selected=["payment"])
    runtime = ProjectRuntime(project, "default")
    await runtime.start()
    assert not runtime.connected
    try:
        service = Service()
        async with TestServer(service.app(), host="127.0.0.1") as server:
            service.port = server.port
            atomic_json(home / "server.json", {"protocol":1,"url":str(server.make_url("")),"proof_key":service.proof_key,"sdk_key":service.sdk_key,"admin_key":service.admin_key}, private=True)
            deadline = time.monotonic()+6
            while not runtime.connected and time.monotonic()<deadline:
                await asyncio.sleep(.1)
            assert runtime.connected
            current = project.policies("default")[0]
            current["document"]["acira_decisions"]["checkout.card_data_entry"] = "block"
            saved = project.save_policy("default", "checkout.json", current["document"], expected=current["hash"])
            await runtime.before_decision()
            assert runtime.snapshot.revision == saved["revision"]
        (home / "server.json").unlink()
        await runtime.before_decision()
        assert runtime.snapshot.config.skill_policies["acira.general.checkout"]["checkout.card_data_entry"] == "block"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_review_response_is_scoped_to_project(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    one, two = [initialize(tmp_path / name) for name in ("one", "two")]
    journal = Journal(one.root)
    review = journal.create_review("run-one", {"grant_eligible":False})
    ids = {row["name"]:row["id"] for row in Registry().projects()}
    service = Service()
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization":"Bearer "+service.admin_key}) as client:
            wrong = await client.post(server.make_url(f"/api/projects/{ids['two']}/reviews/{review['review_id']}/decision"),json={"decision":"approved"})
            assert wrong.status == 400
            assert journal.review(review["review_id"])["state"] == "pending"
            correct = await client.post(server.make_url(f"/api/projects/{ids['one']}/reviews/{review['review_id']}/decision"),json={"decision":"rejected"})
            assert correct.status == 200
            assert journal.review(review["review_id"])["state"] == "rejected"
