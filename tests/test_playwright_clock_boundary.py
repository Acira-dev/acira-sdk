"""Clock instrumentation must not accelerate unreviewed browser effects."""
import asyncio
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright
from acira.adapters.playwright import PlaywrightCDPProxy
from acira_cdp_proxy.action_gateway import AciraActionBlocked


@pytest.mark.asyncio
async def test_uncontracted_clock_cannot_trigger_blocked_click_effect(tmp_path, monkeypatch):
    monkeypatch.setenv('ACIRA_HOME', str(tmp_path / 'private'))
    effects = []
    async def index(request):
        return web.Response(text="""<button id='act' onclick="fetch('/effect',{method:'POST'})">Action</button>
        <script>setTimeout(()=>document.getElementById('act').click(),86400000)</script>""", content_type='text/html')
    async def effect(request):
        effects.append(True)
        return web.Response(text='local fixture effect')
    app=web.Application();app.router.add_get('/',index);app.router.add_post('/effect',effect)
    async with TestServer(app,host='127.0.0.1') as server, async_playwright() as playwright:
        async with PlaywrightCDPProxy(config={'terminal_log':False,'blocked_actions':['click']}) as guard:
            guard.patch_playwright(playwright)
            browser=await playwright.chromium.launch(headless=True,chromium_sandbox=True)
            try:
                page=await browser.new_page()
                blocked=False
                try:
                    await page.clock.install()
                except AciraActionBlocked:
                    blocked=True
                await page.goto(str(server.make_url('/')))
                if not blocked:
                    await asyncio.wait_for(page.clock.run_for(172800000),15)
                await asyncio.sleep(.1)
                assert effects == [], f'Clock instrumentation produced {len(effects)} unreviewed browser effect(s)'
                assert blocked, 'Uncontracted Clock.install was not rejected'
            finally:await browser.close()


@pytest.mark.asyncio
async def test_clock_rejection_is_context_scoped_and_restored(tmp_path, monkeypatch):
    monkeypatch.setenv('ACIRA_HOME', str(tmp_path / 'private'))
    effects = []
    async def index(request):
        return web.Response(text="""<button id='act' onclick="fetch('/effect',{method:'POST'})">Action</button>
        <script>setTimeout(()=>document.getElementById('act').click(),86400000)</script>""", content_type='text/html')
    async def effect(request):
        effects.append(True)
        return web.Response(text='local fixture effect')
    app=web.Application();app.router.add_get('/',index);app.router.add_post('/effect',effect)
    async with TestServer(app,host='127.0.0.1') as server, async_playwright() as playwright:
        browser=await playwright.chromium.launch(headless=True,chromium_sandbox=True)
        try:
            protected_context=await browser.new_context()
            page=await protected_context.new_page()
            unrelated_context=await browser.new_context()
            unrelated=await unrelated_context.new_page()
            saved_install=protected_context.clock.install
            async with PlaywrightCDPProxy(config={'terminal_log':False,'blocked_actions':['click']}) as guard:
                guard.patch_page(page)
                assert page.clock is protected_context.clock
                for source in (page, protected_context):
                    for method, args in {'install':(), 'fast_forward':(172800000,), 'pause_at':('2030-01-01T00:00:00',),
                                         'resume':(), 'run_for':(172800000,), 'set_fixed_time':(0,), 'set_system_time':(0,)}.items():
                        with pytest.raises(AciraActionBlocked,match='Clock instrumentation'):
                            await getattr(source.clock,method)(*args)
                await page.goto(str(server.make_url('/')))
                assert effects == []
                await unrelated.clock.install()
                await unrelated.goto(str(server.make_url('/')))
                await unrelated.clock.run_for(172800000)
                await asyncio.sleep(.1)
                assert effects == [True], 'Unrelated browser context clock was incorrectly modified'
            assert protected_context.clock.install == saved_install
            await protected_context.clock.install()
            await page.reload()
            await protected_context.clock.run_for(172800000)
            await asyncio.sleep(.1)
            assert effects == [True, True], 'Guard shutdown failed to restore caller-owned context clock'
        finally:await browser.close()
