"""Denials and required submission evidence survive other policy reviews."""
from dataclasses import replace

import pytest

from acira.replay import evaluate
from acira.project import catalog_policy
from acira_cdp_proxy.action_gateway import ActionGateway, ActionRequest, AciraActionBlocked
from acira_cdp_proxy.config import AciraGuardConfig, AciraGuardRule
from acira_cdp_proxy.facts import build_browser_event_facts
from acira_cdp_proxy.general_skills import GeneralSkill
from acira_cdp_proxy.guard import ActionClassification, AciraGuardPolicy, GuardTarget, _event_facts_from_classification
from acira_cdp_proxy.skills import SpecificSkill

POLICY = "acira.general.form_submission"
RISK = "submission.outbound_attempt"


def captured_send():
    return SpecificSkill.from_dict({"schema": "acira.specific_skill.v2", "id": "captured-send",
        "name": "Captured Send", "scope": {"url_patterns": ["https://example.test/*"]},
        "targets": [{"id": "send", "decision": "ask", "reason": "Review captured Send",
                     "fingerprint": {"tag": "button", "type": "button", "id": "send", "text": "Send"}}]})


def classification(*, secret=False, evidence=None):
    details = {}
    if secret:
        details["secret_use"] = {"id": "fixture-reference", "policy_decision": "ask"}
    if evidence is not None:
        details["operation_evidence"] = evidence
    return ActionClassification(method="playwright.click", action="click", reason="Resolved Send",
        confidence=1, meaningful=True, target=GuardTarget(tag="button", type="button", id="send",
            text="Send", url="https://example.test/", is_button=True, is_form_control=True,
            is_native_submit=False, confidence=1), details=details)


def config(*, specific=False, general=None, core_block=False, core_ask=False):
    base = AciraGuardConfig.from_dict({"terminal_log": False, "terminal_review": False})
    return replace(base, specific_skills=(captured_send(),) if specific else (),
        general_skills=(GeneralSkill.from_dict(catalog_policy("submissions")),) if general else (),
        skill_policies={POLICY: {RISK: general}} if general else {},
        rules=(AciraGuardRule(id="no-click", actions=("click",), decision="block", reason="Explicit prohibition"),) if core_block else
              (AciraGuardRule(id="review-click", actions=("click",), decision="ask", reason="Core review"),) if core_ask else ())


@pytest.mark.parametrize("source", ["secret", "specific"])
@pytest.mark.parametrize("include_general", [False, True])
def test_review_policy_cannot_mask_explicit_core_denial(source, include_general):
    policy = AciraGuardPolicy(config(specific=source == "specific", core_block=True))
    item = classification(secret=source == "secret")
    # Establish that each real competing policy asks, rather than mocking its evaluator.
    if source == "secret":
        assert policy._secret_policy_decision(item).decision == "ask"
    else:
        assert policy.decision_engine.decide_specific_skills(
            facts=_event_facts_from_classification(item), skills=policy.config.specific_skills).decision == "ask"
    decision, reason, rule, _ = policy._configured_decision(item, include_general=include_general)
    assert decision == "block" and reason == "Explicit prohibition" and rule.id == "no-click"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["configured", "live"])
async def test_captured_review_cannot_override_general_denial(mode):
    class Reviewer:
        async def ask(self, request):
            pytest.fail("A general denial cannot be offered for approval")
    policy = AciraGuardPolicy(config(specific=True, general="block"), review_backend=Reviewer())
    if mode == "configured":
        decision, _, _, cedar = policy._configured_decision(classification())
        assert decision == "block" and cedar.source == "general_skill"
        assert cedar.metadata["skills"]["matches"][0]["skill_id"] == "captured-send"
    else:
        with pytest.raises(AciraActionBlocked):
            await ActionGateway(policy=policy).guard_execute(ActionRequest(action="click", framework="playwright",
                target=classification().target, confidence=1, target_resolved=True),
                lambda: pytest.fail("A denied operation dispatched"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["configured", "live"])
async def test_combined_review_retains_capture_identity_and_submission_requirements(mode):
    seen = []
    class Reviewer:
        async def ask(self, request):
            seen.append(request.classification.details)
            return "block"
    policy = AciraGuardPolicy(config(specific=True, general="ask"), review_backend=Reviewer())
    evidence = {"critical": True, "request_bound": True, "verified_fields": {"note": "Fixture"}}
    if mode == "configured":
        decision, reason, _, cedar = policy._configured_decision(classification(evidence=evidence))
        assert decision == "ask" and reason == "Review captured Send"
        assert cedar.source == "specific_skill" and cedar.skill_decision_id == "captured-send:send"
        details = {"cedar": cedar.metadata}
    else:
        with pytest.raises(AciraActionBlocked):
            await ActionGateway(policy=policy).guard_execute(ActionRequest(action="click", framework="playwright",
                target=classification().target, confidence=1, target_resolved=True, operation_evidence=evidence),
                lambda: pytest.fail("Rejected operation dispatched"))
        assert len(seen) == 1
        details = seen[0]
        assert details["cedar_source"] == "specific_skill"
        assert details["skill_decision_id"] == "captured-send:send"
    cedar_metadata = details["cedar"]
    assert cedar_metadata["skills"]["matches"][0]["target_id"] == "send"
    detection = cedar_metadata["general_skills"]["detections"][0]
    assert (detection["skill_id"], detection["risk"]) == (POLICY, RISK)
    assert policy.config.general_skills[0].review_requirements[RISK] == {"request_binding": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["general", "core", "captured"])
@pytest.mark.parametrize("bound", [None, False, "true", 1], ids=["missing", "false", "string", "integer"])
async def test_legacy_review_backend_cannot_approve_missing_or_nonboolean_submission_binding(source, bound):
    class Reviewer:
        calls = 0
        async def ask(self, request):
            self.calls += 1
            return "allow"
    reviewer = Reviewer()
    policy = AciraGuardPolicy(config(specific=source == "captured", core_ask=source == "core", general="ask"),
                              review_backend=reviewer)
    records = []
    policy.durable_event = lambda kind, **payload: records.append({"type": kind, **payload})
    dispatched = []
    evidence = {"critical": True}
    if bound is not None:
        evidence["request_bound"] = bound
    with pytest.raises(AciraActionBlocked):
        await ActionGateway(policy=policy).guard_execute(ActionRequest(action="click", framework="playwright",
            target=classification().target, confidence=1, target_resolved=True, operation_evidence=evidence),
            lambda: dispatched.append("fixture effect"))
    assert reviewer.calls == 0 and dispatched == []
    assert records == [{"type": "review_evidence_missing", "missing": ["verified request binding"],
        "policy_id": POLICY, "risk": RISK,
        "reason": "Use the supported submit() contract to bind the reviewed destination and values"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["general", "core", "captured"])
async def test_legacy_bound_synthetic_operation_requires_actual_fields_before_review(source):
    document = catalog_policy("submissions")
    document["acira_review"][RISK]["required_fields"] = ["recipient"]
    configured = replace(config(specific=source == "captured", core_ask=source == "core", general="ask"),
                         general_skills=(GeneralSkill.from_dict(document),))
    class Reviewer:
        calls = 0
        async def ask(self, request):
            self.calls += 1
            assert request.classification.details["operation_evidence"]["verified_fields"] == {"recipient": "Fixture"}
            return "allow"
    reviewer = Reviewer()
    policy = AciraGuardPolicy(configured, review_backend=reviewer)
    gateway = ActionGateway(policy=policy)
    dispatched = []
    for value in (None, "", "[REDACTED]"):
        with pytest.raises(AciraActionBlocked):
            await gateway.guard_execute(ActionRequest(action="click", framework="playwright",
                target=classification().target, confidence=1, target_resolved=True,
                operation_evidence={"critical": True, "request_bound": True, "verified_fields": {"recipient": value}}),
                lambda: dispatched.append("fixture effect"))
    assert reviewer.calls == 0 and dispatched == []
    await gateway.guard_execute(ActionRequest(action="click", framework="playwright",
        target=classification().target, confidence=1, target_resolved=True,
        operation_evidence={"critical": True, "request_bound": True, "verified_fields": {"recipient": "Fixture"}}),
        lambda: dispatched.append("fixture effect"))
    assert reviewer.calls == 1 and dispatched == ["fixture effect"]


@pytest.mark.parametrize("bound,expected", [(False, "indeterminate"), (True, "ask"),
    ("true", "indeterminate"), (1, "indeterminate"), (None, "indeterminate")],
    ids=["false", "true", "string", "integer", "missing"])
def test_replay_preserves_general_evidence_requirements_beneath_captured_review(bound, expected):
    item = classification(evidence={"critical": True, "request_bound": bound,
                                    "verified_fields": {"note": "Fixture"}})
    event = {"evaluation_contract": "acira.action_facts.v1", "classification": item.to_metadata(),
             "facts": build_browser_event_facts(item).to_dict(),
             "specific_facts": _event_facts_from_classification(item), "metadata": {}}
    result = evaluate(config(specific=True, general="ask"), event)
    assert result["decision"] == expected
    if bound is True:
        assert result["policy_id"] == "captured-send:send" and result["source"] == "specific_skill"
    else:
        assert "verified request binding" in result["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["specific_skill_mapping_review", "general_skill_preflight"])
@pytest.mark.parametrize("operation", ["outbound", "page_acknowledgement"])
async def test_cached_page_acknowledgement_never_authorizes_combined_outbound_review(monkeypatch, source, operation):
    from acira_cdp_proxy.decision_engine import CedarDecision
    from acira_cdp_proxy.guard import _review_cache_key
    from acira_cdp_proxy.policy import CDPPolicyDecision

    class Reviewer:
        calls = 0
        async def ask(self, request):
            self.calls += 1
            return "allow"
    reviewer = Reviewer()
    policy = AciraGuardPolicy(config(general="ask"), review_backend=reviewer)
    page = {"url": "https://example.test/", "page_signature": "same-observed-page", "decision": "ask"}
    cedar = CedarDecision(decision="ask", reason="Review page acknowledgement", source=source,
        skill_decision_id="fixture-page:ack", metadata={
            "skills": {"page_mapping": [], "page_mapping_session_key": "same-observed-page"},
            "preflight": page})
    # The page gate itself is controlled; the final general-policy evaluator,
    # actual cache selection and actual gateway dispatch remain production code.
    monkeypatch.setattr(policy, "_configured_decision", lambda *args, **kwargs: ("ask", cedar.reason, None, cedar))
    continued = []
    async def continue_page(*args, **kwargs):
        continued.append("page acknowledgement")
        return CDPPolicyDecision(allow=True, metadata={"decision": "continue_after_cached_preflight_review"})
    monkeypatch.setattr(policy, "_continue_after_preflight_review_approval", continue_page)
    request = ActionRequest(action="click" if operation == "outbound" else "mapping_preflight",
        framework="playwright", target=classification().target if operation == "outbound" else None,
        confidence=1, target_resolved=True, operation_evidence={"critical": operation == "outbound"})
    gateway = ActionGateway(policy=policy)
    item = gateway._classification_from_request(request)
    _, _, _, combined = policy._combine_general_decision(item, None, "ask", cedar.reason, None, cedar)
    policy._review_approved_keys.add(_review_cache_key(item, combined))
    dispatched = []
    if operation == "outbound":
        with pytest.raises(AciraActionBlocked):
            await gateway.guard_execute(request, lambda: dispatched.append("fixture effect"))
        assert dispatched == [] and continued == [] and reviewer.calls == 0
    else:
        await gateway.guard_execute(request, lambda: dispatched.append("fixture effect"))
        assert dispatched == ["fixture effect"] and reviewer.calls == 0
        assert continued == (["page acknowledgement"] if source == "general_skill_preflight" else [])


def real_page_review_policy(*, reviewer, general="ask", core_block=False, page_decision="ask"):
    """Seed a real evaluated page snapshot, without mocking any guard method."""
    from acira_cdp_proxy.config import GeneralSkillPreflightConfig
    page = GeneralSkill.from_dict({
        "schema": "acira.general_skill.v3", "id": "fixture.page", "name": "Fixture page review", "version": 1,
        "requires": ["page", "action"], "lexicons": {},
        "rules": {"only_preflight": {"equals": {"path": "action.action", "value": "page_preflight"}}},
        "risks": [{"id": "page.review", "context": "page", "severity": "low", "description": "Controlled page review",
                   "detection": {"when": ["only_preflight"], "reason": "Review the fixture page"}}]})
    base = config(general=general, core_block=core_block)
    configured = replace(base, general_skills=(page, *base.general_skills), has_page_policy=True,
        general_skill_preflight=GeneralSkillPreflightConfig(enabled=True),
        skill_policies={**base.skill_policies, page.id: {"page.review": page_decision}})
    policy = AciraGuardPolicy(configured, review_backend=reviewer)
    target = classification().target
    snapshot = policy.evaluate_page_preflight_targets([target.to_metadata()],
        {"url": target.url, "title": "Fixture", "page_signature": "exact-fixture-page", "status": "ok"},
        scope_key="/acira/action/playwright")
    assert snapshot.decision == page_decision
    assert all(item["context"] == "page" for item in snapshot.cedar_metadata["general_skills"]["detections"])
    policy._remember_preflight_snapshot(snapshot)
    return policy, snapshot


def bound_request(*, evidence=None, details=None):
    return ActionRequest(action="click", framework="playwright", target=classification().target,
        confidence=1, target_resolved=True, details=details or {},
        operation_evidence=evidence if evidence is not None else
        {"critical": True, "request_bound": True, "verified_fields": {"note": "Fixture"}})


@pytest.mark.asyncio
async def test_real_page_acknowledgement_and_operation_review_are_separate_and_next_action_is_fresh():
    import asyncio
    requests = []
    class Reviewer:
        async def ask(self, request):
            requests.append(request.classification)
            assert len(requests) <= 3, "Page acknowledgement must not recursively request the same action review"
            return "allow"
    policy, snapshot = real_page_review_policy(reviewer=Reviewer())
    gateway = ActionGateway(policy=policy)
    effects = []
    for _ in range(2):
        await asyncio.wait_for(gateway.guard_execute(bound_request(), lambda: effects.append("fixture effect")), timeout=1)
    assert effects == ["fixture effect", "fixture effect"]
    assert [request.action for request in requests] == ["page_preflight", "click", "click"]
    assert requests[0].target is None and "operation_evidence" not in requests[0].details
    assert {item["context"] for item in requests[0].details["cedar"]["general_skills"]["detections"]} == {"page"}
    assert policy._preflight_review_cache_key(snapshot) in policy._review_approved_keys
    assert all(request.details["cedar_source"] == "general_skill" for request in requests[1:])
    assert all(request.details["operation_evidence"]["request_bound"] is True for request in requests[1:])
    assert all(any(item["risk"] == RISK and item["context"] == "action"
                   for item in request.details["cedar"]["general_skills"]["detections"]) for request in requests[1:])
    # Only the separately scoped page acknowledgement was cached.
    assert len(policy._review_approved_keys) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [None, False, "true", 1], ids=["missing", "false", "string", "integer"])
async def test_real_combined_preflight_with_missing_submission_binding_blocks_before_page_review(bound):
    class Reviewer:
        async def ask(self, request):
            pytest.fail("Incomplete outbound evidence must block before even a page acknowledgement")
    policy, snapshot = real_page_review_policy(reviewer=Reviewer())
    effects = []
    evidence = {"critical": True, "verified_fields": {"note": "Fixture"}}
    if bound is not None:
        evidence["request_bound"] = bound
    with pytest.raises(AciraActionBlocked):
        await ActionGateway(policy=policy).guard_execute(bound_request(evidence=evidence), lambda: effects.append("fixture effect"))
    assert effects == [] and policy._preflight_review_cache_key(snapshot) not in policy._review_approved_keys


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["core", "general"])
async def test_real_combined_preflight_cannot_turn_core_or_general_denial_into_review(block):
    class Reviewer:
        async def ask(self, request):
            pytest.fail("A page acknowledgement cannot override a policy denial")
    policy, snapshot = real_page_review_policy(reviewer=Reviewer(),
        core_block=block == "core", general="block" if block == "general" else "ask")
    effects = []
    with pytest.raises(AciraActionBlocked):
        await ActionGateway(policy=policy).guard_execute(bound_request(), lambda: effects.append("fixture effect"))
    assert effects == [] and policy._preflight_review_cache_key(snapshot) not in policy._review_approved_keys


@pytest.mark.asyncio
async def test_stale_caller_cedar_cannot_hide_current_general_submission_requirements():
    class Reviewer:
        async def ask(self, request):
            pytest.fail("Stale caller policy metadata cannot hide a required request contract")
    policy = AciraGuardPolicy(config(general="ask"), review_backend=Reviewer())
    effects = []
    with pytest.raises(AciraActionBlocked):
        await ActionGateway(policy=policy).guard_execute(bound_request(
            evidence={"critical": True}, details={"cedar": {"general_skills": {"detections": []}},
                "cedar_source": "stale-page", "skill_decision_id": "stale:page"}), lambda: effects.append("fixture effect"))
    assert effects == []


@pytest.mark.asyncio
async def test_real_page_acknowledgement_continuation_rechecks_specific_target_denial():
    requests = []
    policy = None
    class Reviewer:
        async def ask(self, request):
            requests.append(request.classification.action)
            assert request.classification.action == "page_preflight"
            # Simulate a newly loaded captured-target rule while the page review
            # is pending. The real continuation must evaluate this target again.
            document = {"schema": "acira.specific_skill.v2", "id": "captured-send-denied", "name": "Captured Send denied",
                "scope": {"url_patterns": ["https://example.test/*"]},
                "targets": [{"id": "send", "decision": "block", "reason": "Captured prohibition",
                    "fingerprint": {"tag": "button", "type": "button", "id": "send", "text": "Send"}}]}
            policy.config = replace(policy.config, specific_skills=(SpecificSkill.from_dict(document),))
            return "allow"
    policy, snapshot = real_page_review_policy(reviewer=Reviewer())
    effects = []
    with pytest.raises(AciraActionBlocked, match="Captured prohibition"):
        await ActionGateway(policy=policy).guard_execute(bound_request(), lambda: effects.append("fixture effect"))
    assert requests == ["page_preflight"] and effects == []
    assert policy._preflight_review_cache_key(snapshot) in policy._review_approved_keys


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["core", "captured", "secret"])
async def test_actual_page_denial_cannot_be_masked_by_core_or_captured_action_review(source):
    class Reviewer:
        async def ask(self, request):
            pytest.fail("No review may override an applicable evaluated page denial")
    policy, snapshot = real_page_review_policy(reviewer=Reviewer(), page_decision="block")
    policy.config = replace(policy.config, specific_skills=(captured_send(),) if source == "captured" else (),
        rules=(AciraGuardRule(id="review-click", actions=("click",), decision="ask", reason="Core review"),)
        if source == "core" else ())
    # Prove the competing policy genuinely requires review before the page gate.
    decision, _, _, _ = policy._configured_decision(classification(secret=source == "secret"), include_general=False)
    assert decision == "ask" and snapshot.decision == "block"
    effects = []
    with pytest.raises(AciraActionBlocked):
        await ActionGateway(policy=policy).guard_execute(bound_request(details=classification(secret=source == "secret").details),
            lambda: effects.append("fixture effect"))
    assert effects == [] and policy._preflight_review_cache_key(snapshot) not in policy._review_approved_keys


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["core", "captured"])
@pytest.mark.parametrize("include_submission", [False, True])
async def test_real_page_acknowledgement_preserves_separate_core_or_captured_action_review(source, include_submission):
    requests = []
    class Reviewer:
        async def ask(self, request):
            requests.append(request)
            assert len(requests) <= 2, "Page acknowledgement and operation must not form a recursive review loop"
            return "allow"
    policy, snapshot = real_page_review_policy(reviewer=Reviewer(), general="ask" if include_submission else None)
    policy.config = replace(policy.config, specific_skills=(captured_send(),) if source == "captured" else (),
        rules=(AciraGuardRule(id="review-click", actions=("click",), decision="ask", reason="Core review"),)
        if source == "core" else ())
    gateway = ActionGateway(policy=policy)
    effects = []
    await gateway.guard_execute(bound_request(), lambda: effects.append("fixture effect"))
    assert effects == ["fixture effect"]
    assert [request.classification.action for request in requests] == ["page_preflight", "click"]
    assert requests[0].classification.target is None and "operation_evidence" not in requests[0].classification.details
    assert requests[1].classification.target.id == "send"
    assert requests[1].classification.details["operation_evidence"]["request_bound"] is True
    assert requests[1].reason == ("Review captured Send" if source == "captured" else "Core review")
    outcome = gateway.checkpoint_store.decisions[-1]["metadata"]
    assert outcome["preflight_review"]["review_result"] == "allow"
    if source == "captured":
        assert outcome["source"] == "specific_skill" and outcome["skill_decision_id"] == "captured-send:send"
        assert requests[1].classification.details["cedar"]["skills"]["matches"][0]["target_id"] == "send"
    else:
        assert outcome["rule_id"] == "review-click"
    if include_submission:
        assert any(item["risk"] == RISK for item in requests[1].classification.details["cedar"]["general_skills"]["detections"])
    assert policy._preflight_review_cache_key(snapshot) in policy._review_approved_keys
    assert len(policy._review_approved_keys) == 1
