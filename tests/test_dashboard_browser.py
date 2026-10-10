import asyncio
import os
from pathlib import Path

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright, expect

from acira import protect
from acira.adapters.playwright_boundary import trusted
from acira.journal import Journal
from acira.local import atomic_json
from acira.project import initialize
from acira.service import Service


@pytest.mark.asyncio
@pytest.mark.parametrize("project_count,width", [(0, 390), (2, 1440)])
async def test_connect_project_guide_does_not_create_projects_or_start_runs(tmp_path, monkeypatch, project_count, width):
    from acira.local import Registry

    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    projects = [initialize(tmp_path / name, selected=[]) for name in ("Orders", "Support")[:project_count]]
    documents = [project.path.read_bytes() for project in projects]
    service = Service()
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            response = await client.post(server.make_url("/api/ticket"), json={})
            ticket = (await response.json())["ticket"]
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            ui = await browser.new_page(viewport={"width": width, "height": 1000})
            try:
                await ui.goto(str(server.make_url("/")) + "#ticket=" + ticket)
                connect = ui.get_by_role("button", name="Connect a project", exact=True)
                await connect.wait_for()
                writes = []
                ui.on("request", lambda request: writes.append(request.url) if request.method not in {"GET", "HEAD"} else None)
                await connect.focus()
                await connect.press("Enter")
                modal = ui.get_by_role("dialog", name="Connect your project")
                await expect(modal).to_be_visible()
                assert await modal.get_by_role("button", name="Close dialog").evaluate("el=>el===document.activeElement")
                await expect(modal.get_by_text("acira init --adapter playwright", exact=True)).to_be_visible()
                optional = modal.locator("details")
                assert not await optional.locator("pre").is_visible()
                await modal.get_by_role("combobox", name="Setup framework").select_option("browser_use")
                await expect(modal.get_by_text("acira init --adapter browser_use", exact=True)).to_be_visible()
                assert "existing browser settings and model" in await modal.text_content()
                assert await ui.evaluate("document.body.scrollWidth <= window.innerWidth")
                await modal.get_by_role("button", name="Close dialog").click()
                assert len(Registry().projects()) == project_count
                assert [project.path.read_bytes() for project in projects] == documents
                assert all(not Journal(project.root).runs() for project in projects)
                assert not writes
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_navigation_during_a_view_load_is_not_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    initialize(tmp_path / "Orders", selected=["credentials"], dashboard=False)
    service = Service()
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            response = await client.post(server.make_url("/api/ticket"), json={})
            ticket = (await response.json())["ticket"]
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            ui = await browser.new_page()
            started, release = asyncio.Event(), asyncio.Event()

            async def slow_policies(route):
                started.set()
                await asyncio.wait_for(release.wait(), timeout=10)
                await route.continue_()

            try:
                await ui.goto(str(server.make_url("/")) + "#ticket=" + ticket)
                await ui.get_by_role("heading", name="Orders", exact=True).wait_for()
                await ui.route("**/api/projects/*/policies?*", slow_policies)
                await ui.get_by_role("button", name="Policies", exact=True).click()
                await asyncio.wait_for(started.wait(), timeout=5)
                await ui.get_by_role("button", name="Agents", exact=True).click()
                release.set()
                await expect(ui.get_by_role("heading", name="Agents", exact=True)).to_be_visible()
                await expect(ui.get_by_role("heading", name="Orders", exact=True)).to_be_visible()
                assert await ui.get_by_role("button", name="Agents", exact=True).get_attribute("aria-current") == "page"
                assert await ui.get_by_role("button", name="Add protection", exact=True).count() == 0
            finally:
                release.set()
                await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("device_scale,ui_width",[(1,1440),(2,507),(1,390),(1,320)])
async def test_complete_review_journey_in_shared_dashboard(tmp_path, monkeypatch,device_scale,ui_width):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "Orders", selected=[], dashboard=False)
    initialize(tmp_path / "Support", selected=["credentials"])
    config = project.document()
    config["agents"]["default"]["guard"] = {"review_actions": ["click"]}
    atomic_json(project.path, config)
    service = Service()
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            response = await client.post(server.make_url("/api/ticket"), json={})
            ticket = (await response.json())["ticket"]
        async with async_playwright() as p:
            ui_browser = await p.chromium.launch()
            ui = await ui_browser.new_page(viewport={"width":ui_width,"height":1000})
            errors = []
            requests = []
            ui.on("pageerror", lambda error: errors.append(str(error)))
            ui.on("request", lambda request: requests.append(request.url))
            await ui.goto(str(server.make_url("/")) + "#ticket=" + ticket)
            await ui.get_by_role("heading", name="Orders", exact=True).wait_for()
            await ui.get_by_role("heading", name="Support", exact=True).wait_for()
            # The dashboard is optional; this test enables discovery so an unattended
            # pending review can wait for its human dashboard response.
            async with protect(project=project.root, dashboard=True) as guard:
                guard.patch_playwright(p)
                browser = await p.chromium.launch()
                page = await browser.new_page(device_scale_factor=device_scale)
                await trusted(page, "set_content", '<button type="button" id="next" onclick="this.textContent=\'Done\'">Next</button>')
                action = asyncio.create_task(page.locator("#next").click())
                try:
                    await ui.get_by_role("button", name="Approvals", exact=False).first.click()
                    await ui.get_by_role("button", name="Review operation").wait_for(timeout=12000)
                    await ui.get_by_role("button", name="Review operation").click()
                    await ui.locator('dialog .review-focus-canvas[data-rendered="true"]').wait_for()
                    # Technical identities are available without competing with
                    # the destination and facts the human must review.
                    context = ui.locator("dialog .review-context")
                    assert guard.runtime_control.run_id in await context.text_content()
                    assert not await context.locator("pre").is_visible()
                    assert await ui.get_by_role("button", name="Close dialog").evaluate("el=>el===document.activeElement")
                    # Enter closes the focused dialog; it must not approve an action.
                    await ui.keyboard.press("Enter")
                    assert not action.done()
                    await ui.get_by_role("button", name="Review operation").click()
                    await ui.locator('dialog .review-focus-canvas[data-rendered="true"]').wait_for()
                    await expect(ui.get_by_role("heading", name="Click “Next”?", exact=True)).to_be_visible()
                    assert await ui.locator(".review-companion").evaluate("el=>el.complete && el.naturalWidth > 0")
                    # Compare the visible overlay with the actual browser target,
                    # including the captured device-pixel scaling. This target is
                    # at the viewport's top-left, so its crop begins at (0, 0).
                    real_target = await page.locator("#next").bounding_box()
                    displayed = await ui.locator(".review-focus .target-outline").evaluate("""el=>{
                        const matrix=el.getScreenCTM();
                        const origin=new DOMPoint(el.x.baseVal.value,el.y.baseVal.value).matrixTransform(matrix);
                        return {x:origin.x,width:el.width.baseVal.value*matrix.a};
                    }""")
                    frame = await ui.locator(".review-focus-canvas").bounding_box()
                    physical_width = await ui.locator(".review-focus-canvas").evaluate("el=>el.width")
                    screenshot_width = await ui.locator(".review-overview-canvas").evaluate("el=>el.width")
                    # CDP may normalize a Retina capture to CSS pixels. Use the
                    # actual capture dimensions, rather than assuming its scale.
                    capture_scale = screenshot_width / page.viewport_size["width"]
                    scale = capture_scale * frame["width"] / physical_width
                    assert abs(displayed["x"] - frame["x"] - real_target["x"] * scale) < 1
                    assert abs(displayed["width"] - real_target["width"] * scale) < 1
                    assert await ui.locator(".review-focus .target-outline").evaluate("el=>getComputedStyle(el).stroke") == "rgb(37, 99, 235)"
                    assert await ui.locator(".review-focus .target-outline").get_attribute("vector-effect") == "non-scaling-stroke"
                    overview = ui.get_by_text("Full page screenshot", exact=True)
                    await overview.click()
                    await expect(ui.locator(".review-overview-canvas")).to_be_visible()
                    await overview.click()
                    # The decision controls stay accessible while a long capture
                    # and the technical disclosures remain available above them.
                    approve_box = await ui.get_by_role("button", name="Approve once", exact=True).bounding_box()
                    assert 0 <= approve_box["y"] and approve_box["y"] + approve_box["height"] <= 1000
                    if os.environ.get("ACIRA_TEST_ARTIFACTS"):
                        directory = Path(os.environ["ACIRA_TEST_ARTIFACTS"])
                        directory.mkdir(parents=True, exist_ok=True)
                        await ui.screenshot(path=str(directory / f"approval-{ui_width}.png"), full_page=True)
                    await ui.get_by_role("button", name="Approve once", exact=True).click()
                    await asyncio.wait_for(action, timeout=10)
                    assert await page.locator("#next").inner_text() == "Done"
                    events = Journal(project.root).events(limit=5000)
                    assert any(event["type"] == "browser_call_completed" for event in events)
                    assert any(review.get("artifact", {}).get("id") for review in Journal(project.root).reviews())
                    for view in ("Policies", "Activity", "Backtesting", "Settings", "Agents"):
                        await ui.get_by_role("button", name=view, exact=True).click()
                        await ui.get_by_role("heading", name=view, exact=True).wait_for()
                        assert await ui.evaluate("document.body.scrollWidth <= innerWidth"), f"{view} overflowed the {ui_width}px viewport"
                    if os.environ.get("ACIRA_TEST_ARTIFACTS"):
                        await ui.screenshot(path=str(directory / f"agents-{ui_width}.png"), full_page=True)
                    assert all(url.startswith((str(server.make_url("/")), "data:", "blob:")) for url in requests), "The local dashboard requested an external asset"
                    assert not errors
                finally:
                    if not action.done():
                        action.cancel()
                    await browser.close()
            await ui_browser.close()


@pytest.mark.asyncio
async def test_critical_review_shows_semantic_facts_and_separates_same_agent_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "Orders", selected=[], dashboard=False)
    journal = Journal(project.root)
    evidence = {"critical": True, "request_bound": True, "destination": "https://fixture.invalid/payments",
        "request_method": "POST", "verified_fields": {"Recipient": "Fixture merchant", "Amount": "10.00", "Currency": "USD", "Account": "Fixture billing account"}}
    payload = {"agent": "default", "policy_revision": project.snapshot().revision,
        "reason": "Review the exact payment", "classification": {"action": "submit", "target": {"text": "Pay invoice"}}, "evidence": evidence,
        "grant_eligible": False}
    first = journal.create_review("run-one", payload)
    second = journal.create_review("run-two", payload)
    service = Service()
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            ticket = (await (await client.post(server.make_url("/api/ticket"), json={})).json())["ticket"]
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            ui = await browser.new_page()
            try:
                await ui.goto(str(server.make_url("/")) + "#ticket=" + ticket)
                await ui.get_by_role("heading", name="Orders", exact=True).wait_for()
                await ui.get_by_role("button", name="Approvals", exact=False).first.click()
                await ui.get_by_role("row").filter(has_text="run-one").get_by_role("button", name="Review operation").click()
                await ui.get_by_text("Run and policy revision", exact=True).click()
                assert "run-one" in await ui.locator("dialog .review-context pre").inner_text()
                for fact in evidence["verified_fields"].values():
                    assert await ui.get_by_role("cell", name=fact, exact=True).is_visible()
                assert await ui.get_by_role("checkbox").count() == 0
                await ui.get_by_role("button", name="Approve once", exact=True).click()
                await ui.get_by_role("dialog").wait_for(state="hidden")
                assert journal.review(first["review_id"])["state"] == "approved"
                assert journal.review(second["review_id"])["state"] == "pending"
            finally:
                await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("coordinates", ["missing_box", "missing_viewport", "zero", "offscreen", "invalid", "partial", "expired_image"])
async def test_review_preview_does_not_invent_missing_targets(tmp_path, monkeypatch, coordinates):
    import base64
    import io
    import time
    from PIL import Image, ImageDraw

    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "Capture evidence", selected=[], dashboard=False)
    journal = Journal(project.root)
    pixels = Image.new("RGB", (800, 600), "white")
    ImageDraw.Draw(pixels).rectangle((20, 20, 60, 50), fill="black")
    png = io.BytesIO()
    pixels.save(png, format="PNG")
    artifact = {"capture_status": "ready", "captured_at": time.time(), "mime_type": "image/png",
                "image": base64.b64encode(png.getvalue()).decode(), "viewport": {"width": 400, "height": 300},
                "bounding_box": {"x": 10, "y": 10, "width": 20, "height": 15}}
    if coordinates == "missing_box":
        artifact["bounding_box"] = None
    elif coordinates == "missing_viewport":
        artifact["viewport"] = None
    elif coordinates == "zero":
        artifact["bounding_box"]["width"] = 0
    elif coordinates == "offscreen":
        artifact["bounding_box"]["x"] = 500
    elif coordinates == "invalid":
        artifact["bounding_box"]["x"] = "10"
    elif coordinates == "partial":
        artifact["bounding_box"] = {"x": 380, "y": 280, "width": 60, "height": 40}
    artifact = journal.store_artifact(artifact)
    if coordinates == "expired_image":
        (project.root / ".acira/artifacts" / artifact["id"]).unlink()
    journal.create_review("capture-run", {"agent": "default", "policy_revision": project.snapshot().revision,
        "reason": "Inspect masked capture", "classification": {"action": "click", "target": {"text": "Next"}},
        "evidence": {}, "artifact": artifact, "grant_eligible": False})
    service = Service()
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            ticket = (await (await client.post(server.make_url("/api/ticket"), json={})).json())["ticket"]
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            ui = await browser.new_page(viewport={"width": 390, "height": 850})
            errors = []
            ui.on("pageerror", lambda error: errors.append(str(error)))
            try:
                await ui.goto(str(server.make_url("/")) + "#ticket=" + ticket)
                await ui.get_by_role("heading", name="Capture evidence", exact=True).wait_for()
                await ui.get_by_role("button", name="Approvals", exact=False).first.click()
                await ui.get_by_role("button", name="Review operation").click()
                if coordinates == "expired_image":
                    await expect(ui.get_by_text("Screenshot unavailable or expired. Review the structured action details.", exact=True)).to_be_visible()
                    assert not await ui.locator(".review-preview canvas").count()
                elif coordinates == "partial":
                    await ui.locator('.review-preview[data-loaded="true"]').wait_for()
                    await expect(ui.get_by_text("Part of the target is outside this capture. Review the full page and action details.", exact=True)).to_be_visible()
                    rect = ui.locator(".review-overview .target-outline")
                    assert float(await rect.get_attribute("width")) == 40
                    assert float(await rect.get_attribute("height")) == 40
                else:
                    await expect(ui.get_by_text("Target coordinates are unavailable in this capture. Use the action details; no highlight has been inferred.", exact=True)).to_be_visible()
                    assert not await ui.locator(".review-focus, .target-outline, .target-pointer").count()
                    await expect(ui.locator(".review-overview-canvas")).to_be_visible()
                    # The original masked black region stays black in the UI.
                    assert await ui.locator(".review-overview-canvas").evaluate("el=>Array.from(el.getContext('2d').getImageData(25,25,1,1).data)") == [0, 0, 0, 255]
                assert await ui.locator(".review-companion").evaluate("el=>el.complete && el.naturalWidth > 0")
                assert not errors
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_companion_preferences_keep_evidence_and_respect_reduced_motion(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "Appearance", selected=[], dashboard=False)
    journal = Journal(project.root)
    review = journal.create_review("appearance-run", {"agent": "default", "policy_revision": project.snapshot().revision,
        "reason": "Review the note", "classification": {"action": "click", "target": {"text": "Save note"}},
        "evidence": {"destination": "https://fixture.invalid/notes"}, "grant_eligible": False})
    service = Service()
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            ticket = (await (await client.post(server.make_url("/api/ticket"), json={})).json())["ticket"]
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            ui = await browser.new_page()
            try:
                await ui.goto(str(server.make_url("/")) + "#ticket=" + ticket)
                await ui.get_by_role("heading", name="Appearance", exact=True).wait_for()
                await ui.get_by_role("button", name="Settings", exact=True).click()
                mode = ui.get_by_role("combobox", name="Companion appearance")
                await expect(mode).to_have_value("still")
                await mode.select_option("hidden")
                await ui.get_by_role("button", name="Approvals", exact=False).first.click()
                await ui.get_by_role("button", name="Review operation").click()
                await expect(ui.locator(".review-companion")).not_to_be_visible()
                await expect(ui.get_by_role("heading", name="Click “Save note”?", exact=True)).to_be_visible()
                await expect(ui.get_by_text("Destination: https://fixture.invalid/notes", exact=True)).to_be_visible()
                assert journal.review(review["review_id"])["state"] == "pending"
                await ui.get_by_role("button", name="Close dialog").click()
                await ui.reload()
                await ui.get_by_role("button", name="Settings", exact=True).click()
                await expect(mode).to_have_value("hidden")
                await mode.select_option("animated")
                await ui.emulate_media(reduced_motion="reduce")
                await ui.get_by_role("button", name="Approvals", exact=False).first.click()
                await ui.get_by_role("button", name="Review operation").click()
                assert await ui.locator(".review-companion").evaluate("el=>getComputedStyle(el).animationName") == "none"
                await ui.emulate_media(reduced_motion="no-preference")
                assert await ui.locator(".review-companion").evaluate("el=>getComputedStyle(el).animationName") == "companion-wait"
                journal.decide(review["review_id"], "rejected")
                await ui.locator("dialog .pill").filter(has_text="rejected").wait_for(timeout=12000)
                assert await ui.locator(".review-companion").evaluate("el=>getComputedStyle(el).animationName") == "none"
            finally:
                await browser.close()


async def keyboard_focus(page, locator):
    """Reach a control using ordinary Tab navigation, without synthetic focus."""
    await locator.wait_for(state="visible")
    for _ in range(220):
        if await locator.evaluate("node => node === document.activeElement"):
            return
        await page.keyboard.press("Tab")
    raise AssertionError("Control cannot be reached by keyboard")


@pytest.mark.asyncio
@pytest.mark.parametrize("closed_state", ["approved", "rejected", "expired", "invalidated", "expires_on_submission"])
async def test_open_review_tracks_external_resolution_and_disables_stale_decisions(tmp_path, monkeypatch, closed_state):
    expired_while_deciding = closed_state == "expires_on_submission"
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "Stale review", selected=[], dashboard=False)
    journal = Journal(project.root)
    review = journal.create_review("run-stale", {
        "agent": "default", "policy_revision": project.snapshot().revision,
        "reason": "Save the local test note", "classification": {"action": "click", "target": {"text": "Save draft"}},
        "evidence": {}, "grant_eligible": False,
    })
    service = Service()
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            ticket = (await (await client.post(server.make_url("/api/ticket"), json={})).json())["ticket"]
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            ui = await browser.new_page()
            try:
                await ui.goto(str(server.make_url("/")) + "#ticket=" + ticket)
                await ui.get_by_role("heading", name="Stale review", exact=True).wait_for()
                await ui.get_by_role("button", name="Approvals", exact=False).first.click()
                await ui.get_by_role("button", name="Review operation").click()
                approve = ui.get_by_role("button", name="Approve once", exact=True)
                await expect(approve).to_be_enabled(timeout=5000)
                # Another reviewer, timeout, or policy change resolves the request
                # while this browser still has its original review dialog open.
                if closed_state == "expires_on_submission":
                    async def expire_before_decision(route):
                        # Expiry happens after the UI's fresh read, immediately
                        # before the server processes its decision POST.
                        with journal.db() as db:
                            db.execute("UPDATE reviews SET expires=0 WHERE id=?", (review["review_id"],))
                        await route.continue_()
                    await ui.route("**/reviews/*/decision", expire_before_decision)
                    await approve.click()
                    closed_state = "expired"
                else:
                    assert journal.decide(review["review_id"], closed_state)
                await ui.locator("dialog .pill").filter(has_text=closed_state).wait_for(timeout=12000)
                assert not await approve.is_enabled()
                assert not await ui.get_by_role("button", name="Reject", exact=True).is_enabled()
                assert await ui.locator("#review-status").inner_text()
                if expired_while_deciding:
                    # Keep the dialog open through another live-state refresh.
                    await ui.wait_for_timeout(5500)
                    assert await ui.locator("dialog .pill").inner_text() == closed_state
                await ui.keyboard.press("Enter")
                assert journal.review(review["review_id"])["state"] == closed_state
                await ui.get_by_role("dialog").wait_for(state="hidden")
                await ui.get_by_role("button", name="Refresh", exact=True).click()
                await expect(ui.get_by_role("button", name="Review operation")).to_have_count(0)
            finally:
                await browser.close()


async def keyboard_activate(page, locator):
    await keyboard_focus(page, locator)
    await page.keyboard.press("Enter")


@pytest.mark.asyncio
async def test_keyboard_policy_import_error_recovery_assignments_and_rollback(tmp_path, monkeypatch):
    from acira.project import catalog_policy
    import json

    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "Keyboard", selected=[], dashboard=False)
    service = Service()
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            ticket = (await (await client.post(server.make_url("/api/ticket"), json={})).json())["ticket"]
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            ui = await browser.new_page()
            errors = []
            ui.on("pageerror", lambda error: errors.append(str(error)))
            await ui.goto(str(server.make_url("/")) + "#ticket=" + ticket)
            await keyboard_activate(ui, ui.get_by_role("button", name="Policies", exact=True))
            await keyboard_activate(ui, ui.get_by_role("button", name="Import JSON", exact=True))
            editor = ui.get_by_role("textbox", name="Policy JSON", exact=True)
            await keyboard_focus(ui, editor)
            await ui.keyboard.insert_text("{broken")
            await keyboard_activate(ui, ui.get_by_role("button", name="Validate and import"))
            await ui.locator("#dialog-feedback.error").wait_for()
            assert not project.snapshot().documents
            policy = catalog_policy("credentials")
            await keyboard_focus(ui, editor)
            await ui.keyboard.press("ControlOrMeta+A")
            await ui.keyboard.insert_text(json.dumps(policy))
            await keyboard_activate(ui, ui.get_by_role("button", name="Validate and import"))
            await ui.get_by_role("dialog").wait_for(state="hidden")
            imported = project.snapshot().revision
            await keyboard_activate(ui, ui.get_by_role("button", name="Configure", exact=True))
            await keyboard_activate(ui, ui.get_by_text("Advanced JSON", exact=True))
            editor = ui.get_by_role("textbox", name="Advanced policy JSON")
            await keyboard_focus(ui, editor)
            await ui.keyboard.press("ControlOrMeta+A")
            policy["name"] = "Keyboard edited policy"
            await ui.keyboard.insert_text(json.dumps(policy))
            await keyboard_activate(ui, ui.get_by_role("button", name="Save policy", exact=True))
            await ui.get_by_role("dialog").wait_for(state="hidden")
            assert project.policies("default")[0]["name"] == "Keyboard edited policy"
            await keyboard_activate(ui, ui.get_by_role("button", name="Agent assignments", exact=True))
            toggle = ui.get_by_role("checkbox", name="Keyboard edited policy")
            await keyboard_focus(ui, toggle)
            await ui.keyboard.press("Space")
            await keyboard_activate(ui, ui.get_by_role("button", name="Save assignments", exact=True))
            await ui.get_by_role("dialog").wait_for(state="hidden")
            assert project.document()["agents"]["default"]["disabled_policies"] == [policy["id"]]
            await keyboard_activate(ui, ui.get_by_role("button", name="Revision history", exact=True))
            row = ui.get_by_role("row").filter(has_text=imported[:12])
            await keyboard_activate(ui, row.get_by_role("button", name="Compare with current"))
            await ui.get_by_role("heading", name="Changes since " + imported[:12]).wait_for()
            ui.once("dialog", lambda dialog: dialog.accept())
            await keyboard_activate(ui, row.get_by_role("button", name="Restore", exact=True))
            await ui.get_by_role("dialog").wait_for(state="hidden")
            assert project.policies("default")[0]["name"] == catalog_policy("credentials")["name"]
            assert project.document()["agents"]["default"].get("disabled_policies", []) == []
            assert not errors
            await browser.close()


@pytest.mark.asyncio
async def test_older_revisions_are_available_in_history_and_comparisons(tmp_path, monkeypatch):
    from acira.project import digest
    from acira.revisions import save_configuration

    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "History", selected=[], dashboard=False)
    first = project.snapshot().revision
    for number in range(52):
        document = project.document()
        expected = digest(document)
        document["agents"]["default"]["revision_nonce"] = str(number)
        save_configuration(project, document, expected=expected)
    service = Service()
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            ticket = (await (await client.post(server.make_url("/api/ticket"), json={})).json())["ticket"]
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            ui = await browser.new_page()
            await ui.goto(str(server.make_url("/")) + "#ticket=" + ticket)
            await ui.get_by_role("button", name="Policies", exact=True).click()
            await ui.get_by_role("button", name="Revision history", exact=True).click()
            assert await ui.get_by_role("row").filter(has_text=first[:12]).count() == 0
            await keyboard_activate(ui, ui.get_by_role("button", name="Load older revisions", exact=True))
            await ui.get_by_role("row").filter(has_text=first[:12]).wait_for()
            await ui.get_by_role("button", name="Close dialog").click()
            await ui.get_by_role("button", name="Backtesting", exact=True).click()
            await ui.get_by_role("combobox", name="Baseline revision", exact=True).wait_for()
            assert await ui.locator('select[aria-label="Baseline revision"] option').count() == 51
            await keyboard_activate(ui, ui.get_by_role("button", name="Load older policy revisions", exact=True))
            await ui.locator('select[aria-label="Baseline revision"] option[value="' + first + '"]').wait_for(state="attached")
            assert await ui.locator('select[aria-label="Candidate revision"] option').count() == 54
            await browser.close()


@pytest.mark.asyncio
async def test_graph_assignment_copy_folder_edit_and_accessible_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "Graph", selected=["credentials"], dashboard=False)
    initialize(project.root, agent="research", general="policies/research/general", specific="policies/research/specific", selected=[], dashboard=False)
    source = project.policies("default")[0]
    original = Path(source["path"]).read_bytes()
    service = Service()
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            ticket = (await (await client.post(server.make_url("/api/ticket"), json={})).json())["ticket"]
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            ui = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            ui.on("pageerror", lambda error: errors.append(str(error)))
            await ui.goto(str(server.make_url("/")) + "#ticket=" + ticket)
            await ui.get_by_role("button", name="Policies", exact=True).click()
            await ui.get_by_role("button", name="Assignments / graph", exact=True).click()
            node = ui.get_by_role("button", name="Inspect policy " + source["name"], exact=True)
            await keyboard_activate(ui, node)
            transform = await node.get_attribute("transform")
            await ui.keyboard.press("ArrowDown")
            assert await node.get_attribute("transform") != transform
            await keyboard_activate(ui, ui.get_by_role("button", name="Disable for default", exact=True))
            await ui.get_by_role("dialog").wait_for(state="hidden")
            assert project.document()["agents"]["default"]["disabled_policies"] == [source["id"]]
            assert not project.document()["agents"]["research"].get("disabled_policies")
            assert Path(source["path"]).read_bytes() == original
            await ui.get_by_role("button", name="Assignments / graph", exact=True).click()
            node = ui.get_by_role("button", name="Inspect policy " + source["name"], exact=True)
            assert await node.get_attribute("transform") != transform
            await keyboard_activate(ui, ui.get_by_text("Copy an installed policy to another agent", exact=True))
            await ui.get_by_role("combobox", name="Copy destination agent").select_option("research")
            await keyboard_activate(ui, ui.get_by_role("button", name="Copy policy", exact=True))
            await ui.get_by_role("dialog").wait_for(state="hidden")
            assert project.policies("research")[0]["id"] == source["id"]
            assert project.policies("research")[0]["path"] != source["path"]
            await ui.get_by_role("button", name="Assignments / graph", exact=True).click()
            await ui.get_by_role("button", name="Inspect agent research", exact=True).click()
            await ui.get_by_role("heading", name="research", exact=True).wait_for()
            await keyboard_activate(ui, ui.get_by_role("button", name="Edit folders for research", exact=True))
            await ui.get_by_role("textbox", name="General policy folder", exact=True).fill("policies/general")
            await keyboard_activate(ui, ui.get_by_role("button", name="Save policy folders", exact=True))
            await ui.get_by_role("dialog").wait_for(state="hidden")
            assert project.folders("default")["general"] == project.folders("research")["general"]
            await ui.get_by_role("button", name="Assignments / graph", exact=True).click()
            await keyboard_activate(ui, ui.get_by_role("button", name="Inspect policy " + source["name"], exact=True))
            await ui.get_by_text("Shared file edits affect: default, research", exact=False).wait_for()
            await ui.get_by_role("button", name="Edit policy file", exact=True).click()
            await ui.get_by_role("dialog").get_by_text("This file may be shared.", exact=False).wait_for()
            await ui.get_by_role("button", name="Close dialog", exact=True).click()
            assert not errors
            await browser.close()


@pytest.mark.asyncio
async def test_keyboard_settings_agent_retention_references_and_validation_recovery(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "Setup", selected=[], dashboard=False)
    service = Service()
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            ticket = (await (await client.post(server.make_url("/api/ticket"), json={})).json())["ticket"]
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            ui = await browser.new_page()
            errors = []
            ui.on("pageerror", lambda error: errors.append(str(error)))
            await ui.goto(str(server.make_url("/")) + "#ticket=" + ticket)
            await keyboard_activate(ui, ui.get_by_role("button", name="Settings", exact=True))
            await keyboard_activate(ui, ui.get_by_role("button", name="Add agent", exact=True))
            name = ui.get_by_role("textbox", name="Agent configuration name", exact=True)
            await keyboard_focus(ui, name)
            await ui.keyboard.insert_text("!invalid")
            await keyboard_activate(ui, ui.get_by_role("button", name="Create agent", exact=True))
            await ui.locator("#dialog-feedback.error").wait_for()
            await keyboard_focus(ui, name)
            await ui.keyboard.press("ControlOrMeta+A")
            await ui.keyboard.insert_text("checkout")
            await keyboard_activate(ui, ui.get_by_role("button", name="Create agent", exact=True))
            await ui.get_by_role("dialog").wait_for(state="hidden")
            assert project.folders("checkout")["general"] != project.folders("default")["general"]
            await keyboard_activate(ui, ui.get_by_role("button", name="History retention", exact=True))
            days = ui.get_by_role("spinbutton", name="Audit history · days", exact=True)
            await keyboard_focus(ui, days)
            await ui.keyboard.press("ControlOrMeta+A")
            await ui.keyboard.insert_text("14")
            await keyboard_activate(ui, ui.get_by_role("button", name="Save retention", exact=True))
            await ui.get_by_role("dialog").wait_for(state="hidden")
            assert project.document()["retention"]["audit_days"] == 14
            await keyboard_activate(ui, ui.get_by_role("button", name="Secret references", exact=True))
            await keyboard_activate(ui, ui.get_by_role("button", name="Add reference", exact=True))
            for label, value in [("Reference ID", "login"), ("Display label", "Test account"), ("Environment variable name", "TEST_ACCOUNT")]:
                field = ui.get_by_role("textbox", name=label, exact=True)
                await keyboard_focus(ui, field)
                await ui.keyboard.insert_text(value)
            await keyboard_activate(ui, ui.get_by_role("button", name="Save references", exact=True))
            await ui.get_by_role("dialog").wait_for(state="hidden")
            assert project.document()["agents"]["checkout"]["guard"]["secrets_config_path"]
            await keyboard_activate(ui, ui.get_by_role("button", name="Advanced configuration", exact=True))
            editor = ui.get_by_role("textbox", name="Project configuration JSON", exact=True)
            await keyboard_focus(ui, editor)
            await ui.keyboard.press("ControlOrMeta+A")
            await ui.keyboard.insert_text("{invalid")
            await keyboard_activate(ui, ui.get_by_role("button", name="Validate and save", exact=True))
            await ui.locator("#dialog-feedback.error").wait_for()
            await keyboard_focus(ui, editor)
            await ui.keyboard.press("ControlOrMeta+A")
            await ui.keyboard.insert_text(json.dumps(project.document()))
            await keyboard_activate(ui, ui.get_by_role("button", name="Validate and save", exact=True))
            await ui.get_by_role("dialog").wait_for(state="hidden")
            await keyboard_activate(ui, ui.get_by_role("button", name="Pair capture extension", exact=True))
            await ui.get_by_role("heading", name="Pair Acira Capture", exact=True).wait_for()
            await keyboard_activate(ui, ui.get_by_role("button", name="Close dialog", exact=True))
            assert not errors
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("focused_action", ["pause", "inspect"])
async def test_agents_refresh_ended_run_even_with_control_focused(tmp_path, monkeypatch, focused_action):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "Live run", selected=[])
    journal = Journal(project.root)
    payload = {"agent": "default", "revision": project.snapshot().revision, "state": "running",
        "connected": True, "dashboard_enabled": True, "pending_reviews": 0, "capabilities": {"pause": True}}
    journal.update_run("run-live", payload)
    service = Service()
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            ticket = (await (await client.post(server.make_url("/api/ticket"), json={})).json())["ticket"]
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            ui = await browser.new_page()
            try:
                await ui.goto(str(server.make_url("/")) + "#ticket=" + ticket)
                row = ui.get_by_role("row").filter(has_text="run-live")
                await expect(row).to_contain_text("running")
                control = row.get_by_role("button", name="Pause" if focused_action == "pause" else "Inspect", exact=True)
                await keyboard_focus(ui, control)
                journal.update_run("run-live", {**payload, "state": "ended", "connected": False})
                await expect(row).to_contain_text("ended", timeout=12000)
                assert await row.get_by_role("button", name="Pause", exact=True).count() == 0
                expected = ui.get_by_role("button", name="Refresh", exact=True) if focused_action == "pause" else row.get_by_role("button", name="Inspect", exact=True)
                assert await expected.evaluate("el=>el===document.activeElement")
                assert await ui.locator(".metric").filter(has_text="Live runs").locator("strong").inner_text() == "0"
            finally:
                await browser.close()
