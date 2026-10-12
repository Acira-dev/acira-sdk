"""Anonymous fixture probes for newly introduced public mutation surfaces."""
import asyncio
from importlib import metadata
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright

from acira import protect
from acira.local import atomic_json
from acira.project import initialize
from acira_cdp_proxy.action_gateway import AciraActionBlocked


@asynccontextmanager
async def guarded_fixture(tmp_path, monkeypatch, *, allow_click=False):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", adapter="playwright", selected=[], dashboard=False)
    document = project.document()
    document["agents"]["default"]["guard"] = {
        "blocked_actions": [] if allow_click else ["click", "form_input", "submit", "file_upload", "select", "keyboard", "meaningful_action"],
        "terminal_log": False,
    }
    atomic_json(project.path, document)
    effects = []
    async def index(request):
        return web.Response(text="""<html><body>
        <div id="dropzone" ondragover="event.preventDefault()" ondrop="event.preventDefault();fetch('/effect',{method:'POST',body:'drop'})"
          style="width:200px;height:100px">Drop target</div>
        <script>localStorage.setItem("existing","keep");sessionStorage.setItem("existing","keep")</script>
        <button id="visible" onclick="fetch('/effect',{method:'POST',body:'visible'})">Visible action</button>
        <iframe src="/frame"></iframe>
        </body></html>""", content_type="text/html")
    async def frame(request):
        return web.Response(text="<button id='framed' onclick=\"fetch('/effect',{method:'POST',body:'frame'})\">Frame action</button>", content_type="text/html")
    async def effect(request):
        effects.append(await request.text())
        return web.Response(text="fixture effect observed")
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/frame", frame)
    app.router.add_post("/effect", effect)
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as pw:
        async with protect(project=project.root, dashboard=False) as guard:
            guard.patch_playwright(pw)
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(str(server.make_url("/")))
                yield page, effects, guard
            finally:
                await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["data", "files"])
async def test_uncontracted_locator_drop_has_no_browser_effect(tmp_path, monkeypatch, payload):
    async with guarded_fixture(tmp_path, monkeypatch) as (page, effects, guard):
        blocked = False
        try:
            await page.locator("#dropzone").drop({"data": {"text/plain": "anonymous fixture"}} if payload == "data" else {"files": {"name": "fixture.txt", "mimeType": "text/plain", "buffer": b"anonymous fixture"}})
        except AciraActionBlocked:
            blocked = True
        await asyncio.sleep(.1)
        assert effects == [], f"Unreviewed drop produced browser effect: {effects}"
        assert blocked, "Unsupported Locator.drop was not rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["data", "files", "wait-for-function"])
async def test_new_public_api_fixture_observes_vanilla_effect(payload):
    """Validate that these exact supported upstream calls really mutate the fixture."""
    effects = []
    async def index(request):
        return web.Response(text="""<div id="dropzone" style="width:200px;height:100px"
          ondragover="event.preventDefault()"
          ondrop="event.preventDefault();fetch('/effect',{method:'POST',body:'drop'})">Drop target</div>""", content_type="text/html")
    async def effect(request):
        effects.append(await request.text())
        return web.Response(text="fixture effect observed")
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/effect", effect)
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(str(server.make_url("/")))
            locator = page.locator("#dropzone")
            if payload == "wait-for-function":
                method = getattr(locator, "wait_for_function", None)
                if method is None:
                    assert metadata.version("playwright") in {"1.60.0", "1.61.0"}
                    return
                await method("async el => { await fetch('/effect',{method:'POST',body:'drop'}); return true; }")
            else:
                await locator.drop({"data": {"text/plain": "anonymous fixture"}} if payload == "data" else {"files": {"name": "fixture.txt", "mimeType": "text/plain", "buffer": b"anonymous fixture"}})
            for _ in range(50):
                if effects:
                    break
                await asyncio.sleep(.01)
            assert effects == ["drop"]
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_locator_wait_for_function_cannot_execute_unreviewed_script(tmp_path, monkeypatch):
    async with guarded_fixture(tmp_path, monkeypatch) as (page, effects, guard):
        method = getattr(page.locator("#dropzone"), "wait_for_function", None)
        if method is None:
            assert metadata.version("playwright") in {"1.60.0", "1.61.0"}
            return
        with pytest.raises(AciraActionBlocked):
            await method("async el => { await fetch('/effect',{method:'POST',body:'drop'}); return true; }")
        await asyncio.sleep(.1)
        assert effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["local_storage", "session_storage"])
@pytest.mark.parametrize("method", ["set_item", "remove_item", "clear"])
async def test_uncontracted_storage_write_has_no_browser_effect(tmp_path, monkeypatch, kind, method):
    async with guarded_fixture(tmp_path, monkeypatch) as (page, effects, guard):
        storage = getattr(page, kind, None)
        if storage is None:
            assert metadata.version("playwright") == "1.60.0"
            return
        blocked = False
        try:
            await getattr(storage, method)(*({"set_item": ("fixture-record", "anonymous unreviewed write"), "remove_item": ("existing",), "clear": ()}[method]))
        except AciraActionBlocked:
            blocked = True
        assert await storage.get_item("fixture-record") is None, "Unreviewed storage write changed browser state"
        assert await storage.get_item("existing") == "keep"
        assert await storage.items() == [{"name": "existing", "value": "keep"}]
        assert blocked, f"Unsupported {kind}.{method} was not rejected"
        assert effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["install", "create", "delete", "get"])
async def test_uncontracted_virtual_credential_create_is_rejected(tmp_path, monkeypatch, method):
    async with guarded_fixture(tmp_path, monkeypatch) as (page, effects, guard):
        credentials = getattr(page.context, "credentials", None)
        if credentials is None:
            assert metadata.version("playwright") == "1.60.0"
            return
        original_get = next(saved for obj, name, saved in guard._patched_attrs if obj is credentials and name == "get")
        blocked = False
        try:
            await getattr(credentials, method)(*({"install": (), "create": ("localhost",), "delete": ("fixture-no-credential",), "get": ()}[method]))
        except AciraActionBlocked:
            blocked = True
        assert blocked, f"Unsupported virtual credential {method} was not rejected"
        assert await original_get() == []
        assert effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("allow", [False, True])
@pytest.mark.parametrize("target", ["visible", "across-frame"])
async def test_new_locator_paths_keep_guarded_boundary(tmp_path, monkeypatch, allow, target):
    async with guarded_fixture(tmp_path, monkeypatch, allow_click=allow) as (page, effects, guard):
        if target == "visible":
            locator = page.locator("#visible")
            locator = getattr(locator, "visible", locator)
        else:
            locator = (page.frame_locator() if metadata.version("playwright") == "1.63.0" else page.frame_locator("iframe")).get_by_role("button", name="Frame action")
        if allow:
            await locator.click()
        else:
            with pytest.raises((AciraActionBlocked, RuntimeError), match="block|Block|denied|Denied"):
                await locator.click()
        await asyncio.sleep(.1)
        assert effects == (["visible" if target == "visible" else "frame"] if allow else [])
