"""Optional real browser-use event integration, without an LLM or cloud account."""
import asyncio
import os

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["block", "allow"])
async def test_browser_use_click_rejection_has_no_browser_effect(tmp_path, monkeypatch, decision):
    pytest.importorskip("browser_use")
    pytest.importorskip("playwright")
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    monkeypatch.setenv("BROWSER_USE_CLOUD_SYNC", "false")
    monkeypatch.setenv("BROWSER_USE_CONFIG_DIR", str(tmp_path / "browser-use"))
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    from playwright.async_api import async_playwright
    from browser_use import BrowserSession
    from browser_use.browser.events import NavigateToUrlEvent, ClickElementEvent, TypeTextEvent
    from acira import protect
    from acira.local import atomic_json
    from acira.project import initialize
    hits = []
    async def index(request):
        return web.Response(text='<html><body><input id="notes" name="notes"><button id="act" onclick="fetch(\'/effect\',{method:\'POST\',body:document.querySelector(\'#notes\').value})">Change record</button></body></html>', content_type="text/html")
    async def effect(request):
        hits.append(await request.text())
        return web.Response(text="changed")
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/effect", effect)
    project = initialize(tmp_path / "project", adapter="browser_use", selected=[], dashboard=False)
    document = project.document()
    document["agents"]["default"]["guard"] = {
        "blocked_actions": ["click"] if decision == "block" else [],
        "review_actions": ["click", "form_input"] if decision == "allow" else [],
        "terminal_log": False}
    atomic_json(project.path, document)
    class Reviewer:
        count = 0
        async def ask(self, request):
            self.count += 1
            return decision
    reviewer = Reviewer()
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as p:
        async with protect(project=project.root, dashboard=False, review_backend=reviewer):
            session = BrowserSession(args=["--password-store=basic", "--use-mock-keychain"], headless=True, executable_path=p.chromium.executable_path,
                user_data_dir=tmp_path / "profile", downloads_path=tmp_path / "downloads",
                enable_default_extensions=False, permissions=[], keep_alive=False)
            try:
                await asyncio.wait_for(session.start(), 30)
                navigation = session.event_bus.dispatch(NavigateToUrlEvent(url=str(server.make_url("/"))))
                await asyncio.wait_for(navigation.event_result(raise_if_any=True, raise_if_none=False), 30)
                summary = await asyncio.wait_for(session.get_browser_state_summary(include_screenshot=False), 30)
                if decision == "allow":
                    field = next(node for node in summary.dom_state.selector_map.values() if node.attributes.get("id") == "notes")
                    typed = session.event_bus.dispatch(TypeTextEvent(node=field, text="reviewed note"))
                    await asyncio.wait_for(typed.event_result(raise_if_any=True, raise_if_none=False), 15)
                    summary = await asyncio.wait_for(session.get_browser_state_summary(include_screenshot=False), 30)
                node = next(node for node in summary.dom_state.selector_map.values() if node.attributes.get("id") == "act")
                event = session.event_bus.dispatch(ClickElementEvent(node=node))
                result = await asyncio.wait_for(event.event_result(raise_if_any=True), 10)
                if decision == "block":
                    assert "validation_error" in result and result["acira"]["decision"] == "block"
                await asyncio.sleep(.1)
                assert hits == ([] if decision == "block" else ["reviewed note"])
                assert reviewer.count == (0 if decision == "block" else 2)
            finally:
                await asyncio.wait_for(session.kill(), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["allow", "block", "changed-upload"])
async def test_browser_use_select_upload_frames_and_tab_switches(tmp_path, monkeypatch, decision):
    pytest.importorskip("browser_use")
    pytest.importorskip("playwright")
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    monkeypatch.setenv("BROWSER_USE_CLOUD_SYNC", "false")
    monkeypatch.setenv("BROWSER_USE_CONFIG_DIR", str(tmp_path / "browser-use"))
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    from playwright.async_api import async_playwright
    from browser_use import BrowserSession
    from browser_use.browser.events import (NavigateToUrlEvent, ClickElementEvent,
        SelectDropdownOptionEvent, UploadFileEvent, SwitchTabEvent)
    from acira import protect
    from acira.local import atomic_json
    from acira.project import initialize

    hits = []
    async def index(request):
        return web.Response(text="""<html><body>
            <select id="choice" onchange="fetch('/effect',{method:'POST',body:'select:'+this.value})">
              <option value="one">One</option><option value="two">Two</option>
            </select>
            <input type="file" id="attachment" onchange="this.files[0].text().then(text=>fetch('/effect',{method:'POST',body:'upload:'+text}))">
            <button id="main" onclick="fetch('/effect',{method:'POST',body:'main'})">Main action</button>
            <iframe src="/frame" width="600" height="200"></iframe>
            </body></html>""", content_type="text/html")
    async def frame(request):
        return web.Response(text="""<html><body><button id="framed" onclick="fetch('/effect',{method:'POST',body:'frame'})">Frame action</button></body></html>""", content_type="text/html")
    async def effect(request):
        hits.append(await request.text())
        return web.Response(text="observed")
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/frame", frame)
    app.router.add_post("/effect", effect)
    project = initialize(tmp_path / "project", adapter="browser_use", selected=[], dashboard=False)
    document = project.document()
    document["agents"]["default"]["guard"] = {"review_actions": ["click", "select", "file_upload"], "terminal_log": False}
    atomic_json(project.path, document)
    upload = tmp_path / "reviewed.txt"
    upload.write_text("original reviewed bytes")
    class Reviewer:
        async def ask(self, request):
            if decision == "changed-upload" and request.classification.action == "file_upload":
                upload.write_text("changed after review")
            return "block" if decision == "block" else "allow"
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as p:
        async with protect(project=project.root, dashboard=False, review_backend=Reviewer()) as guard:
            session = BrowserSession(args=["--password-store=basic", "--use-mock-keychain"], headless=True, executable_path=p.chromium.executable_path,
                user_data_dir=tmp_path / "profile", downloads_path=tmp_path / "downloads",
                enable_default_extensions=False, permissions=[], keep_alive=False)
            async def dispatch(event):
                sent = session.event_bus.dispatch(event)
                return await asyncio.wait_for(sent.event_result(raise_if_any=True, raise_if_none=False), 20)
            async def node(identifier):
                summary = await asyncio.wait_for(session.get_browser_state_summary(include_screenshot=False), 30)
                return next(value for value in summary.dom_state.selector_map.values() if value.attributes.get("id") == identifier)
            try:
                await asyncio.wait_for(session.start(), 30)
                await dispatch(NavigateToUrlEvent(url=str(server.make_url("/"))))
                summary = await session.get_browser_state_summary(include_screenshot=False)
                first_tab = next(tab.target_id for tab in summary.tabs if tab.url == str(server.make_url("/")))
                await dispatch(SelectDropdownOptionEvent(node=await node("choice"), text="Two"))
                event = UploadFileEvent(node=await node("attachment"), file_path=str(upload))
                if decision == "changed-upload":
                    with pytest.raises(Exception, match="changed|invalid|blocked|Blocked"):
                        await dispatch(event)
                    assert hits == ["select:two"]
                    state = await guard._send_browser_use_cdp(session, "Runtime.evaluate", {
                        "expression": "document.getElementById('attachment').files.length", "returnByValue": True})
                    assert state["result"]["value"] == 0
                    return
                if decision == "block":
                    with pytest.raises(Exception, match="blocked|Blocked|denied|rejected"):
                        await dispatch(event)
                else:
                    await dispatch(event)
                assert event.file_path == str(upload)
                state = await guard._send_browser_use_cdp(session, "Runtime.evaluate", {
                    "expression": "document.getElementById('attachment').files.length", "returnByValue": True})
                assert state["result"]["value"] == (0 if decision == "block" else 1)
                if decision == "allow":
                    # The selected File must remain readable after the upload call.
                    state = await guard._send_browser_use_cdp(session, "Runtime.evaluate", {
                        "expression": "document.getElementById('attachment').files[0].text()",
                        "awaitPromise": True, "returnByValue": True})
                    assert state["result"]["value"] == "original reviewed bytes"
                # Project mode uses one exact DOM activation for frame elements,
                # avoiding the native pointer/JS retry chain.
                await dispatch(ClickElementEvent(node=await node("framed")))
                await dispatch(NavigateToUrlEvent(url=str(server.make_url("/")), new_tab=True))
                await dispatch(ClickElementEvent(node=await node("main")))
                await dispatch(SwitchTabEvent(target_id=first_tab))
                await dispatch(ClickElementEvent(node=await node("main")))
                for _ in range(20):
                    if decision == "block" or len(hits) == 5:
                        break
                    await asyncio.sleep(.05)
                assert hits == ([] if decision == "block" else
                    ["select:two", "upload:original reviewed bytes", "frame", "main", "main"])
            finally:
                await asyncio.wait_for(session.kill(), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["allow", "block", "payload", "destination", "changed-dom", "retry", "popup", "worker", "two-tabs", "background", "offscreen"])
async def test_browser_use_exact_request_contract(tmp_path, monkeypatch, case):
    pytest.importorskip("browser_use")
    from browser_use import BrowserSession
    from browser_use.browser.events import NavigateToUrlEvent
    from playwright.async_api import async_playwright
    from acira import protect
    from acira.project import initialize
    from acira.local import atomic_json
    from acira_cdp_proxy.action_gateway import AciraActionBlocked
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    monkeypatch.setenv("BROWSER_USE_CLOUD_SYNC", "false")
    hits = []
    amount = "900" if case == "payload" else "10"
    endpoint = "/other" if case == "destination" else "/payment"
    async def index(request):
        if case in {"popup", "worker"}:
            code = "window.open('/popup')" if case == "popup" else "new Worker('/worker.js')"
            return web.Response(text='<button type="button" id="pay" onclick="'+code+'">Pay</button>', content_type="text/html")
        submit = "fetch('"+endpoint+"',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({amount:'"+amount+"',currency:'USD',account:document.querySelector('#account').value})})"
        code = "fetch('/status').catch(()=>{}).finally(()=>" + submit + ")" if case == "background" else submit
        style = '<style>#pay{display:block;margin-top:1100px}</style>' if case == "offscreen" else ""
        return web.Response(text=style+'<form><input id="account" name="account" value="account-123"><button type="button" id="pay" onclick="'+code+'">Pay</button></form>', content_type="text/html")
    async def payment(request):
        hits.append((request.path, await request.json()))
        return web.json_response({"received": True})
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/payment", payment)
    app.router.add_post("/other", payment)
    async def popup(request):
        return web.Response(text="<script>fetch('/payment',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({amount:'10',currency:'USD',account:'account-123'})})</script>", content_type="text/html")
    async def worker(request):
        return web.Response(text="fetch('/payment',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({amount:'10',currency:'USD',account:'account-123'})})", content_type="application/javascript")
    app.router.add_get("/popup", popup)
    app.router.add_get("/worker.js", worker)
    project = initialize(tmp_path / "project", adapter="browser_use", selected=[], dashboard=False)
    document = project.document()
    document["agents"]["default"]["guard"] = {"terminal_log": False}
    atomic_json(project.path, document)
    requests = []
    session = None
    class Reviewer:
        async def ask(self, request):
            requests.append(request)
            if case == "offscreen":
                visible = await next(iter(guard._operation_channels.values())).send("Runtime.evaluate", {
                    "expression": "(()=>{const r=document.getElementById('pay').getBoundingClientRect();return r.y>=0&&r.bottom<=innerHeight})()",
                    "returnByValue": True})
                assert visible["result"]["value"] is True, "Review must show the actual target in its viewport"
            if case == "changed-dom":
                await next(iter(guard._operation_channels.values())).send("Runtime.evaluate", {"expression": "document.getElementById('account').value='new-account'"})
            return "block" if case == "block" else "allow"
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as p:
        async with protect(project=project.root, dashboard=False, review_backend=Reviewer()) as guard:
            session = BrowserSession(args=["--password-store=basic", "--use-mock-keychain"], headless=True, executable_path=p.chromium.executable_path,
                user_data_dir=tmp_path / "profile", downloads_path=tmp_path / "downloads",
                enable_default_extensions=False, permissions=[], keep_alive=False)
            try:
                await asyncio.wait_for(session.start(), 30)
                event = session.event_bus.dispatch(NavigateToUrlEvent(url=str(server.make_url("/"))))
                await asyncio.wait_for(event.event_result(raise_if_any=True, raise_if_none=False), 30)
                summary = await session.get_browser_state_summary(include_screenshot=False)
                node = next(value for value in summary.dom_state.selector_map.values() if value.attributes.get("id") == "pay")
                args = {"destination": str(server.make_url("/payment")), "values": {"amount": "10", "currency": "USD", "account": "account-123"},
                    "facts": {"amount": "amount", "currency": "currency", "account": "account"}}
                if case in {"allow", "retry", "two-tabs", "background", "offscreen"}:
                    await guard.submit(session, node, **args)
                    if case == "two-tabs":
                        event = session.event_bus.dispatch(NavigateToUrlEvent(url=str(server.make_url("/")), new_tab=True))
                        await asyncio.wait_for(event.event_result(raise_if_any=True, raise_if_none=False), 30)
                        summary = await session.get_browser_state_summary(include_screenshot=False)
                        second_node = next(value for value in summary.dom_state.selector_map.values() if value.attributes.get("id") == "pay")
                        await guard.submit(session, second_node, **args)
                    if case == "retry":
                        from acira.adapters.browser_use_operations import channel_for, Element
                        channel = await channel_for(guard, session, node)
                        element = await Element.resolve(channel, node)
                        await element.activate()
                        await element.close()
                        await asyncio.sleep(.1)
                else:
                    with pytest.raises(AciraActionBlocked):
                        await guard.submit(session, node, **args)
                assert len(hits) == (2 if case == "two-tabs" else 1 if case in {"allow", "retry", "background", "offscreen"} else 0)
                assert len(requests) == (2 if case == "two-tabs" else 1)
                evidence = requests[0].classification.details["operation_evidence"]
                assert evidence["request_bound"] is True
                assert evidence["verified_fields"]["account"] == "account-123"
            finally:
                await asyncio.wait_for(session.kill(), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["allow", "block", "changed-dom"])
async def test_browser_use_bounded_fields_need_one_review_and_never_submit(tmp_path, monkeypatch, case):
    pytest.importorskip("browser_use")
    from browser_use import BrowserSession
    from browser_use.browser.events import NavigateToUrlEvent
    from playwright.async_api import async_playwright
    from acira import protect
    from acira.project import initialize
    from acira.local import atomic_json
    from acira_cdp_proxy.action_gateway import AciraActionBlocked
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    monkeypatch.setenv("BROWSER_USE_CLOUD_SYNC", "false")
    effects = []
    async def index(request):
        return web.Response(text='<form onsubmit="fetch(\'/effect\',{method:\'POST\'});return false"><textarea id="first" name="first"></textarea><input id="second" name="second"><select name="currency"><option>USD</option></select><button type="submit">Submit</button></form>', content_type="text/html")
    async def effect(request):
        effects.append(1)
        return web.Response(text="unexpected")
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/effect", effect)
    project = initialize(tmp_path / "project", adapter="browser_use", selected=[], dashboard=False)
    document = project.document()
    document["agents"]["default"]["guard"] = {"review_actions": ["form_input"], "terminal_log": False}
    atomic_json(project.path, document)
    reviews = []
    session = None
    class Reviewer:
        async def ask(self, request):
            reviews.append(request)
            if case == "changed-dom":
                await next(iter(guard._operation_channels.values())).send("Runtime.evaluate", {"expression": "document.getElementById('second').value='changed'"})
            return "block" if case == "block" else "allow"
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as p:
        async with protect(project=project.root, dashboard=False, review_backend=Reviewer()) as guard:
            session = BrowserSession(args=["--password-store=basic", "--use-mock-keychain"], headless=True, executable_path=p.chromium.executable_path,
                user_data_dir=tmp_path / "profile", downloads_path=tmp_path / "downloads",
                enable_default_extensions=False, permissions=[], keep_alive=False)
            try:
                await asyncio.wait_for(session.start(), 30)
                event = session.event_bus.dispatch(NavigateToUrlEvent(url=str(server.make_url("/"))))
                await asyncio.wait_for(event.event_result(raise_if_any=True, raise_if_none=False), 30)
                summary = await session.get_browser_state_summary(include_screenshot=False)
                def node(identifier):
                    return next(value for value in summary.dom_state.selector_map.values() if value.attributes.get("id") == identifier)
                fields = [(node("first"), "one"), (node("second"), "two")]
                if case == "allow":
                    await guard.fill_fields(session, fields)
                else:
                    with pytest.raises(AciraActionBlocked):
                        await guard.fill_fields(session, fields)
                result = await guard._send_browser_use_cdp(session, "Runtime.evaluate", {"expression": "[document.getElementById('first').value,document.getElementById('second').value]", "returnByValue": True})
                assert result["result"]["value"] == (["one", "two"] if case == "allow" else ["", "changed"] if case == "changed-dom" else ["", ""])
                assert len(reviews) == 1
                assert not effects
            finally:
                await asyncio.wait_for(session.kill(), 10)
