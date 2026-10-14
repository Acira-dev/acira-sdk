"""New browser-use PDF exports fail closed before any output file is created."""
import asyncio
from importlib import metadata
import pytest
pytest.importorskip("browser_use")
pytest.importorskip("playwright.async_api")
from aiohttp import web
from aiohttp.test_utils import TestServer

@pytest.mark.asyncio
async def test_browser_use_new_pdf_action_fails_closed_without_export(tmp_path, monkeypatch):
    if metadata.version('browser-use') == '0.11.12':
        from browser_use import Tools
        assert 'save_as_pdf' not in Tools().registry.create_action_model().model_fields
        return  # Verified older release has no PDF action; this is not a skipped test.
    monkeypatch.setenv('ANONYMIZED_TELEMETRY', 'false')
    monkeypatch.setenv('BROWSER_USE_CLOUD_SYNC', 'false')
    monkeypatch.setenv('BROWSER_USE_VERSION_CHECK', 'false')
    monkeypatch.setenv('BROWSER_USE_CONFIG_DIR', str(tmp_path / 'browser-use'))
    monkeypatch.setenv('ACIRA_HOME', str(tmp_path / 'private'))
    from browser_use import BrowserSession, Tools
    from browser_use.browser.events import NavigateToUrlEvent
    from browser_use.filesystem.file_system import FileSystem
    from playwright.async_api import async_playwright
    from acira import protect
    from acira.project import initialize
    project=initialize(tmp_path/'project', adapter='browser_use', selected=[], dashboard=False)
    async def index(request):
        return web.Response(text='<html><title>Private report</title><body>Private local content</body></html>', content_type='text/html')
    app=web.Application(); app.router.add_get('/',index)
    async with TestServer(app,host='127.0.0.1') as server, async_playwright() as p:
        async with protect(project=project.root, dashboard=False):
            session=BrowserSession(args=["--password-store=basic", "--use-mock-keychain"], headless=True,executable_path=p.chromium.executable_path,
                user_data_dir=tmp_path/'profile',downloads_path=tmp_path/'downloads',
                enable_default_extensions=False,permissions=[],keep_alive=False,
                chromium_sandbox=True)
            try:
                await asyncio.wait_for(session.start(),30)
                nav=session.event_bus.dispatch(NavigateToUrlEvent(url=str(server.make_url('/'))))
                await asyncio.wait_for(nav.event_result(raise_if_any=True,raise_if_none=False),30)
                tools=Tools();model=tools.registry.create_action_model()
                action=model(save_as_pdf={'file_name':'captured-page.pdf'})
                fs=FileSystem(tmp_path/'output')
                result=await asyncio.wait_for(tools.act(action,browser_session=session,file_system=fs),15)
                assert result.error, result
                assert 'Unclassified CDP mutation' in result.error, result.error
                assert not list((tmp_path/'output').rglob('*.pdf'))
            finally:
                await asyncio.wait_for(session.kill(),10)
