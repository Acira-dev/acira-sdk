"""Captured specific policies guard real button, input and selection effects."""
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from acira import protect
from acira.cli import run
from acira.extension import install_extension
from acira.project import initialize
from acira_cdp_proxy.action_gateway import AciraActionBlocked


@pytest.mark.asyncio
async def test_capture_suppresses_early_handlers_and_guards_five_control_families(tmp_path, monkeypatch):
    api = pytest.importorskip("playwright.async_api")
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    html = '''<title>Local controls fixture</title><main style="width:400px">
      <button id="preview" onpointerdown="document.querySelector('#early').textContent='changed'" onclick="document.querySelector('#outcome').textContent='opened'">Preview details</button>
      <p id="early">unchanged</p><p id="outcome">closed</p>
      <label for="flag">Include notes</label><input id="flag" type="checkbox">
      <label for="choice">Option one</label><input id="choice" type="radio" name="option">
      <label for="mode">View mode</label><select id="mode"><option value="a">Summary</option><option value="b">Details</option></select>
      <label for="notes">Project notes</label><input id="notes" type="text">
    </main>'''
    app = web.Application()
    async def fixture(request):
        return web.Response(text=html, content_type='text/html')
    app.router.add_get('/', fixture)
    extension = install_extension(tmp_path / "extension")
    manifest = json.loads((extension / 'manifest.json').read_text())
    manifest['host_permissions'] = ['http://127.0.0.1/*']
    (extension / 'manifest.json').write_text(json.dumps(manifest))
    identifiers = ['preview', 'flag', 'choice', 'mode', 'notes']
    async with TestServer(app, host='127.0.0.1') as server:
        url = str(server.make_url('/'))
        async with api.async_playwright() as p:
            context = await p.chromium.launch_persistent_context(tmp_path/'profile', channel='chromium', headless=True,
                args=['--disable-extensions-except='+str(extension), '--load-extension='+str(extension)])
            try:
                worker = context.service_workers[0] if context.service_workers else await context.wait_for_event('serviceworker', timeout=10000)
                page = await context.new_page()
                await page.goto(url)
                await worker.evaluate('''async url=>{const tab=(await chrome.tabs.query({})).find(t=>t.url===url);
                  await chrome.scripting.insertCSS({target:{tabId:tab.id},files:['capture.css']});
                  await chrome.scripting.executeScript({target:{tabId:tab.id},files:['content.js']});}''',url)
                for identifier in identifiers:
                    await page.locator('#'+identifier).click()
                    await page.get_by_role('button',name='Block target',exact=True).click()
                assert await page.locator('#early').inner_text() == 'unchanged'
                assert await page.locator('#outcome').inner_text() == 'closed'
                assert not await page.locator('#flag').is_checked() and not await page.locator('#choice').is_checked()
                assert await page.locator('#mode').input_value() == 'a'
                assert await page.locator('#notes').input_value() == ''
                async with page.expect_download() as download:
                    await page.get_by_role('button',name='Download',exact=True).click()
                exported = tmp_path/'controls.json'
                await (await download.value).save_as(exported)
                policy = json.loads(exported.read_text())
                assert len(policy['targets']) == 5
            finally:
                await context.close()
        project = initialize(tmp_path/'project',selected=[],dashboard=False)
        common = ['--project',str(project.root)]
        run(['policy','import',str(exported),*common])
        class Reviewer:
            allow = False
            operations = 0
            async def ask(self, request):
                if request.classification.action == 'mapping_preflight':
                    mappings=request.classification.details.get('cedar',{}).get('skills',{}).get('page_mapping',[])
                    if mappings and all(item.get('candidate_fingerprint',{}).get('id') in identifiers for item in mappings):
                        return 'allow'
                    return 'block'
                self.operations += 1
                assert request.classification.target.id in identifiers
                return 'allow' if self.allow else 'block'
        reviewer = Reviewer()
        async with protect(project=project.root,dashboard=False,review_backend=reviewer) as guard, api.async_playwright() as p:
            guard.patch_playwright(p)
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                async def perform(identifier):
                    target=page.locator('#'+identifier)
                    if identifier in {'flag','choice'}:
                        await target.check()
                    elif identifier == 'mode':
                        await target.select_option('b')
                    elif identifier == 'notes':
                        await target.fill('Public fixture notes')
                    else:
                        await target.click()
                async def unchanged():
                    assert await page.locator('#early').inner_text() == 'unchanged'
                    assert await page.locator('#outcome').inner_text() == 'closed'
                    assert not await page.locator('#flag').is_checked() and not await page.locator('#choice').is_checked()
                    assert await page.locator('#mode').input_value() == 'a' and await page.locator('#notes').input_value() == ''
                await page.goto(url)
                for identifier in identifiers:
                    with pytest.raises(AciraActionBlocked):
                        await perform(identifier)
                    await unchanged()
                assert reviewer.operations == 0
                for index, identifier in enumerate(identifiers):
                    target_id=next(t['id'] for t in policy['targets'] if t['fingerprint']['id']==identifier)
                    run(['policy','configure',policy['id'],'--target',target_id,'--decision','ask',*common])
                    await page.goto(url)
                    reviewer.allow=False
                    with pytest.raises(AciraActionBlocked):
                        await perform(identifier)
                    await unchanged()
                    reviewer.allow=True
                    await perform(identifier)
                    assert reviewer.operations == 2*(index+1)
                    if identifier in {'flag','choice'}:
                        assert await page.locator('#'+identifier).is_checked()
                    elif identifier == 'mode':
                        assert await page.locator('#mode').input_value() == 'b'
                    elif identifier == 'notes':
                        assert await page.locator('#notes').input_value() == 'Public fixture notes'
                    else:
                        assert await page.locator('#early').inner_text() == 'changed'
                        assert await page.locator('#outcome').inner_text() == 'opened'
            finally:
                await browser.close()
