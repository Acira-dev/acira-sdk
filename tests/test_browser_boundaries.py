"""Observe real DOM effects: a block verdict alone is not sufficient evidence."""
import asyncio
import os

import pytest

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright

from acira import protect
from acira.adapters.playwright import PlaywrightCDPProxy
from acira_cdp_proxy.action_gateway import AciraActionBlocked
from acira.project import initialize


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", [
    "javascript:document.body.textContent='changed'",
    "java\nscript:document.body.textContent='changed'",
    "data:text/html,<script>document.body.textContent='changed'</script>",
])
async def test_navigation_cannot_execute_a_script_to_bypass_guarded_actions(destination):
    from acira.adapters.playwright_boundary import trusted
    async with async_playwright() as p:
        async with PlaywrightCDPProxy(config={"terminal_log": False}) as guard:
            guard.patch_playwright(p)
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await trusted(page, "set_content", "<p>Original page</p>")
            with pytest.raises(AciraActionBlocked, match="navigation URLs"):
                await page.goto(destination)
            assert await page.locator("body").inner_text() == "Original page"
            assert page.url == "about:blank"
            await browser.close()


@pytest.mark.asyncio
async def test_locator_frame_and_page_blocks_have_no_side_effects(tmp_path):
    from acira.adapters.playwright_boundary import trusted
    async with async_playwright() as p:
        async with PlaywrightCDPProxy(config={"blocked_actions": ["click", "form_input"], "terminal_log": False}) as guard:
            guard.patch_playwright(p)
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await trusted(page, "set_content", '<button id="buy" onclick="this.textContent=\'clicked\'">Buy</button><input id="field"><iframe srcdoc="<button id=inner onclick=&quot;this.textContent=\'clicked\'&quot;>Inner</button>"></iframe>')
            for action in (lambda: page.click("#buy"), lambda: page.locator("#buy").click(),
                           lambda: page.get_by_role("button", name="Buy").click(),
                           lambda: page.locator("#field").fill("hello"),
                           lambda: page.frame_locator("iframe").locator("#inner").click()):
                with pytest.raises(AciraActionBlocked):
                    await action()
            assert await page.locator("#buy").inner_text() == "Buy"
            assert await page.locator("#field").input_value() == ""
            assert await page.frame_locator("iframe").locator("#inner").inner_text() == "Inner"
            await browser.close()


@pytest.mark.asyncio
async def test_changed_form_invalidates_approved_submit(tmp_path):
    from acira.adapters.playwright_boundary import trusted
    class ChangeDuringReview:
        page = None
        async def ask(self, request):
            await trusted(self.page, "evaluate", "document.querySelector('#amount').value='900'")
            return "allow"
    reviewer = ChangeDuringReview()
    async with async_playwright() as p:
        async with PlaywrightCDPProxy(config={"review_actions": ["click", "submit"], "terminal_log": False}, review_backend=reviewer) as guard:
            guard.patch_playwright(p)
            browser = await p.chromium.launch()
            page = reviewer.page = await browser.new_page()
            await trusted(page, "set_content", '<form onsubmit="event.preventDefault(); document.title=\'submitted\'"><input id="amount" name="amount" value="10"><button type="submit">Send</button></form>')
            with pytest.raises(AciraActionBlocked, match="changed before dispatch"):
                await page.get_by_role("button", name="Send").click()
            assert await page.title() != "submitted"
            await browser.close()


@pytest.mark.asyncio
async def test_local_project_reload_blocks_next_action(tmp_path, monkeypatch):
    from acira.adapters.playwright_boundary import trusted
    from acira.local import atomic_json
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", selected=[], dashboard=False)
    async with async_playwright() as p:
        async with protect(project=project.root, dashboard=False) as guard:
            guard.patch_playwright(p)
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await trusted(page, "set_content", '<button id="action" onclick="this.textContent=\'done\'">Run</button>')
            document = project.document()
            document["agents"]["default"]["guard"] = {"blocked_actions": ["click"]}
            atomic_json(project.path, document)
            with pytest.raises(AciraActionBlocked):
                await page.locator("#action").click()
            assert await page.locator("#action").inner_text() == "Run"
            await browser.close()


@pytest.mark.asyncio
async def test_keyboard_upload_mouse_touch_and_raw_paths_do_not_bypass(tmp_path):
    from acira.adapters.playwright_boundary import trusted
    async with async_playwright() as p:
        async with PlaywrightCDPProxy(config={"blocked_actions": ["click", "submit", "form_input", "file_upload"], "terminal_log": False}) as guard:
            guard.patch_playwright(p)
            browser = await p.chromium.launch()
            page = await browser.new_page(has_touch=True)
            await trusted(page, "set_content", '<form onsubmit="event.preventDefault();document.title=\'submitted\'"><input id="field"><button type="submit">Send</button></form><input id="upload" type="file"><a id="download" download="report.txt" href="data:text/plain,report">Download</a>')
            input_file = tmp_path / "upload.txt"
            input_file.write_text("test data")
            box = await page.locator("button").bounding_box()
            await trusted(page.locator("#field"), "focus")
            for action in (lambda: page.keyboard.press("Enter"), lambda: page.keyboard.insert_text("secret"),
                           lambda: page.locator("#upload").set_input_files(input_file),
                           lambda: page.mouse.click(box["x"] + 2, box["y"] + 2),
                           lambda: page.touchscreen.tap(box["x"] + 2, box["y"] + 2),
                           lambda: page.locator("#download").click(),
                           lambda: page.evaluate("document.title='bypassed'")):
                with pytest.raises(AciraActionBlocked):
                    await action()
            session = await page.context.new_cdp_session(page)
            with pytest.raises(AciraActionBlocked):
                await session.send("Runtime.evaluate", {"expression": "document.title='bypassed'"})
            assert await page.title() == ""
            assert await page.locator("#field").input_value() == ""
            assert await trusted(page.locator("#upload"), "evaluate", "el=>el.files.length") == 0
            await browser.close()


@pytest.mark.asyncio
async def test_persistent_context_and_popup_inherit_protection(tmp_path):
    from acira.adapters.playwright_boundary import trusted
    async with async_playwright() as p:
        async with PlaywrightCDPProxy(config={"blocked_actions": ["click"], "terminal_log": False}) as guard:
            guard.patch_playwright(p)
            context = await p.chromium.launch_persistent_context(tmp_path / "profile", headless=True)
            page = context.pages[0]
            async with page.expect_popup() as opened:
                await trusted(page, "evaluate", "window.open('about:blank')")
            popup = await opened.value
            await trusted(popup, "set_content", '<button onclick="document.title=\'bypassed\'">Act</button>')
            with pytest.raises(AciraActionBlocked):
                await popup.locator("button").click()
            assert await popup.title() == ""
            with pytest.raises(AciraActionBlocked):
                await context.add_init_script("window.bypassed=true")
            await context.close()


@pytest.mark.asyncio
async def test_cdp_binding_rejects_replaced_node_and_changed_values():
    from acira.adapters.playwright_boundary import trusted
    from acira_cdp_proxy.operation import bind_cdp
    from acira_cdp_proxy.guard import GuardTarget
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content('<form><input name="amount" value="10"><input type="hidden" value="csrf-secret"><textarea name="note">Original</textarea><select name="currency"><option>USD</option><option>EUR</option></select><input name="confirm" type="checkbox"><button id="send">Send</button></form>')
        session = await page.context.new_cdp_session(page)
        async def probe(method, params, session_id):
            return await session.send(method, params)
        binding = await bind_cdp(probe, GuardTarget(id="send", url="about:blank"), "submit")
        assert binding.evidence["fields"][1]["display"] == "[REDACTED]"
        assert binding.evidence["fields"][2]["checked"] is None
        assert binding.evidence["fields"][3]["checked"] is None
        assert binding.evidence["fields"][4]["checked"] is False
        assert await binding.revalidate()
        await page.locator("input[name=amount]").fill("900")
        assert not await binding.revalidate()
        await binding.release()
        for selector, value in [("textarea", "Edited"), ("select", "EUR"), ("input[type=checkbox]", True)]:
            binding = await bind_cdp(probe, GuardTarget(id="send", url="about:blank"), "submit")
            if value is True:
                await page.locator(selector).check()
            elif selector == "select":
                await page.locator(selector).select_option(value)
            else:
                await page.locator(selector).fill(value)
            assert not await binding.revalidate()
            await binding.release()
        binding = await bind_cdp(probe, GuardTarget(id="send", url="about:blank"), "submit")
        await page.evaluate("const old=document.querySelector('#send');old.replaceWith(old.cloneNode(true))")
        assert not await binding.revalidate()
        await binding.release()
        await browser.close()


@pytest.mark.asyncio
async def test_bounded_form_fill_reviews_all_steps_once_and_never_submits():
    from acira.adapters.playwright_boundary import trusted
    class Review:
        requests = []
        async def ask(self, request):
            self.requests.append(request)
            return "allow"
    reviewer = Review()
    async with async_playwright() as p:
        async with PlaywrightCDPProxy(config={"review_actions": ["form_input"], "terminal_log": False}, review_backend=reviewer) as guard:
            guard.patch_playwright(p)
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await trusted(page, "set_content", '<form onsubmit="event.preventDefault();document.title=\'submitted\'"><textarea name="recipient"></textarea><input name="amount"><select name="currency"><option>USD</option></select><button>Send</button></form>')
            await guard.fill_fields([(page.locator("[name=recipient]"), "Example"), (page.locator("[name=amount]"), "10")])
            assert len(reviewer.requests) == 1
            assert len(reviewer.requests[0].classification.details["operation_evidence"]["bounded_steps"]) == 2
            assert await page.locator("[name=recipient]").input_value() == "Example"
            assert await page.locator("[name=amount]").input_value() == "10"
            assert await page.title() == ""
            await browser.close()


@pytest.mark.asyncio
async def test_review_image_masks_fields_and_registered_secret_text():
    import base64
    import io
    from PIL import Image
    from acira_cdp_proxy.redaction import SECRET_REDACTION_REGISTRY
    from acira.adapters.playwright_boundary import masked_artifact
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width":800,"height":600})
        secret = "only-for-screenshot-privacy-test"
        SECRET_REDACTION_REGISTRY.register_secret(secret_id="image-test", label="Test secret", value=secret)
        try:
            await page.set_content('<input value="private-field"><p id="secret">'+secret+'</p><button>Send</button>')
            field_box = await page.locator("input").bounding_box()
            text_box = await page.locator("#secret").bounding_box()
            artifact = await masked_artifact(page, await page.locator("button").element_handle())
            assert artifact["capture_status"] == "ready"
            pixels = Image.open(io.BytesIO(base64.b64decode(artifact["image"])))
            for box in (field_box, text_box):
                assert pixels.getpixel((int(box["x"]+2),int(box["y"]+2))) == (0,0,0)
        finally:
            SECRET_REDACTION_REGISTRY.clear()
            await browser.close()


@pytest.mark.asyncio
async def test_clear_filechooser_and_composite_enter_share_guard_boundary(tmp_path):
    from acira.adapters.playwright_boundary import trusted
    async with async_playwright() as p:
        async with PlaywrightCDPProxy(config={"blocked_actions": ["form_input", "submit", "file_upload"], "terminal_log": False}) as guard:
            guard.patch_playwright(p)
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await trusted(page, "set_content", '<form onsubmit="event.preventDefault();document.title=\'submitted\'"><input id="field" value="unchanged"><button>Send</button></form><input id="upload" type="file">')
            with pytest.raises(AciraActionBlocked):
                await page.locator("#field").clear()
            with pytest.raises(AciraActionBlocked):
                await page.locator("#field").press("Control+Enter")
            async with page.expect_file_chooser() as chosen:
                await trusted(page.locator("#upload"), "click")
            with pytest.raises(AciraActionBlocked):
                await (await chosen.value).set_files({"name":"example.txt","mimeType":"text/plain","buffer":b"test"})
            assert await page.locator("#field").input_value() == "unchanged"
            assert await page.title() == ""
            assert await trusted(page.locator("#upload"), "evaluate", "el=>el.files.length") == 0
            await browser.close()


@pytest.mark.asyncio
async def test_upload_changed_during_review_is_not_sent(tmp_path):
    from acira.adapters.playwright_boundary import trusted
    path = tmp_path / "report.txt"
    path.write_text("reviewed content")
    class Reviewer:
        async def ask(self, request):
            assert request.classification.details["operation_evidence"]["files"][0]["name"] == "report.txt"
            path.write_text("different content")
            return "allow"
    async with async_playwright() as p:
        async with PlaywrightCDPProxy(config={"review_actions":["file_upload"],"terminal_log":False}, review_backend=Reviewer()) as guard:
            guard.patch_playwright(p)
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await trusted(page, "set_content", '<input type="file">')
            with pytest.raises(AciraActionBlocked, match="changed before dispatch"):
                await page.locator("input").set_input_files(path)
            assert await trusted(page.locator("input"), "evaluate", "el=>el.files.length") == 0
            await browser.close()


@pytest.mark.asyncio
async def test_real_cdp_connection_rejects_locator_without_http_side_effect(tmp_path):
    import subprocess
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    received = []
    app = web.Application()
    async def index(request):
        return web.Response(text='<button id="act" onclick="fetch(\'/effect\',{method:\'POST\'})">Act</button>', content_type="text/html")
    async def effect(request):
        received.append(True)
        return web.Response(text="done")
    app.router.add_get("/", index)
    app.router.add_post("/effect", effect)
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as p:
        profile = tmp_path / "cdp-profile"
        process = subprocess.Popen([p.chromium.executable_path, "--headless=new", "--no-sandbox", "--remote-debugging-port=0",
                                    "--no-first-run", "--user-data-dir="+str(profile), str(server.make_url("/"))],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if (profile / "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(.05)
            port = (profile / "DevToolsActivePort").read_text().splitlines()[0]
            async with PlaywrightCDPProxy(config={"blocked_actions":["click"], "terminal_log":False, "terminal_review":False}) as guard:
                guard.patch_playwright(p)
                browser = await p.chromium.connect_over_cdp("http://127.0.0.1:"+port, timeout=10000)
                page = browser.contexts[0].pages[0]
                await page.locator("#act").wait_for(timeout=5000)
                with pytest.raises(AciraActionBlocked):
                    await page.locator("#act").click()
                assert received == []
                await browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)


@pytest.mark.asyncio
@pytest.mark.parametrize("scale", [1, 2])
async def test_review_image_scroll_frame_and_zoom_coordinates(scale):
    import base64
    import io
    from PIL import Image
    from acira.adapters.playwright_boundary import masked_artifact
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width":800,"height":600}, device_scale_factor=2)
        await page.set_content('<div style="height:700px"></div><input value="private"><iframe srcdoc="<button>Send</button>"></iframe><div style="height:900px"></div>')
        await page.evaluate("scrollTo(0,650)")
        session = await page.context.new_cdp_session(page)
        await session.send("Emulation.setPageScaleFactor", {"pageScaleFactor":scale})
        element = await page.frame_locator("iframe").locator("button").element_handle()
        expected = await element.bounding_box()
        artifact = await masked_artifact(page, element)
        assert artifact["capture_status"] == "ready"
        offsets = await page.evaluate("({x:visualViewport.offsetLeft,y:visualViewport.offsetTop})")
        assert artifact["bounding_box"]["x"] == pytest.approx(expected["x"]-offsets["x"])
        assert artifact["bounding_box"]["y"] == pytest.approx(expected["y"]-offsets["y"])
        pixels = Image.open(io.BytesIO(base64.b64decode(artifact["image"])))
        box = await page.locator("input").bounding_box()
        sx,sy = pixels.width/artifact["viewport"]["width"],pixels.height/artifact["viewport"]["height"]
        x,y = int((box["x"]-offsets["x"]+2)*sx),int((box["y"]-offsets["y"]+2)*sy)
        assert pixels.getpixel((x,y)) == (0,0,0)
        await browser.close()


@pytest.mark.asyncio
async def test_policy_change_replaces_pending_review_before_dispatch(tmp_path, monkeypatch):
    from acira.adapters.playwright_boundary import trusted
    from acira.local import atomic_json
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", selected=[])
    config = project.document()
    config["agents"]["default"]["guard"] = {"review_actions":["click"]}
    atomic_json(project.path, config)
    async with async_playwright() as p:
        async with protect(project=project.root, dashboard=True) as guard:
            guard.patch_playwright(p)
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await trusted(page,"set_content",'<button id="next" onclick="this.textContent=\'Done\'">Next</button>')
            operation = asyncio.create_task(page.locator("#next").click())
            journal = guard.runtime_control.journal
            async def pending_review():
                for _ in range(500):
                    reviews = [item for item in journal.reviews() if item["state"]=="pending"]
                    if reviews:
                        return reviews[0]
                    await asyncio.sleep(.01)
                raise AssertionError("No pending review")
            try:
                first = await pending_review()
                config["agents"]["default"]["guard"]["review_actions"].append("file_upload")
                atomic_json(project.path, config)
                for _ in range(500):
                    if journal.review(first["review_id"])["state"] == "invalidated":
                        break
                    await asyncio.sleep(.01)
                second = await pending_review()
                assert second["review_id"] != first["review_id"]
                assert second["policy_revision"] != first["policy_revision"]
                assert not journal.decide(first["review_id"],"approved")
                assert await page.locator("#next").inner_text() == "Next"
                journal.decide(second["review_id"],"approved")
                await asyncio.wait_for(operation, 5)
                assert await page.locator("#next").inner_text() == "Done"
                assert sum(event["type"]=="action_dispatched" for event in journal.events()) == 1
            finally:
                if not operation.done():
                    operation.cancel()
                await browser.close()
