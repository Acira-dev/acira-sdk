"""A direct Page factory must not bypass its owning context's guard."""
from importlib import metadata

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright

from acira.adapters.playwright import PlaywrightCDPProxy
from acira_cdp_proxy.action_gateway import AciraActionBlocked


@pytest.fixture
async def local_effect_page():
    effects = []
    async def index(request):
        return web.Response(text='<button id="act" onclick="fetch(\'/effect\',{method:\'POST\'})">Act</button>', content_type="text/html")
    async def effect(request):
        effects.append(True)
        return web.Response(text="local fixture effect")
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/effect", effect)
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(str(server.make_url("/")))
            yield browser, context, page, server, effects
        finally:
            await browser.close()


def guard():
    return PlaywrightCDPProxy(config={"blocked_actions": ["click"], "terminal_log": False, "terminal_review": False})


@pytest.mark.asyncio
async def test_direct_page_guards_context_scripts_and_future_pages(local_effect_page):
    browser, context, page, server, effects = local_effect_page
    async with guard() as protected:
        protected.patch_page(page)
        with pytest.raises(AciraActionBlocked, match="Context script"):
            await context.add_init_script("fetch('/effect',{method:'POST'})")
        await page.reload()
        another = await context.new_page()
        await another.goto(str(server.make_url("/")))
        with pytest.raises(AciraActionBlocked):
            await another.locator("#act").click()
        assert effects == []
    # Detaching an explicit Page does not close the caller-owned browser.
    await page.locator("#act").click()
    await page.wait_for_timeout(50)
    assert effects == [True]


@pytest.mark.asyncio
async def test_direct_page_guards_credential_surface_and_restores_it(local_effect_page):
    browser, context, page, server, effects = local_effect_page
    credentials = getattr(context, "credentials", None)
    if credentials is None:
        assert metadata.version("playwright") == "1.60.0"
        return
    create = credentials.create
    async with guard() as protected:
        protected.patch_page(page)
        with pytest.raises(AciraActionBlocked, match="Virtual credential"):
            await context.credentials.create("localhost")
        assert effects == []
    assert credentials.create == create
    await credentials.create("localhost")
    assert len(await credentials.get()) == 1


@pytest.mark.asyncio
async def test_direct_page_rejects_shared_context_run_ownership(local_effect_page):
    browser, context, page, server, effects = local_effect_page
    other = await context.new_page()
    await other.goto(str(server.make_url("/")))
    async with guard() as first:
        first.patch_page(page)
        async with guard() as second:
            with pytest.raises(ValueError, match="another Acira run"):
                second.patch_page(other)
        with pytest.raises(AciraActionBlocked):
            await page.locator("#act").click()
        with pytest.raises(AciraActionBlocked):
            await other.locator("#act").click()
        assert effects == []


@pytest.mark.asyncio
async def test_unrelated_context_keeps_normal_public_methods(local_effect_page):
    browser, context, page, server, effects = local_effect_page
    other_context = await browser.new_context()
    other = await other_context.new_page()
    await other.goto(str(server.make_url("/")))
    async with guard() as protected:
        protected.patch_page(page)
        if hasattr(other, "local_storage"):
            await other.local_storage.set_item("fixture", "anonymous value")
            assert await other.local_storage.get_item("fixture") == "anonymous value"
        await other.locator("#act").click()
        await other.wait_for_timeout(50)
        assert effects == [True]
        with pytest.raises(AciraActionBlocked):
            await page.locator("#act").click()
        assert effects == [True]
