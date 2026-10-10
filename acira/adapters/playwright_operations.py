"""Explicit bounded form filling. Submission is a separate operation."""
from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from acira_cdp_proxy.action_gateway import ActionRequest, AciraActionBlocked
from acira_cdp_proxy.facts import build_browser_event_facts
from acira_cdp_proxy.guard import RUNTIME_OBJECT_PROBE_JS, _target_from_probe_value, _event_facts_from_classification
from acira_cdp_proxy.operation import STATE_JS, evidence_for
from acira_cdp_proxy.policy import AGENT_TO_BROWSER, CDPMessageContext

from .playwright_boundary import CAPTURE, OWNERS, masked_artifact, trusted


async def fill_fields(manager: Any, fields: list[tuple[Any, str]], *, purpose: str = "Fill form fields") -> None:
    if not fields or len(fields) > 30:
        raise ValueError("Supply between one and thirty explicit (Locator, text) pairs")
    prepared = []
    page = None
    form = None
    for locator, text in fields:
        if not isinstance(text, str) or not hasattr(locator, "page") or await locator.count() != 1:
            raise ValueError("Each step requires exactly one Locator and a string value")
        if OWNERS.get(locator.page) is not manager or (page is not None and locator.page is not page):
            raise ValueError("All fields must belong to one page protected by this Acira run")
        page = locator.page
        element = await locator.element_handle(timeout=5000)
        state = await trusted(element, "evaluate", STATE_JS)
        if not state or not state["has_form"] or state["target"]["tag"] not in {"INPUT", "TEXTAREA"} or state["target"].get("type") in {"file", "hidden", "checkbox", "radio", "submit", "button"}:
            raise ValueError("Bounded filling supports text controls in one HTML form")
        if form is not None and (state["form_id"], state["url"]) != form:
            raise ValueError("All fields must belong to the same form and frame")
        if prepared and not await trusted(element, "evaluate", "(el,first)=>el.form===first.form", prepared[0][0]):
            raise ValueError("All fields must belong to the same form and frame")
        identifier = state["target"].get("name") or state["target"].get("id")
        if not identifier or sum(f["name"] == identifier for f in state["fields"]) != 1:
            raise ValueError("Each bounded field needs a unique name or id within its form")
        form = (state["form_id"], state["url"])
        target = _target_from_probe_value(await trusted(element, "evaluate", "el => (" + RUNTIME_OBJECT_PROBE_JS + ").call(el)"))
        if target is None:
            raise AciraActionBlocked("Cannot resolve the form target")
        prepared.append((element, state, text, target))
    if manager.guard_policy.before_decision is not None:
        await manager.guard_policy.before_decision()
    await manager._ensure_page_general_preflight(page, trigger="before_action")
    await manager._ensure_page_mapping_preflight(page)
    policy = manager.guard_policy
    if any(skill.matches_url(page.url) for skill in policy.config.specific_skills):
        raise AciraActionBlocked("Use individual guarded fills with application-specific mapped policies; bounded filling currently supports general policies and explicit rules")
    revision = policy.policy_revision
    expires = time.monotonic() + 300
    operation = uuid4().hex
    event = getattr(manager.action_gateway.checkpoint_store, "event", lambda *args, **kwargs: None)
    evidence = evidence_for(prepared[0][1], "form_input")
    evidence.pop("grant_scope", None)
    evidence["bounded_steps"] = []
    evidence["critical"] = True  # A group approval is never a reusable grant.
    decisions = []
    for element, state, text, target in prepared:
        sensitive = any(f["sensitive"] and f["name"] in {state["target"].get("name"), state["target"].get("id")} for f in state["fields"])
        evidence["bounded_steps"].append({"field": target.name or target.id, "text": "[REDACTED]" if sensitive else text})
        request = ActionRequest(action="form_input", framework="playwright", target=target,
            details={"value": text}, target_resolved=True, operation_evidence=evidence)
        classification = manager.action_gateway._classification_from_request(request)
        decision, reason, rule, cedar = policy._configured_decision(classification)
        decisions.append((classification, decision, reason))
        event("action_evaluated", operation_id=operation, policy_revision=revision,
            evaluation_contract="acira.action_facts.v1", classification=classification.to_metadata(),
            facts=build_browser_event_facts(classification, None).to_dict(), specific_facts=_event_facts_from_classification(classification),
            decision=decision, metadata={"source": cedar.source if cedar else "configured_guard", "reason": reason})
    if any(decision == "block" for _, decision, _ in decisions):
        raise AciraActionBlocked("A policy blocks one of the declared fields; no field was filled")
    async def verify(expected: list[dict]) -> bool:
        for (element, _, _, _), state in zip(prepared, expected):
            try:
                if await trusted(element, "evaluate", STATE_JS) != state:
                    return False
            except Exception:
                return False
        return True
    expected = [state for _, state, _, _ in prepared]
    token = CAPTURE.set(lambda: masked_artifact(page, prepared[0][0]))
    try:
        if any(decision == "ask" for _, decision, _ in decisions):
            classification = decisions[0][0]
            allowed = await policy._ask("{}", CDPMessageContext(AGENT_TO_BROWSER, "playwright-bounded", 0,
                "/acira/action/playwright", send_probe=await manager._make_page_probe(page)), classification,
                purpose + ": approve the listed fields and values once; submission is excluded")
            if not allowed:
                raise AciraActionBlocked("Bounded form operation rejected; no field was filled")
        if policy.before_decision is not None:
            await policy.before_decision()
        if policy.policy_revision != revision or not await verify(expected):
            raise AciraActionBlocked("The form or policy changed during review; no field was filled")
        event("bounded_operation_authorized", operation_id=operation, purpose=purpose, evidence=evidence)
        for index, (element, state, text, target) in enumerate(prepared):
            if policy.before_decision is not None:
                await policy.before_decision()
            if time.monotonic() >= expires or policy.policy_revision != revision or not await verify(expected):
                raise AciraActionBlocked("The bounded form operation changed between steps; no further steps were sent")
            transaction = policy.begin_action_transaction(framework="playwright", action="form_input", target=target,
                allow=True, reason="This step belongs to the reviewed bounded operation", metadata={"operation_id": operation}, duration=10)
            event("action_dispatched", operation_id=operation, step=index)
            try:
                await trusted(element, "fill", text)
            finally:
                policy.end_action_transaction(transaction)
            # Only the declared control may change between steps. Dependent field
            # updates require another review instead of widening the approval.
            identifier = state["target"].get("name") or state["target"].get("id")
            if not identifier:
                raise AciraActionBlocked("Fields need stable names or ids for bounded operation verification")
            for expected_state in expected:
                for field in expected_state["fields"]:
                    if field["name"] == identifier:
                        field["value"] = text
            if not await verify(expected):
                raise AciraActionBlocked("Browser effects exceeded the declared form steps; the result needs inspection")
            event("browser_call_completed", operation_id=operation, step=index, business_outcome="unverified")
    except BaseException:
        event("bounded_operation_stopped", operation_id=operation, reason="No automatic retries; inspect any already dispatched steps")
        raise
    finally:
        CAPTURE.reset(token)
        for element, _, _, _ in prepared:
            await element.dispose()
