"""Known API-key fields stay private without altering live revalidation values."""
from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from acira.project import initialize
from acira.runtime import ProjectRuntime
from acira_cdp_proxy.guard import ActionClassification, AciraGuardPolicy, GuardReviewRequest, GuardTarget
from acira_cdp_proxy.operation import bind_cdp, evidence_for, is_critical_operation
from acira_cdp_proxy.policy import AGENT_TO_BROWSER, CDPMessageContext

ALIASES = ("api_key", "api-key", "apiKey", "X-API-Key", "client_api_key")
MATERIAL = "local-fixture-key"


def state_for(name="api_key", *, sensitive=False):
    return {"url": "https://example.invalid/settings", "has_form": True,
        "form_id": "settings", "destination": "https://example.invalid/settings", "method": "get",
        "target": {"tag": "BUTTON", "id": "preview", "name": "", "type": "button", "text": "Preview"},
        "fields": [{"name": name, "type": "text", "value": MATERIAL, "checked": None,
                    "disabled": False, "sensitive": sensitive},
                   {"name": "message", "type": "text", "value": "Ordinary local draft",
                    "checked": None, "disabled": False, "sensitive": False}]}


@pytest.mark.parametrize("alias", ALIASES)
def test_false_observation_flag_cannot_expose_known_api_field_or_enable_routine_grant(alias):
    state = state_for(alias)
    original = copy.deepcopy(state)
    evidence = evidence_for(state, "click")
    assert is_critical_operation(state, "click") is True
    assert evidence["critical"] is True and "grant_scope" not in evidence
    assert evidence["fields"][0]["display"] == "[REDACTED]"
    assert evidence["fields"][1]["display"] == "Ordinary local draft"
    assert state == original and state["fields"][0]["value"] == MATERIAL


@pytest.mark.parametrize("ordinary", ["key", "monkey", "apikeyboard"])
def test_ordinary_field_names_are_not_broadened_to_api_secrets(ordinary):
    state = state_for(ordinary)
    evidence = evidence_for(state, "click")
    assert is_critical_operation(state, "click") is False
    assert evidence["fields"][0]["display"] == MATERIAL
    assert "grant_scope" in evidence


def test_existing_true_sensitive_flag_remains_private_without_api_alias():
    evidence = evidence_for(state_for("custom_private_field", sensitive=True), "click")
    assert evidence["critical"] is True
    assert evidence["fields"][0]["display"] == "[REDACTED]"
    assert "grant_scope" not in evidence


@pytest.mark.asyncio
async def test_bound_evidence_masks_api_field_but_revalidates_its_original_private_value():
    state = state_for()
    released = []
    async def probe(method, params, session_id):
        if method == "Runtime.evaluate":
            return {"result": {"objectId": "owned-local-object"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": copy.deepcopy(state)}}
        if method == "Runtime.releaseObject":
            released.append(params["objectId"])
            return {}
        raise AssertionError("Unexpected private probe")
    target = GuardTarget(tag="button", id="preview", url=state["url"])
    binding = await bind_cdp(probe, target, "click")
    try:
        assert binding.evidence["critical"] is True
        assert binding.evidence["fields"][0]["display"] == "[REDACTED]"
        assert await binding.revalidate() is True
        state["fields"][0]["value"] = "changed-local-fixture-key"
        assert await binding.revalidate() is False
    finally:
        await binding.release()
    assert released == ["owned-local-object"]


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ALIASES)
async def test_actual_captured_only_runtime_cannot_reuse_api_field_review_or_persist_its_value(tmp_path, monkeypatch, alias):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "existing", selected=[], dashboard=False)
    document = {"schema": "acira.specific_skill.v2", "id": "captured-preview", "name": "Captured preview",
        "scope": {"url_patterns": ["https://example.invalid/*"]}, "targets": [{"id": "preview",
            "decision": "ask", "reason": "Review captured preview", "fingerprint": {
                "tag": "button", "id": "preview", "type": "button", "text": "Preview"}}]}
    (project.root / "policies/specific/captured.json").write_text(json.dumps(document))

    class Reviewer:
        calls = 0
        async def ask(self, request):
            self.calls += 1
            projected = request.classification.details["operation_evidence"]
            assert projected["fields"][0]["display"] == "[REDACTED]"
            assert projected["fields"][1]["display"] == "Ordinary local draft"
            review = next(item for item in runtime.journal.reviews() if item["state"] == "pending")
            assert review["grant_eligible"] is False
            with pytest.raises(ValueError, match="cannot receive a reusable grant"):
                runtime.journal.decide(review["review_id"], "approved", grant=True)
            assert runtime.journal.review(review["review_id"])["state"] == "pending"
            assert runtime.journal.decide(review["review_id"], "rejected")
            return "block"

    reviewer = Reviewer()
    runtime = ProjectRuntime(project, "default", dashboard=False, review_backend=reviewer)
    try:
        state = state_for(alias)
        target = GuardTarget(tag="button", id="preview", type="button", text="Preview", url=state["url"],
                            is_button=True, is_native_submit=False, confidence=1)
        item = ActionClassification(method="playwright.click", action="click", reason="Captured preview",
                                    meaningful=True, target=target)
        policy = AciraGuardPolicy(replace(runtime.snapshot.config, adapters={}, terminal_log=False))
        decision, _, _, cedar = policy._configured_decision(item)
        assert decision == "ask" and cedar.source == "specific_skill"
        evidence = evidence_for(state, "click")
        item = replace(item, details={"operation_evidence": evidence, "cedar": cedar.metadata})
        request = GuardReviewRequest(payload="{}", context=CDPMessageContext(AGENT_TO_BROWSER, "local", 1, "/"),
                                    classification=item, reason="Review captured preview")
        assert [await runtime.ask(request), await runtime.ask(request)] == ["block", "block"]
        assert reviewer.calls == 2
        events = runtime.journal.events(limit=1000)
        assert not any(event["type"] in {"grant_created", "grant_used"} for event in events)
        assert MATERIAL not in json.dumps(events)
        assert MATERIAL not in json.dumps(runtime.journal.reviews())
        with runtime.journal.db() as database:
            assert database.execute("SELECT COUNT(*) FROM grants").fetchone()[0] == 0
        assert state["fields"][0]["value"] == MATERIAL
    finally:
        await runtime.close()
