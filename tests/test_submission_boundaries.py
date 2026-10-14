"""Only local fixtures receive submissions; blocked operations have zero effects."""
import asyncio
from contextlib import asynccontextmanager
import html
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright

from acira import protect
from acira.adapters.playwright import PlaywrightCDPProxy
from acira.adapters.playwright_boundary import trusted
from acira.project import initialize
from acira_cdp_proxy.action_gateway import AciraActionBlocked


VALUES = {"recipient": "fixture-recipient", "message": "Reviewed local draft"}


@asynccontextmanager
async def local_site(*, label="Send", native=False, method="post", control="button", actual=None, confirmation=False, routine=False):
    received = []
    payload = VALUES if actual is None else actual
    async def index(request):
        if native:
            if control == "input":
                target = '<input id="send" type="submit" value="Send">'
            elif control == "image":
                target = '<input id="send" type="image" aria-label="Submit" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7">'
            else:
                kind = '' if control in {"default", "external"} else ' type="submit"'
                if control == "external":
                    kind += ' form="compose"'
                target = '<button id="send"'+kind+'>'+html.escape(label)+'</button>'
            markup = '<form id="compose" action="/outbound" method="'+method+'" onsubmit="window.attempts++"><input id="recipient" name="recipient" value="'+VALUES["recipient"]+'"><textarea name="message">'+VALUES["message"]+'</textarea>'+('' if control == "external" else target)+'</form>'+ (target if control == "external" else '')
        else:
            code = "window.attempts++;fetch('/outbound',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify("+json.dumps(payload)+")}).then(()=>window.finished=true)"
            if routine:
                code = "window.attempts++;document.querySelector('#modal-status').textContent=String(window.attempts)"
            if isinstance(confirmation, str) and confirmation.startswith("chained-confirm"):
                # Neither branch sends a request; canceling the first physical
                # modal immediately opens a second modal on the same page.
                code = "window.modal_openings++;window.confirmed=confirm('Submit this local draft?');if(!window.confirmed){window.modal_openings++;window.second_confirmed=confirm('Submit this local draft?')};document.querySelector('#modal-status').textContent=JSON.stringify({confirmed:window.confirmed,second_confirmed:window.second_confirmed,attempts:window.attempts,modal_openings:window.modal_openings})"
            elif confirmation:
                modal = "prompt('Submit this local draft?')!==null" if confirmation == "prompt" else "confirm('Submit this local draft?')"
                code = "window.modal_openings++;window.confirmed="+modal+";if(window.confirmed){"+code+"};document.querySelector('#modal-status').textContent=JSON.stringify({confirmed:window.confirmed,attempts:window.attempts,modal_openings:window.modal_openings})"
            markup = '<button id="'+('preview' if routine else 'send')+'" type="button" onclick="'+html.escape(code, quote=True)+'">'+html.escape(label)+'</button><output id="modal-status">Not handled</output>'
        return web.Response(text='<title>Local submission fixture</title><script>window.attempts=0;window.finished=false;window.confirmed=false;window.modal_openings=0;</script>'+markup,
            content_type="text/html")
    async def outbound(request):
        body = dict(await request.post()) if request.content_type == "application/x-www-form-urlencoded" else await request.json() if request.can_read_body else dict(request.query)
        received.append({"method": request.method, "path": request.path, "body": body})
        return web.Response(text='<title>Local response</title><p>One local request observed</p>', content_type="text/html")
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_route("*", "/outbound", outbound)
    app.router.add_route("*", "/wrong-destination", outbound)
    async with TestServer(app, host="127.0.0.1") as server:
        yield server, received


class Reviewer:
    def __init__(self, decision="allow", mutate=None):
        self.decision, self.mutate = decision, mutate
        self.requests = []
        self.page = None

    async def ask(self, request):
        self.requests.append(request)
        if self.mutate is not None:
            await trusted(self.page, "evaluate", self.mutate)
        return self.decision


def submissions_project(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    return initialize(tmp_path / "existing", selected=["submissions"], dashboard=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("general_decision", ["ask", "block"])
async def test_core_click_review_cannot_mask_general_submission_requirements(tmp_path, monkeypatch, general_decision):
    from acira.local import atomic_json
    project = submissions_project(tmp_path, monkeypatch)
    document = project.document()
    profile = document["agents"]["default"]
    profile["guard"] = {"review_actions": ["click"]}
    profile["decisions"]["acira.general.form_submission"] = {"submission.outbound_attempt": general_decision}
    atomic_json(project.path, document)
    reviewer = Reviewer("allow")
    async with local_site(label="Send") as (server, received), protect(project=project.root, dashboard=False, review_backend=reviewer) as guard, async_playwright() as p:
        guard.patch_playwright(p)
        browser = await p.chromium.launch(chromium_sandbox=True)
        try:
            page = await browser.new_page()
            await page.goto(str(server.make_url("/")))
            with pytest.raises(AciraActionBlocked):
                await page.locator("#send").click()
            assert reviewer.requests == [] and received == []
            assert await trusted(page, "evaluate", "window.attempts") == 0
            if general_decision == "block":
                with pytest.raises(AciraActionBlocked):
                    await guard.submit(page.locator("#send"), destination=str(server.make_url("/outbound")), values=VALUES, timeout=2)
                assert reviewer.requests == [] and received == []
                assert await trusted(page, "evaluate", "window.attempts") == 0
            else:
                await guard.submit(page.locator("#send"), destination=str(server.make_url("/outbound")), values=VALUES, timeout=2)
                assert len(reviewer.requests) == 1
                assert received == [{"method": "POST", "path": "/outbound", "body": VALUES}]
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,control,label", [("post", "button", "Send"), ("get", "default", "Search"), ("post", "input", "Send"), ("post", "image", "Submit"), ("post", "external", "Review form")])
async def test_native_submit_controls_without_request_contract_send_nothing(tmp_path, monkeypatch, capsys, method, control, label):
    project = submissions_project(tmp_path, monkeypatch)
    reviewer = Reviewer()
    async with local_site(native=True, method=method, control=control, label=label) as (server, received), protect(project=project.root, dashboard=False, review_backend=reviewer) as guard, async_playwright() as p:
        guard.patch_playwright(p)
        browser = await p.chromium.launch(chromium_sandbox=True)
        try:
            page = await browser.new_page()
            await page.goto(str(server.make_url("/")))
            with pytest.raises(AciraActionBlocked) as failure:
                await page.locator("#send").click()
            assert received == []
            assert await trusted(page, "evaluate", "window.attempts") == 0
            assert reviewer.requests == []
            assert "guard.submit" in str(failure.value) + capsys.readouterr().err
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["Submit", "Send", "Post", "Publish", "Apply", "Finalize"])
async def test_direct_outbound_buttons_without_contract_cannot_dispatch(tmp_path, monkeypatch, label):
    project = submissions_project(tmp_path, monkeypatch)
    reviewer = Reviewer()
    async with local_site(label=label) as (server, received), protect(project=project.root, dashboard=False, review_backend=reviewer) as guard, async_playwright() as p:
        guard.patch_playwright(p)
        browser = await p.chromium.launch(chromium_sandbox=True)
        try:
            page = await browser.new_page()
            await page.goto(str(server.make_url("/")))
            with pytest.raises(AciraActionBlocked):
                await page.locator("#send").click()
            assert reviewer.requests == [] and received == []
            assert await trusted(page, "evaluate", "window.attempts") == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["Preview", "Search", "Continue", "Save"])
async def test_default_buttons_outside_forms_remain_routine_controls(tmp_path, monkeypatch, label):
    project = submissions_project(tmp_path, monkeypatch)
    reviewer = Reviewer()
    async def index(request):
        return web.Response(text='<script>window.previews=0</script><button id="routine" onclick="window.previews++">'+label+'</button>', content_type="text/html")
    app = web.Application()
    app.router.add_get("/", index)
    async with TestServer(app, host="127.0.0.1") as server, protect(project=project.root, dashboard=False, review_backend=reviewer) as guard, async_playwright() as p:
        guard.patch_playwright(p)
        browser = await p.chromium.launch(chromium_sandbox=True)
        try:
            page = await browser.new_page()
            await page.goto(str(server.make_url("/")))
            await page.locator("#routine").click()
            assert await trusted(page, "evaluate", "window.previews") == 1
            assert reviewer.requests == []
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["page", "element", "keyboard-enter", "locator-enter", "frame"])
async def test_public_action_paths_share_submission_boundary(tmp_path, monkeypatch, path):
    project = submissions_project(tmp_path, monkeypatch)
    async with local_site(native=True) as (server, received), protect(project=project.root, dashboard=False) as guard, async_playwright() as p:
        guard.patch_playwright(p)
        browser = await p.chromium.launch(chromium_sandbox=True)
        try:
            page = await browser.new_page()
            await page.goto(str(server.make_url("/")))
            if path == "page":
                operation = lambda: page.click("#send")
            elif path == "element":
                target = await page.locator("#send").element_handle()
                operation = target.click
            elif path == "keyboard-enter":
                await trusted(page.locator("#recipient"), "focus")
                operation = lambda: page.keyboard.press("Enter")
            elif path == "locator-enter":
                operation = lambda: page.locator("#recipient").press("Enter")
            else:
                await trusted(page, "set_content", '<iframe src="'+str(server.make_url("/"))+'"></iframe>')
                target = page.frame_locator("iframe").locator("#send")
                await target.wait_for(state="visible")
                operation = target.click
            with pytest.raises(AciraActionBlocked):
                await operation()
            assert received == []
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False], ids=["native-form", "fetch-json"])
@pytest.mark.parametrize("decision", ["allow", "block"])
async def test_exact_submission_contract_sends_one_attempt_or_zero(tmp_path, monkeypatch, native, decision):
    project = submissions_project(tmp_path, monkeypatch)
    reviewer = Reviewer(decision)
    async with local_site(native=native) as (server, received), protect(project=project.root, dashboard=False, review_backend=reviewer) as guard, async_playwright() as p:
        guard.patch_playwright(p)
        browser = await p.chromium.launch(chromium_sandbox=True)
        try:
            page = await browser.new_page()
            await page.goto(str(server.make_url("/")))
            operation = lambda: guard.submit(page.locator("#send"), destination=str(server.make_url("/outbound")), values=VALUES,
                encoding="form" if native else "json", timeout=2)
            if decision == "block":
                with pytest.raises(AciraActionBlocked):
                    await operation()
            else:
                await operation()
            assert received == ([{"method": "POST", "path": "/outbound", "body": VALUES}] if decision == "allow" else [])
            assert len(reviewer.requests) == 1
            evidence = reviewer.requests[0].classification.details["operation_evidence"]
            assert evidence["critical"] and evidence["request_bound"]
            assert "grant_scope" not in evidence
            assert evidence["destination"] == str(server.make_url("/outbound"))
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["field", "destination", "target", "method"])
async def test_reviewed_native_submission_change_invalidates_approval_before_dispatch(tmp_path, monkeypatch, change):
    project = submissions_project(tmp_path, monkeypatch)
    mutations = {"field": "document.querySelector('[name=message]').value='Unreviewed draft'",
        "destination": "document.querySelector('form').action='/wrong-destination'",
        "target": "const old=document.querySelector('#send');old.replaceWith(old.cloneNode(true))",
        "method": "document.querySelector('form').method='get'"}
    reviewer = Reviewer(mutate=mutations[change])
    async with local_site(native=True) as (server, received), protect(project=project.root, dashboard=False, review_backend=reviewer) as guard, async_playwright() as p:
        guard.patch_playwright(p)
        browser = await p.chromium.launch(chromium_sandbox=True)
        try:
            page = reviewer.page = await browser.new_page()
            await page.goto(str(server.make_url("/")))
            with pytest.raises(AciraActionBlocked, match="changed before dispatch"):
                await guard.submit(page.locator("#send"), destination=str(server.make_url("/outbound")), values=VALUES, encoding="form", timeout=2)
            assert len(reviewer.requests) == 1 and received == []
            assert await trusted(page, "evaluate", "window.attempts") == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_actual_payload_change_is_not_sent_even_after_approval(tmp_path, monkeypatch):
    project = submissions_project(tmp_path, monkeypatch)
    reviewer = Reviewer()
    async with local_site(actual={**VALUES, "message": "Unreviewed hidden payload"}) as (server, received), protect(project=project.root, dashboard=False, review_backend=reviewer) as guard, async_playwright() as p:
        guard.patch_playwright(p)
        browser = await p.chromium.launch(chromium_sandbox=True)
        try:
            page = await browser.new_page()
            await page.goto(str(server.make_url("/")))
            with pytest.raises(AciraActionBlocked, match="actual submission differed"):
                await guard.submit(page.locator("#send"), destination=str(server.make_url("/outbound")), values=VALUES, timeout=2)
            assert len(reviewer.requests) == 1 and received == []
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing-target", "ambiguous-target", "wrong-destination"])
async def test_invalid_target_or_destination_cannot_dispatch_submission(tmp_path, monkeypatch, case):
    project = submissions_project(tmp_path, monkeypatch)
    reviewer = Reviewer()
    async with local_site() as (server, received), protect(project=project.root, dashboard=False, review_backend=reviewer) as guard, async_playwright() as p:
        guard.patch_playwright(p)
        browser = await p.chromium.launch(chromium_sandbox=True)
        try:
            page = await browser.new_page()
            await page.goto(str(server.make_url("/")))
            if case == "ambiguous-target":
                await trusted(page, "evaluate", "const el=document.querySelector('#send');el.after(el.cloneNode(true))")
            selector = "#missing" if case == "missing-target" else "#send"
            destination = str(server.make_url("/wrong-destination" if case == "wrong-destination" else "/outbound"))
            error = AciraActionBlocked if case == "wrong-destination" else ValueError
            with pytest.raises(error):
                await guard.submit(page.locator(selector), destination=destination, values=VALUES, timeout=2)
            assert received == []
            assert len(reviewer.requests) == (1 if case == "wrong-destination" else 0)
            if case != "wrong-destination":
                assert await trusted(page, "evaluate", "window.attempts") == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["request-submit", "form-submit", "dispatch-event", "cdp-script", "cdp-keyboard"])
async def test_uncontracted_scripts_and_raw_cdp_cannot_submit_local_forms(tmp_path, monkeypatch, path):
    project = submissions_project(tmp_path, monkeypatch)
    async with local_site(native=True) as (server, received), protect(project=project.root, dashboard=False) as guard, async_playwright() as p:
        guard.patch_playwright(p)
        browser = await p.chromium.launch(chromium_sandbox=True)
        try:
            page = await browser.new_page()
            await page.goto(str(server.make_url("/")))
            if path in {"request-submit", "form-submit"}:
                script = "document.querySelector('form')." + ("requestSubmit()" if path == "request-submit" else "submit()")
                operation = lambda: page.evaluate(script)
            elif path == "dispatch-event":
                operation = lambda: page.locator("form").dispatch_event("submit")
            else:
                session = await page.context.new_cdp_session(page)
                if path == "cdp-script":
                    operation = lambda: session.send("Runtime.evaluate", {"expression": "document.querySelector('form').requestSubmit()"})
                else:
                    await trusted(page.locator("#recipient"), "focus")
                    operation = lambda: session.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13})
            with pytest.raises(AciraActionBlocked):
                await operation()
            assert received == []
            assert await trusted(page, "evaluate", "window.attempts") == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["dialog-accept", "cdp-accept", "dialog-dismiss"])
async def test_native_confirm_cannot_trigger_followup_submission(path):
    # This fixture intentionally starts the modal through a test-owned original
    # call. The supported Dialog/CDP boundary, rather than click classification,
    # must prevent its confirmation from dispatching the outbound operation.
    async with local_site(label="Open modal", confirmation=True) as (server, received), PlaywrightCDPProxy(config={"terminal_log": False}) as guard, async_playwright() as p:
        guard.patch_playwright(p)
        browser = await p.chromium.launch(chromium_sandbox=True)
        try:
            page = await browser.new_page()
            await page.goto(str(server.make_url("/")))
            async with page.expect_event("dialog", timeout=3000) as opened:
                clicked = asyncio.create_task(trusted(page.locator("#send"), "click", timeout=3000))
            dialog = await opened.value
            try:
                if path == "dialog-accept":
                    with pytest.raises(AciraActionBlocked):
                        await dialog.accept()
                elif path == "cdp-accept":
                    session = await page.context.new_cdp_session(page)
                    with pytest.raises(AciraActionBlocked):
                        await session.send("Page.handleJavaScriptDialog", {"accept": True})
                    await dialog.dismiss()
                else:
                    await dialog.dismiss()
                await asyncio.wait_for(clicked, 3)
                assert received == []
                assert await trusted(page, "evaluate", "window.confirmed") is False
                assert await trusted(page, "evaluate", "window.attempts") == 0
            finally:
                if not clicked.done():
                    clicked.cancel()
                    await asyncio.gather(clicked, return_exceptions=True)
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("general_enabled", [False, True], ids=["specific-only", "specific-and-submissions"])
async def test_specific_capture_terminal_import_and_bound_review_observe_local_request(tmp_path, monkeypatch, select_capture_target, general_enabled):
    from acira.cli import run
    from acira.extension import install_extension
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "existing", selected=["submissions"] if general_enabled else [], dashboard=False)
    extension = install_extension(tmp_path / "extension")
    manifest = json.loads((extension / "manifest.json").read_text())
    assert "debugger" not in manifest.get("permissions", [])
    manifest["host_permissions"] = ["http://127.0.0.1/*"]
    (extension / "manifest.json").write_text(json.dumps(manifest))
    async with local_site(label="Send") as (server, received):
        async with async_playwright() as p:
            capture = await p.chromium.launch_persistent_context(tmp_path / "capture-profile", channel="chromium", headless=True,
                chromium_sandbox=True, service_workers="block",
                args=["--disable-extensions-except="+str(extension), "--load-extension="+str(extension)])
            try:
                worker = capture.service_workers[0] if capture.service_workers else await capture.wait_for_event("serviceworker", timeout=10000)
                page = await capture.new_page()
                await page.goto(str(server.make_url("/")))
                await worker.evaluate("""async url=>{const tab=(await chrome.tabs.query({})).find(tab=>tab.url===url);
                    await chrome.scripting.insertCSS({target:{tabId:tab.id},files:['capture.css']});
                    await chrome.scripting.executeScript({target:{tabId:tab.id},files:['content.js']});}""", page.url)
                await select_capture_target(page, page.locator("#send"))
                assert received == [] and await page.evaluate("window.attempts") == 0
                await page.get_by_role("button", name="Ask first", exact=True).click()
                async with page.expect_download() as downloaded:
                    await page.get_by_role("button", name="Download", exact=True).click()
                exported = tmp_path / "captured-send.json"
                await (await downloaded.value).save_as(exported)
                document = json.loads(exported.read_text())
                assert document["targets"][0]["decision"] == "ask"
                assert document["targets"][0]["fingerprint"]["id"] == "send"
            finally:
                await capture.close()
        common = ["--project", str(project.root)]
        run(["policy", "import", str(exported), *common])
        run(["policy", "import", str(exported), *common])
        run(["policy", "validate", *common])
        assert len(project.policies("default")) == (2 if general_enabled else 1)
        reviewer = Reviewer("block")
        async with protect(project=project.root, dashboard=False, review_backend=reviewer) as guard, async_playwright() as p:
            guard.patch_playwright(p)
            browser = await p.chromium.launch(chromium_sandbox=True)
            try:
                page = await browser.new_page()
                await page.goto(str(server.make_url("/")))
                if general_enabled:
                    reviewer.decision = "allow"
                    with pytest.raises(AciraActionBlocked):
                        await page.locator("#send").click()
                    assert received == [] and reviewer.requests == []
                    assert await trusted(page, "evaluate", "window.attempts") == 0
                    reviewer.decision = "block"
                contract = {"destination": str(server.make_url("/outbound")), "values": VALUES, "timeout": 2}
                with pytest.raises(AciraActionBlocked):
                    await guard.submit(page.locator("#send"), **contract)
                assert received == [] and len(reviewer.requests) == 1
                reviewer.decision = "allow"
                await guard.submit(page.locator("#send"), **contract)
                assert received == [{"method": "POST", "path": "/outbound", "body": VALUES}]
                assert len(reviewer.requests) == 2
            finally:
                await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["confirm", "prompt", "chained-confirm", "chained-confirm-held-response"])
async def test_browser_use_native_dialog_is_dismissed_without_local_submission(tmp_path, monkeypatch, kind):
    pytest.importorskip("browser_use")
    from browser_use import BrowserSession
    from browser_use.browser.events import NavigateToUrlEvent, ClickElementEvent
    from acira.journal import Journal
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    monkeypatch.setenv("BROWSER_USE_CLOUD_SYNC", "false")
    monkeypatch.setenv("BROWSER_USE_CONFIG_DIR", str(tmp_path / "browser-use"))
    project = initialize(tmp_path / "existing", adapter="browser_use", selected=[], dashboard=False)
    async with local_site(label="Open modal", confirmation=kind) as (server, received), async_playwright() as p:
        async with protect(project=project.root, dashboard=False) as guard:
            session = BrowserSession(args=["--password-store=basic", "--use-mock-keychain"], headless=True, executable_path=p.chromium.executable_path, chromium_sandbox=True,
                user_data_dir=tmp_path / "profile", downloads_path=tmp_path / "downloads",
                enable_default_extensions=False, permissions=[], keep_alive=False)
            original_handle = None
            try:
                await asyncio.wait_for(session.start(), 30)
                if kind == "chained-confirm-held-response":
                    page_api = session._cdp_client_root.send.Page
                    original_handle = page_api.handleJavaScriptDialog
                    second_entered = asyncio.Event()
                    dismissal_count = 0
                    first_returned = False
                    overlap_observed = False
                    async def hold_first_return(params, session_id=None):
                        nonlocal dismissal_count, first_returned, overlap_observed
                        dismissal_count += 1
                        ordinal = dismissal_count
                        assert params == {"accept": False}
                        if ordinal == 2:
                            overlap_observed = not first_returned
                            second_entered.set()
                        result = await original_handle(params=params, session_id=session_id)
                        if ordinal == 1:
                            # The actual first dismissal already reached Chrome.
                            # Hold only its Python return, below the SDK's 2s
                            # timeout, until the second real dismissal enters.
                            await asyncio.wait_for(second_entered.wait(), 1)
                            first_returned = True
                        return result
                    page_api.handleJavaScriptDialog = hold_first_return
                event = session.event_bus.dispatch(NavigateToUrlEvent(url=str(server.make_url("/"))))
                await asyncio.wait_for(event.event_result(raise_if_any=True, raise_if_none=False), 30)
                summary = await asyncio.wait_for(session.get_browser_state_summary(include_screenshot=False), 30)
                target = next(node for node in summary.dom_state.selector_map.values() if node.attributes.get("id") == "send")
                clicked = session.event_bus.dispatch(ClickElementEvent(node=target))
                await asyncio.wait_for(clicked.event_result(raise_if_any=True), 10)
                state = await guard._send_browser_use_cdp(session, "Runtime.evaluate", {
                    "expression": "document.querySelector('#modal-status').textContent", "returnByValue": True})
                chained = kind.startswith("chained-confirm")
                expected = {"confirmed": False, "attempts": 0, "modal_openings": 2 if chained else 1}
                if chained:
                    expected["second_confirmed"] = False
                assert json.loads(state["result"]["value"]) == expected
                if kind == "chained-confirm-held-response":
                    assert dismissal_count == 2 and overlap_observed
                assert received == []
                events = Journal(project.root).events(limit=1000)
                blocked = [item for item in events if item["type"] == "native_dialog_confirmation_blocked"]
                assert len(blocked) == expected["modal_openings"]
                assert all(item["dialog_type"] == ("confirm" if chained else kind) for item in blocked)
                assert all(item["acceptance_dispatched"] is False for item in blocked)
                assert not any(item["type"] == "native_dialog_cancel_unavailable" for item in events)
                assert "Submit this local draft" not in json.dumps(events)
            finally:
                if original_handle is not None:
                    page_api.handleJavaScriptDialog = original_handle
                await asyncio.wait_for(session.kill(), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True], ids=["direct-send", "native-post"])
async def test_browser_use_submission_policy_gates_click_and_one_bound_request(tmp_path, monkeypatch, native):
    pytest.importorskip("browser_use")
    from browser_use import BrowserSession
    from browser_use.browser.events import NavigateToUrlEvent, ClickElementEvent
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    monkeypatch.setenv("BROWSER_USE_CLOUD_SYNC", "false")
    monkeypatch.setenv("BROWSER_USE_CONFIG_DIR", str(tmp_path / "browser-use"))
    project = initialize(tmp_path / "existing", adapter="browser_use", selected=["submissions"], dashboard=False)
    reviewer = Reviewer("allow")
    async with local_site(native=native, label="Send") as (server, received), async_playwright() as p:
        async with protect(project=project.root, dashboard=False, review_backend=reviewer) as guard:
            session = BrowserSession(args=["--password-store=basic", "--use-mock-keychain"], headless=True, executable_path=p.chromium.executable_path, chromium_sandbox=True,
                user_data_dir=tmp_path / "profile", downloads_path=tmp_path / "downloads",
                enable_default_extensions=False, permissions=[], keep_alive=False)
            try:
                await asyncio.wait_for(session.start(), 30)
                navigation = session.event_bus.dispatch(NavigateToUrlEvent(url=str(server.make_url("/"))))
                await asyncio.wait_for(navigation.event_result(raise_if_any=True, raise_if_none=False), 30)
                summary = await asyncio.wait_for(session.get_browser_state_summary(include_screenshot=False), 30)
                node = next(value for value in summary.dom_state.selector_map.values() if value.attributes.get("id") == "send")
                clicked = session.event_bus.dispatch(ClickElementEvent(node=node))
                result = await asyncio.wait_for(clicked.event_result(raise_if_any=True), 10)
                assert result["acira"]["decision"].startswith("block") and "validation_error" in result
                assert received == [] and reviewer.requests == []
                evaluations = [entry for entry in guard.runtime_control.journal.events() if entry["type"] == "action_evaluated" and entry.get("classification", {}).get("action") == "click"]
                assert evaluations[-1]["facts"]["target"]["is_native_submit"] is native
                contract = {"destination": str(server.make_url("/outbound")), "values": VALUES,
                    "encoding": "form" if native else "json", "timeout": 2}
                reviewer.decision = "block"
                with pytest.raises(AciraActionBlocked):
                    await guard.submit(session, node, **contract)
                assert received == [] and len(reviewer.requests) == 1
                reviewer.decision = "allow"
                await guard.submit(session, node, **contract)
                assert received == [{"method": "POST", "path": "/outbound", "body": VALUES}]
                assert len(reviewer.requests) == 2
            finally:
                await asyncio.wait_for(session.kill(), 10)


@pytest.mark.asyncio
async def test_browser_use_known_outside_form_preview_stays_allowed(tmp_path, monkeypatch):
    pytest.importorskip("browser_use")
    from browser_use import BrowserSession
    from browser_use.browser.events import NavigateToUrlEvent, ClickElementEvent
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    monkeypatch.setenv("BROWSER_USE_CLOUD_SYNC", "false")
    monkeypatch.setenv("BROWSER_USE_CONFIG_DIR", str(tmp_path / "browser-use"))
    project = initialize(tmp_path / "existing", adapter="browser_use", selected=["submissions"], dashboard=False)
    reviewer = Reviewer("block")
    async with local_site(label="Preview", routine=True) as (server, received), async_playwright() as p:
        async with protect(project=project.root, dashboard=False, review_backend=reviewer) as guard:
            session = BrowserSession(args=["--password-store=basic", "--use-mock-keychain"], headless=True, executable_path=p.chromium.executable_path, chromium_sandbox=True,
                user_data_dir=tmp_path / "profile", downloads_path=tmp_path / "downloads",
                enable_default_extensions=False, permissions=[], keep_alive=False)
            try:
                await asyncio.wait_for(session.start(), 30)
                navigation = session.event_bus.dispatch(NavigateToUrlEvent(url=str(server.make_url("/"))))
                await asyncio.wait_for(navigation.event_result(raise_if_any=True, raise_if_none=False), 30)
                summary = await asyncio.wait_for(session.get_browser_state_summary(include_screenshot=False), 30)
                node = next(value for value in summary.dom_state.selector_map.values() if value.attributes.get("id") == "preview")
                clicked = session.event_bus.dispatch(ClickElementEvent(node=node))
                await asyncio.wait_for(clicked.event_result(raise_if_any=True), 10)
                state = await guard._send_browser_use_cdp(session, "Runtime.evaluate", {
                    "expression": "document.querySelector('#modal-status').textContent", "returnByValue": True})
                assert state["result"]["value"] == "1"
                assert received == [] and reviewer.requests == []
                evaluations = [entry for entry in guard.runtime_control.journal.events() if entry["type"] == "action_evaluated" and entry.get("classification", {}).get("action") == "click"]
                assert evaluations[-1]["facts"]["target"]["is_native_submit"] is False
            finally:
                await asyncio.wait_for(session.kill(), 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["input", "textarea"])
async def test_enter_outside_forms_is_conservatively_gated_without_request_contract(tmp_path, monkeypatch, tag):
    project = submissions_project(tmp_path, monkeypatch)
    reviewer = Reviewer("allow")
    async def index(request):
        markup = '<input id="field" value="draft">' if tag == "input" else '<textarea id="field">draft</textarea>'
        return web.Response(text='<script>window.enter_dispatches=0</script>'+markup+'<script>document.querySelector("#field").addEventListener("keydown",e=>{if(e.key==="Enter")window.enter_dispatches++})</script>', content_type="text/html")
    app = web.Application()
    app.router.add_get("/", index)
    async with TestServer(app, host="127.0.0.1") as server, protect(project=project.root, dashboard=False, review_backend=reviewer) as guard, async_playwright() as p:
        guard.patch_playwright(p)
        browser = await p.chromium.launch(chromium_sandbox=True)
        try:
            page = await browser.new_page()
            await page.goto(str(server.make_url("/")))
            with pytest.raises(AciraActionBlocked):
                await page.locator("#field").press("Enter")
            assert reviewer.requests == []
            assert await trusted(page, "evaluate", "window.enter_dispatches") == 0
            assert await page.locator("#field").input_value() == "draft"
        finally:
            await browser.close()
