import asyncio
import json

import pytest

from acira import protect
from acira.journal import Journal
from acira.local import atomic_json
from acira.project import catalog_policy, digest, initialize
from acira.revisions import rollback, save_configuration
from acira.runtime import ProjectRuntime
from acira_cdp_proxy.action_gateway import ActionRequest, AciraActionBlocked
from acira_cdp_proxy.guard import ActionClassification, GuardReviewRequest, GuardTarget
from acira_cdp_proxy.policy import CDPMessageContext, AGENT_TO_BROWSER


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))


def review_request(critical=False):
    return GuardReviewRequest("{}", CDPMessageContext(AGENT_TO_BROWSER, "test", 1, "/acira/action/playwright"),
        ActionClassification("playwright.click", "click", "Continue", target=GuardTarget(id="next"),
          details={"operation_evidence": {"critical": critical, "grant_scope": {"origin":"https://example.test", "target":"next", "action":"click"}}}), "Continue")


async def pending(runtime):
    for _ in range(100):
        reviews = [r for r in runtime.journal.reviews() if r["run_id"] == runtime.run_id and r["state"] == "pending"]
        if reviews:
            return reviews[0]
        await asyncio.sleep(.01)
    raise AssertionError("Review did not appear")


@pytest.mark.asyncio
async def test_reviews_and_controls_are_isolated_and_grants_are_bounded(tmp_path):
    project = initialize(tmp_path / "one", selected=[])
    one, two = ProjectRuntime(project, "default"), ProjectRuntime(project, "default")
    await one.start()
    await two.start()
    try:
        first = asyncio.create_task(one.ask(review_request()))
        second = asyncio.create_task(two.ask(review_request()))
        a, b = await pending(one), await pending(two)
        one.journal.control(one.run_id, "paused")
        await asyncio.wait_for(two.before_decision(), .5)
        one.journal.decide(a["review_id"], "approved", grant=True)
        two.journal.decide(b["review_id"], "rejected")
        assert await first == "allow"
        assert await second == "block"
        assert await asyncio.wait_for(one.ask(review_request()), .5) == "allow"
        # A critical action cannot consume the routine grant or create one.
        critical = asyncio.create_task(one.ask(review_request(critical=True)))
        c = await pending(one)
        with pytest.raises(ValueError):
            one.journal.decide(c["review_id"], "approved", grant=True)
        one.journal.decide(c["review_id"], "rejected")
        assert await critical == "block"
        one.observe_context({"page":"one"})
        one.observe_context({"page":"two"})
        with one.journal.db() as db:
            assert db.execute("SELECT COUNT(*) FROM grants").fetchone()[0] == 0
    finally:
        await one.close()
        await two.close()


@pytest.mark.asyncio
async def test_policy_change_invalidates_pending_review(tmp_path):
    project = initialize(tmp_path / "one", selected=[])
    runtime = ProjectRuntime(project, "default")
    task = asyncio.create_task(runtime.ask(review_request()))
    review = await pending(runtime)
    document = project.document()
    document["agents"]["default"]["guard"] = {"blocked_actions": ["click"]}
    atomic_json(project.path, document)
    assert await asyncio.wait_for(task, 1) == "block"
    assert runtime.journal.review(review["review_id"])["state"] == "invalidated"


def test_review_and_audit_response_are_atomic(tmp_path, monkeypatch):
    journal = Journal(tmp_path)
    review = journal.create_review("run", {})
    def disk_full(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(journal, "_append_event", disk_full)
    with pytest.raises(OSError):
        journal.decide(review["review_id"], "approved")
    assert journal.review(review["review_id"])["state"] == "pending"


def test_configuration_assignments_and_rollback_preserve_other_agents(tmp_path):
    project = initialize(tmp_path / "one", selected=["payment"])
    initialize(project.root, agent="independent", general="separate/general", specific="separate/specific")
    before = project.snapshot()
    independent = project.snapshot("independent").revision
    policy = project.policies("default")[0]
    value = policy["document"]
    value["acira_decisions"]["checkout.card_data_entry"] = "block"
    project.save_policy("default", "checkout.json", value, expected=policy["hash"])
    changed = project.snapshot()
    result = rollback(project, "default", before.revision, expected=changed.revision)
    assert result["revision"] not in {before.revision, changed.revision}
    assert project.snapshot().config.skill_policies == before.config.skill_policies
    assert project.snapshot("independent").revision == independent
    assert not (project.root / ".acira/policy-transaction.json").exists()
    config = project.document()
    config["agents"]["default"]["decisions"] = {policy["id"]: {"nonexistent-risk": "allow"}}
    with pytest.raises(ValueError):
        save_configuration(project, config, expected=digest(project.document()))


@pytest.mark.asyncio
async def test_replay_uses_recorded_facts_without_invented_values(tmp_path):
    from acira.replay import evaluate
    project = initialize(tmp_path / "one", selected=[])
    config = project.document()
    config["agents"]["default"]["guard"] = {"blocked_actions": ["click"]}
    atomic_json(project.path, config)
    async with protect(project=project.root, dashboard=False) as guard:
        with pytest.raises(AciraActionBlocked):
            await guard.action_gateway.guard_execute(ActionRequest(action="click", framework="playwright",
                target=GuardTarget(id="send", text="Send", confidence=1), target_resolved=True), lambda: pytest.fail("Blocked action executed"))
    event = next(e for e in Journal(project.root).events() if e["type"] == "action_evaluated")
    result = evaluate(project.snapshot().config, event)
    assert result["decision"] == event["decision"] == "block"
    del event["facts"]["action"]["confidence"]
    assert evaluate(project.snapshot().config, event)["decision"] == "indeterminate"


@pytest.mark.asyncio
async def test_replay_separates_review_policy_from_human_authorization(tmp_path):
    from acira.replay import evaluate
    project = initialize(tmp_path / "one", selected=[])
    config = project.document()
    config["agents"]["default"]["guard"] = {"review_actions":["click"]}
    atomic_json(project.path, config)
    class Reviewer:
        async def ask(self, request):
            return "allow"
    async with protect(project=project.root, dashboard=False, review_backend=Reviewer()) as guard:
        await guard.action_gateway.guard_execute(ActionRequest(action="click", framework="playwright",
            target=GuardTarget(id="next", text="Next", confidence=1), target_resolved=True),
            lambda: None)
    event = next(item for item in Journal(project.root).events() if item["type"] == "action_evaluated")
    assert event["decision"] == "allow"
    assert event["policy_decision"] == "ask"
    assert evaluate(project.snapshot().config,event)["decision"] == "ask"


@pytest.mark.asyncio
async def test_missing_payment_facts_block_before_human_review(tmp_path):
    from dataclasses import replace
    project = initialize(tmp_path / "one", selected=["payment"])
    class Reviewer:
        async def ask(self, request):
            pytest.fail("Incomplete payment evidence must not be offered for approval")
    runtime = ProjectRuntime(project,"default",dashboard=False,review_backend=Reviewer())
    request = review_request(critical=True)
    classification = replace(request.classification, details={**request.classification.details,
        "cedar":{"general_skills":{"detections":[{"skill_id":"acira.general.checkout","risk":"checkout.payment_submit_attempt","severity":"critical"}]}}})
    assert await runtime.ask(replace(request,classification=classification)) == "block"
    assert not runtime.journal.reviews()
    assert any(event["type"] == "review_evidence_missing" for event in runtime.journal.events())


@pytest.mark.asyncio
async def test_invalidation_signal_after_dispatch_never_retries():
    from acira_cdp_proxy.action_gateway import ActionGateway, PolicyRevisionChanged
    gateway = ActionGateway(config={"terminal_log":False})
    attempts = []
    async def valid():
        return True
    def execute():
        attempts.append(True)
        raise PolicyRevisionChanged()
    with pytest.raises(AciraActionBlocked, match="automatic retry"):
        await gateway.guard_execute(ActionRequest(action="click", framework="playwright",
            target=GuardTarget(id="next"), revalidate=valid), execute)
    assert attempts == [True]
