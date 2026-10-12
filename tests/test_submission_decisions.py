"""Outbound operations never consume routine grants or stale authorization."""
import asyncio
import json

import pytest

from acira.project import initialize
from acira.runtime import ProjectRuntime
from acira_cdp_proxy.action_gateway import ActionGateway, ActionRequest, AciraActionBlocked
from acira_cdp_proxy.guard import AciraGuardPolicy, ActionClassification, GuardReviewRequest, GuardTarget
from acira_cdp_proxy.operation import evidence_for
from acira_cdp_proxy.policy import AGENT_TO_BROWSER, CDPMessageContext


def outgoing_state(label, *, native=False):
    return {"url": "https://example.test/compose", "has_form": native,
        "form_id": "compose" if native else None, "destination": "https://example.test/search" if native else "https://example.test/compose",
        "method": "get" if native else None, "fields": [],
        "target": {"tag": "BUTTON", "id": "action", "type": "submit" if native else "button", "text": label}}


async def pending(runtime):
    for _ in range(100):
        reviews = [item for item in runtime.journal.reviews() if item["state"] == "pending"]
        if reviews:
            return reviews[0]
        await asyncio.sleep(.01)
    raise AssertionError("An operation-specific review did not appear")


@pytest.mark.asyncio
@pytest.mark.parametrize("label,native", [(name, False) for name in ("Submit", "Send", "Post", "Publish", "Apply", "Finalize")] + [("Search", True)])
async def test_outbound_classification_cannot_create_or_consume_routine_grant(tmp_path, monkeypatch, label, native):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "existing", selected=[], dashboard=False)
    evidence = evidence_for(outgoing_state(label, native=native), "click")
    assert evidence["critical"] is True
    assert "grant_scope" not in evidence
    # Even a caller supplying the exact scope of an existing routine grant
    # cannot turn this outgoing operation into a grant-eligible review.
    scope = {"origin": "https://example.test", "action": "click", "target": "action"}
    evidence["grant_scope"] = scope
    backend_wait = asyncio.Event()
    class Reviewer:
        async def ask(self, request):
            await backend_wait.wait()
            return "allow"
    runtime = ProjectRuntime(project, "default", dashboard=False, review_backend=Reviewer())
    context = CDPMessageContext(AGENT_TO_BROWSER, "playwright-action", 0, "/acira/action/playwright")
    target = GuardTarget(id="action", text=label, is_native_submit=native)
    def request(value):
        return GuardReviewRequest("{}", context, ActionClassification("playwright.click", "click", label,
            target=target, details={"operation_evidence": value}), label)
    task = None
    try:
        routine = {"critical": False, "grant_scope": scope}
        task = asyncio.create_task(runtime.ask(request(routine)))
        first = await pending(runtime)
        runtime.journal.decide(first["review_id"], "approved", grant=True)
        assert await task == "allow"
        with runtime.journal.db() as db:
            assert db.execute("SELECT COUNT(*) FROM grants").fetchone()[0] == 1
        task = asyncio.create_task(runtime.ask(request(evidence)))
        review = await pending(runtime)
        assert review["grant_eligible"] is False
        with pytest.raises(ValueError):
            runtime.journal.decide(review["review_id"], "approved", grant=True)
        runtime.journal.decide(review["review_id"], "rejected")
        assert await task == "block"
        assert not any(item["type"] == "grant_used" for item in runtime.journal.events())
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_target_change_after_submission_review_blocks_before_execute():
    reviewed_target = {"id": "send", "destination": "https://example.test/messages"}
    live_target = dict(reviewed_target)
    requests, dispatched = [], []
    class Reviewer:
        async def ask(self, request):
            requests.append(request)
            live_target["id"] = "post-to-other-room"
            return "allow"
    policy = AciraGuardPolicy({"terminal_log": False}, review_backend=Reviewer())
    gateway = ActionGateway(policy=policy)
    async def same_target():
        return live_target == reviewed_target
    with pytest.raises(AciraActionBlocked, match="changed before dispatch"):
        await gateway.guard_execute(ActionRequest(action="click", framework="playwright", target=GuardTarget(id="send", text="Send"),
            target_resolved=True, require_review=True, revalidate=same_target,
            operation_evidence={"critical": True, "request_bound": True, "destination": reviewed_target["destination"]}),
            lambda: dispatched.append("sent"))
    assert len(requests) == 1
    assert dispatched == []


@pytest.mark.asyncio
@pytest.mark.parametrize("accept", [True, "true", 1, None])
async def test_cdp_confirmation_cannot_reuse_a_prior_allowed_click(accept):
    policy = AciraGuardPolicy({"terminal_log": False})
    policy.begin_action_transaction(framework="browser_use", action="click", target=GuardTarget(id="send", text="Send"),
        allow=True, reason="A prior DOM click was reviewed")
    message = {"id": 1, "method": "Page.handleJavaScriptDialog", "params": {"accept": accept}}
    context = CDPMessageContext(AGENT_TO_BROWSER, "owned", 1, "/devtools/page/owned", parsed_message=message)
    decision = await policy.decide(json.dumps(message), context)
    assert decision.allow is False
    assert decision.metadata["decision"] == "block_uncontracted_confirmation"


@pytest.mark.asyncio
async def test_cdp_dialog_dismissal_remains_allowed():
    policy = AciraGuardPolicy({"terminal_log": False})
    message = {"id": 1, "method": "Page.handleJavaScriptDialog", "params": {"accept": False}}
    context = CDPMessageContext(AGENT_TO_BROWSER, "owned", 1, "/devtools/page/owned", parsed_message=message)
    assert (await policy.decide(json.dumps(message), context)).allow is True


@pytest.mark.parametrize("native,expected", [(True, "ask"), (False, "allow"), (None, "indeterminate"), ("missing", "indeterminate"), (1, "indeterminate"), ("false", "indeterminate")])
def test_replay_distinguishes_recorded_native_association_from_missing_or_invalid_facts(tmp_path, monkeypatch, native, expected):
    from acira.replay import evaluate
    from acira_cdp_proxy.facts import build_browser_event_facts
    from acira_cdp_proxy.guard import _event_facts_from_classification
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "existing", selected=["submissions"], dashboard=False)
    known = native if isinstance(native, bool) else None
    target = GuardTarget(tag="button", id="preview", text="Preview", is_button=True,
        is_submit=True, is_native_submit=known, url="https://example.test/compose", confidence=1)
    evidence = {"critical": True, "request_bound": True} if native is True else {}
    classification = ActionClassification("playwright.click", "click", "Preview", target=target,
        meaningful=True, confidence=1, details={"operation_evidence": evidence})
    facts = build_browser_event_facts(classification, None).to_dict()
    if native == "missing":
        facts["target"].pop("is_native_submit")
    elif not isinstance(native, bool):
        facts["target"]["is_native_submit"] = native
    event = {"evaluation_contract": "acira.action_facts.v1", "facts": facts,
        "classification": classification.to_metadata(), "metadata": {},
        "specific_facts": _event_facts_from_classification(classification)}
    result = evaluate(project.snapshot().config, event)
    assert result["decision"] == expected
    if expected == "indeterminate":
        assert "native form-association evidence" in result["reason"]
