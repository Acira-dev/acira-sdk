"""Capture identity and human terminal consent stay bound to one operation.

The PTY cases use the real runtime/journal and operating-system terminal. Browser
cases load the actual unpacked extension and use inert loopback pages only.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import select
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from acira.cli import run
from acira.journal import Journal
from acira.project import initialize, validate_policy
from acira_cdp_proxy.config import AciraGuardConfig
from acira_cdp_proxy.guard import (
    AciraGuardPolicy, ActionClassification, GuardReviewRequest, GuardTarget,
    TerminalReviewBackend, _mapping_entry_requires_page_review,
)
from acira_cdp_proxy.policy import AGENT_TO_BROWSER, CDPMessageContext
from acira_cdp_proxy.skills import SpecificSkill, SpecificSkillTarget, evaluate_specific_skills
from acira_cdp_proxy.target_mapping import (
    InMemoryTargetMappingStore, JsonlTargetMappingStore, TargetMappingRecord,
    fingerprint_hash, target_mapping_key, url_scope_key,
)


def capture_document(ids=("first", "second")):
    return {"schema": "acira.specific_skill.v2", "id": "capture-audit", "name": "Capture audit",
        "scope": {"url_patterns": ["http://127.0.0.1:*/*"]}, "targets": [
            {"id": identifier, "decision": "ask", "fingerprint": {
                "tag": "a", "id": identifier.strip(), "text": "Continue", "href": "/guide"}}
            for identifier in ids]}


@pytest.mark.parametrize("ids", [("same", "same"), (" same ", "same"), ("same", "\tsame\n")])
def test_duplicate_normalized_capture_target_ids_fail_before_terminal_import(tmp_path, monkeypatch, ids):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", selected=[], dashboard=False)
    document = capture_document(ids)
    before = project.snapshot().revision
    with pytest.raises(ValueError, match="duplicate target ids"):
        validate_policy(document)
    exported = tmp_path / "capture.json"
    exported.write_text(json.dumps(document))
    with pytest.raises(SystemExit) as error:
        run(["policy", "import", str(exported), "--project", str(project.root)])
    assert error.value.code == 2
    assert project.snapshot().revision == before and project.policies("default") == []


def terminal_request(label):
    return GuardReviewRequest("{}", CDPMessageContext(AGENT_TO_BROWSER, label, 1, "/acira/action/playwright"),
        ActionClassification("playwright.click", "click", "Review " + label,
            target=GuardTarget(tag="button", id=label, text=label)), "Review " + label)


@pytest.mark.asyncio
async def test_terminal_owner_serializes_context_evidence_and_answer(tmp_path, monkeypatch, capsys):
    from acira_cdp_proxy import guard as module
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    entered, release = asyncio.Event(), asyncio.Event()
    prompts = []
    async def read(prompt, *, stop_event=None):
        prompts.append(prompt)
        if "Alpha" in prompt:
            entered.set()
            await release.wait()
            return "n"
        return "y"
    monkeypatch.setattr(module, "_terminal_input_until", read)
    backend = TerminalReviewBackend(AciraGuardConfig())
    one = asyncio.create_task(backend.ask_until(terminal_request("Alpha"),
        display_context={"run_id": "run-alpha"}, display_evidence={"ordinary": "alpha evidence"}))
    await asyncio.wait_for(entered.wait(), 1)
    two = asyncio.create_task(backend.ask_until(terminal_request("Beta"),
        display_context={"run_id": "run-beta"}, display_evidence={"ordinary": "beta evidence"}))
    try:
        await asyncio.sleep(0)
        output = capsys.readouterr().out
        assert "run-alpha" in output and "alpha evidence" in output
        assert "run-beta" not in output and "beta evidence" not in output and len(prompts) == 1
        release.set()
        assert await asyncio.wait_for(one, 1) == "n"
        assert await asyncio.wait_for(two, 1) == "y"
        output = capsys.readouterr().out
        assert "run-beta" in output and "beta evidence" in output and len(prompts) == 2
    finally:
        release.set()
        for task in (one, two):
            if not task.done():
                task.cancel()
        await asyncio.gather(one, two, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", ["queued_stop", "active_cancel"])
async def test_canceled_terminal_review_never_steals_next_review_input(monkeypatch, capsys, cancel):
    from acira_cdp_proxy import guard as module
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    entered, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
    prompts = []
    async def read(prompt, *, stop_event=None):
        prompts.append(prompt)
        if "Alpha" in prompt:
            entered.set()
            await release.wait()
            return "n"
        return "y"
    monkeypatch.setattr(module, "_terminal_input_until", read)
    backend = TerminalReviewBackend(AciraGuardConfig())
    first = asyncio.create_task(backend.ask_until(terminal_request("Alpha")))
    await asyncio.wait_for(entered.wait(), 1)
    queued = asyncio.create_task(backend.ask_until(terminal_request("Beta"), stop))
    try:
        await asyncio.sleep(0)
        if cancel == "queued_stop":
            stop.set()
            assert await asyncio.wait_for(queued, 1) is None
            release.set()
            assert await asyncio.wait_for(first, 1) == "n"
            assert len(prompts) == 1
        else:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert await asyncio.wait_for(queued, 1) == "y"
        assert await backend.ask_until(terminal_request("Next")) == "y"
    finally:
        release.set()
        for task in (first, queued):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, queued, return_exceptions=True)


@pytest.mark.asyncio
async def test_terminal_eof_blocks_even_legacy_default_allow(monkeypatch):
    from acira_cdp_proxy import guard as module
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    async def eof(*args, **kwargs):
        raise EOFError
    monkeypatch.setattr(module, "_terminal_input_until", eof)
    backend = TerminalReviewBackend(AciraGuardConfig.from_dict({"default_review_decision": "allow"}))
    assert await backend.ask_until(terminal_request("Alpha")) == "block"


PTY_CHILD = r'''
import asyncio,json,sys
from pathlib import Path
from acira.local import atomic_json
from acira.project import initialize
from acira.runtime import ProjectRuntime
from acira_cdp_proxy.guard import ActionClassification,GuardReviewRequest,GuardTarget
from acira_cdp_proxy.policy import AGENT_TO_BROWSER,CDPMessageContext
async def main():
    path=Path(sys.argv[1])
    project=initialize(path,agent="Alpha",selected=[],dashboard=False)
    initialize(path,agent="Beta",selected=[],dashboard=False)
    document=project.document()
    for profile in document["agents"].values():
        profile["guard"]={"terminal_review":True,"terminal_log":False,"default_review_decision":"allow"}
    atomic_json(project.path,document)
    one,two=ProjectRuntime(project,"Alpha",dashboard=False),ProjectRuntime(project,"Beta",dashboard=False)
    def request(label):
        return GuardReviewRequest("{}",CDPMessageContext(AGENT_TO_BROWSER,label,1,"/acira/action/playwright"),
          ActionClassification("playwright.click","click","Review "+label,target=GuardTarget(tag="button",id=label,text=label),
          details={"operation_evidence":{"critical":True,"ordinary":label+" evidence"}}),"Review "+label)
    try:
        results=await asyncio.gather(one.ask(request("Alpha")),two.ask(request("Beta")))
        print("PTY_RESULT="+json.dumps({"answers":dict(zip(("Alpha","Beta"),results)),
          "reviews":[{"agent":r["agent"],"state":r["state"]} for r in one.journal.reviews()]}),flush=True)
    finally:
        await one.close()
        await two.close()
asyncio.run(main())
'''


@pytest.mark.parametrize("first_input", [b"n\n", b"\x04"])
def test_actual_pty_two_runtime_runs_route_consent_and_eof_to_owned_review(tmp_path, first_input):
    if sys.platform == "win32":
        pytest.skip("POSIX PTY evidence is a macOS release check")
    import pty
    master, slave = pty.openpty()
    child = None
    transcript = bytearray()
    env = {**os.environ, "ACIRA_HOME": str(tmp_path / "private"), "PYTHONUNBUFFERED": "1",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"}
    # Source tests need the checkout; installed-wheel mode must keep the tested
    # interpreter's installed package and never add the source checkout.
    if os.environ.get("ACIRA_TEST_INSTALLED") != "1":
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    def until(predicate):
        deadline = time.monotonic() + 15
        while not predicate(transcript.decode(errors="replace")):
            if time.monotonic() >= deadline:
                raise AssertionError("PTY review did not reach its bounded stage")
            ready, _, _ = select.select([master], [], [], .1)
            if ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    raise AssertionError("PTY child exited before its expected review stage") from None
                if not data:
                    raise AssertionError("PTY child closed before its expected review stage")
                transcript.extend(data)
    try:
        child = subprocess.Popen([sys.executable, "-c", PTY_CHILD, str(tmp_path / "project")],
            stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        slave = None
        until(lambda text: "[y/N]" in text)
        initial = transcript.decode(errors="replace")
        assert '"agent": "Alpha"' in initial and '"run_id":' in initial and '"review_id":' in initial
        context_line = next(line for line in initial.splitlines() if line.startswith("Acira review context:"))
        first_context = json.loads(context_line.split(":", 1)[1])
        assert first_context["terminal_review"] and first_context["project"] == "project"
        assert "Alpha evidence" in initial and "Beta evidence" not in initial and '"agent": "Beta"' not in initial
        os.write(master, first_input)
        until(lambda text: text.count("[y/N]") == 2)
        second_line = [line for line in transcript.decode(errors="replace").splitlines()
                       if line.startswith("Acira review context:")][-1]
        second_context = json.loads(second_line.split(":", 1)[1])
        assert second_context["agent"] == "Beta"
        assert second_context["terminal_review"] != first_context["terminal_review"]
        os.write(master, b"y\n")
        until(lambda text: "PTY_RESULT=" in text and "\n" in text.split("PTY_RESULT=", 1)[1])
        line = next(line for line in transcript.decode(errors="replace").splitlines() if line.startswith("PTY_RESULT="))
        result = json.loads(line.split("=", 1)[1])
        assert result["answers"] == {"Alpha": "block", "Beta": "allow"}
        assert sorted((item["agent"], item["state"]) for item in result["reviews"]) == [
            ("Alpha", "rejected"), ("Beta", "approved")]
        assert child.wait(timeout=5) == 0
    finally:
        primary = sys.exc_info()[1]
        try:
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        except Exception as error:
            if primary is None:
                raise
            primary.add_note("Owned PTY cleanup failed: " + type(error).__name__)
        finally:
            os.close(master)
            if slave is not None:
                os.close(slave)


def mapped_candidate():
    return {"tag": "a", "type": None, "role": None, "id": "report", "name": None,
        "href": "/reports", "text": "Open report", "url": "http://127.0.0.1:4173/",
        "dom_path": "html>body:nth-of-type(1)>main:nth-of-type(1)>a:nth-of-type(1)",
        "control_index": 0, "same_kind_index": 0, "attributes": {"id": "report", "href": "/reports"},
        "structure_signature": {"element_state": {"hidden": False, "disabled": False},
                                "layout": {"rect": {"x": 10, "y": 20}}}}


def mapping_entries(store, candidate, *, fingerprint=None, page_targets=None):
    fingerprint = copy.deepcopy(fingerprint if fingerprint is not None else mapped_candidate())
    target = SpecificSkillTarget("report-target", "ask", "Review report", fingerprint)
    policy = AciraGuardPolicy(AciraGuardConfig(adapters={}, terminal_log=False))
    policy.target_mapping_store = store
    return policy._resolve_page_target_mappings(scoped_targets=[("capture-audit", "Capture audit", ("http://127.0.0.1:*/*",), target)],
        page_targets=[candidate] if page_targets is None else page_targets,
        facts={"url": candidate["url"], "target": candidate},
        classification=ActionClassification("playwright.click", "click", "Report"))


@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("change", ["href", "label", "disabled", "hidden", "formaction", "control_kind"])
def test_saved_mapping_revalidates_identity_and_current_hard_constraints(tmp_path, persistent, change):
    candidate = mapped_candidate()
    store = JsonlTargetMappingStore(tmp_path / "mappings.jsonl") if persistent else InMemoryTargetMappingStore()
    record = TargetMappingRecord("capture-audit", "report-target", fingerprint_hash(candidate),
        url_scope_key(candidate["url"], ("http://127.0.0.1:*/*",)), target_mapping_key(candidate),
        copy.deepcopy(candidate), {}, "ask", "approved")
    store.record(record)
    entry, = mapping_entries(store, candidate)
    assert entry["status"] == "approved" and not _mapping_entry_requires_page_review(entry)
    changed = copy.deepcopy(candidate)
    if change == "href":
        changed["href"] = changed["attributes"]["href"] = "/another-destination"
    elif change == "label":
        changed["text"] = "Different operation"
    elif change in {"hidden", "disabled"}:
        changed["structure_signature"]["element_state"][change] = True
    elif change == "formaction":
        changed["form_action"] = "/another-form"
        changed["action_signature"] = {"form_action": "/another-form"}
    else:
        changed["role"] = "option"
    changed_entry, = mapping_entries(store, changed)
    assert changed_entry["status"] != "approved"
    assert _mapping_entry_requires_page_review(changed_entry)
    # These are mapping acknowledgements, never saved action approvals.
    assert record.status == "approved"


def test_mapping_acknowledgement_ignores_layout_but_keeps_its_own_saved_snapshot():
    candidate = mapped_candidate()
    store = InMemoryTargetMappingStore()
    store.record(TargetMappingRecord("capture-audit", "report-target", fingerprint_hash(candidate),
        url_scope_key(candidate["url"], ("http://127.0.0.1:*/*",)), target_mapping_key(candidate),
        candidate, {}, "ask", "approved"))
    changed = copy.deepcopy(candidate)
    changed["structure_signature"]["layout"]["rect"] = {"x": 500, "y": 600}
    assert mapping_entries(store, changed)[0]["status"] == "approved"
    candidate["attributes"]["href"] = "/changed-after-record"
    assert store.records[0].candidate_fingerprint["attributes"]["href"] == "/reports"


def same_label_skill():
    targets = []
    for index, (identity, decision) in enumerate((("second", "ask"), ("third", "block")), 2):
        candidate = mapped_candidate()
        candidate.update(id=identity, href="/" + identity, text="Continue", control_index=index - 1,
            same_kind_index=index - 1,
            dom_path=f"html>body:nth-of-type(1)>main:nth-of-type(1)>a:nth-of-type({index})")
        candidate["attributes"] = {"id": identity, "href": "/" + identity}
        candidate.update(control_kind="link", nearby_text="Continue", parent_label="Continue",
            labels={"accessible": "Continue", "associated": "Continue", "source": "label"},
            semantic_hints=["a", "continue", identity], identity_tokens=[identity, "continue"],
            previous_control_text="Continue", next_control_text="Continue",
            action_signature={"kind": "link", "attrs": {"href": "/" + identity},
                              "intent_tokens": ["link", "continue", identity], "text": "Continue"})
        candidate["structural_context"] = {key: candidate[key] for key in (
            "control_index", "same_kind_index", "dom_path", "parent_label", "previous_control_text", "next_control_text")}
        candidate["structure_signature"].update(version=1, control_index=index - 1,
            same_kind_index=index - 1, control_count=3, same_kind_count=3, target_kind="link",
            kind_sequence=["link"] * 3,
            control_window=[{"offset": offset, "kind": "link", "tag": "a", "text": "Continue"}
                            for offset in range(-index + 1, 4 - index)],
            ancestor_path=[{"tag": "html"}, {"tag": "body"}, {"tag": "main"}, {"tag": "a", "kind": "link"}])
        targets.append({"id": identity, "decision": decision, "fingerprint": candidate})
    return SpecificSkill.from_dict({"schema": "acira.specific_skill.v2", "id": "same-label",
        "scope": {"url_patterns": ["http://127.0.0.1:*/*"]}, "targets": targets})


@pytest.mark.parametrize("identity,decision", [("second", "ask"), ("third", "block")])
def test_same_label_controls_keep_their_own_unchanged_capture_decision(identity, decision):
    skill = same_label_skill()
    target = copy.deepcopy(next(item.fingerprint for item in skill.targets if item.id == identity))
    facts = {"url": target["url"], "target": target, "meaningful": True, "action": "click",
        "details": {"page_target_probe": {"status": "ok"},
                    "page_targets": [copy.deepcopy(item.fingerprint) for item in skill.targets]}}
    result = evaluate_specific_skills((skill,), facts)
    assert [(item.target_id, item.decision) for item in result.matches] == [(identity, decision)]


def test_same_control_captured_twice_keeps_block_precedence_and_other_skills():
    from acira_cdp_proxy.decision_engine import CedarDecisionEngine
    skill = same_label_skill()
    exact = copy.deepcopy(skill.targets[0].fingerprint)
    duplicate = replace(skill.targets[1], fingerprint=copy.deepcopy(exact))
    skill = replace(skill, targets=(skill.targets[0], duplicate))
    other = replace(skill, id="independent-capture")
    facts = {"url": exact["url"], "target": exact, "meaningful": True, "action": "click"}
    result = evaluate_specific_skills((skill, other), facts)
    assert len(result.matches) == 4 and {item.skill_id for item in result.matches} == {skill.id, other.id}
    assert CedarDecisionEngine().decide_specific_skills(facts=facts, skills=(skill, other)).decision == "block"


def test_exact_ask_cannot_override_an_intentionally_overlapping_generic_block():
    from acira_cdp_proxy.decision_engine import CedarDecisionEngine
    skill = same_label_skill()
    generic_fingerprint = copy.deepcopy(skill.targets[0].fingerprint)
    for key in ("id", "href", "dom_path", "structural_context", "attributes", "stable_attributes", "structure_signature"):
        generic_fingerprint.pop(key, None)
    generic = replace(skill.targets[1], fingerprint=generic_fingerprint)
    skill = replace(skill, targets=(skill.targets[0], generic))
    target = copy.deepcopy(skill.targets[0].fingerprint)
    facts = {"url": target["url"], "target": target, "meaningful": True, "action": "click",
        "details": {"page_target_probe": {"status": "ok"}, "page_targets": [target]}}
    result = CedarDecisionEngine().decide_specific_skills(facts=facts, skills=(skill,))
    assert result.decision == "block" and result.skill_decision_id == "same-label:third"


@pytest.mark.parametrize("observation", ["missing", "unavailable", "duplicated", "drifted"])
def test_different_control_exclusion_requires_unique_unchanged_page_evidence(observation):
    from acira_cdp_proxy.decision_engine import CedarDecisionEngine
    skill = same_label_skill()
    target = copy.deepcopy(skill.targets[0].fingerprint)
    other = copy.deepcopy(skill.targets[1].fingerprint)
    observed = [target, other]
    status = "ok"
    if observation == "missing":
        observed = [target]
    elif observation == "unavailable":
        status = "failed"
    elif observation == "duplicated":
        observed.append(copy.deepcopy(other))
    else:
        other["href"] = other["attributes"]["href"] = "/changed"
    facts = {"url": target["url"], "target": target, "meaningful": True, "action": "click",
        "details": {"page_target_probe": {"status": status}, "page_targets": observed}}
    assert CedarDecisionEngine().decide_specific_skills(facts=facts, skills=(skill,)).decision == "block"


@pytest.mark.parametrize("missing", ["dom_path", "attributes", "structure_signature"])
def test_incomplete_identity_does_not_exempt_other_same_label_captured_controls(missing):
    skill = same_label_skill()
    target = copy.deepcopy(skill.targets[0].fingerprint)
    target.pop(missing)
    if missing == "attributes":
        target["id"] = "changed"
        target["href"] = "/changed"
    result = evaluate_specific_skills((skill,), {"url": target["url"], "target": target,
        "meaningful": True, "action": "click"})
    assert {item.target_id for item in result.matches} == {"second", "third"}


@pytest.mark.asyncio
async def test_actual_localhost_saved_mapping_needs_fresh_review_after_same_slot_change(tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from acira_cdp_proxy.guard import PAGE_TARGETS_PROBE_JS
    from playwright.async_api import async_playwright

    html = '<link rel="icon" href="data:,"><main><a id="report" href="/reports">Open report</a></main>'
    hits = []
    async def serve(request):
        hits.append(request.path)
        return web.Response(text=html, content_type="text/html")
    app = web.Application()
    app.router.add_get("/{path:.*}", serve)
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as p:
        browser = await p.chromium.launch(headless=True, chromium_sandbox=True,
            args=["--password-store=basic", "--use-mock-keychain"])
        try:
            page = await browser.new_page()
            await page.goto(str(server.make_url("/")))
            policy = AciraGuardPolicy(AciraGuardConfig(adapters={}, terminal_log=False))
            initial, = policy._normalize_page_targets(await page.evaluate(PAGE_TARGETS_PROBE_JS))
            fingerprint = copy.deepcopy(initial)
            store = JsonlTargetMappingStore(tmp_path / "mappings.jsonl")
            store.record(TargetMappingRecord("capture-audit", "report-target", fingerprint_hash(fingerprint),
                url_scope_key(initial["url"], ("http://127.0.0.1:*/*",)), target_mapping_key(initial),
                copy.deepcopy(initial), {}, "ask", "approved"))
            assert mapping_entries(store, initial, fingerprint=fingerprint)[0]["status"] == "approved"
            before = list(hits)
            for mutation in ("href", "label", "disabled", "hidden"):
                await page.set_content(html)
                await page.locator("#report").evaluate("""(element, mutation) => {
                    if (mutation === 'href') element.setAttribute('href', '/another-recipient');
                    if (mutation === 'label') element.textContent = 'Another operation';
                    if (mutation === 'disabled') element.setAttribute('aria-disabled', 'true');
                    if (mutation === 'hidden') element.hidden = true;
                }""", mutation)
                observed = policy._normalize_page_targets(await page.evaluate(PAGE_TARGETS_PROBE_JS))
                candidate = observed[0] if observed else initial
                entry, = mapping_entries(store, candidate, fingerprint=fingerprint, page_targets=observed)
                assert entry["status"] != "approved", mutation
                assert _mapping_entry_requires_page_review(entry), mutation
                assert hits == before, "Mapping/capture checks must not activate the local link"
        finally:
            primary = sys.exc_info()[1]
            try:
                await browser.close()
            except Exception as error:
                if primary is None:
                    raise
                primary.add_note("Owned browser cleanup failed: " + type(error).__name__)
