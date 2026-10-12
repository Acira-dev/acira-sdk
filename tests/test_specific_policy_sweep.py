"""The opt-in live tester may approve only an exact captured public link."""
import copy
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_tools = Path(__file__).resolve().parents[1] / "tools"
_spec = importlib.util.spec_from_file_location("acira_specific_sweep_tests", _tools / "specific_policy_sweep.py")
sweep = importlib.util.module_from_spec(_spec)
sys.path.insert(0, str(_tools))
try:
    _spec.loader.exec_module(sweep)
finally:
    sys.path.remove(str(_tools))


def review_fixture():
    original = {"text": "DOCUMENTATION", "aria_label": "API documentation"}
    policy = {"id": "captured-policy", "targets": [{"id": "docs-link", "fingerprint": original}]}
    mapping = {"skill_id": policy["id"], "target_id": "docs-link", "evidence_gate": {"concrete": True},
               "candidate_fingerprint": {"url": "https://fixture.invalid/", "href": "/guide", "text": "API documentation"}}
    return policy, mapping


def request_for(mappings):
    return SimpleNamespace(classification=SimpleNamespace(action="mapping_preflight",
        details={"cedar": {"skills": {"page_mapping": mappings}}}))


@pytest.mark.asyncio
async def test_review_accepts_exact_accessible_label_with_exact_public_destination():
    policy, mapping = review_fixture()
    reviewer = sweep.CaptureReviewer(policy, "https://fixture.invalid/guide")
    assert await reviewer.ask(request_for([mapping])) == "allow"
    assert reviewer.mapping_reviews == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["destination", "fragment", "label", "redacted", "policy", "target", "evidence", "multiple", "empty"])
async def test_mapping_test_reviewer_rejects_changed_or_ambiguous_evidence(change):
    policy, mapping = review_fixture()
    mappings = [mapping]
    if change == "destination":
        mapping["candidate_fingerprint"]["href"] = "/different-destination"
    elif change == "fragment":
        mapping["candidate_fingerprint"]["href"] = "/guide#another-operation"
    elif change in {"label", "redacted"}:
        mapping["candidate_fingerprint"]["text"] = "Different operation" if change == "label" else "[REDACTED]"
    elif change in {"policy", "target"}:
        mapping["skill_id" if change == "policy" else "target_id"] = "another-identity"
    elif change == "evidence":
        mapping["evidence_gate"]["concrete"] = False
    elif change == "multiple":
        mappings.append(copy.deepcopy(mapping))
    else:
        mappings = []
    reviewer = sweep.CaptureReviewer(policy, "https://fixture.invalid/guide")
    assert await reviewer.ask(request_for(mappings)) == "block"
    assert reviewer.mapping_reviews == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_selector", [False, True])
async def test_live_capture_driver_keeps_page_fence_and_loads_only_local_selector_assets(tmp_path, monkeypatch, missing_selector):
    from playwright.async_api import async_playwright
    original = sweep.safe.Fence._route
    requests = []
    async def fixture(self, route):
        if route.request.url == "https://fixture.invalid/":
            requests.append((route.request.method, route.request.url))
            return await route.fulfill(content_type="text/html", body='''<h1>Public guide index</h1>
              <a href="/guide#overview"><span>Guide</span></a><script>
                window.addEventListener('click',e=>{if(e.target.closest('a')) history.pushState(null,'','/activated')},true);
              </script>''')
        return await original(self, route)
    monkeypatch.setattr(sweep.safe.Fence, "_route", fixture)
    if missing_selector:
        install = sweep.install_extension
        def broken_selector(destination):
            directory = install(destination)
            (directory / "selection.js").unlink()
            return directory
        monkeypatch.setattr(sweep, "install_extension", broken_selector)
    async with async_playwright() as p:
        result, policy = await sweep.capture(p, {"url":"https://fixture.invalid/"},
            "https://fixture.invalid/guide#overview", tmp_path, timeout=2000, robots=None,
            pacer=sweep.safe.DocumentPacer(.01), headed=False)
    if missing_selector:
        assert result["status"] == "capture_selector_unavailable" and policy is None
        assert "Selection is paused" in result["selector_status"]
    else:
        assert result["status"] == "verified" and result["capture_selection_document_requests"] == 0
        assert policy["targets"][0]["fingerprint"]["href"] == "/guide"
        assert policy["scope"]["url_patterns"] == ["https://fixture.invalid/"]
    assert requests == [("GET", "https://fixture.invalid/")]


@pytest.mark.asyncio
async def test_duplicate_destination_keeps_one_control_through_capture_import_block_and_review(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright
    from acira import protect
    from acira.project import initialize
    from acira.journal import Journal
    monkeypatch.setenv("ACIRA_HOME",str(tmp_path / "private"))
    source_loads=0
    async def fixture(self, route):
        nonlocal source_loads
        assert route.request.method == "GET"
        if route.request.is_navigation_request():
            self.navigations.append(route.request.url)
        self.allowed += 1
        if route.request.url.endswith('/'):
            source_loads+=1
            hidden='style="display:none"' if source_loads==1 else ''
            body = f'''<h1>Public docs</h1><a id="start" href="/guide" {hidden}>GET STARTED</a>
              <a id="docs" href="/guide">Docs</a>'''
        else:
            body='<h1>Public guide</h1>'
        await route.fulfill(content_type="text/html",body=body)
    monkeypatch.setattr(sweep.safe.Fence,"_route",fixture)
    site={"url":"https://fixture.invalid/"}
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        try:
            baseline=await sweep.safe.bounded_case(browser,site,timeout=3000)
            assert baseline["status"]=="verified" and baseline["target_text"]=="Docs"
            # The newly visible first link has the same destination. Selecting
            # by URL alone would silently switch the captured control.
            page=await browser.new_page()
            await page.route("**/*",lambda route:fixture(sweep.safe.Fence(site["url"]),route))
            await page.goto(site["url"])
            duplicate,_=await sweep.safe.ready_link(page,baseline["target_url"])
            assert await duplicate[0].get_attribute("id")=="start"
            await page.close()
            capture,policy=await sweep.capture(p,site,baseline["target_url"],tmp_path,timeout=3000,
                robots=None,pacer=sweep.safe.DocumentPacer(.01),headed=False,
                expected_control=baseline["target_control_sha256"])
        finally:
            await browser.close()
    assert capture["status"]=="verified" and policy["targets"][0]["fingerprint"]["id"]=="docs"
    assert capture["target_control_sha256"]==baseline["target_control_sha256"]
    project=initialize(tmp_path / "project",selected=[],dashboard=False)
    export=tmp_path / "capture.json";export.write_text(__import__('json').dumps(policy))
    options=["--project",str(project.root)]
    sweep.cli("policy","import",str(export),*options)
    sweep.cli("policy","import",str(export),*options)
    assert len(project.policies("default"))==1
    reviewer=sweep.CaptureReviewer(policy,baseline["target_url"])
    async with protect(project=project.root,dashboard=False,review_backend=reviewer) as guard,async_playwright() as p:
        guard.patch_playwright(p)
        browser=await p.chromium.launch()
        try:
            for mode,decision in (("blocked","block"),("rejected","ask"),("approved","ask")):
                sweep.cli("policy","configure",policy["id"],"--target",policy["targets"][0]["id"],"--decision",decision,*options)
                case=await sweep.safe.bounded_case(browser,site,timeout=3000,mode=mode,guard=guard,
                    journal=Journal(project.root),reviewer=reviewer,expected=baseline["target_url"],
                    expected_landing=baseline["landing_url"],expected_control=capture["target_control_sha256"])
                assert case["status"]=="verified",case
                assert case["target_text"]=="Docs" and case["target_control_sha256"]==capture["target_control_sha256"]
                assert case["click_dispatches"]==(1 if mode=="approved" else 0)
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed",["label","identity","position"])
async def test_same_destination_does_not_substitute_a_changed_control(changed):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser=await p.chromium.launch()
        try:
            page=await browser.new_page()
            await page.route("**/*",lambda route:route.fulfill(content_type="text/html",body='''
              <a id="docs" href="/guide">Docs</a><a id="other" href="/guide">Docs</a>
              <script>window.inputs=0;addEventListener('click',()=>inputs++)</script>'''))
            await page.goto("https://fixture.invalid/")
            first,_=await sweep.safe.ready_link(page)
            control=(await sweep.safe.link_control_identity(first[0]))["sha256"]
            await page.evaluate("""change=>{const el=document.querySelector('#docs');
              if(change==='label')el.textContent='Learn';
              else if(change==='identity')el.id='replacement';
              else el.before(document.createElement('a'));
            }""",changed)
            selected,diagnostics=await sweep.safe.ready_link(page,"https://fixture.invalid/guide",expected_control=control)
            assert selected is None and diagnostics["target_identity_mismatch"]
            assert page.url=="https://fixture.invalid/" and await page.evaluate("inputs")==0
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_capture_cleanup_failure_preserves_export_stage_and_retires_its_browser(tmp_path, monkeypatch):
    from playwright.async_api import async_playwright
    async def fixture(self,route):
        await route.fulfill(content_type="text/html",body='<h1>Public docs</h1><a id="docs" href="/guide">Docs</a>')
    async def failed_cleanup(context,fence):
        raise TimeoutError("Local fixture teardown failure")
    monkeypatch.setattr(sweep.safe.Fence,"_route",fixture)
    monkeypatch.setattr(sweep.safe,"close_context",failed_cleanup)
    progress={};contexts=[]
    async with async_playwright() as p:
        original=p.chromium.launch_persistent_context
        async def tracked(*args,**kwargs):
            context=await original(*args,**kwargs);contexts.append(context);return context
        monkeypatch.setattr(p.chromium,"launch_persistent_context",tracked)
        with pytest.raises(sweep.CaptureCleanupError,match="worker must stop"):
            await sweep.capture(p,{"url":"https://fixture.invalid/"},"https://fixture.invalid/guide",tmp_path,
                timeout=3000,robots=None,pacer=sweep.safe.DocumentPacer(.01),headed=False,progress=progress)
        assert progress["last_operation_stage"]=="exports_verified"
        assert progress["cleanup_status"]=="failed" and progress["cleanup_error"]=="TimeoutError"
        assert progress["emergency_browser_close"]=="complete"
        assert len(contexts)==1 and not contexts[0].browser.is_connected()
