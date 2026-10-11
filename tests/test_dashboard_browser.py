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
@pytest.mark.parametrize("width,windows", [(1440, False), (390, True)])
async def test_policy_filenames_reveal_locations_without_editing_files(tmp_path, monkeypatch, width, windows):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "Orders", selected=["checkout", "credentials"], dashboard=False)
    original = [Path(policy["path"]).read_bytes() for policy in project.policies("default")]
    document = project.path.read_bytes()
    service = Service()
    # Two identically named files remain distinguishable by their full source path.
    # Exercise Windows separators and literal markup without touching backend identity.
    roots = [r"C:\Users\Developer\Orders\policies\general", r"C:\Users\Developer\Shared policies"] if windows else ["/Users/Developer/Orders/policies/general", "/Users/Developer/Shared policies"]
    separator = "\\" if windows else "/"
    filename = "review & <input>.json"
    locations = [root + separator + filename for root in roots]
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            ticket = (await (await client.post(server.make_url("/api/ticket"), json={})).json())["ticket"]
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await browser.new_context(viewport={"width": width, "height": 900}, permissions=["clipboard-read", "clipboard-write"])
            ui = await context.new_page()
            errors, writes = [], []
            ui.on("pageerror", lambda error: errors.append(str(error)))
            async def paths(route):
                response = await route.fetch()
                data = await response.json()
                for policy, location in zip(data["policies"], locations):
                    policy["path"] = location
                await route.fulfill(response=response, json=data)
            await ui.route("**/api/projects/*/policies?*", paths)
            try:
                await ui.goto(str(server.make_url("/")) + "#ticket=" + ticket)
                await ui.get_by_role("button", name="Policies", exact=True).click()
                files = ui.get_by_role("button", name="File details for " + filename, exact=True)
                await expect(files).to_have_count(2)
                ui.on("request", lambda request: writes.append(request.url) if request.method not in {"GET", "HEAD"} else None)
                for index, location in enumerate(locations):
                    file = files.nth(index)
                    assert await file.get_attribute("title") == location
                    assert (await file.locator("..").locator("..").inner_text()).splitlines() == ["{}", filename]
                    await file.hover()
                    await file.click(button="right")
                    popup = ui.get_by_role("dialog", name="File location for " + filename, exact=True)
                    await expect(popup).to_be_visible()
                    await expect(popup.locator("pre")).to_have_text(location)
                    assert await popup.locator("input").count() == 0
                    assert await file.get_attribute("aria-expanded") == "true"
                    box = await popup.bounding_box()
                    assert box["x"] >= 0 and box["x"] + box["width"] <= width
                    await popup.get_by_role("button", name="Copy file path", exact=True).click()
                    assert await ui.evaluate("navigator.clipboard.readText()") == location
                    await expect(popup.get_by_role("status")).to_have_text("File path copied.")
                    await ui.keyboard.press("Escape")
                    await expect(popup).not_to_be_visible()
                    await expect(file).to_be_focused()
                    await file.press("Shift+F10")
                    await expect(popup).to_be_visible()
                    await popup.get_by_role("button", name="Close file details").click()
                    await expect(file).to_be_focused()
                    await file.press("Enter")
                    await expect(popup).to_be_visible()
                    await ui.keyboard.press("Escape")
                # Browser restrictions on clipboard access still leave a selectable path.
                await ui.evaluate("Object.defineProperty(navigator, 'clipboard', {value: {writeText: async()=>{throw new Error('denied')}}})")
                await files.first.click()
                popup = ui.get_by_role("dialog", name="File location for " + filename, exact=True)
                await popup.get_by_role("button", name="Copy file path", exact=True).click()
                await expect(popup.get_by_role("status")).to_contain_text("Select and copy")
                await ui.get_by_role("heading", name="Policies", exact=True).click()
                await expect(popup).not_to_be_visible()
                assert [Path(policy["path"]).read_bytes() for policy in project.policies("default")] == original
                assert project.path.read_bytes() == document
                assert not writes and not errors
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
                    await expect(ui.get_by_role("dialog").get_by_role("heading", name="Click “Next”?", exact=True)).to_be_visible()
                    assert await ui.locator("dialog .acira-mascot").count() == 0
                    # Compare the visible overlay with the actual browser target,
                    # including the captured device-pixel scaling. This target is
                    # at the viewport's top-left, so its crop begins at (0, 0).
                    real_target = await page.locator("#next").bounding_box()
                    displayed = await ui.locator(".review-stage .target-bounds").evaluate("""el=>{
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
                    # The close-up can shrink on narrow screens but must not
                    # enlarge a captured target beyond its browser CSS scale.
                    assert frame["width"] <= physical_width / capture_scale + 1
                    assert await ui.locator(".target-pointer").count() == 0
                    assert abs(displayed["x"] - frame["x"] - real_target["x"] * scale) < 1
                    assert abs(displayed["width"] - real_target["width"] * scale) < 1
                    ring = ui.locator(".review-stage .target-outline")
                    bounds = ui.locator(".review-stage .target-bounds")
                    # Decoration is outside the exact target, including its halo.
                    assert float(await ring.get_attribute("x")) < float(await bounds.get_attribute("x"))
                    assert float(await ring.get_attribute("y")) < float(await bounds.get_attribute("y"))
                    assert float(await ring.get_attribute("width")) > float(await bounds.get_attribute("width"))
                    assert await ring.evaluate("el=>getComputedStyle(el).strokeWidth") == "1.5px"
                    caption_box = await ui.locator(".target-caption").bounding_box()
                    caption_text = await ui.locator(".target-caption-name").bounding_box()
                    assert caption_text["x"] >= caption_box["x"] + 16
                    assert caption_text["x"] + caption_text["width"] <= caption_box["x"] + caption_box["width"] - 16
                    assert caption_text["y"] + caption_text["height"] < caption_box["y"] + caption_box["height"]
                    assert frame["y"] >= caption_box["y"] + caption_box["height"] + 16
                    assert await ring.evaluate("el=>getComputedStyle(el).stroke") == "rgb(185, 28, 28)"
                    assert await ui.locator(".review-stage .target-outline").get_attribute("vector-effect") == "non-scaling-stroke"
                    assert "outlined in red" in await ui.locator(".review-focus-canvas").get_attribute("aria-label")
                    overview = ui.get_by_text("Full page screenshot", exact=True)
                    await overview.click()
                    await expect(ui.locator(".review-overview-canvas")).to_be_visible()
                    assert await ui.locator(".review-overview .target-outline").evaluate("el=>getComputedStyle(el).stroke") == "rgb(185, 28, 28)"
                    assert "outlined in red" in await ui.locator(".review-overview-canvas").get_attribute("aria-label")
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
                        await click_control(ui, ui.get_by_role("button", name=view, exact=True, include_hidden=True))
                        await ui.get_by_role("heading", name=view, exact=True, include_hidden=True).wait_for()
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
@pytest.mark.parametrize("width,device_scale", [(1440, 2), (320, 1)])
async def test_payment_preview_preserves_context_and_keeps_evidence_readable(tmp_path, monkeypatch, width, device_scale):
    import base64
    import time

    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "Payment evidence", selected=[], dashboard=False)
    service = Service()
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            ticket = (await (await client.post(server.make_url("/api/ticket"), json={})).json())["ticket"]
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            source = await browser.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=device_scale)
            ui = await browser.new_page(viewport={"width": width, "height": 1000})
            try:
                # An inert fixture: this test never submits a payment. The black
                # region represents masking already performed by the runtime.
                await source.set_content('''<style>
                    body { font: 16px Arial; padding: 50px; color: #17181c; }
                    main { width: 620px; } h1 { font-size: 26px; }
                    .masked { width: 420px; height: 30px; background: black; margin: 18px 0; }
                    button { background: #3047d6; color: white; border: 0;
                        border-radius: 8px; padding: 18px 28px; font: 600 22px Arial; }
                    </style><main><h1>Invoice checkout</h1><p>Fixture merchant · USD 10.00</p>
                    <div class="masked"></div><button type="button">Pay $10.00</button>
                    <h2>Account records</h2><p>Fixture billing account</p></main>''')
                target = await source.get_by_role("button", name="Pay $10.00", exact=True).bounding_box()
                masked = await source.locator(".masked").bounding_box()
                artifact = Journal(project.root).store_artifact({
                    "capture_status": "ready", "captured_at": time.time(), "mime_type": "image/png",
                    "image": base64.b64encode(await source.screenshot()).decode(),
                    "viewport": source.viewport_size, "bounding_box": target,
                })
                Journal(project.root).create_review("payment-preview-run", {
                    "agent": "default", "policy_revision": project.snapshot().revision,
                    "classification": {"action": "click", "target": {"text": "Pay $10.00"}},
                    "evidence": {"critical": True, "destination": "https://fixture.invalid/payments",
                        "verified_fields": {"Recipient": "Fixture merchant", "Amount": "10.00", "Currency": "USD", "Account": "Fixture billing account"}},
                    "artifact": artifact, "grant_eligible": False,
                })
                await ui.goto(str(server.make_url("/")) + "#ticket=" + ticket)
                await ui.get_by_role("heading", name="Payment evidence", exact=True).wait_for()
                await ui.get_by_role("button", name="Approvals", exact=False).first.click()
                await ui.get_by_role("button", name="Review operation").click()
                await ui.locator('.review-preview[data-loaded="true"]').wait_for()
                frame = await ui.locator(".review-focus-canvas").bounding_box()
                assert frame["width"] <= 640 + 1
                assert await ui.locator(".review-focus-canvas").evaluate("el=>el.width") == 640 * device_scale
                await expect(ui.get_by_role("cell", name="Fixture merchant", exact=True)).to_be_visible()
                # The annotation lives above the masked source, never
                # rasterized into it. All words and masking remain original.
                crop_x = max(0, min(1280 - 640, target["x"] + target["width"] / 2 - 320))
                crop_y = max(0, min(720 - 260, target["y"] + target["height"] / 2 - 130))
                masked_pixel = {"x": (masked["x"] + masked["width"] / 2 - crop_x) * device_scale,
                    "y": (masked["y"] + masked["height"] / 2 - crop_y) * device_scale}
                assert await ui.locator(".review-focus-canvas").evaluate(
                    """(el, pixel)=>Array.from(el.getContext('2d').getImageData(pixel.x,pixel.y,1,1).data)""", masked_pixel
                ) == [0, 0, 0, 255]
                text = await ui.locator(".target-caption-name").bounding_box()
                caption = await ui.locator(".target-caption").bounding_box()
                assert text["x"] + text["width"] <= caption["x"] + caption["width"] - 16
                assert text["y"] + text["height"] < frame["y"]
                assert await ui.locator("dialog .acira-mascot").count() == 0
                assert await ui.locator(".review-stage .target-outline").evaluate("el=>getComputedStyle(el).stroke") == "rgb(185, 28, 28)"
                assert await ui.locator("dialog").evaluate("el=>el.scrollWidth<=el.clientWidth")
                await ui.get_by_role("button", name="Approve once", exact=True).scroll_into_view_if_needed()
                if os.environ.get("ACIRA_TEST_ARTIFACTS"):
                    directory = Path(os.environ["ACIRA_TEST_ARTIFACTS"])
                    directory.mkdir(parents=True, exist_ok=True)
                    await ui.screenshot(path=str(directory / f"payment-preview-{width}.png"))
            finally:
                await browser.close()


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
                await ui.locator('[data-review-card][data-run-id="run-one"]').get_by_role("button", name="Review operation").click()
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
                    rect = ui.locator(".review-overview .target-bounds")
                    assert float(await rect.get_attribute("width")) == 40
                    assert float(await rect.get_attribute("height")) == 40
                else:
                    await expect(ui.get_by_text("Target coordinates are unavailable in this capture. Use the action details; no highlight has been inferred.", exact=True)).to_be_visible()
                    assert not await ui.locator(".review-focus, .target-outline, .target-pointer").count()
                    await expect(ui.locator(".review-overview-canvas")).to_be_visible()
                    # The original masked black region stays black in the UI.
                    assert await ui.locator(".review-overview-canvas").evaluate("el=>Array.from(el.getContext('2d').getImageData(25,25,1,1).data)") == [0, 0, 0, 255]
                assert await ui.locator("dialog .acira-mascot").count() == 0
                assert not errors
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_companion_preferences_keep_approvals_mascot_free(tmp_path, monkeypatch):
    import base64
    import io
    import time
    from PIL import Image

    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "Appearance", selected=[], dashboard=False)
    journal = Journal(project.root)
    png = io.BytesIO()
    Image.new("RGB", (800, 600), "white").save(png, format="PNG")
    artifact = journal.store_artifact({"capture_status": "ready", "captured_at": time.time(), "mime_type": "image/png",
        "image": base64.b64encode(png.getvalue()).decode(), "viewport": {"width": 800, "height": 600},
        "bounding_box": {"x": 40, "y": 40, "width": 120, "height": 40}})
    review = journal.create_review("appearance-run", {"agent": "default", "policy_revision": project.snapshot().revision,
        "reason": "Review the note", "classification": {"action": "click", "target": {"text": "Save note"}},
        "evidence": {"destination": "https://fixture.invalid/notes"}, "artifact": artifact, "grant_eligible": False})
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
                async def preview_geometry():
                    await ui.evaluate("() => document.fonts.ready.then(() => true)")
                    return await ui.locator(".review-focus").evaluate("""el => ({
                        frame: el.querySelector('.review-focus-canvas').getBoundingClientRect().toJSON(),
                        caption: el.querySelector('.target-caption').getBoundingClientRect().toJSON()
                    })""")
                await click_control(ui, ui.get_by_role("button", name="Settings", exact=True, include_hidden=True))
                mode = ui.get_by_role("combobox", name="Companion appearance")
                await expect(mode).to_have_value("still")
                await mode.select_option("hidden")
                await ui.get_by_role("button", name="Approvals", exact=False).first.click()
                await ui.get_by_role("button", name="Review operation").click()
                assert await ui.locator("dialog .acira-mascot").count() == 0
                await expect(ui.locator(".review-stage .target-outline")).to_be_visible()
                hidden_geometry = await preview_geometry()
                hidden_frame, hidden_caption = hidden_geometry["frame"], hidden_geometry["caption"]
                preview_gap = hidden_frame["y"] - hidden_caption["y"] - hidden_caption["height"]
                await expect(ui.get_by_role("dialog").get_by_role("heading", name="Click “Save note”?", exact=True)).to_be_visible()
                await expect(ui.locator(".review-destination").get_by_text("Destination", exact=True)).to_be_visible()
                await expect(ui.locator(".review-destination").get_by_text("https://fixture.invalid/notes", exact=True)).to_be_visible()
                assert journal.review(review["review_id"])["state"] == "pending"
                await ui.get_by_role("button", name="Close dialog").click()
                await ui.reload()
                await ui.get_by_role("button", name="Agents", exact=True).click()
                await expect(ui.locator(".overview-companion")).not_to_be_visible()
                await click_control(ui, ui.get_by_role("button", name="Settings", exact=True, include_hidden=True))
                await expect(mode).to_have_value("hidden")
                await mode.select_option("animated")
                await ui.get_by_role("button", name="Agents", exact=True).click()
                await expect(ui.locator(".overview-companion")).to_be_visible()
                assert await ui.locator(".overview-companion").evaluate("el=>el.complete && el.naturalWidth > 0")
                await ui.emulate_media(reduced_motion="reduce")
                await ui.get_by_role("button", name="Approvals", exact=False).first.click()
                await ui.get_by_role("button", name="Review operation").click()
                await expect(ui.locator(".review-stage .target-outline")).to_be_visible()
                assert await ui.locator("dialog .acira-mascot").count() == 0
                animated_geometry = await preview_geometry()
                animated_frame, animated_caption = animated_geometry["frame"], animated_geometry["caption"]
                assert abs(animated_frame["width"] - hidden_frame["width"]) < 1
                assert abs(animated_frame["height"] - hidden_frame["height"]) < 1
                assert abs(animated_frame["y"] - animated_caption["y"] - animated_caption["height"] - preview_gap) < 1
                await ui.emulate_media(reduced_motion="no-preference")
                assert await ui.locator("dialog .acira-mascot").count() == 0
                moving_geometry = await preview_geometry()
                moving_frame, moving_caption = moving_geometry["frame"], moving_geometry["caption"]
                assert abs(moving_frame["width"] - hidden_frame["width"]) < 1
                assert abs(moving_frame["height"] - hidden_frame["height"]) < 1
                assert abs(moving_frame["y"] - moving_caption["y"] - moving_caption["height"] - preview_gap) < 1
                assert await ui.locator(".review-stage .target-outline").evaluate("el=>getComputedStyle(el).stroke") == "rgb(185, 28, 28)"
                journal.decide(review["review_id"], "rejected")
                await ui.locator("dialog .pill").filter(has_text="rejected").wait_for(timeout=12000)
                assert await ui.locator("dialog .acira-mascot").count() == 0
                await expect(ui.get_by_role("button", name="Approve once", exact=True)).to_be_disabled()
            finally:
                await browser.close()


async def reveal_control(page, locator, *, keyboard=False):
    """Use actual disclosure controls before reaching an advanced action."""
    await locator.wait_for(state="attached")
    for section in await locator.locator("xpath=ancestor::details").all():
        if await locator.is_visible():
            break
        if await section.get_attribute("open") is None:
            summary = section.locator(":scope > summary")
            if keyboard:
                await keyboard_activate(page, summary)
            else:
                await summary.click()
    await locator.wait_for(state="visible")


async def click_control(page, locator):
    await reveal_control(page, locator)
    await locator.click()


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1440, 320])
async def test_quiet_overview_keeps_reviews_errors_and_advanced_tools_accessible(tmp_path, monkeypatch, width):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    orders = initialize(tmp_path / "Orders", selected=["checkout"], dashboard=False)
    support = initialize(tmp_path / "Support", selected=["credentials"], dashboard=False)
    journal = Journal(orders.root)
    journal.update_run("old-run-private-id", {"agent": "default", "state": "ended", "connected": False})
    review = journal.create_review("review-run-private-id", {
        "agent": "default", "policy_revision": orders.snapshot().revision,
        "reason": "Review before saving the draft", "classification": {"action": "click", "target": {"text": "Save draft"}},
        "evidence": {}, "grant_eligible": False,
    })
    # A real invalid policy must remain discoverable even with its project closed.
    invalid = Path(support.policies("default")[0]["path"])
    invalid.write_text("{")
    before = [project.path.read_bytes() for project in (orders, support)]
    service = Service()
    async with TestServer(service.app(), host="127.0.0.1") as server:
        service.port = server.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            ticket = (await (await client.post(server.make_url("/api/ticket"), json={})).json())["ticket"]
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            ui = await browser.new_page(viewport={"width": width, "height": 1000})
            errors, writes = [], []
            ui.on("pageerror", lambda error: errors.append(str(error)))
            async def unavailable_empty_queue(route):
                response = await route.fetch()
                data = await response.json()
                if not data["reviews"]:
                    await route.fulfill(status=503, json={"error": "Temporarily unavailable"})
                else:
                    await route.fulfill(response=response, json=data)
            if width == 320:
                await ui.route("**/api/projects/*/reviews", unavailable_empty_queue)
            try:
                await ui.goto(str(server.make_url("/")) + "#ticket=" + ticket)
                await expect(ui.get_by_role("heading", name="Orders", exact=True)).to_be_visible()
                ui.on("request", lambda request: writes.append(request.url) if request.method not in {"GET", "HEAD"} else None)
                await expect(ui.get_by_role("heading", name="No agents running", exact=True)).to_be_visible()
                assert await ui.locator("nav > button:visible").count() == 3
                assert not await ui.get_by_role("button", name="Backtesting", exact=True, include_hidden=True).is_visible()
                assert not await ui.get_by_role("combobox", name="Project", exact=True).is_visible()
                assert not await ui.get_by_role("button", name="default", exact=True).first.is_visible()
                assert "old-run-private-id" not in await ui.locator("main").inner_text()
                assert str(orders.root) not in await ui.locator("main").inner_text()
                await expect(ui.locator(".attention-strip")).to_contain_text("1 action needs your review")
                if width == 320:
                    await expect(ui.get_by_text("Some review requests could not be loaded.", exact=False)).to_be_visible()
                await expect(ui.locator(".project-error")).to_contain_text("Support / default: configuration needs attention")
                assert not await ui.locator(".project-error pre").is_visible()
                assert await ui.locator(".metric").count() == 0
                project_row = ui.locator(".project-row").filter(has=ui.get_by_role("heading", name="Orders", exact=True))
                await keyboard_activate(ui, project_row.locator(":scope > summary"))
                folder = project_row.get_by_text("Project folder", exact=True)
                await keyboard_activate(ui, folder)
                await expect(project_row.locator("pre")).to_have_text(str(orders.root))
                # Actual polling must preserve the open sections and keyboard focus.
                journal.update_run("second-old-run", {"agent": "default", "state": "ended", "connected": False})
                await expect(ui.locator(".previous-runs > summary")).to_have_text("Previous runs (2)", timeout=12000)
                assert await project_row.get_attribute("open") is not None
                await expect(folder).to_be_focused()
                await expect(project_row.locator("pre")).to_be_visible()
                if width == 320:
                    await ui.unroute("**/api/projects/*/reviews", unavailable_empty_queue)
                await keyboard_activate(ui, ui.get_by_role("button", name="Open approvals", exact=True))
                card = ui.locator('[data-review-card="' + review["review_id"] + '"]')
                await expect(card).to_contain_text("Save draft")
                assert "review-run-private-id" not in await card.inner_text()
                await keyboard_activate(ui, card.get_by_role("button", name="Review operation"))
                await expect(ui.get_by_role("button", name="Approve once", exact=True)).to_be_enabled()
                await keyboard_activate(ui, ui.get_by_role("button", name="Close dialog", exact=True))
                assert journal.review(review["review_id"])["state"] == "pending"
                await keyboard_activate(ui, ui.get_by_role("button", name="Settings", exact=True, include_hidden=True))
                await expect(ui.get_by_role("combobox", name="Companion appearance")).to_be_visible()
                assert not await ui.get_by_role("button", name="Advanced configuration", exact=True, include_hidden=True).is_visible()
                await keyboard_activate(ui, ui.get_by_role("button", name="Advanced configuration", exact=True, include_hidden=True))
                await expect(ui.get_by_role("dialog")).to_be_visible()
                await keyboard_activate(ui, ui.get_by_role("button", name="Close dialog", exact=True))
                await keyboard_activate(ui, ui.get_by_role("button", name="Policies", exact=True))
                await expect(ui.get_by_role("button", name="Add protection", exact=True)).to_be_visible()
                assert not await ui.get_by_role("button", name="Revision history", exact=True, include_hidden=True).is_visible()
                await keyboard_activate(ui, ui.get_by_role("button", name="Revision history", exact=True, include_hidden=True))
                await expect(ui.get_by_role("dialog")).to_be_visible()
                await keyboard_activate(ui, ui.get_by_role("button", name="Close dialog", exact=True))
                assert await ui.evaluate("document.body.scrollWidth <= innerWidth")
                assert [project.path.read_bytes() for project in (orders, support)] == before
                assert not writes and not errors
                if os.environ.get("ACIRA_TEST_ARTIFACTS"):
                    directory = Path(os.environ["ACIRA_TEST_ARTIFACTS"])
                    directory.mkdir(parents=True, exist_ok=True)
                    await keyboard_activate(ui, ui.get_by_role("button", name="Agents", exact=True))
                    await ui.screenshot(path=str(directory / f"quiet-agents-{width}.png"), full_page=True)
            finally:
                await browser.close()


async def keyboard_focus(page, locator):
    """Reach a control using ordinary Tab navigation, without synthetic focus."""
    await reveal_control(page, locator, keyboard=True)
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
            await keyboard_activate(ui, ui.get_by_role("button", name="Agent assignments", exact=True, include_hidden=True))
            toggle = ui.get_by_role("checkbox", name="Keyboard edited policy")
            await keyboard_focus(ui, toggle)
            await ui.keyboard.press("Space")
            await keyboard_activate(ui, ui.get_by_role("button", name="Save assignments", exact=True))
            await ui.get_by_role("dialog").wait_for(state="hidden")
            assert project.document()["agents"]["default"]["disabled_policies"] == [policy["id"]]
            await keyboard_activate(ui, ui.get_by_role("button", name="Revision history", exact=True, include_hidden=True))
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
            await click_control(ui, ui.get_by_role("button", name="Revision history", exact=True, include_hidden=True))
            assert await ui.get_by_role("row").filter(has_text=first[:12]).count() == 0
            await keyboard_activate(ui, ui.get_by_role("button", name="Load older revisions", exact=True))
            await ui.get_by_role("row").filter(has_text=first[:12]).wait_for()
            await ui.get_by_role("button", name="Close dialog").click()
            await click_control(ui, ui.get_by_role("button", name="Backtesting", exact=True, include_hidden=True))
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
            await click_control(ui, ui.get_by_role("button", name="Assignments / graph", exact=True, include_hidden=True))
            file = ui.locator("#dialog").get_by_role("button", name="File details for " + Path(source["path"]).name, exact=True)
            await file.press("Enter")
            location = ui.get_by_role("dialog", name="File location for " + Path(source["path"]).name, exact=True)
            await expect(location.locator("pre")).to_have_text(source["path"])
            await ui.keyboard.press("Escape")
            await expect(file).to_be_focused()
            assert await ui.locator("#dialog").is_visible()
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
            await click_control(ui, ui.get_by_role("button", name="Assignments / graph", exact=True, include_hidden=True))
            node = ui.get_by_role("button", name="Inspect policy " + source["name"], exact=True)
            assert await node.get_attribute("transform") != transform
            await keyboard_activate(ui, ui.get_by_text("Copy an installed policy to another agent", exact=True))
            await ui.get_by_role("combobox", name="Copy destination agent").select_option("research")
            await keyboard_activate(ui, ui.get_by_role("button", name="Copy policy", exact=True))
            await ui.get_by_role("dialog").wait_for(state="hidden")
            assert project.policies("research")[0]["id"] == source["id"]
            assert project.policies("research")[0]["path"] != source["path"]
            await click_control(ui, ui.get_by_role("button", name="Assignments / graph", exact=True, include_hidden=True))
            await ui.get_by_role("button", name="Inspect agent research", exact=True).click()
            await ui.get_by_role("heading", name="research", exact=True).wait_for()
            await keyboard_activate(ui, ui.get_by_role("button", name="Edit folders for research", exact=True))
            await ui.get_by_role("textbox", name="General policy folder", exact=True).fill("policies/general")
            await keyboard_activate(ui, ui.get_by_role("button", name="Save policy folders", exact=True))
            await ui.get_by_role("dialog").wait_for(state="hidden")
            assert project.folders("default")["general"] == project.folders("research")["general"]
            await click_control(ui, ui.get_by_role("button", name="Assignments / graph", exact=True, include_hidden=True))
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
            await keyboard_activate(ui, ui.get_by_role("button", name="Settings", exact=True, include_hidden=True))
            await keyboard_activate(ui, ui.get_by_role("button", name="Add agent", exact=True, include_hidden=True))
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
            await keyboard_activate(ui, ui.get_by_role("button", name="History retention", exact=True, include_hidden=True))
            days = ui.get_by_role("spinbutton", name="Audit history · days", exact=True)
            await keyboard_focus(ui, days)
            await ui.keyboard.press("ControlOrMeta+A")
            await ui.keyboard.insert_text("14")
            await keyboard_activate(ui, ui.get_by_role("button", name="Save retention", exact=True))
            await ui.get_by_role("dialog").wait_for(state="hidden")
            assert project.document()["retention"]["audit_days"] == 14
            await keyboard_activate(ui, ui.get_by_role("button", name="Secret references", exact=True, include_hidden=True))
            await keyboard_activate(ui, ui.get_by_role("button", name="Add reference", exact=True))
            for label, value in [("Reference ID", "login"), ("Display label", "Test account"), ("Environment variable name", "TEST_ACCOUNT")]:
                field = ui.get_by_role("textbox", name=label, exact=True)
                await keyboard_focus(ui, field)
                await ui.keyboard.insert_text(value)
            await keyboard_activate(ui, ui.get_by_role("button", name="Save references", exact=True))
            await ui.get_by_role("dialog").wait_for(state="hidden")
            assert project.document()["agents"]["checkout"]["guard"]["secrets_config_path"]
            await keyboard_activate(ui, ui.get_by_role("button", name="Advanced configuration", exact=True, include_hidden=True))
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
            await keyboard_activate(ui, ui.get_by_role("button", name="Pair capture extension", exact=True, include_hidden=True))
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
                row = ui.locator('.running-section [data-run-card="run-live"]')
                await expect(row).to_contain_text("running")
                control = row.get_by_role("button", name="Pause" if focused_action == "pause" else "Inspect", exact=True)
                await keyboard_focus(ui, control)
                journal.update_run("run-live", {**payload, "state": "ended", "connected": False})
                await expect(row).to_have_count(0, timeout=12000)
                await expect(ui.get_by_role("heading", name="No agents running", exact=True)).to_be_visible()
                await expect(ui.get_by_role("button", name="Refresh", exact=True)).to_be_focused()
                previous = ui.locator(".previous-runs")
                assert not await previous.locator('[data-run-card="run-live"]').is_visible()
                await keyboard_activate(ui, previous.locator("summary"))
                ended = previous.locator('[data-run-card="run-live"]')
                await expect(ended).to_contain_text("ended")
                assert await ended.get_by_role("button", name="Pause", exact=True).count() == 0
                await ended.get_by_role("button", name="Inspect", exact=True).click()
                await expect(ui.get_by_role("dialog")).to_be_visible()
                await ui.get_by_text("Run details", exact=True).click()
                await expect(ui.get_by_role("dialog").locator("pre").last).to_contain_text("run-live")
            finally:
                await browser.close()
