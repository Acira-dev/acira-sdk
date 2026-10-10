"""Fresh operation reviews support legitimate repeated work on one browser page."""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright
from acira import protect
from acira.journal import Journal
from acira.project import initialize
from acira.local import atomic_json
from acira_cdp_proxy.action_gateway import AciraActionBlocked


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["playwright", "browser_use"])
async def test_each_repeated_submission_requires_new_review_and_rejection_has_no_effect(tmp_path, monkeypatch, adapter):
    if adapter == "browser_use":
        pytest.importorskip("browser_use")
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("BROWSER_USE_CONFIG_DIR", str(tmp_path / "browser-use"))
    for name in ("ANONYMIZED_TELEMETRY", "BROWSER_USE_CLOUD_SYNC", "BROWSER_USE_VERSION_CHECK"):
        monkeypatch.setenv(name, "false")
    hits = []
    async def index(request):
        return web.Response(text="""<button id="pay" onclick="fetch('/payment',{method:'POST',
        headers:{'content-type':'application/json'},body:JSON.stringify({amount:'10'})}).catch(()=>{})">Pay</button>""", content_type="text/html")
    async def payment(request):
        hits.append(await request.json())
        return web.json_response({"received": True})
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/payment", payment)
    project = initialize(tmp_path / "project", adapter=adapter, selected=[], dashboard=False)
    document = project.document()
    document["agents"]["default"]["guard"] = {"terminal_log": False}
    atomic_json(project.path, document)
    answers = iter(["allow", "block", "allow"])
    reviews = []
    class Reviewer:
        async def ask(self, request):
            reviews.append(request)
            return next(answers)
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as p:
        async with protect(project=project.root, dashboard=False, review_backend=Reviewer()) as guard:
            if adapter == "playwright":
                guard.patch_playwright(p)
                browser = await p.chromium.launch()
                page = await browser.new_page()
                await page.goto(str(server.make_url("/")))
                args = (page.locator("#pay"),)
            else:
                from browser_use import BrowserSession
                from browser_use.browser.events import NavigateToUrlEvent
                browser = BrowserSession(headless=True, executable_path=p.chromium.executable_path,
                    user_data_dir=tmp_path / "profile", downloads_path=tmp_path / "downloads",
                    enable_default_extensions=False, permissions=[], keep_alive=False)
                await asyncio.wait_for(browser.start(), 30)
                navigation = browser.event_bus.dispatch(NavigateToUrlEvent(url=str(server.make_url("/"))))
                await asyncio.wait_for(navigation.event_result(raise_if_any=True, raise_if_none=False), 30)
                summary = await browser.get_browser_state_summary(include_screenshot=False)
                node = next(n for n in summary.dom_state.selector_map.values() if n.attributes.get("id") == "pay")
                args = (browser, node)
            contract = {"destination": str(server.make_url("/payment")), "values": {"amount": "10"}}
            try:
                await guard.submit(*args, **contract)
                assert hits == [{"amount": "10"}]
                with pytest.raises(AciraActionBlocked):
                    await guard.submit(*args, **contract)
                assert hits == [{"amount": "10"}]
                await guard.submit(*args, **contract)
                assert hits == [{"amount": "10"}, {"amount": "10"}]
                assert len(reviews) == 3
                records = Journal(project.root).reviews()
                assert len({row["review_id"] for row in records}) == 3
                assert sorted(row["state"] for row in records) == ["approved", "approved", "rejected"]
            finally:
                await asyncio.wait_for(browser.close() if adapter == "playwright" else browser.kill(), 10)
    assert hits == [{"amount": "10"}, {"amount": "10"}]
    assert Journal(project.root).runs()[0]["state"] == "ended"
