"""Capture identity must survive noisy navigation without accepting drift."""
import copy
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from acira import protect
from acira.cli import run
from acira.extension import install_extension
from acira.project import initialize
from acira_cdp_proxy.action_gateway import AciraActionBlocked
from acira_cdp_proxy.target_mapping import unchanged_capture_identity


def identity_pair():
    fingerprint = {"tag": "a", "href": "/guide", "text": "Documentation",
                   "aria_label": "API documentation", "dom_path": "main>nav>a:nth-of-type(1)",
                   "stable_attributes": {"href": "/guide", "aria-label": "API documentation"},
                   "structure_signature": {"element_state": {"hidden": False, "disabled": False}}}
    target = copy.deepcopy(fingerprint)
    target["attributes"] = target.pop("stable_attributes")
    target["text"] = "API documentation"
    return fingerprint, target


def test_unchanged_identity_uses_accessible_label_and_stable_anchor():
    assert unchanged_capture_identity(*identity_pair())


def test_document_root_spelling_is_equivalent_but_other_ordinals_are_not():
    original, target = identity_pair()
    original["dom_path"] = "html>body:nth-of-type(1)>a:nth-of-type(1)"
    target["dom_path"] = "html:nth-of-type(1)>body:nth-of-type(1)>a:nth-of-type(1)"
    assert unchanged_capture_identity(original, target)
    target["dom_path"] = "html:nth-of-type(1)>body:nth-of-type(1)>a:nth-of-type(2)"
    assert not unchanged_capture_identity(original, target)


def test_identical_dom_path_in_another_frame_is_not_an_unchanged_page_target():
    original, target = identity_pair()
    target["url"] = "https://fixture.invalid/frame"
    assert not unchanged_capture_identity(original, target, page_url="https://fixture.invalid/")
    target["url"] = "https://fixture.invalid/"
    assert unchanged_capture_identity(original, target, page_url="https://fixture.invalid/")


@pytest.mark.parametrize("change", ["path", "destination", "label", "kind", "state", "form", "anchor"])
def test_changed_or_unanchored_identity_never_shortcuts_mapping_review(change):
    original, target = identity_pair()
    if change == "path":
        target["dom_path"] = "main>nav>a:nth-of-type(2)"
    elif change == "destination":
        target["href"] = target["attributes"]["href"] = "/different-recipient"
    elif change == "label":
        target["text"] = target["aria_label"] = "Another operation"
        target["attributes"]["aria-label"] = "Another operation"
    elif change == "kind":
        target["tag"] = "button"
    elif change == "state":
        target["structure_signature"]["element_state"]["disabled"] = True
    elif change == "form":
        original["form"] = {"id": "original-account"}
        target["form_id"] = "another-account"
    else:
        for value in (original, target):
            value.pop("href")
        original["stable_attributes"].pop("href")
        target["attributes"].pop("href")
    assert not unchanged_capture_identity(original, target)


@pytest.mark.asyncio
async def test_real_capture_terminal_import_and_review_with_noisy_navigation(tmp_path, monkeypatch, select_capture_target):
    api = pytest.importorskip("playwright.async_api")
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    hits = []
    html = '''<title>Navigation fixture</title><main><h1>Documentation</h1><nav>
      <button aria-label="Toggle navigation menu">Documentation</button>
      <a href="/guide" aria-label="API documentation" class="navigation link">DOCUMENTATION</a>
      <a href="/other" class="navigation link">Other documentation</a>
    </nav><footer><a href="/guide" aria-label="API documentation" class="navigation link">DOCUMENTATION</a></footer></main>'''
    async def respond(request):
        hits.append(request.path)
        return web.Response(text=html if request.path == "/" else "<h1>Public guide</h1>", content_type="text/html")
    app = web.Application()
    app.router.add_get("/{path:.*}", respond)
    extension = install_extension(tmp_path / "extension")
    manifest = json.loads((extension / "manifest.json").read_text())
    manifest["host_permissions"] = ["http://127.0.0.1/*"]
    (extension / "manifest.json").write_text(json.dumps(manifest))
    async with TestServer(app, host="127.0.0.1") as server:
        url = str(server.make_url("/"))
        async with api.async_playwright() as p:
            context = await p.chromium.launch_persistent_context(tmp_path / "capture", channel="chromium", headless=True,
                args=["--disable-extensions-except=" + str(extension), "--load-extension=" + str(extension)])
            try:
                worker = context.service_workers[0] if context.service_workers else await context.wait_for_event("serviceworker")
                page = await context.new_page()
                await page.goto(url)
                await worker.evaluate('''async url => {const tab=(await chrome.tabs.query({})).find(t=>t.url===url);
                  await chrome.scripting.insertCSS({target:{tabId:tab.id},files:['capture.css']});
                  await chrome.scripting.executeScript({target:{tabId:tab.id},files:['content.js']});}''', url)
                before = list(hits)
                await select_capture_target(page, page.locator("nav a[href='/guide']"))
                panel = page.locator("#acira-capture-panel")
                await panel.get_by_role("button", name="Block target", exact=True).click()
                assert hits == before and page.url == url
                async with page.expect_download() as download:
                    await panel.get_by_role("button", name="Download", exact=True).click()
                exported = tmp_path / "capture.json"
                await (await download.value).save_as(exported)
                policy = json.loads(exported.read_text())
            finally:
                await context.close()
        project = initialize(tmp_path / "project", selected=[], dashboard=False)
        common = ["--project", str(project.root)]
        run(["policy", "import", str(exported), *common])
        run(["policy", "import", str(exported), *common])
        assert len(project.policies("default")) == 1
        class Reviewer:
            decision = "block"
            actions = []
            async def ask(self, request):
                self.actions.append(request.classification.action)
                return self.decision
        reviewer = Reviewer()
        async with protect(project=project.root, dashboard=False, review_backend=reviewer) as guard, api.async_playwright() as p:
            guard.patch_playwright(p)
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.goto(url)
                before = list(hits)
                with pytest.raises(AciraActionBlocked):
                    await page.locator("nav a[href='/guide']").click()
                assert hits == before and reviewer.actions == []
                run(["policy", "configure", policy["id"], "--decision", "ask", *common])
                with pytest.raises(AciraActionBlocked):
                    await page.locator("nav a[href='/guide']").click()
                assert hits == before and reviewer.actions == ["click"]
                reviewer.decision = "allow"
                await page.locator("nav a[href='/guide']").click()
                assert hits.count("/guide") == 1 and reviewer.actions == ["click", "click"]
            finally:
                await browser.close()
