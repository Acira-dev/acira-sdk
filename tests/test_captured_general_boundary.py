"""An in-scope capture never exempts other controls from general protections.

The reported Archive capture / unrelated Delete bypass used the page-mapping
allow branch. Exercise that actual branch, terminal import, both adapters, local
DOM/HTTP effects and replay without treating an approval as business success.
"""
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from acira import protect
from acira.cli import run
from acira.extension import install_extension
from acira.journal import Journal
from acira.project import catalog_policy, initialize, read_json, validate_policy
from acira.replay import evaluate, revision_config
from acira.revisions import rollback
from acira.runtime import ProjectRuntime
from acira_cdp_proxy.action_gateway import ActionGateway, ActionRequest, AciraActionBlocked
from acira_cdp_proxy.config import AciraGuardConfig, AciraGuardRule
from acira_cdp_proxy.facts import build_browser_event_facts
from acira_cdp_proxy.general_skills import GeneralSkill
from acira_cdp_proxy.guard import AciraGuardPolicy, GuardReviewRequest, GuardTarget, _event_facts_from_classification
from acira_cdp_proxy.policy import AGENT_TO_BROWSER, CDPMessageContext
from acira_cdp_proxy.skills import SpecificSkill

GENERAL_ID = "acira.general.destructive_action"
RISK = "destructive.action_attempt"
CASES = [(capture, scope) for capture in ("ask", "block") for scope in ("exact", "broad")]


def legacy_capture(identity_form):
    value = {"schema": "acira.specific_skill.v2", "name": "Legacy named capture",
        "scope": {"url_patterns": ["https://fixture.invalid/*"]},
        "targets": [{"id": "guide", "decision": "block",
            "fingerprint": {"tag": "a", "id": "guide", "href": "/guide", "text": "Guide"}}]}
    if identity_form == "padded-id":
        value["id"] = "  legacy-capture  "
    return value


def frozen_guide_event(config):
    policy = AciraGuardPolicy(config)
    gateway = ActionGateway(policy=policy)
    classification = gateway._classification_from_request(ActionRequest(action="click", framework="playwright",
        target=GuardTarget(tag="a", id="guide", href="/guide", text="Guide", url="https://fixture.invalid/settings",
            confidence=1), target_resolved=True, confidence=1))
    return {"evaluation_contract": "acira.action_facts.v1", "facts": build_browser_event_facts(classification).to_dict(),
        "specific_facts": _event_facts_from_classification(classification),
        "classification": classification.to_metadata(), "metadata": {"source": "specific_skill"}}


@pytest.mark.parametrize("identity_form", ["name-only", "padded-id"])
def test_legacy_capture_identity_survives_terminal_revision_and_replay(tmp_path, monkeypatch, capsys, identity_form):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", selected=[], dashboard=False)
    document = legacy_capture(identity_form)
    kind, policy_id = validate_policy(document)
    assert kind == "specific" and policy_id == ("legacy-capture" if identity_form == "padded-id" else document["name"])
    source = tmp_path / "capture.json"
    source.write_text(json.dumps(document))
    common = ["--project", str(project.root)]
    run(["policy", "import", str(source), *common])
    run(["policy", "import", str(source), *common])
    entry, = project.policies("default")
    assert entry["id"] == policy_id and entry["document"] == document
    path = Path(entry["path"])
    original_revision = project.snapshot().revision
    original = revision_config(project, "default", original_revision)
    assert original.specific_skills[0].id == policy_id
    assert original.specific_skills[0].source == str(path)
    assert evaluate(original, frozen_guide_event(original))["decision"] == "block"
    capsys.readouterr()
    run(["policy", "list", "--json", *common])
    listed, = json.loads(capsys.readouterr().out)["policies"]
    assert listed["id"] == policy_id and listed["name"] == document["name"]
    run(["policy", "configure", policy_id, "--target", "guide", "--decision", "ask", *common])
    configured = read_json(path)
    assert configured.get("id") == document.get("id") and configured["name"] == document["name"]
    configured_revision = project.snapshot().revision
    assert configured_revision != original_revision
    configured_config = revision_config(project, "default", configured_revision)
    replay = evaluate(configured_config, frozen_guide_event(configured_config))
    assert replay["decision"] == "ask" and replay["policy_id"] == policy_id + ":guide"
    # Reimport is idempotent for the same saved content and cannot silently undo
    # configuration with an older capture that has the same parsed identity.
    source.write_text(json.dumps(configured))
    run(["policy", "import", str(source), *common])
    assert project.snapshot().revision == configured_revision and len(project.policies("default")) == 1
    source.write_text(json.dumps(document))
    with pytest.raises(SystemExit) as error:
        run(["policy", "import", str(source), *common])
    assert error.value.code == 2 and read_json(path) == configured
    run(["policy", "disable", policy_id, *common])
    disabled_revision = project.snapshot().revision
    assert project.snapshot().config.specific_skills == ()
    assert revision_config(project, "default", disabled_revision).specific_skills == ()
    run(["policy", "enable", policy_id, *common])
    assert project.snapshot().config.specific_skills[0].id == policy_id
    restored = rollback(project, "default", original_revision, expected=project.snapshot().revision)
    assert restored["revision"] != original_revision and read_json(path) == document
    assert evaluate(revision_config(project, "default", restored["revision"]), frozen_guide_event(original))["decision"] == "block"


def test_legacy_and_explicit_identity_collision_is_rejected_before_write(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", selected=[], dashboard=False)
    first = legacy_capture("name-only")
    project.save_policy("default", "legacy.json", first, expected=None)
    before = project.snapshot().revision
    same_identity = {**first, "id": first["name"], "name": "Different display name"}
    with pytest.raises(ValueError, match="Duplicate policy id"):
        project.save_policy("default", "other.json", same_identity, expected=None)
    assert project.snapshot().revision == before
    assert read_json(project.folders("default")["specific"] / "legacy.json") == first
    assert not (project.folders("default")["specific"] / "other.json").exists()


def test_normalized_general_identity_keeps_source_assignment_and_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", selected=[], dashboard=False)
    document = catalog_policy("destructive-actions")
    document["id"] = "  " + GENERAL_ID + "  "
    assert validate_policy(document) == ("general", GENERAL_ID)
    source = tmp_path / "general.json"
    source.write_text(json.dumps(document))
    common = ["--project", str(project.root)]
    run(["policy", "import", str(source), *common])
    run(["policy", "import", str(source), *common])
    entry, = project.policies("default")
    snapshot = project.snapshot()
    assert entry["id"] == GENERAL_ID and entry["document"] == document
    assert snapshot.config.general_skills[0].id == GENERAL_ID
    assert snapshot.config.general_skills[0].source == entry["path"]
    assert snapshot.raw["general_skills"][GENERAL_ID]["path"] == entry["path"]
    assert revision_config(project, "default", snapshot.revision).general_skills[0].id == GENERAL_ID
    run(["policy", "configure", GENERAL_ID, "--risk", RISK, "--decision", "block", *common])
    assert project.snapshot().config.skill_policies[GENERAL_ID][RISK] == "block"
    assert read_json(Path(entry["path"]))["id"] == document["id"]
    run(["policy", "disable", GENERAL_ID, *common])
    assert project.snapshot().config.general_skills == ()
    assert revision_config(project, "default", project.snapshot().revision).general_skills == ()
    run(["policy", "enable", GENERAL_ID, *common])
    assert project.snapshot().config.general_skills[0].id == GENERAL_ID


def captured_archive(url, decision, scope):
    return {"schema": "acira.specific_skill.v2", "id": "captured-archive", "name": "Captured Archive",
        "scope": {"url_patterns": [url if scope == "exact" else url + "*"]},
        "targets": [{"id": "archive", "decision": decision, "reason": "Review or block captured Archive",
            "fingerprint": {"tag": "button", "type": "button", "id": "archive",
                "text": "Archive this repository", "dom_path": "html:nth-of-type(1)>body:nth-of-type(1)>button:nth-of-type(1)",
                "stable_attributes": {"id": "archive", "type": "button"}}}]}


def target(identifier, url):
    return GuardTarget(tag="button", type="button", id=identifier,
        text={"archive": "Archive this repository", "delete": "Delete this repository"}[identifier],
        url=url, is_button=True, is_form_control=True, is_native_submit=False, confidence=1,
        dom_path="html:nth-of-type(1)>body:nth-of-type(1)>button:nth-of-type(" + ("1" if identifier == "archive" else "2") + ")",
        attributes={"id": identifier, "type": "button"})


class Reviewer:
    def __init__(self, decision="block"):
        self.decision = decision
        self.requests = []

    async def ask(self, request):
        self.requests.append(request)
        return self.decision


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_form", ["name-only", "padded-id"])
@pytest.mark.parametrize("approved", [False, True], ids=["reject", "approve"])
async def test_legacy_capture_terminal_import_reaches_runtime_review_and_one_attempt(
        tmp_path, monkeypatch, identity_form, approved):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", selected=[], dashboard=False)
    document = legacy_capture(identity_form)
    source = tmp_path / "capture.json"
    source.write_text(json.dumps(document))
    common = ["--project", str(project.root)]
    run(["policy", "import", str(source), *common])
    _, policy_id = validate_policy(document)
    run(["policy", "configure", policy_id, "--target", "guide", "--decision", "ask", *common])
    saved, = project.policies("default")
    assert saved["document"].get("id") == document.get("id")
    reviewer = Reviewer("allow" if approved else "block")
    attempts = []
    async with protect(project=project.root, dashboard=False, review_backend=reviewer) as guard:
        request = ActionRequest(action="click", framework="playwright", target=GuardTarget(
            tag="a", id="guide", href="/guide", text="Guide", url="https://fixture.invalid/settings",
            confidence=1), target_resolved=True, confidence=1)
        if approved:
            await guard.action_gateway.guard_execute(request, lambda: attempts.append("fixture callback"))
        else:
            with pytest.raises(AciraActionBlocked):
                await guard.action_gateway.guard_execute(request, lambda: attempts.append("fixture callback"))
    assert attempts == (["fixture callback"] if approved else [])
    assert len(reviewer.requests) == 1
    reviewed = reviewer.requests[0].classification.details
    assert reviewed["skill_decision_id"] == policy_id + ":guide"
    journal = Journal(project.root)
    review, = journal.reviews()
    assert review["state"] == ("approved" if approved else "rejected")
    assert review["agent"] == "default" and review["policy_revision"] == project.snapshot().revision
    events = journal.events()
    assert sum(event["type"] == "review_requested" for event in events) == 1
    assert sum(event["type"] == "review_decided" for event in events) == 1
    assert sum(event["type"] == "action_dispatched" for event in events) == int(approved)
    assert sum(event["type"] == "run_started" for event in events) == 1
    assert sum(event["type"] == "run_ended" for event in events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["missing-field", "missing-binding", "approve", "reject"])
async def test_padded_general_identity_preserves_runtime_review_requirements(tmp_path, monkeypatch, outcome):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", selected=[], dashboard=False)
    document = catalog_policy("destructive-actions")
    document["id"] = "  " + GENERAL_ID + "  "
    document["acira_review"] = {RISK: {"required_fields": ["record"], "request_binding": True}}
    source = tmp_path / "general.json"
    source.write_text(json.dumps(document))
    run(["policy", "import", str(source), "--project", str(project.root)])
    saved, = project.policies("default")
    assert saved["id"] == GENERAL_ID and saved["document"]["id"] == document["id"]
    reviewer = Reviewer("block" if outcome == "reject" else "allow")
    runtime = ProjectRuntime(project, "default", dashboard=False, review_backend=reviewer)
    await runtime.start()
    try:
        evidence = {"critical": True, "verified_fields": {"record": "fixture-only"}, "request_bound": True}
        if outcome == "missing-field":
            evidence["verified_fields"] = {}
        elif outcome == "missing-binding":
            evidence.pop("request_bound")
        request = ActionRequest(action="click", framework="playwright",
            target=target("delete", "https://fixture.invalid/settings"), target_resolved=True, confidence=1,
            details={"cedar": {"general_skills": {"detections": [{
                "skill_id": GENERAL_ID, "risk": RISK, "severity": "high"}]}}}, operation_evidence=evidence)
        classification = ActionGateway()._classification_from_request(request)
        review_request = GuardReviewRequest("{}", CDPMessageContext(
            AGENT_TO_BROWSER, "fixture", 1, "/acira/action/playwright"), classification, "Review fixture only")
        assert await runtime.ask(review_request) == ("allow" if outcome == "approve" else "block")
    finally:
        await runtime.close()
    journal = Journal(project.root)
    events = journal.events()
    missing = [event for event in events if event["type"] == "review_evidence_missing"]
    if outcome.startswith("missing-"):
        assert reviewer.requests == [] and journal.reviews() == []
        missing_event, = missing
        assert missing_event["policy_id"] == GENERAL_ID and missing_event["risk"] == RISK
        assert missing_event["missing"] == (["record"] if outcome == "missing-field" else ["verified request binding"])
    else:
        assert len(reviewer.requests) == 1 and missing == []
        review, = journal.reviews()
        assert review["state"] == ("approved" if outcome == "approve" else "rejected")
        assert review["evidence"]["verified_fields"] == {"record": "fixture-only"}
        assert review["evidence"]["request_bound"] is True
        assert review["policy_revision"] == project.snapshot().revision
        assert sum(event["type"] == "review_requested" for event in events) == 1
        assert sum(event["type"] == "review_decided" for event in events) == 1
    assert sum(event["type"] == "run_started" for event in events) == 1
    assert sum(event["type"] == "run_ended" for event in events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_decision,scope", CASES)
@pytest.mark.parametrize("outcome", ["block", "reject", "approve"])
async def test_mapped_archive_preserves_unrelated_delete_general_gateway_and_replay(capture_decision, scope, outcome):
    url = "https://fixture.invalid/settings"
    general_decision = "block" if outcome == "block" else "ask"
    reviewer = Reviewer("allow" if outcome == "approve" else "block")
    config = replace(AciraGuardConfig.from_dict({"terminal_log": False, "terminal_review": False}),
        specific_skills=(SpecificSkill.from_dict(captured_archive(url, capture_decision, scope)),),
        general_skills=(GeneralSkill.from_dict(catalog_policy("destructive-actions")),),
        skill_policies={GENERAL_ID: {RISK: general_decision}})
    policy = AciraGuardPolicy(config, review_backend=reviewer)
    gateway = ActionGateway(policy=policy)
    request = ActionRequest(action="click", framework="playwright", target=target("delete", url),
        target_resolved=True, confidence=1,
        details={"page_targets": [target("archive", url).to_metadata(), target("delete", url).to_metadata()],
                 "page_target_probe": {"status": "ok", "url": url}},
        operation_evidence={"critical": True, "request_bound": True, "verified_fields": {"record": "fixture-only"}})
    classification = gateway._classification_from_request(request)
    context = CDPMessageContext(AGENT_TO_BROWSER, "fixture", 0, "/acira/action/playwright")
    mapping = await policy._page_missing_target_decision(classification, context)
    # Establish the exact formerly dangerous allow branch using real mapping.
    assert mapping.decision == "allow" and mapping.source == "specific_skill_mapping"
    assert mapping.reason == "Page mapping produced no protected target for the current action."
    assert mapping.metadata["skills"]["page_mapping"][0]["status"] == "auto_mapped"
    dispatched = []
    if outcome == "approve":
        await gateway.guard_execute(request, lambda: dispatched.append("fixture dispatch"))
    else:
        with pytest.raises(AciraActionBlocked):
            await gateway.guard_execute(request, lambda: dispatched.append("fixture dispatch"))
    assert dispatched == (["fixture dispatch"] if outcome == "approve" else [])
    assert len(reviewer.requests) == (0 if outcome == "block" else 1)
    metadata = gateway.checkpoint_store.decisions[-1]["metadata"]
    assert metadata["policy_decision"] == general_decision
    assert metadata["source"] == "general_skill"
    assert metadata["skill_decision_id"] == GENERAL_ID + ":" + RISK
    event = {"evaluation_contract": "acira.action_facts.v1", "facts": build_browser_event_facts(classification).to_dict(),
        "specific_facts": _event_facts_from_classification(classification),
        "classification": classification.to_metadata(), "metadata": metadata}
    replay = evaluate(config, event)
    if general_decision == "block":
        assert replay["decision"] == "block" and replay["policy_id"] == GENERAL_ID + ":" + RISK
    else:
        assert replay["decision"] == "indeterminate" and "mapping" in replay["reason"]
        assert "policy_id" not in replay, "Missing page mapping must not be attributed to captured Archive"


@pytest.mark.parametrize("candidate", ["allow", "ask", "core-block", "general-block"])
def test_mapping_dependent_replay_preserves_only_independently_proven_candidate_denials(candidate):
    config = AciraGuardConfig.from_dict({"terminal_log": False, "terminal_review": False})
    if candidate in {"ask", "general-block"}:
        config = replace(config, general_skills=(GeneralSkill.from_dict(catalog_policy("destructive-actions")),),
            skill_policies={GENERAL_ID: {RISK: "block" if candidate == "general-block" else "ask"}})
    elif candidate == "core-block":
        config = replace(config, rules=(AciraGuardRule(id="deny-click", actions=("click",),
            decision="block", reason="Independent configured denial"),))
    gateway = ActionGateway(policy=AciraGuardPolicy(config))
    classification = gateway._classification_from_request(ActionRequest(action="click", framework="playwright",
        target=target("delete", "https://fixture.invalid/settings"), target_resolved=True, confidence=1))
    event = {"evaluation_contract": "acira.action_facts.v1", "facts": build_browser_event_facts(classification).to_dict(),
        "specific_facts": _event_facts_from_classification(classification),
        "classification": classification.to_metadata(), "metadata": {"source": "specific_skill_mapping"}}
    result = evaluate(config, event)
    if candidate.endswith("block"):
        assert result["decision"] == "block"
        assert result["rule_id"] == ("deny-click" if candidate == "core-block" else None)
    else:
        assert result["decision"] == "indeterminate" and "mapping" in result["reason"]
        assert "policy_id" not in result


async def close_owned(resource, method):
    primary = sys.exc_info()[1]
    try:
        await asyncio.wait_for(getattr(resource, method)(), 10)
    except BaseException as error:
        if primary is None:
            raise
        primary.add_note("Owned local browser cleanup failed: " + type(error).__name__)


async def export_archive(tmp_path, url, capture_decision, scope, select_capture_target):
    from playwright.async_api import async_playwright
    extension = install_extension(tmp_path / "extension")
    manifest = json.loads((extension / "manifest.json").read_text())
    manifest["host_permissions"] = ["http://127.0.0.1/*"]
    (extension / "manifest.json").write_text(json.dumps(manifest))
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(tmp_path / "capture-profile", channel="chromium",
            headless=True, chromium_sandbox=True, service_workers="block",
            args=["--disable-extensions-except=" + str(extension), "--load-extension=" + str(extension)])
        try:
            worker = context.service_workers[0] if context.service_workers else await context.wait_for_event("serviceworker", timeout=10000)
            page = await context.new_page()
            await page.goto(url)
            await worker.evaluate("""async url => {const tab=(await chrome.tabs.query({})).find(t=>t.url===url);
                await chrome.scripting.insertCSS({target:{tabId:tab.id},files:['capture.css']});
                await chrome.scripting.executeScript({target:{tabId:tab.id},files:['content.js']});} """, url)
            await select_capture_target(page, page.locator("#archive"))
            assert await page.locator("#delete-attempts").inner_text() == "0"
            assert await page.locator("#archive-attempts").inner_text() == "0"
            await page.get_by_role("button", name="Ask first" if capture_decision == "ask" else "Block target", exact=True).click()
            async with page.expect_download() as downloaded:
                await page.get_by_role("button", name="Download", exact=True).click()
            path = tmp_path / "captured-archive.json"
            await (await downloaded.value).save_as(path)
            document = json.loads(path.read_text())
            assert document["targets"][0]["fingerprint"]["id"] == "archive"
            assert document["targets"][0]["decision"] == capture_decision
            # Scope is a declarative user-editable field; exercise both reported forms.
            document["scope"]["url_patterns"] = [url if scope == "exact" else url + "*"]
            path.write_text(json.dumps(document))
            return path, document
        finally:
            await close_owned(context, "close")


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["playwright", "browser_use"])
@pytest.mark.parametrize("capture_decision,scope", CASES)
@pytest.mark.parametrize("outcome", ["block", "reject", "approve"])
async def test_real_capture_import_keeps_same_page_delete_protected_before_and_after_archive(
        tmp_path, monkeypatch, select_capture_target, adapter, capture_decision, scope, outcome):
    from playwright.async_api import async_playwright
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    monkeypatch.setenv("BROWSER_USE_CLOUD_SYNC", "false")
    monkeypatch.setenv("BROWSER_USE_VERSION_CHECK", "false")
    monkeypatch.setenv("BROWSER_USE_CONFIG_DIR", str(tmp_path / "browser-use"))
    project = initialize(tmp_path / "project", adapter=adapter, selected=["destructive-actions"], dashboard=False)
    common = ["--project", str(project.root)]
    general_decision = "block" if outcome == "block" else "ask"
    run(["policy", "configure", "destructive-actions", "--risk", RISK, "--decision", general_decision, *common])
    received = []
    async def index(request):
        return web.Response(content_type="text/html", text='''<!doctype html><title>Local repository settings</title>
            <button id="archive" type="button">Archive this repository</button>
            <button id="delete" type="button">Delete this repository</button>
            <output id="archive-attempts">0</output><output id="delete-attempts">0</output>
            <script>for (const id of ['archive','delete']) {
              document.getElementById(id).addEventListener('click', async () => {
                const output=document.getElementById(id+'-attempts');output.textContent=String(Number(output.textContent)+1);
                await fetch('/'+id,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({record:'fixture-only'})});
              });}</script>''')
    async def mutate(request):
        received.append({"path": request.path, "body": await request.json()})
        return web.Response(text="Local fixture only")
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/delete", mutate)
    app.router.add_post("/archive", mutate)
    async with TestServer(app, host="127.0.0.1") as server:
        url = str(server.make_url("/"))
        exported, document = await export_archive(tmp_path, url, capture_decision, scope, select_capture_target)
        assert received == [], "Capture must not activate either repository control"
        run(["policy", "import", str(exported), *common])
        run(["policy", "import", str(exported), *common])
        run(["policy", "validate", *common])
        assert len(project.policies("default")) == 2
        capture_id = document["id"]
        # Exercise the public terminal target selector as well as import.
        run(["policy", "configure", capture_id, "--target", document["targets"][0]["id"], "--decision", capture_decision, *common])
        for order in ("delete-first", "archive-first"):
            reviewer = Reviewer()
            async with protect(project=project.root, dashboard=False, review_backend=reviewer) as guard, async_playwright() as p:
                browser = session = None
                try:
                    if adapter == "playwright":
                        guard.patch_playwright(p)
                        browser = await p.chromium.launch(chromium_sandbox=True)
                        page = await browser.new_page()
                        await page.goto(url)
                        async def activate(identifier, *, contract=False):
                            if contract:
                                await guard.submit(page.locator("#" + identifier), destination=str(server.make_url("/" + identifier)),
                                    method="POST", encoding="json", values={"record": "fixture-only"})
                                return True
                            try:
                                await page.locator("#" + identifier).click()
                                return True
                            except AciraActionBlocked:
                                return False
                        async def attempts(identifier):
                            return await page.locator("#" + identifier + "-attempts").inner_text()
                    else:
                        from browser_use import BrowserSession
                        from browser_use.browser.events import NavigateToUrlEvent, ClickElementEvent
                        session = BrowserSession(headless=True, executable_path=p.chromium.executable_path, chromium_sandbox=True,
                            user_data_dir=tmp_path / ("profile-" + order), downloads_path=tmp_path / ("downloads-" + order),
                            enable_default_extensions=False, permissions=[], keep_alive=False)
                        await asyncio.wait_for(session.start(), 30)
                        navigation = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
                        await asyncio.wait_for(navigation.event_result(raise_if_any=True, raise_if_none=False), 30)
                        async def activate(identifier, *, contract=False):
                            summary = await asyncio.wait_for(session.get_browser_state_summary(include_screenshot=False), 30)
                            node = next(item for item in summary.dom_state.selector_map.values() if item.attributes.get("id") == identifier)
                            if contract:
                                await guard.submit(session, node, destination=str(server.make_url("/" + identifier)),
                                    method="POST", encoding="json", values={"record": "fixture-only"})
                                return True
                            clicked = session.event_bus.dispatch(ClickElementEvent(node=node))
                            result = await asyncio.wait_for(clicked.event_result(raise_if_any=True), 10)
                            return "validation_error" not in result
                        async def attempts(identifier):
                            result = await guard._send_browser_use_cdp(session, "Runtime.evaluate", {
                                "expression": "document.querySelector('#" + identifier + "-attempts').textContent", "returnByValue": True})
                            return result["result"]["value"]
                    for identifier in (("delete", "archive") if order == "delete-first" else ("archive", "delete")):
                        assert await activate(identifier) is False
                        assert await attempts(identifier) == "0"
                    assert not any(row["path"] == "/archive" for row in received)
                    expected_reviews = ([] if outcome == "block" else
                        (["delete", "archive"] if capture_decision == "ask" and order == "delete-first" else
                         ["archive", "delete"] if capture_decision == "ask" else ["delete"]))
                    assert [request.classification.target.id for request in reviewer.requests] == expected_reviews
                    assert sum(row["path"] == "/delete" for row in received) == (1 if outcome == "approve" and order == "archive-first" else 0)
                    if outcome == "approve":
                        reviewer.decision = "allow"
                        assert await activate("delete", contract=True)
                        assert await attempts("delete") == "1"
                        assert len([request for request in reviewer.requests if request.classification.target.id == "delete"]) == 2
                finally:
                    if browser is not None:
                        await close_owned(browser, "close")
                    if session is not None:
                        await close_owned(session, "kill")
        assert received == ([{"path": "/delete", "body": {"record": "fixture-only"}}] * 2 if outcome == "approve" else [])
    events = [event for event in Journal(project.root).events(limit=1000) if event["type"] == "action_evaluated"
        and (event.get("classification", {}).get("target") or {}).get("id") == "delete"]
    assert len(events) == (4 if outcome == "approve" else 2)
    assert len({event["operation_id"] for event in events}) == len(events)
    for event in events:
        assert event["policy_decision"] == general_decision
        result = evaluate(project.snapshot().config, event)
        if general_decision == "block":
            assert result["decision"] == "block" and result["policy_id"] == GENERAL_ID + ":" + RISK
        else:
            assert result["decision"] == "indeterminate" and "mapping" in result["reason"]
