"""Loaded Chromium extension journey; native permission prompts remain a manual gate."""
import json
import shutil
from pathlib import Path

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright, expect

from acira.project import initialize
from acira.service import Service


@pytest.mark.asyncio
async def test_extension_pairs_captures_and_imports_into_selected_project(tmp_path, monkeypatch, select_capture_target):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    first = initialize(tmp_path / "Orders", selected=[])
    other = initialize(tmp_path / "Support", selected=[])
    service = Service()
    fixture = web.Application()
    async def index(request):
        return web.Response(text='''<button id="send" style="margin:60px" formaction="/send?access_token=CAPTURE_SECRET#PRIVATE" onclick="fetch('HANDLER_SECRET')">Send invoice</button>''', content_type="text/html")
    fixture.router.add_get("/", index)
    extension = tmp_path / "extension"
    shutil.copytree(Path(__file__).parents[1] / "tools/acira-capture-extension", extension)
    manifest = json.loads((extension / "manifest.json").read_text())
    # Pregrant only the fixture loopback origin. The shipped manifest retains
    # optional host access and the actual options-page pairing code runs unchanged.
    manifest["host_permissions"] = ["http://127.0.0.1/*"]
    (extension / "manifest.json").write_text(json.dumps(manifest))
    async with TestServer(service.app(), host="127.0.0.1") as server, TestServer(fixture, host="127.0.0.1") as website:
        service.port = server.port
        async with ClientSession(headers={"Authorization":"Bearer "+service.admin_key}) as client:
            pairing = await (await client.post(server.make_url("/api/pairing"),json={})).json()
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(tmp_path / "profile", channel="chromium", headless=True,
                args=["--disable-extensions-except="+str(extension),"--load-extension="+str(extension)])
            try:
                worker = context.service_workers[0] if context.service_workers else await context.wait_for_event("serviceworker", timeout=10000)
                extension_id = worker.url.split("/")[2]
                options = await context.new_page()
                await options.goto("chrome-extension://"+extension_id+"/connection.html")
                await options.get_by_label("Local dashboard address").fill(pairing["url"])
                await options.get_by_label("Pairing code").fill(pairing["code"])
                await options.get_by_role("button",name="Pair dashboard").click()
                await expect(options.locator("#status")).to_contain_text("Paired.",timeout=10000)
                await options.get_by_label("Project",exact=True).select_option(label="Orders")
                await options.get_by_role("button",name="Save destination").click()
                await expect(options.locator("#status")).to_contain_text("Orders / default")
                page = await context.new_page()
                await page.goto(str(website.make_url("/")) + "?token=PAGE_SECRET")
                await worker.evaluate("""async url=>{
                  const tabs=await chrome.tabs.query({});
                  const tab=tabs.find(tab=>tab.url===url);
                  await chrome.scripting.insertCSS({target:{tabId:tab.id},files:["capture.css"]});
                  await chrome.scripting.executeScript({target:{tabId:tab.id},files:["content.js"]});
                }""", page.url)
                await page.evaluate("""() => {
                  const frame=document.querySelector('.acira-capture-shield');
                  window.dispatchEvent(new MessageEvent('message', {source:frame.contentWindow,
                    origin:frame.src.split('/').slice(0,3).join('/'),
                    data:{type:'acira.selection',kind:'select',x:65,y:65}}));
                }""")
                await expect(page.get_by_role("button",name="Ask first",exact=True)).to_be_disabled()
                await select_capture_target(page, page.locator("#send"))
                await page.get_by_role("button",name="Ask first",exact=True).click()
                page.on("dialog",lambda dialog:dialog.accept())
                await page.get_by_role("button",name="Save to dashboard",exact=True).click()
                await expect(page.locator("#acira-capture-status")).to_contain_text("Saved to Orders / default",timeout=10000)
                assert len(first.policies("default")) == 1
                assert other.policies("default") == []
                async with page.expect_download() as saved:
                    await page.get_by_role("button",name="Download",exact=True).click()
                exported = json.loads(Path(await (await saved.value).path()).read_text())
                async with page.expect_download() as second:
                    await page.get_by_role("button",name="Download",exact=True).click()
                again = json.loads(Path(await (await second.value).path()).read_text())
                assert exported == again, "Repeated exports must keep the capture identity"
                await page.get_by_role("button",name="Save to dashboard",exact=True).click()
                await expect(page.locator("#acira-capture-status")).to_contain_text("Already saved to Orders / default",timeout=10000)
                assert len(first.policies("default")) == 1, "Repeated saves must not add duplicate policies"
                assert exported["schema"] == "acira.specific_skill.v2"
                assert "CAPTURE_SECRET" not in json.dumps(exported)
                assert "HANDLER_SECRET" not in json.dumps(exported)
                assert "PRIVATE" not in json.dumps(exported)
                assert "PAGE_SECRET" not in json.dumps(exported)
                await page.locator(".acira-capture-shield").evaluate("frame => frame.remove()")
                await expect(page.locator("#acira-capture-pill")).to_have_text("Paused")
                await expect(page.locator("#acira-capture-status")).to_contain_text("Close and reopen")
                async with page.expect_download() as recovered:
                    await page.get_by_role("button", name="Download", exact=True).click()
                recovery = json.loads(Path(await (await recovered.value).path()).read_text())
                assert recovery == exported, "Bridge failure must preserve the already captured draft"
            finally:
                await context.close()
