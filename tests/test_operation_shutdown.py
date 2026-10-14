"""A paused HTTP operation must not escape a timeout or guard teardown."""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright
from acira import protect
from acira.journal import Journal
from acira.local import atomic_json
from acira.project import initialize
from acira_cdp_proxy.action_gateway import AciraActionBlocked


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter,termination", [
    (adapter, termination) for adapter in ["playwright", "browser_use"]
    for termination in ["guard-exit", "authorization-timeout"]
] + [("browser_use", "transport-loss")])
async def test_paused_request_cannot_resume_after_operation_ends(tmp_path, monkeypatch, adapter, termination):
    if adapter == "browser_use":
        pytest.importorskip("browser_use")
    for key in ("ANONYMIZED_TELEMETRY", "BROWSER_USE_CLOUD_SYNC", "BROWSER_USE_VERSION_CHECK"):
        monkeypatch.setenv(key, "false")
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("BROWSER_USE_CONFIG_DIR", str(tmp_path / "browser-use"))
    hits = []
    arrived = asyncio.Event()
    async def index(request):
        return web.Response(text="""<!doctype html><html><body><button id="pay"
            onclick="fetch('/payment',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({amount:'10'})}).catch(()=>{})">Pay</button></body></html>""", content_type="text/html")
    async def payment(request):
        hits.append(await request.json())
        arrived.set()
        return web.json_response({"received": True})
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/payment", payment)
    project = initialize(tmp_path / "project", adapter=adapter, selected=[], dashboard=False)
    document = project.document()
    document["agents"]["default"]["guard"] = {"terminal_log": False}
    atomic_json(project.path, document)
    class Reviewer:
        async def ask(self, request):
            return "allow"
    ready, release = asyncio.Event(), asyncio.Event()
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as p:
        context = protect(project=project.root, dashboard=False, review_backend=Reviewer())
        guard = await context.__aenter__()
        browser, task, closed = None, None, False
        try:
            if adapter == "playwright":
                guard.patch_playwright(p)
                browser = await p.chromium.launch()
                page = await browser.new_page()
                await page.goto(str(server.make_url("/")))
                args = (page.locator("#pay"),)
            else:
                from browser_use import BrowserSession
                from browser_use.browser.events import NavigateToUrlEvent
                browser = BrowserSession(args=["--password-store=basic", "--use-mock-keychain"], headless=True, executable_path=p.chromium.executable_path,
                    user_data_dir=tmp_path / "profile", downloads_path=tmp_path / "downloads",
                    enable_default_extensions=False, permissions=[], keep_alive=False)
                await asyncio.wait_for(browser.start(), 30)
                event = browser.event_bus.dispatch(NavigateToUrlEvent(url=str(server.make_url("/"))))
                await asyncio.wait_for(event.event_result(raise_if_any=True, raise_if_none=False), 30)
                summary = await asyncio.wait_for(browser.get_browser_state_summary(include_screenshot=False), 30)
                node = next(n for n in summary.dom_state.selector_map.values() if n.attributes.get("id") == "pay")
                args = (browser, node)
            original = guard.guard_policy.before_decision
            async def held_boundary():
                installed = bool(guard._submission_routes) if adapter == "playwright" else any(g.active for g in guard._network_gates.values())
                if installed:
                    ready.set()
                    await release.wait()
                await original()
            guard.guard_policy.before_decision = held_boundary
            task = asyncio.create_task(guard.submit(*args, destination=str(server.make_url("/payment")),
                values={"amount": "10"}, timeout=.1 if termination == "authorization-timeout" else 5))
            await asyncio.wait_for(ready.wait(), 30)
            assert hits == []
            if termination == "guard-exit":
                async with asyncio.timeout(8):
                    await context.__aexit__(None, None, None)
                closed = True
            elif termination == "authorization-timeout":
                with pytest.raises(AciraActionBlocked):
                    await asyncio.wait_for(asyncio.shield(task), 3)
            else:
                channel = next(iter(guard._operation_channels.values()))
                await channel.socket.close()
                with pytest.raises(AciraActionBlocked):
                    await asyncio.wait_for(asyncio.shield(task), 3)
                runtime = guard.runtime_control
                assert runtime.state == "failed"
                runtime.refresh()
                assert runtime.state == "failed" and runtime.error
                with pytest.raises(RuntimeError, match="connection ended unexpectedly"):
                    await runtime.before_decision()
                # Chromium can release the existing request on abrupt detach.
                # Do not turn an unknown outcome into a false zero-effect claim.
                try:
                    await asyncio.wait_for(arrived.wait(), 2)
                except TimeoutError:
                    pass
                previous = list(hits)
                from browser_use.browser.events import ClickElementEvent
                clicked = browser.event_bus.dispatch(ClickElementEvent(node=node))
                with pytest.raises(Exception) as failure:
                    await asyncio.wait_for(clicked.event_result(raise_if_any=True, raise_if_none=False), 3)
                assert not isinstance(failure.value, TimeoutError)
                assert hits == previous and len(hits) <= 1
            release.set()
            if adapter == "playwright":
                callbacks = {t for _, handler in guard._submission_routes for t in handler.__self__.tasks}
            else:
                callbacks = {t for channel in guard._operation_channels.values() for t in channel.tasks}
            if callbacks:
                _, pending = await asyncio.wait(callbacks, timeout=3)
                assert not pending, "The paused request's callback did not finish"
            with pytest.raises(AciraActionBlocked):
                await asyncio.wait_for(task, 3)
            if termination != "transport-loss":
                assert hits == [], "Chromium sent a request after its authorization ended"
            events = Journal(project.root).events(limit=1000)
            assert not any(e["type"] == "submission_dispatch_attempt" for e in events)
            assert any(e["type"] == "action_outcome_unknown" for e in events)
            if termination == "transport-loss":
                assert any(e["type"] == "integration_failed" for e in events)
        finally:
            release.set()
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
            if not closed:
                async with asyncio.timeout(8):
                    await context.__aexit__(None, None, None)
            if browser is not None:
                await asyncio.wait_for(browser.close() if adapter == "playwright" else browser.kill(), 10)
    assert Journal(project.root).runs()[0]["state"] == "ended"
