"""Critical approval must never follow an unreviewed HTTP redirect."""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright
from acira_cdp_proxy.action_gateway import AciraActionBlocked


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["playwright", "browser_use"])
@pytest.mark.parametrize("status,delay,audit_failure",
    [(status, delay, False) for delay in [0, .4] for status in [301, 302, 303, 307, 308]] + [(200, 0, True)],
    ids=[f"{'after-timeout' if delay else 'immediate'}-{status}" for delay in [0, .4] for status in [301, 302, 303, 307, 308]] + ["audit-full"])
async def test_approved_submission_never_follows_redirect(tmp_path, monkeypatch, adapter, status, delay, audit_failure):
    reviewed, unreviewed = [], []
    async def index(request):
        return web.Response(text="""<button id="pay" onclick="fetch('/payment',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({amount:'10'})}).catch(()=>{})">Pay</button>""", content_type="text/html")
    async def payment(request):
        reviewed.append(await request.json())
        await asyncio.sleep(delay)
        return web.Response(status=status, headers={"Location": "/unreviewed"})
    async def redirect_target(request):
        unreviewed.append({"method": request.method, "body": await request.text()})
        return web.Response(text="unreviewed destination reached")
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/payment", payment)
    app.router.add_route("*", "/unreviewed", redirect_target)
    class Reviewer:
        async def ask(self, request):
            return "allow"
    if adapter == "browser_use":
        pytest.importorskip("browser_use")
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("BROWSER_USE_CONFIG_DIR", str(tmp_path / "browser-use"))
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    monkeypatch.setenv("BROWSER_USE_CLOUD_SYNC", "false")
    from acira import protect
    from acira.project import initialize
    from acira.local import atomic_json
    project = initialize(tmp_path / "project", adapter=adapter, selected=[], dashboard=False)
    document = project.document()
    document["agents"]["default"]["guard"] = {"terminal_log": False}
    atomic_json(project.path, document)
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as p:
        async with protect(project=project.root, dashboard=False, review_backend=Reviewer()) as guard:
            if audit_failure:
                store = guard.action_gateway.checkpoint_store
                original_event = store.event
                def failed_dispatch_event(event_type, **fields):
                    if event_type == "submission_dispatch_attempt":
                        raise OSError("The required audit disk is full")
                    return original_event(event_type, **fields)
                monkeypatch.setattr(store, "event", failed_dispatch_event)
            if adapter == "playwright":
                guard.patch_playwright(p)
                browser = await p.chromium.launch()
                page = await browser.new_page()
                await page.goto(str(server.make_url("/")))
                target = page.locator("#pay")
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
                target = next(node for node in summary.dom_state.selector_map.values() if node.attributes.get("id") == "pay")
            try:
                error = None
                try:
                    args = (target,) if adapter == "playwright" else (browser, target)
                    await guard.submit(*args, destination=str(server.make_url("/payment")), values={"amount": "10"}, timeout=.1 if delay else 5)
                except AciraActionBlocked as failure:
                    error = failure
                await asyncio.sleep(delay + .1)
                assert reviewed == ([] if audit_failure else [{"amount": "10"}])
                assert unreviewed == [], "The browser followed a destination that was never reviewed"
                assert error is not None, "The redirected operation must not be reported as confirmed"
            finally:
                await asyncio.wait_for(browser.close() if adapter == "playwright" else browser.kill(), 10)


@pytest.mark.asyncio
async def test_late_browser_use_redirect_does_not_cancel_new_operation(tmp_path, monkeypatch):
    pytest.importorskip("browser_use")
    from browser_use import BrowserSession
    from browser_use.browser.events import NavigateToUrlEvent
    from acira import protect
    from acira.project import initialize
    from acira.local import atomic_json
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("BROWSER_USE_CONFIG_DIR", str(tmp_path / "browser-use"))
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    monkeypatch.setenv("BROWSER_USE_CLOUD_SYNC", "false")
    hits, escaped = [], []
    async def index(request):
        return web.Response(text="""<button id="first" onclick="fetch('/first',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({amount:'10'})}).catch(()=>{})">First</button><button id="second" onclick="fetch('/second',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({amount:'20'})}).catch(()=>{})">Second</button>""", content_type="text/html")
    async def original(request):
        hits.append((request.path, await request.json()))
        await asyncio.sleep(.4 if request.path == "/first" else .8)
        return web.Response(status=307, headers={"Location": "/unreviewed"}) if request.path == "/first" else web.json_response({"received": True})
    async def unreviewed(request):
        escaped.append(request.path)
        return web.Response(text="unexpected")
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/first", original)
    app.router.add_post("/second", original)
    app.router.add_route("*", "/unreviewed", unreviewed)
    project = initialize(tmp_path / "project", adapter="browser_use", selected=[], dashboard=False)
    document = project.document()
    document["agents"]["default"]["guard"] = {"terminal_log": False}
    atomic_json(project.path, document)
    class Reviewer:
        async def ask(self, request):
            return "allow"
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as p:
        async with protect(project=project.root, dashboard=False, review_backend=Reviewer()) as guard:
            browser = BrowserSession(headless=True, executable_path=p.chromium.executable_path,
                user_data_dir=tmp_path / "profile", downloads_path=tmp_path / "downloads",
                enable_default_extensions=False, permissions=[], keep_alive=False)
            try:
                await asyncio.wait_for(browser.start(), 30)
                event = browser.event_bus.dispatch(NavigateToUrlEvent(url=str(server.make_url("/"))))
                await asyncio.wait_for(event.event_result(raise_if_any=True, raise_if_none=False), 30)
                summary = await browser.get_browser_state_summary(include_screenshot=False)
                nodes = {node.attributes.get("id"): node for node in summary.dom_state.selector_map.values()}
                with pytest.raises(AciraActionBlocked):
                    await guard.submit(browser, nodes["first"], destination=str(server.make_url("/first")), values={"amount": "10"}, timeout=.1)
                await guard.submit(browser, nodes["second"], destination=str(server.make_url("/second")), values={"amount": "20"}, timeout=3)
                assert hits == [("/first", {"amount": "10"}), ("/second", {"amount": "20"})]
                assert escaped == []
            finally:
                await asyncio.wait_for(browser.kill(), 10)
