"""Offline browser-effect checks for the public-site journey and its restrictions."""
import asyncio
import json
from urllib.parse import urljoin

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

pytest.importorskip("playwright.async_api")
from playwright.async_api import Browser, BrowserType, async_playwright, expect

from acira.journal import Journal
from acira.project import atomic_json
from acira.public_demo import PublicDemo, SITES, request_allowed


@pytest.mark.parametrize("url,method,navigation", [
    ("https://docs.python.org/3/tutorial/", "POST", True),
    ("http://docs.python.org/3/tutorial/", "GET", True),
    ("https://other.example/3/tutorial/", "GET", True),
    ("https://docs.python.org:444/3/tutorial/", "GET", True),
    ("https://name:secret@docs.python.org/3/tutorial/", "GET", True),
    ("https://docs.python.org/3/tutorial/?submit=true", "GET", True),
    ("https://docs.python.org/3/%61ccount", "GET", True),
    ("https://docs.python.org/checkout", "GET", False),
    ("https://docs.python.org/elsewhere", "GET", True),
    ("https://docs.python.org/3/_static/style.css?v=abcd", "GET", True),
    ("https://docs.python.org/3/_static/style.css?v=abcd&submit=true", "GET", False),
    ("https://docs.python.org/3/_static/style.css?v=not-a-version", "GET", False),
    ("https://docs.python.org/3/tutorial/?v=abcd", "GET", False),
])
def test_public_test_drive_rejects_unsafe_requests(url, method, navigation):
    assert not request_allowed(SITES["python"], url, method, navigation=navigation)
    assert request_allowed(SITES["python"], "https://docs.python.org/3/tutorial/", "GET", navigation=True)
    assert request_allowed(SITES["python"], "https://docs.python.org/_static/style.css", "HEAD", navigation=False)
    assert request_allowed(SITES["python"], "https://docs.python.org/3/_static/style.css?v=abcd1234", "GET", navigation=False)


@pytest.fixture
async def public_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    original_launch, original_context = BrowserType.launch, Browser.new_context
    observed = []

    async def headless(self, **kwargs):
        kwargs["headless"] = True
        return await original_launch(self, **kwargs)

    async def fixture_context(self, **kwargs):
        context = await original_context(self, **kwargs)

        async def fixtures(route):
            url = route.request.url
            selected = next((site for site in SITES.values() if url.startswith(site["url"].split("/", 3)[0] + "//" + site["url"].split("/", 3)[2])), None)
            if selected is None:
                await route.fallback()
                return
            observed.append((route.request.method, url))
            if url == selected["url"]:
                client_navigation = ('document.querySelector("#next").addEventListener("click",event=>{event.preventDefault();history.pushState({},"",' + json.dumps(selected["href"]) + ');document.querySelector("h1").textContent=' + json.dumps(selected["heading"]) + ';});') if selected == SITES["playwright"] else ''
                await route.fulfill(content_type="text/html", body='<html><h1>Documentation</h1><a hidden href="' + selected["href"] + '">Hidden menu link</a><a id="next" href="' + selected["href"] + '">Read the next chapter</a><script>fetch("/3/account",{method:"POST",body:"fixture"}).catch(()=>{});fetch("https://other.example/change",{method:"POST"}).catch(()=>{});' + client_navigation + '</script></html>')
            elif url == urljoin(selected["url"], selected["href"]):
                await route.fulfill(content_type="text/html", body="<h1>" + selected["heading"] + "</h1>")
            else:
                await route.fulfill(status=404, body="Unavailable")

        # The production read-only route is registered later and runs first. Its
        # permitted requests fall through to these offline fixtures instead of
        # using real public websites in repeatable CI.
        await context.route("**/*", fixtures)
        return context

    monkeypatch.setattr(BrowserType, "launch", headless)
    monkeypatch.setattr(Browser, "new_context", fixture_context)
    demo = PublicDemo(tmp_path / "public")
    async with TestServer(demo.app(), host="127.0.0.1") as server:
        demo.base = str(server.make_url("/")).rstrip("/")
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await browser.new_context()
            page = await context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(demo.base)
            try:
                yield page, demo, observed, errors
            finally:
                await demo.cancel()
                await browser.close()
                assert not errors


@pytest.mark.parametrize("site,mode,decision,outcome", [
    ("python", "browse", None, "navigated"),
    ("mdn", "blocked", None, "policy_block"),
    ("playwright", "review", "approved", "navigated"),
    ("python", "review", "rejected", "rejected"),
])
async def test_public_ui_observes_navigation_or_no_effect(public_ui, site, mode, decision, outcome):
    page, demo, observed, _ = public_ui
    await page.locator("#site").select_option(site)
    await page.locator('[data-mode="' + mode + '"]').click()
    project = demo.projects[site]
    if decision:
        for _ in range(150):
            review = next((r for r in Journal(project.root).reviews() if r["run_id"] == demo.state["run"] and r["state"] == "pending"), None)
            if review:
                break
            await asyncio.sleep(.1)
        assert review and review["artifact"]["capture_status"] == "ready"
        assert review["artifact"]["bounding_box"]["width"] > 0
        assert demo.state["link_requests"] == 0
        assert Journal(project.root).decide(review["review_id"], decision)
        assert not Journal(project.root).decide(review["review_id"], decision)
    await expect(page.locator("#status")).to_contain_text("RUN ENDED", timeout=20000)
    assert demo.state["outcome"] == outcome
    expected = urljoin(SITES[site]["url"], SITES[site]["href"])
    assert observed.count(("GET", expected)) == (1 if outcome == "navigated" and site != "playwright" else 0)
    if outcome == "navigated":
        assert demo.state["navigation_kind"] == ("client" if site == "playwright" else "document")
    assert all(method in {"GET", "HEAD"} for method, _ in observed)
    assert demo.state["stopped_requests"] >= 2
    run = next(r for r in Journal(project.root).runs() if r["run_id"] == demo.state["run"])
    assert run["state"] == "ended" and run["pending_reviews"] == 0
    assert await page.locator('[data-mode="review"]').is_enabled()


async def test_public_review_cancellation_and_local_boundary(public_ui):
    page, demo, observed, _ = public_ui
    await page.locator('[data-mode="review"]').click()
    journal = Journal(demo.projects["python"].root)
    for _ in range(150):
        reviews = [r for r in journal.reviews() if r["state"] == "pending"]
        if reviews:
            break
        await asyncio.sleep(.1)
    assert len(reviews) == 1
    async with ClientSession() as client:
        async with client.post(demo.base + "/run", json={"site": "mdn", "mode": "browse"}) as response:
            assert response.status == 409
        async with client.post(demo.base + "/run", json={"site": "python", "mode": "browse"}, headers={"Origin": "https://other.example"}) as response:
            assert response.status == 403
        async with client.post(demo.base + "/run", json={"site": [], "mode": "browse"}) as response:
            assert response.status == 400
    await page.locator("#cancel").click()
    await expect(page.locator("#status")).to_contain_text("RUN CANCELLED", timeout=15000)
    assert journal.review(reviews[0]["review_id"])["state"] == "invalidated"
    assert observed.count(("GET", urljoin(SITES["python"]["url"], SITES["python"]["href"]))) == 0


def test_restart_preserves_policy_edits(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    demo = PublicDemo(tmp_path / "public")
    project = demo.projects["python"]
    data = project.document()
    data["agents"]["browse"]["guard"]["blocked_actions"] = ["click"]
    atomic_json(project.path, data)
    restarted = PublicDemo(tmp_path / "public")
    assert restarted.projects["python"].snapshot("browse").raw["blocked_actions"] == ["click"]
