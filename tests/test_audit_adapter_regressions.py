"""Actual localhost effects for handle, selection and submission boundaries."""
import asyncio
import contextlib
import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright

from acira import protect
from acira.adapters.playwright import PlaywrightCDPProxy
from acira.adapters.playwright_boundary import trusted
from acira.project import initialize
from acira_cdp_proxy.action_gateway import AciraActionBlocked


PAYLOAD = {"amount": "10"}
SEND = "fetch('/payment',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({amount:'10'})})"


@asynccontextmanager
async def site(mode="normal"):
    hits = []
    async def index(request):
        action = {
            "normal": SEND,
            "double": f"{SEND};{SEND}",
            "iframe": "document.querySelector('iframe').contentWindow.postMessage('send','*')",
            "other": "new BroadcastChannel('acira-local-fixture').postMessage('send')",
            "worker": "worker.postMessage('send')",
            "lateworker": "new Worker('/worker.js').postMessage('send')",
        }[mode]
        worker_script = "<script>const worker=new Worker('/worker.js');</script>" if mode == "worker" else ""
        return web.Response(text=f"""<title>Anonymous fixture</title>
            <button id="send" type="button" onclick="{action}">Send</button>
            <select id="choice" onchange="fetch('/effect',{{method:'POST',body:this.value}})">
            <option value="safe">Safe</option><option value="danger">Danger</option></select>
            <iframe src="/frame"></iframe>
            {worker_script}""", content_type="text/html")
    async def frame(request):
        return web.Response(text=f"""<button id="sendframe" onclick="{SEND}">Send frame</button>
            <script>addEventListener('message',e=>{{if(e.data==='send'){{{SEND}}}}})</script>""", content_type="text/html")
    async def competitor(request):
        return web.Response(text=f"<script>new BroadcastChannel('acira-local-fixture').onmessage=()=>{{{SEND}}}</script>", content_type="text/html")
    async def worker(request):
        return web.Response(text=f"onmessage=()=>{{{SEND}}}", content_type="application/javascript")
    async def payment(request):
        hits.append(await request.json())
        return web.Response(text="Observed one local request")
    async def effect(request):
        hits.append(await request.text())
        return web.Response(text="Observed local selection")
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/frame", frame)
    app.router.add_get("/competitor", competitor)
    app.router.add_get("/worker.js", worker)
    app.router.add_post("/payment", payment)
    app.router.add_post("/effect", effect)
    async with TestServer(app, host="127.0.0.1") as server:
        yield server, hits


async def wait_for_hit(hits):
    async with asyncio.timeout(5):
        while not hits:
            await asyncio.sleep(.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["page", "frame", "property", "properties"])
@pytest.mark.parametrize("method", ["evaluate", "evaluate_handle"])
async def test_public_handle_scripts_cannot_dispatch(source, method):
    async with site() as (server, hits), async_playwright() as pw:
        async with PlaywrightCDPProxy(config={"terminal_log": False}) as guard:
            guard.patch_playwright(pw)
            browser = await pw.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = await browser.new_page()
                await page.goto(str(server.make_url("/")))
                if source == "page":
                    handle = await page.evaluate_handle("document.title")
                elif source == "frame":
                    frame = page.frames[1]
                    handle = await frame.evaluate_handle("document.title")
                else:
                    element = await page.locator("#send").element_handle()
                    handle = await element.get_property("ownerDocument")
                    if source == "properties":
                        # An own property yields a further ordinary JSHandle.
                        await trusted(page, "evaluate", "document.fixture={value:1}")
                        handle = (await handle.get_properties())["fixture"]
                with pytest.raises(AciraActionBlocked):
                    await getattr(handle, method)(f"() => {SEND}")
                assert hits == []
                await handle.dispose()
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_handle_as_element_keeps_its_page_owner():
    async with site() as (server, hits), async_playwright() as pw:
        async with PlaywrightCDPProxy(config={"blocked_actions": ["click"], "terminal_log": False}) as guard:
            guard.patch_playwright(pw)
            browser = await pw.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = await browser.new_page()
                await page.goto(str(server.make_url("/")))
                control = await page.locator("#send").element_handle()
                document = await control.get_property("ownerDocument")
                handle = await document.get_property("body")
                element = handle.as_element()
                assert element is not None
                with pytest.raises(AciraActionBlocked):
                    await element.click()
                with pytest.raises(AciraActionBlocked):
                    await element.evaluate(f"() => {SEND}")
                assert hits == []
            finally:
                await browser.close()


@pytest.mark.asyncio
async def test_unregistered_page_handle_keeps_normal_behavior():
    async with site() as (server, hits), async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, chromium_sandbox=True)
        try:
            page = await browser.new_page()
            await page.goto(str(server.make_url("/")))
            async with PlaywrightCDPProxy(config={"terminal_log": False}) as guard:
                protected = await browser.new_page()
                await protected.goto(str(server.make_url("/")))
                guard.patch_page(protected)
                handle = await page.evaluate_handle("document.title")
                await handle.evaluate(f"() => {SEND}")
                await wait_for_hit(hits)
                assert hits == [PAYLOAD]
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", ["value", "label", "index", "element", "positional", "page-positional"])
async def test_requested_selection_is_frozen_before_review(selector):
    requests = []
    requested = []
    replacement = []
    class Reviewer:
        async def ask(self, request):
            requests.append(request)
            requested[:] = replacement
            return "allow"
    async with site() as (server, hits), async_playwright() as pw:
        async with PlaywrightCDPProxy(config={"review_actions": ["select"], "terminal_log": False}, review_backend=Reviewer()) as guard:
            guard.patch_playwright(pw)
            browser = await pw.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = await browser.new_page()
                await page.goto(str(server.make_url("/")))
                if selector == "element":
                    requested[:] = [await page.locator("option").nth(0).element_handle()]
                    replacement[:] = [await page.locator("option").nth(1).element_handle()]
                else:
                    requested[:] = [0 if selector == "index" else "Safe" if selector == "label" else "safe"]
                    replacement[:] = [1 if selector == "index" else "Danger" if selector == "label" else "danger"]
                if selector == "positional":
                    actual = await page.locator("#choice").select_option(requested)
                elif selector == "page-positional":
                    actual = await page.select_option("#choice", requested)
                else:
                    actual = await page.locator("#choice").select_option(**{selector: requested})
                await wait_for_hit(hits)
                assert actual == ["safe"] and hits == ["safe"]
                assert requested == replacement
                assert len(requests) == 1
                selection = requests[0].classification.details["operation_evidence"]["requested_selection"]
                assert selection == ({"byElement": [{"index": 0, "optionValue": "safe", "optionLabel": "Safe"}]} if selector == "element" else {"byValue" if selector in {"positional", "page-positional"} else "by" + selector.title(): [0 if selector == "index" else "Safe" if selector == "label" else "safe"]})
            finally:
                await browser.close()


@asynccontextmanager
async def submission_browser(tmp_path, monkeypatch, adapter, server):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", adapter=adapter, selected=["submissions"], dashboard=False)
    class Reviewer:
        async def ask(self, request):
            return "allow"
    async with async_playwright() as pw, protect(project=project.root, dashboard=False, review_backend=Reviewer()) as guard:
        if adapter == "playwright":
            guard.patch_playwright(pw)
            browser = await pw.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(str(server.make_url("/")))
                yield guard, browser, page
            finally:
                await browser.close()
        else:
            from browser_use import BrowserSession
            from browser_use.browser.events import NavigateToUrlEvent
            browser = BrowserSession(args=["--password-store=basic", "--use-mock-keychain"], headless=True,
                chromium_sandbox=True, executable_path=pw.chromium.executable_path,
                user_data_dir=tmp_path / "profile", downloads_path=tmp_path / "downloads",
                enable_default_extensions=False, permissions=[], keep_alive=False)
            try:
                await asyncio.wait_for(browser.start(), 30)
                event = browser.event_bus.dispatch(NavigateToUrlEvent(url=str(server.make_url("/"))))
                await asyncio.wait_for(event.event_result(raise_if_any=True, raise_if_none=False), 30)
                yield guard, browser, None
            finally:
                await asyncio.wait_for(browser.kill(), 30)


async def control(adapter, browser, page, identifier="send"):
    if adapter == "playwright":
        return (page.locator("#" + identifier) if identifier == "send" else page.frame_locator("iframe").locator("#" + identifier),)
    summary = await asyncio.wait_for(browser.get_browser_state_summary(include_screenshot=False), 30)
    node = next(node for node in summary.dom_state.selector_map.values() if node.attributes.get("id") == identifier)
    return browser, node


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["playwright", "browser_use"])
async def test_concurrent_matching_requests_use_only_one_attempt(tmp_path, monkeypatch, adapter):
    async with site("double") as (server, hits), submission_browser(tmp_path, monkeypatch, adapter, server) as (guard, browser, page):
        args = await control(adapter, browser, page)
        original = guard.guard_policy.before_decision
        ready, release = asyncio.Event(), asyncio.Event()
        waiting = 0
        async def before_decision():
            nonlocal waiting
            active = bool(guard._submission_routes) if adapter == "playwright" else any(gate.active for gate in guard._network_gates.values())
            if active:
                waiting += 1
                if waiting == 2:
                    ready.set()
                await release.wait()
            if original:
                await original()
        guard.guard_policy.before_decision = before_decision
        operation = asyncio.create_task(guard.submit(*args, destination=str(server.make_url("/payment")), values=PAYLOAD, timeout=10))
        try:
            await asyncio.wait_for(ready.wait(), 8)
            assert hits == []
            release.set()
            with contextlib.suppress(AciraActionBlocked):
                await asyncio.wait_for(operation, 15)
            await wait_for_hit(hits)
            assert hits == [PAYLOAD]
        finally:
            release.set()
            if not operation.done():
                operation.cancel()
            with contextlib.suppress(asyncio.CancelledError, AciraActionBlocked):
                await operation
            guard.guard_policy.before_decision = original


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter,initiator", [("playwright", "other"), ("playwright", "iframe"), ("playwright", "worker"), ("playwright", "lateworker"), ("browser_use", "iframe")])
async def test_matching_competing_initiator_cannot_consume_approval(tmp_path, monkeypatch, adapter, initiator):
    async with site(initiator) as (server, hits), submission_browser(tmp_path, monkeypatch, adapter, server) as (guard, browser, page):
        if initiator == "other":
            other = await page.context.new_page()
            await other.goto(str(server.make_url("/competitor")))
        args = await control(adapter, browser, page)
        with pytest.raises(AciraActionBlocked):
            await guard.submit(*args, destination=str(server.make_url("/payment")), values=PAYLOAD, timeout=3)
        assert hits == []


@pytest.mark.asyncio
async def test_closed_worker_history_still_refuses_ambiguous_context(tmp_path, monkeypatch):
    async with site("worker") as (server, hits), submission_browser(tmp_path, monkeypatch, "playwright", server) as (guard, browser, page):
        async with asyncio.timeout(5):
            while not page.workers:
                await asyncio.sleep(.01)
        await trusted(page, "evaluate", "worker.terminate()")
        async with asyncio.timeout(5):
            while page.workers:
                await asyncio.sleep(.01)
        with pytest.raises(AciraActionBlocked, match="worker-free"):
            await guard.submit(page.locator("#send"), destination=str(server.make_url("/payment")), values=PAYLOAD)
        assert hits == []


@pytest.mark.asyncio
@pytest.mark.parametrize("hint,sensitive", [(None, False), ("name", True), ("aria-label", True), ("title", True), ("autocomplete", True)],
                         ids=["False", "True", "aria-label-api-key", "credential-title", "cc-number-autocomplete"])
async def test_selection_evidence_masks_sensitive_fields_and_rechecks_options(hint, sensitive):
    requests = []
    class Reviewer:
        async def ask(self, request):
            requests.append(request)
            if not sensitive:
                await trusted(page, "evaluate", "document.querySelector('option').label='Changed during review'")
            return "allow"
    async with site() as (server, hits), async_playwright() as pw:
        async with PlaywrightCDPProxy(config={"review_actions": ["select"], "terminal_log": False}, review_backend=Reviewer()) as guard:
            guard.patch_playwright(pw)
            browser = await pw.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = await browser.new_page()
                await page.goto(str(server.make_url("/")))
                if sensitive:
                    label = {"name": "api_key", "aria-label": "API key", "title": "Credential choice", "autocomplete": "cc-number"}[hint]
                    await trusted(page, "evaluate", "([name,value])=>document.querySelector('#choice').setAttribute(name,value)", [hint, label])
                    await page.locator("#choice").select_option(value=["safe"])
                    await wait_for_hit(hits)
                    evidence = requests[0].classification.details["operation_evidence"]
                    assert evidence["requested_selection"] == "[REDACTED]"
                    assert evidence["critical"] is True and "grant_scope" not in evidence
                    assert hits == ["safe"]
                else:
                    with pytest.raises(AciraActionBlocked, match="changed before dispatch"):
                        await page.locator("#choice").select_option(value=["safe"])
                    assert hits == []
                assert len(requests) == 1
            finally:
                await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["playwright", "browser_use"])
async def test_reviewed_iframe_can_submit_from_its_exact_frame(tmp_path, monkeypatch, adapter):
    async with site() as (server, hits), submission_browser(tmp_path, monkeypatch, adapter, server) as (guard, browser, page):
        args = await control(adapter, browser, page, "sendframe")
        await guard.submit(*args, destination=str(server.make_url("/payment")), values=PAYLOAD, timeout=5)
        assert hits == [PAYLOAD]
