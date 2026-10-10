"""PR2 step 2: the guard's decide() wrapper emits exactly one audit event per
AGENT_TO_BROWSER decision, never for browser→agent, and never breaks on audit failure."""

from __future__ import annotations

from acira_cdp_proxy.audit_schema import EventValidator
from acira_cdp_proxy.guard import AciraGuardPolicy
from acira_cdp_proxy.policy import (
    AGENT_TO_BROWSER,
    BROWSER_TO_AGENT,
    CDPMessageContext,
    CDPPolicyDecision,
)


def _ctx(direction, method="Input.dispatchKeyEvent"):
    return CDPMessageContext(
        direction=direction,
        connection_id="c1",
        message_number=1,
        websocket_path="/devtools/page/x",
        parsed_message={"id": 1, "method": method, "params": {}},
    )


def _fake_inner(decision):
    async def inner(payload, context):
        return decision
    return inner


async def test_decide_emits_exactly_one_valid_event(monkeypatch):
    policy = AciraGuardPolicy(config={"enabled": True, "agent_name": "t", "audit": {"mode": "volatile"}})
    monkeypatch.setattr(
        policy, "_decide_inner",
        _fake_inner(CDPPolicyDecision(allow=True, metadata={"decision": "allow", "classification": {"action": "click"}})),
    )
    await policy.decide(b"{}", _ctx(AGENT_TO_BROWSER))
    rb = policy.audit_pipeline.ring_buffer
    assert rb.size == 1
    event = rb.snapshot()[0]
    assert EventValidator().validate(event) == []
    assert event["decision"] == "allow" and event["event_type"] == "guard_decision"
    assert event["action_type"] == "click"


async def test_block_decision_audited_as_block(monkeypatch):
    policy = AciraGuardPolicy(config={"enabled": True, "agent_name": "t"})
    monkeypatch.setattr(
        policy, "_decide_inner",
        _fake_inner(CDPPolicyDecision(allow=False, reason="nope", metadata={"decision": "block", "rule_id": "blocked_actions", "classification": {"action": "form_input"}})),
    )
    await policy.decide(b"{}", _ctx(AGENT_TO_BROWSER))
    event = policy.audit_pipeline.ring_buffer.snapshot()[0]
    assert event["decision"] == "block" and event["decision_source"] == "explicit_rule"


async def test_browser_to_agent_not_audited(monkeypatch):
    policy = AciraGuardPolicy(config={"enabled": True})
    monkeypatch.setattr(policy, "_decide_inner", _fake_inner(CDPPolicyDecision(allow=True)))
    await policy.decide(b"{}", _ctx(BROWSER_TO_AGENT))
    assert policy.audit_pipeline.ring_buffer.size == 0


async def test_audit_failure_does_not_break_decide(monkeypatch):
    policy = AciraGuardPolicy(config={"enabled": True})
    monkeypatch.setattr(policy, "_decide_inner", _fake_inner(CDPPolicyDecision(allow=True, metadata={"decision": "allow"})))
    pipe = policy.audit_pipeline

    def boom(**_kwargs):
        raise RuntimeError("audit boom")

    monkeypatch.setattr(pipe, "record_decision", boom)
    decision = await policy.decide(b"{}", _ctx(AGENT_TO_BROWSER))
    assert decision.allow is True  # guard hot path unaffected by audit failure
