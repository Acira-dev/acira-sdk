"""Actual extension capture → terminal configuration → observed guarded effects."""
import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright, expect

from acira import protect
from acira.cli import run
from acira.extension import install_extension
from acira.project import initialize
from acira.journal import Journal
from acira.service import Service
from acira_cdp_proxy.action_gateway import AciraActionBlocked


@pytest.mark.asyncio
async def test_specific_capture_terminal_and_dashboard_control_real_navigation(tmp_path, monkeypatch, select_capture_target):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    hits = []
    async def landing(request):
        hits.append(request.path)
        if request.path == "/":
            return web.Response(content_type="text/html", text='''<link rel="icon" href="data:,"><title>Fixture guide</title><main><a href="/quickstart">Quickstart</a><a id="guide" href="/guide">Open guide</a><a id="about" href="/about">About this project</a></main><script>window.addEventListener('click', event => { if (event.target.closest('#guide')) history.pushState(null, '', '/unexpected'); }, true);</script>''')
        return web.Response(content_type="text/html", text='<title>Public content</title><h1>Fixture guide content</h1>')
    app = web.Application()
    app.router.add_get('/{tail:.*}', landing)
    project = initialize(tmp_path / "existing", selected=[], dashboard=False)
    initialize(project.root, agent="unrelated", general="policies/unrelated/general", specific="policies/unrelated/specific", selected=[], dashboard=False)
    extension = install_extension(tmp_path / "extension")
    manifest = json.loads((extension / "manifest.json").read_text())
    manifest["host_permissions"] = ["http://127.0.0.1/*"]
    (extension / "manifest.json").write_text(json.dumps(manifest))
    async with TestServer(app, host="127.0.0.1") as website:
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(tmp_path / "capture-profile", channel="chromium", headless=True,
                service_workers="block", args=["--disable-extensions-except="+str(extension), "--load-extension="+str(extension)])
            try:
                worker = context.service_workers[0] if context.service_workers else await context.wait_for_event("serviceworker", timeout=10000)
                page = await context.new_page()
                await page.goto(str(website.make_url("/")))
                await worker.evaluate("""async url=>{const tab=(await chrome.tabs.query({})).find(tab=>tab.url===url);
                  await chrome.scripting.insertCSS({target:{tabId:tab.id},files:['capture.css']});
                  await chrome.scripting.executeScript({target:{tabId:tab.id},files:['content.js']});}""", page.url)
                before = list(hits)
                await select_capture_target(page, page.locator("#guide"))
                assert hits == before and page.url == str(website.make_url("/")), "Capture selection must not activate its target"
                await page.get_by_role("button", name="Block target", exact=True).click()
                async with page.expect_download() as downloaded:
                    await page.get_by_role("button", name="Download", exact=True).click()
                export = tmp_path / "capture.json"
                await (await downloaded.value).save_as(export)
                captured = json.loads(export.read_text())
                fingerprint = captured["targets"][0]["fingerprint"]
                assert fingerprint["text"] == fingerprint["labels"]["accessible"] == "Open guide"
                assert fingerprint["previous_control_text"] == "Quickstart"
                original_scope = captured["scope"]
                # A later SPA route change cannot rewrite an already captured
                # policy's source scope to its destination page.
                await page.evaluate("history.pushState(null, '', '/different-page')")
                async with page.expect_download() as again:
                    await page.get_by_role("button", name="Download", exact=True).click()
                after_route = json.loads(Path(await (await again.value).path()).read_text())
                assert after_route["id"] == captured["id"] and after_route["scope"] == original_scope
            finally:
                await context.close()
        common = ["--project", str(project.root)]
        run(["policy", "import", str(export), *common])
        run(["policy", "import", str(export), *common])
        assert len(project.policies("default")) == 1 and project.policies("unrelated") == []
        class Reviewer:
            decision = "block"
            requests = 0
            async def ask(self, request):
                self.requests += 1
                return self.decision
        reviewer = Reviewer()
        async with protect(project=project.root, dashboard=False, review_backend=reviewer) as guard, async_playwright() as p:
            guard.patch_playwright(p)
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.goto(str(website.make_url("/")))
                before = list(hits)
                with pytest.raises(AciraActionBlocked):
                    await page.locator("#guide").click()
                assert hits == before and reviewer.requests == 0
                await page.locator("#about").click()
                assert page.url == str(website.make_url("/about")), "A specific target must not block an unrelated link"
                run(["policy", "configure", captured["id"], "--decision", "ask", *common])
                await page.goto(str(website.make_url("/")))
                before = list(hits)
                with pytest.raises(AciraActionBlocked):
                    await page.locator("#guide").click()
                assert hits == before and reviewer.requests == 1
                reviewer.decision = "allow"
                await page.locator("#guide").click()
                assert hits.count("/guide") == 1 and reviewer.requests == 2
                # A second fake origin contains the same target but is outside this policy's scope.
                await page.context.route("https://outside.example.test/**", lambda route: route.fulfill(content_type="text/html", body='<a id="guide" href="/guide">Open guide</a>'))
                await page.goto("https://outside.example.test/")
                await page.locator("#guide").click()
                assert page.url == "https://outside.example.test/guide" and reviewer.requests == 2
            finally:
                await browser.close()
        service = Service()
        async with TestServer(service.app(), host="127.0.0.1") as server:
            service.port = server.port
            async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
                ticket = (await (await client.post(server.make_url('/api/ticket'), json={})).json())["ticket"]
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                try:
                    ui = await browser.new_page()
                    await ui.goto(str(server.make_url('/'))+'#ticket='+ticket)
                    await ui.get_by_role('button', name='Policies', exact=True).click()
                    await ui.get_by_role('button', name='Configure', exact=True).click()
                    decision = ui.get_by_label('Decision for Open guide', exact=True)
                    await expect(decision).to_have_value('ask')
                    assert await decision.locator('option').all_text_contents() == ['Ask for human review', 'Block action']
                    await decision.select_option('block')
                    await ui.get_by_role('button', name='Save policy', exact=True).click()
                    await expect(ui.get_by_role('dialog')).not_to_be_visible()
                    assert project.policies('default')[0]['document']['targets'][0]['decision'] == 'block'
                finally:
                    await browser.close()
        reviews = Journal(project.root).reviews()
        assert len(reviews) == 2 and {review['state'] for review in reviews} == {'approved','rejected'}
