"""Exercise the shipped free journey through its UI, SDK and observed HTTP effects."""
import asyncio
from pathlib import Path

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

pytest.importorskip("playwright.async_api")
from playwright.async_api import BrowserType, async_playwright, expect

from acira import demo
from acira.journal import Journal
from acira.service import Service


@pytest.fixture
async def demo_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    original_launch = BrowserType.launch

    async def headless_launch(self, **kwargs):
        # Production uses visible Chromium. CI uses the same browser/SDK path
        # in a disposable headless window, without a model or external website.
        kwargs["headless"] = True
        return await original_launch(self, **kwargs)

    monkeypatch.setattr(BrowserType, "launch", headless_launch)
    app = demo.create_app(tmp_path / "playground")
    service = Service()
    async with TestServer(app, host="127.0.0.1") as test_drive, TestServer(service.app(), host="127.0.0.1") as dashboard:
        demo.BASE = str(test_drive.make_url("/")).rstrip("/")
        service.port = dashboard.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            ticket = (await (await client.post(dashboard.make_url("/api/ticket"), json={})).json())["ticket"]
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            drive = await browser.new_page()
            ui = await browser.new_page()
            errors = []
            drive.on("pageerror", lambda error: errors.append(str(error)))
            ui.on("pageerror", lambda error: errors.append(str(error)))
            await drive.goto(demo.BASE)
            await ui.goto(str(dashboard.make_url("/")) + "#ticket=" + ticket)
            await ui.get_by_role("heading", name="Acira Playground", exact=True).wait_for()
            try:
                yield drive, ui, errors
            finally:
                await demo.cleanup_tasks(app)
                await browser.close()
                assert not errors


async def open_review(ui, project="Acira Playground"):
    await ui.get_by_role("button", name="Approvals", exact=False).first.click()
    row = ui.get_by_role("row").filter(has_text=project + " / default")
    await row.get_by_role("button", name="Review operation").wait_for(timeout=15000)
    await row.get_by_role("button", name="Review operation").click()
    await expect(ui.get_by_role("button", name="Approve once", exact=True)).to_be_enabled(timeout=5000)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,expected_count", [("Approve once", 1), ("Reject", 0)])
async def test_demo_decision_executes_once_and_finishes_without_manual_refresh(demo_ui, decision, expected_count):
    drive, ui, _ = demo_ui
    await drive.get_by_role("button", name="Start payment review").click()
    await open_review(ui)
    run_id = demo.RUNS["checkout"]["run"]
    await ui.get_by_role("button", name=decision, exact=True).click()
    await expect(drive.locator("#checkout-status")).to_contain_text("RUN ENDED", timeout=15000)
    await expect(drive.locator("#payments")).to_have_text(str(expected_count))
    assert not await drive.get_by_role("button", name="Cancel checkout run").is_enabled()
    assert await drive.get_by_role("button", name="Start payment review").is_enabled()
    assert await drive.locator("#checkout-next").is_hidden()
    assert demo.RUNS["checkout"]["outcome"] == ("completed" if expected_count else "rejected")
    journal = Journal(demo.ROOT)
    run = next(r for r in journal.runs() if r["run_id"] == run_id)
    assert run["state"] == "ended" and not run["pending_reviews"]
    events = journal.events(run=run_id, limit=1000)
    assert any(e["type"] == "run_ended" for e in events)
    if expected_count:
        assert any(e["type"] == "submission_response_observed" for e in events)
    await ui.get_by_role("button", name="Agents", exact=True).click()
    await expect(ui.get_by_role("row").filter(has_text=run_id)).to_contain_text("ended")


@pytest.mark.asyncio
async def test_demo_block_and_cancellation_end_without_browser_effects(demo_ui):
    drive, ui, _ = demo_ui
    await drive.get_by_role("button", name="Try blocked delete").click()
    await expect(drive.locator("#checkout-status")).to_contain_text("RUN ENDED", timeout=15000)
    await expect(drive.locator("#checkout-status")).to_contain_text("BLOCKED BY POLICY")
    await expect(drive.locator("#deletes")).to_have_text("0")
    await drive.get_by_role("button", name="Start payment review").click()
    await open_review(ui)
    run_id = demo.RUNS["checkout"]["run"]
    await drive.get_by_role("button", name="Cancel checkout run").click()
    await expect(drive.locator("#checkout-status")).to_contain_text("RUN CANCELLED", timeout=15000)
    await expect(drive.locator("#payments")).to_have_text("0")
    await expect(ui.locator("dialog .pill")).to_have_text("invalidated", timeout=12000)
    assert not await ui.get_by_role("button", name="Approve once", exact=True).is_enabled()
    assert next(r for r in Journal(demo.ROOT).runs() if r["run_id"] == run_id)["state"] == "ended"


@pytest.mark.asyncio
async def test_demo_two_projects_complete_independently(demo_ui):
    drive, ui, _ = demo_ui
    await drive.get_by_role("button", name="Start payment review").click()
    await drive.get_by_role("button", name="Start draft review").click()
    await open_review(ui, "Acira Research Playground")
    checkout = demo.RUNS["checkout"]["run"]
    await ui.get_by_role("button", name="Approve once", exact=True).click()
    await expect(drive.locator("#research-status")).to_contain_text("RUN ENDED", timeout=15000)
    await expect(drive.locator("#drafts")).to_have_text("1")
    assert next(r for r in Journal(demo.ROOT).reviews() if r["run_id"] == checkout)["state"] == "pending"
    await expect(drive.locator("#checkout-next")).to_be_visible()
    await open_review(ui)
    await ui.get_by_role("button", name="Reject", exact=True).click()
    await expect(drive.locator("#checkout-status")).to_contain_text("RUN ENDED", timeout=15000)
    await expect(drive.locator("#payments")).to_have_text("0")


@pytest.mark.asyncio
async def test_demo_keep_active_is_explicit_and_policy_revision_is_loaded(demo_ui):
    drive, ui, _ = demo_ui
    await drive.locator("#checkout-keep").check()
    await drive.get_by_role("button", name="Try blocked delete").click()
    await expect(drive.locator("#checkout-status")).to_contain_text("Run kept active", timeout=15000)
    run_id = demo.RUNS["checkout"]["run"]
    journal = Journal(demo.ROOT)
    run = next(r for r in journal.runs() if r["run_id"] == run_id)
    before = run["revision"]
    from acira.project import Project, digest
    project = Project(demo.ROOT)
    policy = next(p for p in project.policies("default") if p["kind"] == "general")
    changed = {**policy["document"], "name": "Policy name edited during active demo"}
    project.save_policy("default", Path(policy["path"]).name, changed, expected=digest(policy["document"]))
    for _ in range(80):
        run = next(r for r in journal.runs() if r["run_id"] == run_id)
        if run["revision"] != before:
            break
        await asyncio.sleep(.1)
    assert run["revision"] != before
    assert run["state"] != "ended"
    await drive.get_by_role("button", name="End active checkout run").click()
    await expect(drive.locator("#checkout-status")).to_contain_text("RUN ENDED", timeout=15000)
    await expect(drive.locator("#deletes")).to_have_text("0")
