"""Repeatable offline checks of the live sweep's safety and evidence contracts."""
import argparse
import importlib.util
import json
from pathlib import Path
from urllib.robotparser import RobotFileParser

import pytest

pytest.importorskip("playwright.async_api")
from playwright.async_api import Browser, BrowserContext, Route, async_playwright
_tool = Path(__file__).resolve().parents[1] / "tools" / "public_site_sweep.py"
_spec = importlib.util.spec_from_file_location("acira_public_site_sweep_tests", _tool)
sweep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep)


@pytest.mark.parametrize("url,method,navigation", [
    ("https://fixture.invalid/about", "POST", True),
    ("https://fixture.invalid/about", "PUT", False),
    ("https://fixture.invalid/about", "DELETE", False),
    ("http://fixture.invalid/about", "GET", True),
    ("https://fixture.invalid:444/about", "GET", True),
    ("https://person:secret@fixture.invalid/about", "GET", True),
    ("https://fixture.invalid/%2561ccount", "GET", True),
    ("https://fixture.invalid/about?confirm=true", "GET", True),
    ("https://other.invalid/about", "GET", True),
    ("https://127.0.0.1/about", "GET", False),
    ("https://[::1]/about", "GET", False),
    ("https://computer.local/about", "GET", False),
    ("https://fixture.invalid/logout", "GET", False),
    ("https://fixture.invalid/payment", "GET", True),
    ("https://fixture.invalid/about.pdf", "GET", True),
    ("https://fixture.invalid/research.zip", "GET", True),
    ("https://fixture.invalid/about.json", "GET", True),
])
def test_sweep_rejects_unsafe_network_paths(url, method, navigation):
    assert not sweep.Fence("https://fixture.invalid/").permits(url, method, navigation)


def test_sweep_document_scope_and_canonical_redirects():
    fence = sweep.Fence("https://fixture.invalid/")
    fence.documents = {sweep.document_key("https://fixture.invalid/about")}
    assert fence.permits("https://fixture.invalid/about/", "GET", True)
    assert not fence.permits("https://fixture.invalid/research", "GET", True)
    assert sweep.destination_matches("https://fixture.invalid/about/", "https://fixture.invalid/about")
    assert not sweep.destination_matches("https://fixture.invalid/about/#other", "https://fixture.invalid/about")
    assert not sweep.destination_matches("https://other.invalid/about", "https://fixture.invalid/about")
    assert not sweep.destination_matches("https://fixture.invalid/about?changed=true", "https://fixture.invalid/about")


def test_public_catalog_has_hundreds_of_unique_safe_sites():
    catalog = json.loads(Path(sweep.__file__).with_name("public-sites.json").read_text())
    sites = catalog["sites"]
    assert len(sites) >= 300
    assert len({sweep.urlsplit(site["url"]).hostname for site in sites}) == len(sites)
    assert len({site["category"] for site in sites}) >= 6
    assert all(sweep.public_url(site["url"], document=True) for site in sites)


def test_report_excludes_challenges_and_blank_html_from_public_coverage(tmp_path):
    rows = []
    for url, title, length, challenge in (
        ("https://content.invalid/", "Public guide", 100, False),
        ("https://challenge.invalid/", "Verify you are human", 100, True),
        ("https://blank.invalid/", "Loading", 0, False),
    ):
        rows.append({"url": url, "category": "fixture", "status": "partial",
            "robots": "allowed", "cases": [{"mode": "normal", "status": "no_safe_link",
                "http_status": 200, "title": title, "text_length": length, "challenge": challenge}]})
    (tmp_path / "results-0.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    report = sweep.summary(tmp_path)
    assert report["sites_attempted"] == 3
    assert report["sites_with_observed_html"] == report["sites_with_guarded_html"] == 1
    assert report["full_workflow_passes"] == 0
    assert "challenge.invalid" in (tmp_path / "report.html").read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_redirect", [False, True, "client-route", "different-client-route"])
async def test_sweep_observes_actual_effects_and_fresh_sessions(tmp_path, monkeypatch, unsafe_redirect):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    original = Browser.new_context
    requests, starting_cookies = [], []
    initial_pages = 0

    async def respond(request):
        nonlocal initial_pages
        requests.append((request.method, "https://fixture.invalid" + request.path))
        if request.path == "/":
            initial_pages += 1
            if unsafe_redirect in {"client-route", "different-client-route"}:
                landing = "/guide/introduction" if initial_pages == 1 or unsafe_redirect == "client-route" else "/guide/changed"
                return web.Response(content_type="text/html",text=f'''<h1>Public fixture</h1>
                    <a href="/guide">Public guide</a><script>
                    document.querySelector('a').addEventListener('click',e=>{{e.preventDefault();
                    history.pushState({{}},'',{json.dumps(landing)});
                    document.querySelector('h1').textContent='Guide content'}})</script>''')
            return web.Response(content_type="text/html", headers={"Set-Cookie": "anonymous=fixture; Secure"}, text='''
                <main><h1>Fixture website</h1><a href="#main">Skip to main content</a>
                <a href="/about">About this project</a><input type="password" value="fixture-secret"></main>
                <script>fetch('/payment',{method:'POST',body:'fixture'}).catch(()=>{});</script>''')
        if request.path == "/about":
            return web.Response(status=301, headers={"Location": "/payment" if unsafe_redirect else "/about/"})
        if request.path == "/about/":
            return web.Response(content_type="text/html", text="<h1>About the fixture project</h1>")
        return web.Response(status=404)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", respond)

    async def fixtures(self, **kwargs):
        context = await original(self, **kwargs)
        starting_cookies.append(await context.cookies())

        return context

    async def robots(*args):
        parser = RobotFileParser()
        parser.parse(["User-agent: *", "Disallow: /private/"])
        return parser, "allowed"

    monkeypatch.setattr(Browser, "new_context", fixtures)
    monkeypatch.setattr(sweep, "robots_check", robots)
    catalog = tmp_path / "sites.json"
    catalog.write_text(json.dumps({"sites": [{"category": "fixture", "url": "https://fixture.invalid/"}]}))
    args = argparse.Namespace(output=tmp_path / "results", catalog=catalog, shards=1, shard=0,
        limit=None, timeout=5000, headed=False, resume=False)
    async with TestServer(app, host="127.0.0.1") as server:
        async def local_transport(self, **kwargs):
            assert kwargs["max_redirects"] == 0 and kwargs["max_retries"] == 0
            assert self.request.method in {"GET", "HEAD"}
            # The fixture transport uses a real local server. Public sweep
            # traffic retains HTTPS certificate checks; no real site is used.
            path = sweep.urlsplit(self.request.url).path
            return await self.request.frame.page.context.request.get(str(server.make_url(path)), max_redirects=0, max_retries=0)

        monkeypatch.setattr(Route, "fetch", local_transport)
        await sweep.worker(args)
    row = json.loads((args.output / "results-0.jsonl").read_text())
    if unsafe_redirect is True:
        assert row["status"] == "unavailable", row
        assert row["cases"][0]["requests_stopped"]["redirect_not_permitted"] == 1
        assert requests == [("GET", "https://fixture.invalid/"), ("GET", "https://fixture.invalid/about")]
        return
    if unsafe_redirect == "different-client-route":
        assert row["status"] == "partial"
        assert row["cases"][0]["status"] == "verified"
        assert row["cases"][1]["status"] == row["cases"][4]["status"] == "destination_failure"
        assert row["cases"][2]["click_dispatches"] == row["cases"][3]["click_dispatches"] == 0
        return
    assert row["status"] == "complete", row
    assert [case["status"] for case in row["cases"]] == ["verified"] * 5
    assert [case.get("click_dispatches") for case in row["cases"]] == [None, 1, 0, 0, 1]
    if unsafe_redirect == "client-route":
        assert [case.get("document_requests_after_click") for case in row["cases"]] == [0]*5
        assert row["cases"][0]["target_url"] == "https://fixture.invalid/guide"
        assert row["cases"][0]["landing_url"] == "https://fixture.invalid/guide/introduction"
        assert row["cases"][1]["expected_landing"] == row["cases"][0]["landing_url"]
        assert all(method=="GET" and url=="https://fixture.invalid/" for method,url in requests)
        return
    assert [case.get("document_requests_after_click") for case in row["cases"]] == [2, 2, 0, 0, 2]
    assert all(case["fresh_cookie_count"] == 0 for case in row["cases"])
    assert starting_cookies == [[]] * 5
    assert all(method in {"GET", "HEAD"} for method, _ in requests)
    assert requests.count(("GET", "https://fixture.invalid/about/")) == 3
    assert all(case["requests_stopped"].get("method", 0) >= 1 for case in row["cases"])
    assert row["cases"][3]["reviews"][0]["state"] == "rejected"
    assert row["cases"][4]["reviews"][0]["state"] == "approved"
    assert all(case["reviews"][0]["capture"] == "ready" for case in row["cases"][3:])
    report = sweep.summary(args.output)
    assert report["site_status"] == {"complete": 1}


@pytest.mark.asyncio
async def test_teardown_keeps_background_mutations_off_the_network(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    import asyncio

    received_posts = []
    async def respond(request):
        if request.method == "POST":
            received_posts.append(request.path)
            return web.Response(text="fixture only")
        return web.Response(content_type="text/html", text='''<h1>Local fixture</h1>
            <script>setInterval(() => fetch('/payment', {method:'POST',body:'fixture'})
                .catch(() => {}), 20);</script>''')
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", respond)
    original_unroute = BrowserContext.unroute_all
    async def delayed_unroute(self, **kwargs):
        await original_unroute(self, **kwargs)
        # Deliberately hold a live page in the exact former cleanup window.
        await asyncio.sleep(.3)
    monkeypatch.setattr(BrowserContext, "unroute_all", delayed_unroute)
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            for control in (True, False):
                received_posts.clear()
                url = str(server.make_url("/"))
                fence = sweep.Fence("https://fixture.invalid/")
                # Only this local, inert fixture uses HTTP/loopback. Live site
                # traffic retains the normal HTTPS/public-URL checks.
                fence.permits = lambda address, method, navigation: address.startswith(url) and method in {"GET", "HEAD"}
                context = await sweep.new_context(browser, fence)
                page = await context.new_page()
                await page.goto(url)
                await asyncio.sleep(.15)
                assert received_posts == []
                if control:
                    # Demonstrate that the old cleanup order really leaks a
                    # background request, using only the inert local endpoint.
                    await context.unroute_all(behavior="ignoreErrors")
                    await context.close()
                    assert received_posts, "Negative control did not exercise the gap"
                else:
                    await sweep.close_context(context, fence)
                    assert received_posts == []
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("guarded", [False, True])
async def test_selected_link_stays_pinned_when_menu_order_changes(guarded):
    import contextlib
    from acira.adapters.playwright import PlaywrightCDPProxy
    from acira.adapters.playwright_boundary import trusted
    reviewer = sweep.TestReviewer()
    reviewer.expected = "https://fixture.invalid/about"
    reviewer.decision = "allow"
    async with contextlib.AsyncExitStack() as stack:
        p = await stack.enter_async_context(async_playwright())
        if guarded:
            guard = await stack.enter_async_context(PlaywrightCDPProxy(
                config={"review_actions": ["click"], "terminal_log": False},
                review_backend=reviewer))
            guard.patch_playwright(p)
        browser = await p.chromium.launch()
        stack.push_async_callback(browser.close)
        page = await browser.new_page()
        async def fixture(route):
            text = '<h1>Local fixture</h1><a href="/about">About this project</a>' if route.request.url.endswith('/') else '<h1>Public content</h1>'
            await route.fulfill(content_type="text/html", body=text)
        await page.route("**/*", fixture)
        await page.goto("https://fixture.invalid/")
        selected, destination, _ = await sweep.find_link(page)
        # A live menu inserts a new link ahead of the previously selected one.
        # An ordinal Locator would now point to a different operation.
        await trusted(page, "evaluate", '''() => {
            const item = document.createElement('a');
            item.href = '/research'; item.textContent = 'Research';
            document.body.insertBefore(item, document.querySelector('a'));
        }''')
        await selected.click()
        assert page.url == destination == reviewer.expected
        assert reviewer.requests == (1 if guarded else 0)


@pytest.mark.asyncio
async def test_selection_skips_absolute_fragment_and_obstructed_duplicate_without_clicking():
    from acira.adapters.playwright_boundary import trusted
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width":800,"height":600})
            await page.route("**/*", lambda route: route.fulfill(content_type="text/html", body='''
                <a href="https://fixture.invalid/#resources">Resources</a>
                <div style="position:relative;width:300px;height:60px">
                  <a href="/about" style="position:absolute;top:10px">About covered menu</a>
                  <div style="position:absolute;inset:0;background:white;z-index:2"></div>
                </div>
                <a href="/about" id="content">About visible content</a>
                <script>window.clicks=0;document.addEventListener('click',()=>window.clicks++)</script>
            '''))
            await page.goto("https://fixture.invalid/")
            from collections import Counter
            diagnostics = Counter()
            selected, target, label = await sweep.find_link(page, diagnostics=diagnostics)
            assert target == "https://fixture.invalid/about"
            assert label == "About visible content"
            assert await selected.get_attribute("id") == "content"
            assert diagnostics == {"target_not_actionable": 1}
            assert page.url == "https://fixture.invalid/"
            assert await trusted(page,"evaluate","window.clicks") == 0
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_ready_link_waits_for_hydrated_public_target_without_any_click():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            await page.route("**/*",lambda route: route.fulfill(content_type="text/html", body='''
                <h1>Loading public content</h1><script>
                setTimeout(()=>{const a=document.createElement('a');a.href='/about';
                  a.textContent='About the project';document.body.append(a)},500)
                </script>'''))
            await page.goto("https://fixture.invalid/")
            link, diagnostics = await sweep.ready_link(page)
            assert link[1] == "https://fixture.invalid/about"
            assert page.url == "https://fixture.invalid/"
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_failed_browser_call_retains_dispatch_review_and_navigation_evidence(tmp_path,monkeypatch):
    from acira import protect
    from acira.project import initialize
    from acira.local import atomic_json
    from acira.journal import Journal
    from playwright.async_api import ElementHandle
    monkeypatch.setenv("ACIRA_HOME",str(tmp_path/"private"))
    project=initialize(tmp_path/"project",selected=[],dashboard=False)
    document=project.document()
    document["agents"]["default"]["guard"]={"review_actions":["click"],"terminal_log":False}
    atomic_json(project.path,document)
    saved_click=ElementHandle.click
    async def failed_click(self,*args,**kwargs):
        result=await saved_click(self,*args,**kwargs)
        if not kwargs.get("trial"):
            raise RuntimeError("Fixture: browser result unavailable after click")
        return result
    monkeypatch.setattr(ElementHandle,"click",failed_click)
    async def response(route):
        return await route.fulfill(content_type="text/html",body='<h1>Public fixture</h1><a href="/about">About</a>')
    monkeypatch.setattr(sweep.Fence,"_route",lambda self,route: response(route))
    reviewer=sweep.TestReviewer()
    async with protect(project=project.root,dashboard=False,review_backend=reviewer) as guard, async_playwright() as p:
        guard.patch_playwright(p)
        browser=await p.chromium.launch()
        try:
            result=await sweep.case(browser,{"url":"https://fixture.invalid/"},timeout=2000,
                mode="approved",guard=guard,journal=Journal(project.root),reviewer=reviewer)
            assert result["status"]=="error"
            assert result["click_dispatches"]==1
            assert result["reviews"][0]["state"]=="approved"
            assert result["final_url"]=="https://fixture.invalid/about"
            assert result["document_requests_after_click"]==0  # fixture does not use live transport
            assert result["failure"]["scope"]=="needs_investigation"
        finally:
            await browser.close()
