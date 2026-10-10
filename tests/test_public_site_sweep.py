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


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_redirect", [False, True])
async def test_sweep_observes_actual_effects_and_fresh_sessions(tmp_path, monkeypatch, unsafe_redirect):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    original = Browser.new_context
    requests, starting_cookies = [], []

    async def respond(request):
        requests.append((request.method, "https://fixture.invalid" + request.path))
        if request.path == "/":
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
    if unsafe_redirect:
        assert row["status"] == "unavailable", row
        assert row["cases"][0]["requests_stopped"]["redirect_not_permitted"] == 1
        assert requests == [("GET", "https://fixture.invalid/"), ("GET", "https://fixture.invalid/about")]
        return
    assert row["status"] == "complete", row
    assert [case["status"] for case in row["cases"]] == ["verified"] * 5
    assert [case.get("click_dispatches") for case in row["cases"]] == [None, 1, 0, 0, 1]
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
