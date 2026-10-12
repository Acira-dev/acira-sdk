"""Recognized outgoing controls never receive routine reusable grants."""
from dataclasses import replace
import json

import pytest

from acira.project import initialize
from acira.runtime import ProjectRuntime
from acira_cdp_proxy.guard import ActionClassification, AciraGuardPolicy, GuardReviewRequest, GuardTarget
from acira_cdp_proxy.operation import evidence_for, is_critical_operation
from acira_cdp_proxy.policy import AGENT_TO_BROWSER, CDPMessageContext
from acira_cdp_proxy.redaction import redact_value


def observed_state(**identity):
    return {"url": "https://example.test/", "has_form": False, "form_id": None,
        "destination": "https://example.test/", "method": None,
        "target": {"tag": "BUTTON", "id": "action", "name": "", "type": "button", "text": "", **identity},
        "fields": []}


@pytest.mark.parametrize("word", ["submit", "send", "post", "publish", "apply", "finalize"])
def test_outbound_identity_separator_is_critical(word):
    state = observed_state(id=word + "_draft")
    assert is_critical_operation(state, "click")
    assert "grant_scope" not in evidence_for(state, "click")


@pytest.mark.parametrize("attribute", ["aria_label", "title", "href", "placeholder", "autocomplete"])
def test_outbound_identity_metadata_is_critical(attribute):
    state = observed_state(**{attribute: "Send draft"})
    assert is_critical_operation(state, "click")
    assert "grant_scope" not in evidence_for(state, "click")


@pytest.mark.parametrize("identity", [
    {"id": "preview", "text": "Preview"},
    {"id": "resendable", "text": "View"},
    {"id": "sendable_preview", "text": "Preview"},
])
def test_unrecognized_substring_does_not_disable_routine_scope(identity):
    state = observed_state(**identity)
    assert is_critical_operation(state, "click") is False
    assert evidence_for(state, "click")["grant_scope"]["target"] == state["target"]


def test_absolute_identity_href_keeps_routine_scope_query_values_redacted():
    # STATE_JS resolves raw relative href before adding it to the identity
    # snapshot, so the existing URL redactor can safely handle its query.
    state = observed_state(id="preview", text="Preview", href="https://example.test/preview?code=private-fixture-value")
    evidence = evidence_for(state, "click")
    assert "grant_scope" in evidence
    persisted = redact_value(evidence)
    assert "private-fixture-value" not in json.dumps(persisted)
    assert "code" in persisted["grant_scope"]["target"]["href"]


@pytest.mark.asyncio
@pytest.mark.parametrize("identity,critical", [
    ({"id": "send_email"}, True),
    ({"aria_label": "Send"}, True),
    ({"title": "Publish draft"}, True),
    ({"href": "/api/post_message"}, True),
    ({"tag": "INPUT", "text": "Send"}, True),
    ({"id": "preview", "text": "Preview"}, False),
], ids=["underscore", "icon-aria", "icon-title", "role-link-href", "input-button-value", "routine-preview"])
async def test_captured_only_review_cannot_grant_a_recognized_outbound_operation(tmp_path, monkeypatch, identity, critical):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "existing", selected=[], dashboard=False)
    state = observed_state(**identity)
    observed = state["target"]
    fingerprint = {key: observed[key] for key in ("id", "text", "type")}
    fingerprint["tag"] = observed["tag"].lower()
    for key in ("aria_label", "title", "href"):
        if key in observed:
            fingerprint[key] = observed[key]
    document = {"schema": "acira.specific_skill.v2", "id": "captured-control", "name": "Captured control",
        "scope": {"url_patterns": ["https://example.test/*"]}, "targets": [{"id": "fixture",
            "decision": "ask", "reason": "Review captured control", "fingerprint": fingerprint}]}
    (project.root / "policies/specific/captured.json").write_text(json.dumps(document))

    class Reviewer:
        calls = 0
        refused_grants = 0
        async def ask(self, request):
            self.calls += 1
            review = next(item for item in runtime.journal.reviews() if item["state"] == "pending")
            assert review["grant_eligible"] is (not critical)
            if critical:
                with pytest.raises(ValueError, match="cannot receive a reusable grant"):
                    runtime.journal.decide(review["review_id"], "approved", grant=True)
                self.refused_grants += 1
                assert runtime.journal.decide(review["review_id"], "approved", grant=False)
            else:
                assert runtime.journal.decide(review["review_id"], "approved", grant=True)
            return "allow"

    reviewer = Reviewer()
    runtime = ProjectRuntime(project, "default", dashboard=False, review_backend=reviewer)
    try:
        target = GuardTarget(tag=observed["tag"].lower(), type="button", id=observed["id"], text=observed["text"],
            aria_label=observed.get("aria_label"), title=observed.get("title"), href=observed.get("href"),
            url=state["url"], is_button=True, is_form_control=True, is_native_submit=False, confidence=1)
        item = ActionClassification(method="playwright.click", action="click", reason="Exact captured control",
            meaningful=True, confidence=1, target=target)
        policy = AciraGuardPolicy(replace(runtime.snapshot.config, adapters={}, terminal_log=False))
        decision, _, _, cedar = policy._configured_decision(item)
        assert decision == "ask" and cedar.source == "specific_skill"
        evidence = evidence_for(state, "click")
        assert evidence["critical"] is critical
        item = replace(item, details={"operation_evidence": evidence, "cedar": cedar.metadata})
        request = GuardReviewRequest(payload="{}", context=CDPMessageContext(AGENT_TO_BROWSER, "fixture", 1, "/"),
            classification=item, reason="Review captured control")
        assert [await runtime.ask(request), await runtime.ask(request)] == ["allow", "allow"]
        events = runtime.journal.events(limit=1000)
        assert reviewer.calls == (2 if critical else 1)
        assert reviewer.refused_grants == (2 if critical else 0)
        assert sum(item["type"] == "grant_created" for item in events) == (0 if critical else 1)
        assert sum(item["type"] == "grant_used" for item in events) == (0 if critical else 1)
    finally:
        await runtime.close()
