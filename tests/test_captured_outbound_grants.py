"""Actual extension captures cannot turn outbound controls into routine grants."""
import asyncio
import html
import json
import sys
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

pytest.importorskip('playwright.async_api')
from playwright.async_api import async_playwright

from acira import protect
from acira.cli import run
from acira.extension import install_extension
from acira.journal import Journal
from acira.project import initialize
from acira.adapters.playwright_boundary import trusted
from acira_cdp_proxy.action_gateway import AciraActionBlocked


@asynccontextmanager
async def captured_outbound_site(identity):
    received=[]
    label='aria-label="Send"' if identity=='icon' else 'aria-label="Continue"'
    identifier={'icon':'glyph','underscore-id':'send_email','input-value':'control'}[identity]
    code="window.attempts++;document.querySelector('#attempts').textContent=String(window.attempts);fetch('/outbound',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({message:'Local fixture draft'})})"
    async def index(request):
        control=('<input id="'+identifier+'" type="button" value="Send" onclick="'+html.escape(code,quote=True)+'">'
          if identity=='input-value' else '<button id="'+identifier+'" type="button" '+label+' onclick="'+html.escape(code,quote=True)+'"><svg aria-hidden="true" width="16" height="16"><path d="M1 1L15 8L1 15Z"/></svg></button>')
        return web.Response(content_type='text/html',text='<title>Local captured outbound control</title><script>window.attempts=0</script>'+control+'<output id="attempts">0</output>')
    async def outbound(request):
        received.append({'method':request.method,'path':request.path,'body':await request.json()})
        return web.Response(text='Local fixture only')
    app=web.Application();app.router.add_get('/',index);app.router.add_post('/outbound',outbound)
    async with TestServer(app,host='127.0.0.1') as server:
        yield server,received,identifier


async def capture_and_import(project, tmp_path, url, identifier, select_capture_target):
    extension=install_extension(tmp_path/'extension')
    manifest=json.loads((extension/'manifest.json').read_text())
    assert 'debugger' not in manifest.get('permissions',[])
    manifest['host_permissions']=['http://127.0.0.1/*']
    (extension/'manifest.json').write_text(json.dumps(manifest))
    async with async_playwright() as p:
        context=await p.chromium.launch_persistent_context(tmp_path/'capture-profile',channel='chromium',headless=True,
            chromium_sandbox=True,service_workers='block',args=['--disable-extensions-except='+str(extension),'--load-extension='+str(extension)])
        try:
            worker=context.service_workers[0] if context.service_workers else await context.wait_for_event('serviceworker',timeout=10000)
            page=await context.new_page();await page.goto(url)
            await worker.evaluate('''async url=>{const tab=(await chrome.tabs.query({})).find(tab=>tab.url===url);
              await chrome.scripting.insertCSS({target:{tabId:tab.id},files:['capture.css']});
              await chrome.scripting.executeScript({target:{tabId:tab.id},files:['content.js']});}''',page.url)
            await select_capture_target(page,page.locator('#'+identifier))
            assert await page.evaluate('window.attempts')==0
            await page.get_by_role('button',name='Ask first',exact=True).click()
            async with page.expect_download() as downloaded:
                await page.get_by_role('button',name='Download',exact=True).click()
            exported=tmp_path/'captured-outbound.json';await (await downloaded.value).save_as(exported)
            captured=json.loads(exported.read_text());target=captured['targets'][0]
            assert target['decision']=='ask' and target['fingerprint']['id']==identifier
        finally:
            primary=sys.exc_info()[1]
            try:await asyncio.wait_for(context.close(),10)
            except BaseException as error:
                if primary is None:raise
                primary.add_note('Owned capture cleanup failed: '+type(error).__name__)
    run(['policy','import',str(exported),'--project',str(project.root)])
    run(['policy','validate','--project',str(project.root)])
    documents=list(project.snapshot('default').documents.values())
    assert len(documents)==1 and documents[0]['schema']=='acira.specific_skill.v2'
    return captured


class RejectReusableOutboundApproval:
    def __init__(self,project):
        self.journal=Journal(project.root);self.reviews=[]
    async def ask(self,request):
        pending=[r for r in self.journal.reviews() if r['state']=='pending']
        assert len(pending)==1
        review=pending[0];self.reviews.append(review)
        assert review['grant_eligible'] is False
        assert review['evidence']['critical'] is True
        assert 'grant_scope' not in review['evidence']
        with pytest.raises(ValueError,match='cannot receive a reusable grant'):
            self.journal.decide(review['review_id'],'approved',grant=True)
        assert self.journal.review(review['review_id'])['state']=='pending'
        assert self.journal.decide(review['review_id'],'rejected')
        return 'block'


def assert_two_independent_rejections(reviewer,received,identifier):
    assert len(reviewer.reviews)==2
    ids=[review['review_id'] for review in reviewer.reviews]
    assert len(set(ids))==2
    assert all(reviewer.journal.review(identifier)['state']=='rejected' for identifier in ids)
    assert received==[]
    with reviewer.journal.db() as db:
        assert db.execute('SELECT COUNT(*) FROM grants').fetchone()[0]==0
    events=reviewer.journal.events(limit=1000)
    assert not any(e['type'] in {'grant_created','grant_used'} for e in events)
    evaluations=[e for e in events if e['type']=='action_evaluated' and e.get('classification',{}).get('action')=='click'
        and e['classification'].get('target',{}).get('id')==identifier]
    assert len(evaluations)==2 and all(e['decision']=='block' for e in evaluations)
    operation_ids={e['operation_id'] for e in evaluations}
    assert not any(e['type']=='action_dispatched' and e.get('operation_id') in operation_ids for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize('adapter',['playwright','browser_use'])
@pytest.mark.parametrize('identity',['icon','underscore-id','input-value'])
async def test_captured_only_outbound_control_requires_fresh_review_and_rejects_grant(tmp_path,monkeypatch,select_capture_target,adapter,identity):
    monkeypatch.setenv('ACIRA_HOME',str(tmp_path/'private'))
    monkeypatch.setenv('ANONYMIZED_TELEMETRY','false')
    monkeypatch.setenv('BROWSER_USE_CLOUD_SYNC','false')
    monkeypatch.setenv('BROWSER_USE_CONFIG_DIR',str(tmp_path/'browser-use'))
    project=initialize(tmp_path/'existing',adapter=adapter,selected=[],dashboard=False)
    async with captured_outbound_site(identity) as (server,received,identifier):
        url=str(server.make_url('/'))
        await capture_and_import(project,tmp_path,url,identifier,select_capture_target)
        assert received==[]
        reviewer=RejectReusableOutboundApproval(project)
        async with protect(project=project.root,dashboard=False,review_backend=reviewer) as guard,async_playwright() as p:
            if adapter=='playwright':
                guard.patch_playwright(p);browser=await p.chromium.launch(chromium_sandbox=True)
                try:
                    page=await browser.new_page();await page.goto(url)
                    for _ in range(2):
                        with pytest.raises(AciraActionBlocked):
                            await page.locator('#'+identifier).click()
                        assert await trusted(page,'evaluate','window.attempts')==0 and received==[]
                finally:
                    primary=sys.exc_info()[1]
                    try:await asyncio.wait_for(browser.close(),10)
                    except BaseException as error:
                        if primary is None:raise
                        primary.add_note('Owned browser cleanup failed: '+type(error).__name__)
            else:
                pytest.importorskip('browser_use')
                from browser_use import BrowserSession
                from browser_use.browser.events import NavigateToUrlEvent,ClickElementEvent
                session=BrowserSession(args=["--password-store=basic", "--use-mock-keychain"], headless=True,executable_path=p.chromium.executable_path,chromium_sandbox=True,
                    user_data_dir=tmp_path/'profile',downloads_path=tmp_path/'downloads',enable_default_extensions=False,permissions=[],keep_alive=False)
                try:
                    await asyncio.wait_for(session.start(),30)
                    navigation=session.event_bus.dispatch(NavigateToUrlEvent(url=url))
                    await asyncio.wait_for(navigation.event_result(raise_if_any=True,raise_if_none=False),30)
                    for _ in range(2):
                        summary=await asyncio.wait_for(session.get_browser_state_summary(include_screenshot=False),30)
                        node=next(value for value in summary.dom_state.selector_map.values() if value.attributes.get('id')==identifier)
                        clicked=session.event_bus.dispatch(ClickElementEvent(node=node))
                        result=await asyncio.wait_for(clicked.event_result(raise_if_any=True),10)
                        assert result['acira']['decision'].startswith('block') and 'validation_error' in result
                        state=await guard._send_browser_use_cdp(session,'Runtime.evaluate',{
                            'expression':"document.querySelector('#attempts').textContent",'returnByValue':True})
                        assert state['result']['value']=='0' and received==[]
                finally:
                    primary=sys.exc_info()[1]
                    try:await asyncio.wait_for(session.kill(),10)
                    except BaseException as error:
                        if primary is None:raise
                        primary.add_note('Owned browser-use cleanup failed: '+type(error).__name__)
        assert_two_independent_rejections(reviewer,received,identifier)
