"""Closing a browser invalidates its live review before vendor state saving."""
import asyncio
import contextlib
from contextvars import ContextVar

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

pytest.importorskip("browser_use")
pytest.importorskip("playwright")

from playwright.async_api import async_playwright
from browser_use import BrowserSession
from browser_use.browser.events import NavigateToUrlEvent, ClickElementEvent
from acira import protect
from acira.journal import Journal
from acira.local import atomic_json
from acira.project import initialize
from acira_cdp_proxy.action_gateway import AciraActionBlocked


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["kill", "stop"])
@pytest.mark.parametrize("operation", ["submit", "click"])
async def test_shutdown_invalidates_review_without_browser_effect(tmp_path, monkeypatch, shutdown, operation):
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    monkeypatch.setenv("BROWSER_USE_CLOUD_SYNC", "false")
    monkeypatch.setenv("BROWSER_USE_VERSION_CHECK", "false")
    monkeypatch.setenv("BROWSER_USE_CONFIG_DIR", str(tmp_path / "browser-use"))
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    hits = []
    async def index(request):
        return web.Response(text="""<!doctype html><html><body><button id="pay"
          onclick="fetch('/payment',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({amount:'10'})})">Pay</button></body></html>""", content_type="text/html")
    async def payment(request):
        hits.append(await request.json())
        return web.json_response({"received": True})
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/payment", payment)
    project = initialize(tmp_path / "project", adapter="browser_use", selected=[], dashboard=False)
    document = project.document()
    document["agents"]["default"]["guard"] = {"review_actions": ["click"], "terminal_log": False}
    atomic_json(project.path, document)
    ready = asyncio.Event()
    answer = asyncio.get_running_loop().create_future()
    class Reviewer:
        async def ask(self, request):
            ready.set()
            return await answer
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as p:
        async with protect(project=project.root, dashboard=False, review_backend=Reviewer()) as guard:
            browser = BrowserSession(headless=True, executable_path=p.chromium.executable_path,
                user_data_dir=tmp_path / "profile", downloads_path=tmp_path / "downloads",
                enable_default_extensions=False, permissions=[], keep_alive=False)
            task = None
            try:
                await asyncio.wait_for(browser.start(), 30)
                event = browser.event_bus.dispatch(NavigateToUrlEvent(url=str(server.make_url("/"))))
                await asyncio.wait_for(event.event_result(raise_if_any=True, raise_if_none=False), 30)
                summary = await asyncio.wait_for(browser.get_browser_state_summary(include_screenshot=False), 30)
                node = next(n for n in summary.dom_state.selector_map.values() if n.attributes.get("id") == "pay")
                if operation == "submit":
                    task = asyncio.create_task(guard.submit(browser, node,
                        destination=str(server.make_url("/payment")), values={"amount": "10"}))
                else:
                    clicked = browser.event_bus.dispatch(ClickElementEvent(node=node))
                    task = asyncio.create_task(clicked.event_result(raise_if_any=True, raise_if_none=False))
                await asyncio.wait_for(ready.wait(), 30)
                review = Journal(project.root).reviews()[0]
                assert review["state"] == "pending"
                await asyncio.wait_for(getattr(browser, shutdown)(), 5)
                if operation == "submit":
                    with pytest.raises(AciraActionBlocked, match="cancelled without retry"):
                        await asyncio.wait_for(task, 3)
                else:
                    with pytest.raises(Exception) as failure:
                        await asyncio.wait_for(task, 3)
                    assert not isinstance(failure.value, TimeoutError)
                assert Journal(project.root).review(review["review_id"])["state"] == "invalidated"
                assert not Journal(project.root).decide(review["review_id"], "approved")
                assert answer.cancelled()
                assert hits == [], "A stopped browser must not execute a stale approval"
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
                await asyncio.wait_for(browser.kill(), 10)
        run = Journal(project.root).runs()[0]
        assert run["state"] == "ended" and run["pending_reviews"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_run,termination", [(False, "kill"), (True, "kill"), (False, "transport-loss")],
    ids=["separate-agents", "same-agent-two-browsers", "separate-agent-transport-loss"])
async def test_stopping_one_agent_preserves_another_agents_review(tmp_path, monkeypatch, shared_run, termination):
    for key in ("ANONYMIZED_TELEMETRY", "BROWSER_USE_CLOUD_SYNC", "BROWSER_USE_VERSION_CHECK"):
        monkeypatch.setenv(key, "false")
    monkeypatch.setenv("BROWSER_USE_CONFIG_DIR", str(tmp_path / "browser-use"))
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    hits = []
    async def index(request):
        return web.Response(text="""<!doctype html><html><body><button id="pay"
          onclick="fetch('/payment',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({amount:'10'})})">Pay</button></body></html>""", content_type="text/html")
    async def payment(request):
        hits.append(await request.json())
        return web.json_response({"received": True})
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/payment", payment)
    ready = [asyncio.Event(), asyncio.Event()]
    prepared = [asyncio.Event(), asyncio.Event()]
    answers = [asyncio.get_running_loop().create_future() for _ in range(2)]
    review_index = ContextVar("lifecycle_test_browser", default=0)
    class SharedReviewer:
        async def ask(self, request):
            index = review_index.get()
            ready[index].set()
            return await answers[index]
    stop = asyncio.Event()
    projects = [initialize(tmp_path / str(i), adapter="browser_use", selected=[], dashboard=False) for i in range(2)]
    if shared_run:
        projects[1] = projects[0]
    for project in projects:
        document = project.document()
        document["agents"]["default"]["guard"] = {"terminal_log": False}
        atomic_json(project.path, document)
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as p, contextlib.AsyncExitStack() as stack:
        shared_guard = await stack.enter_async_context(protect(project=projects[0].root, dashboard=False, review_backend=SharedReviewer())) if shared_run else None
        async def worker(index):
            review_index.set(index)
            class Reviewer:
                async def ask(self, request):
                    ready[index].set()
                    return await answers[index]
            context = contextlib.nullcontext(shared_guard) if shared_run else protect(project=projects[index].root, dashboard=False, review_backend=Reviewer())
            async with context as guard:
                browser = BrowserSession(headless=True, executable_path=p.chromium.executable_path,
                    user_data_dir=tmp_path / f"profile-{index}", downloads_path=tmp_path / f"downloads-{index}",
                    enable_default_extensions=False, permissions=[], keep_alive=False)
                task = None
                try:
                    await asyncio.wait_for(browser.start(), 30)
                    event = browser.event_bus.dispatch(NavigateToUrlEvent(url=str(server.make_url("/"))))
                    await asyncio.wait_for(event.event_result(raise_if_any=True, raise_if_none=False), 30)
                    summary = await asyncio.wait_for(browser.get_browser_state_summary(include_screenshot=False), 30)
                    node = next(n for n in summary.dom_state.selector_map.values() if n.attributes.get("id") == "pay")
                    prepared[index].set()
                    await asyncio.wait_for(prepared[1-index].wait(), 30)
                    task = asyncio.create_task(guard.submit(browser, node,
                        destination=str(server.make_url("/payment")), values={"amount": "10"}))
                    await asyncio.wait_for(ready[index].wait(), 30)
                    if index == 0:
                        await asyncio.wait_for(ready[1].wait(), 30)
                        if termination == "kill":
                            await asyncio.wait_for(browser.kill(), 5)
                        else:
                            channel = next(iter(guard._operation_channels.values()))
                            await channel.socket.close()
                        expected = RuntimeError if termination == "transport-loss" else AciraActionBlocked
                        with pytest.raises(expected):
                            await asyncio.wait_for(task, 3)
                        stop.set()
                    else:
                        await asyncio.wait_for(task, 40)
                finally:
                    if task is not None and not task.done():
                        task.cancel()
                        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
                    await asyncio.wait_for(browser.kill(), 10)
        workers = [asyncio.create_task(worker(i)) for i in range(2)]
        stopped = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait([stopped, *workers], timeout=40, return_when=asyncio.FIRST_COMPLETED)
            if stopped not in done:
                for worker_task in done:
                    await worker_task
                raise TimeoutError("No browser shutdown completed within the lifecycle deadline")
            assert not answers[1].done()
            assert sum(review["state"] == "pending" for review in Journal(projects[1].root).reviews()) == 1
            answers[1].set_result("allow")
            await asyncio.wait_for(asyncio.gather(*workers), 10)
            assert hits == [{"amount": "10"}]
            if shared_run:
                assert sorted(review["state"] for review in Journal(projects[0].root).reviews()) == ["approved", "invalidated"]
            else:
                assert Journal(projects[0].root).reviews()[0]["state"] == "invalidated"
                assert Journal(projects[1].root).reviews()[0]["state"] == "approved"
        finally:
            stopped.cancel()
            for task in workers:
                task.cancel()
            await asyncio.wait_for(asyncio.gather(stopped, *workers, return_exceptions=True), 15)
    assert all(Journal(project.root).runs()[0]["state"] == "ended" for project in projects)
