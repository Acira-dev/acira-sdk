"""Reviewer views stay private while exact internal scope hashes remain distinct."""
from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from acira.journal import journal_redact
from acira.project import digest, initialize
from acira.runtime import ProjectRuntime
from acira_cdp_proxy.config import AciraGuardConfig
from acira_cdp_proxy.guard import (
    ActionClassification, AciraGuardPolicy, GuardReviewRequest, GuardTarget,
    TerminalReviewBackend, redact_review_request,
)
from acira_cdp_proxy.operation import evidence_for
from acira_cdp_proxy.policy import AGENT_TO_BROWSER, CDPMessageContext

MATERIAL = "local-fixture-key"
ROUTING_SESSION = "A193C42D190ED384B06F07CF3D22715C"


def request_for(material=MATERIAL, *, as_bytes=False, probe=None):
    url = "https://example.invalid/preview?api_key=" + material + "&page=overview"
    message = {"id": 7, "method": "Input.dispatchMouseEvent", "sessionId": ROUTING_SESSION,
               "params": {"url": url, "api_key": material, "type": "mousePressed"}}
    payload = json.dumps(message)
    if as_bytes:
        payload = payload.encode()
    state = {"url": url, "has_form": False, "form_id": None, "destination": url,
        "method": None, "target": {"tag": "BUTTON", "id": "preview", "name": "",
            "type": "button", "text": "Preview"}, "fields": []}
    evidence = evidence_for(state, "click")
    classification = ActionClassification(method="playwright.click", action="click", reason="Review " + url,
        meaningful=True, target=GuardTarget(tag="button", id="preview", text="Preview", url=url,
            href=url, attributes={"data-local": "ordinary", "api_key": material}),
        details={"operation_evidence": evidence, "ordinary": "Normal display", "api_key": material},
        replacement_payload=payload)
    context = CDPMessageContext(AGENT_TO_BROWSER, "owned-local", 1, "/review?api_key="+material,
                               parsed_message=message, send_probe=probe)
    return GuardReviewRequest(payload, context, classification, "Review " + url)


def rendered_content(request):
    return json.dumps({"payload": request.payload.decode() if isinstance(request.payload, bytes) else request.payload,
        "context": request.context.parsed_message, "path": request.context.websocket_path,
        "classification": request.classification.to_metadata(), "reason": request.reason})


@pytest.mark.parametrize("as_bytes", [False, True])
def test_projected_payload_context_and_classification_are_copies_with_exact_routing(as_bytes):
    async def probe(*arguments):
        return {"ordinary": True}
    request = request_for(as_bytes=as_bytes, probe=probe)
    original_payload = request.payload
    original_message = copy.deepcopy(request.context.parsed_message)
    original_details = copy.deepcopy(request.classification.details)
    view = redact_review_request(request)
    assert MATERIAL not in rendered_content(view)
    assert "page=overview" in rendered_content(view)
    assert view.classification.target.text == "Preview"
    assert view.classification.details["ordinary"] == "Normal display"
    assert isinstance(view.payload, type(request.payload))
    assert view.context is not request.context and view.classification is not request.classification
    assert view.classification.target is not request.classification.target
    assert view.context.message_id == 7 and view.context.method == "Input.dispatchMouseEvent"
    assert view.context.session_id == ROUTING_SESSION and view.context.send_probe is probe
    assert view.context.connection_id == request.context.connection_id
    assert json.loads(view.payload)["sessionId"] == ROUTING_SESSION
    assert json.loads(view.classification.replacement_payload)["id"] == 7
    view.context.parsed_message["params"]["url"] = "changed-view-only"
    view.classification.details["operation_evidence"]["grant_scope"]["url"] = "changed-view-only"
    assert request.context.parsed_message == original_message
    assert request.classification.details == original_details
    assert request.payload == original_payload


@pytest.mark.asyncio
async def test_review_context_probe_retains_exact_session_not_the_masked_view():
    observed = []
    async def probe(method, params, session_id):
        observed.append((method, params, session_id))
        return {"ordinary": True}
    request = request_for(probe=probe)
    view = redact_review_request(request)
    assert await view.context.probe("Page.captureScreenshot", {"format": "png"}) == {"ordinary": True}
    assert observed == [("Page.captureScreenshot", {"format": "png"}, ROUTING_SESSION)]


@pytest.mark.parametrize("payload", ["https://example.invalid/?api_key=local-fixture-key&page=overview", b"https://example.invalid/?api_key=local-fixture-key&page=overview"])
def test_non_json_reviewer_payload_masks_sensitive_queries_without_touching_original(payload):
    request = replace(request_for(), payload=payload)
    view = redact_review_request(request)
    display = view.payload.decode() if isinstance(view.payload, bytes) else view.payload
    assert MATERIAL not in display and "page=overview" in display
    assert request.payload == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_style", ["callable", "backend"])
async def test_legacy_guard_reviewers_receive_only_projected_views(handler_style):
    received = []
    async def handler(request):
        received.append(request)
        assert MATERIAL not in rendered_content(request)
        return "block"
    class Reviewer:
        async def ask(self, request):
            return await handler(request)
    config = AciraGuardConfig(adapters={}, terminal_log=False, terminal_review=False)
    options = {"review_handler": handler} if handler_style == "callable" else {"review_backend": Reviewer()}
    policy = AciraGuardPolicy(config, **options)
    original = request_for()
    assert await policy._ask_backend(original) is False
    assert len(received) == 1 and received[0] is not original
    assert MATERIAL in original.classification.details["operation_evidence"]["grant_scope"]["url"]


def test_projection_preserves_opaque_private_match_reference_without_modifying_it():
    marker = object()
    request = request_for()
    request.classification.details["_specific_match"] = marker
    view = redact_review_request(request)
    assert view.classification.details["_specific_match"] is marker
    assert request.classification.details["_specific_match"] is marker


def test_terminal_prompt_masks_target_and_reason_urls_but_retains_normal_description():
    request = request_for()
    target = replace(request.classification.target, text="Preview " + request.classification.target.url)
    request = replace(request, classification=replace(request.classification, target=target))
    backend = TerminalReviewBackend(AciraGuardConfig(adapters={}, terminal_log=False))
    prompt = backend.prompt_for(request)
    assert MATERIAL not in prompt and "page=overview" in prompt
    assert "Preview" in prompt and "[y/N]" in prompt
    assert MATERIAL in request.reason and MATERIAL in request.classification.target.text


def test_actual_preflight_terminal_output_masks_page_url_and_title_only_at_emission(capsys):
    policy = AciraGuardPolicy(AciraGuardConfig(adapters={}, terminal_log=True, terminal_review=False))
    url = request_for().classification.target.url
    snapshot = policy.evaluate_page_preflight_targets([], {"url": url, "title": "Preview " + url, "status": "ok"})
    policy._terminal_preflight_snapshot(snapshot, trigger="local_test")
    output = capsys.readouterr().err
    assert MATERIAL not in output and "page=overview" in output
    assert "PAGE_PREFLIGHT" in output and "Preview" in output
    assert snapshot.url == url and MATERIAL in snapshot.title


@pytest.mark.asyncio
async def test_actual_runtime_hashes_private_scopes_before_redacting_backend_and_grant_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "existing", selected=[], dashboard=False)
    projected_scopes = []
    class Reviewer:
        async def ask(self, view):
            assert "local-fixture-key" not in rendered_content(view)
            projected_scopes.append(copy.deepcopy(view.classification.details["operation_evidence"]["grant_scope"]))
            pending = [item for item in runtime.journal.reviews() if item["state"] == "pending"]
            assert len(pending) == 1
            assert runtime.journal.decide(pending[0]["review_id"], "approved", grant=True)
            return "allow"
    runtime = ProjectRuntime(project, "default", dashboard=False, review_backend=Reviewer())
    policy = AciraGuardPolicy(replace(runtime.snapshot.config, adapters={}, terminal_log=False), review_backend=runtime)
    first, second = request_for(MATERIAL + "-alpha"), request_for(MATERIAL + "-beta")
    try:
        first_scope = copy.deepcopy(first.classification.details["operation_evidence"]["grant_scope"])
        second_scope = copy.deepcopy(second.classification.details["operation_evidence"]["grant_scope"])
        assert first_scope != second_scope and journal_redact(first_scope) == journal_redact(second_scope)
        # Guard passes private data only to its internal Runtime scope owner.
        assert await policy._ask_backend(first) is True
        assert await policy._ask_backend(first) is True
        assert len(projected_scopes) == 1
        # A second scope with identical redacted display MUST require its own review.
        assert await policy._ask_backend(second) is True
        assert len(projected_scopes) == 2 and projected_scopes[0] == projected_scopes[1]
        assert await policy._ask_backend(first) is True
        assert await policy._ask_backend(second) is True
        assert len(projected_scopes) == 2
        with runtime.journal.db() as database:
            rows = [dict(row) for row in database.execute("SELECT id,payload FROM grants")]
        expected_ids = {digest({"scope": scope, "run": runtime.run_id, "revision": runtime.snapshot.revision})
                        for scope in (first_scope, second_scope)}
        assert len(expected_ids) == 2 and {row["id"] for row in rows} == expected_ids
        assert all(MATERIAL not in row["payload"] for row in rows)
        assert all(json.loads(row["payload"])["scope"] == projected_scopes[0] for row in rows)
        assert MATERIAL not in json.dumps({"reviews": runtime.journal.reviews(), "events": runtime.journal.events()})
        assert first.classification.details["operation_evidence"]["grant_scope"] == first_scope
        assert second.classification.details["operation_evidence"]["grant_scope"] == second_scope
        assert len([event for event in runtime.journal.events() if event["type"] == "grant_created"]) == 2
        assert len([event for event in runtime.journal.events() if event["type"] == "grant_used"]) == 3
    finally:
        await runtime.close()


@pytest.mark.parametrize("as_bytes", [False, True])
@pytest.mark.parametrize("field", ["arguments", "password", "authorization"])
def test_json_payload_views_use_full_message_secret_masking_not_wire_projection(as_bytes, field):
    message = {"id": 7, "method": "Runtime.callFunctionOn", "sessionId": ROUTING_SESSION,
        "params": {field: [{"value": MATERIAL}] if field == "arguments" else MATERIAL,
                   "returnByValue": True}}
    payload = json.dumps(message)
    if as_bytes:
        payload = payload.encode()
    request = request_for(as_bytes=as_bytes)
    request = replace(request, payload=payload, context=replace(request.context, parsed_message=message))
    view = redact_review_request(request)
    parsed = json.loads(view.payload)
    assert MATERIAL not in json.dumps(parsed) and MATERIAL not in json.dumps(view.context.parsed_message)
    assert parsed == view.context.parsed_message
    assert parsed["id"] == 7 and parsed["method"] == "Runtime.callFunctionOn" and parsed["sessionId"] == ROUTING_SESSION
    assert parsed["params"]["returnByValue"] is True
    assert request.payload == payload and MATERIAL in json.dumps(request.context.parsed_message)


@pytest.mark.parametrize("as_bytes", [False, True])
@pytest.mark.parametrize("target", [
    GuardTarget(tag="input", type="text", name="api_key"),
    GuardTarget(tag="input", type="text", id="api-key"),
    GuardTarget(tag="input", type="text", aria_label="apiKey"),
    GuardTarget(tag="input", type="text", name="X-API-Key"),
    GuardTarget(tag="input", type="text", name="client_api_key"),
    GuardTarget(tag="input", type="password", name="ordinary-name"),
])
def test_known_sensitive_text_entry_payload_and_context_never_expose_raw_text(as_bytes, target):
    message = {"id": 7, "method": "Input.insertText", "sessionId": ROUTING_SESSION,
               "params": {"text": MATERIAL, "unmodifiedText": MATERIAL}}
    payload = json.dumps(message)
    if as_bytes:
        payload = payload.encode()
    request = request_for(as_bytes=as_bytes)
    request = replace(request, payload=payload, context=replace(request.context, parsed_message=message),
        classification=replace(request.classification, action="form_input", target=target,
            replacement_payload=payload, details={"value": MATERIAL}))
    view = redact_review_request(request)
    assert MATERIAL not in rendered_content(view)
    assert MATERIAL not in json.dumps(json.loads(view.classification.replacement_payload))
    assert json.loads(view.payload) == view.context.parsed_message
    assert view.context.session_id == ROUTING_SESSION and view.context.method == "Input.insertText"
    assert view.classification.target.name == target.name
    assert request.payload == payload and request.context.parsed_message["params"]["text"] == MATERIAL
    assert request.classification.details["value"] == MATERIAL


@pytest.mark.parametrize("as_bytes", [False, True])
def test_ordinary_text_entry_content_remains_reviewable_without_secret_target_hint(as_bytes):
    message = {"id": 7, "method": "Input.insertText", "sessionId": ROUTING_SESSION,
               "params": {"text": "Ordinary local draft"}}
    payload = json.dumps(message)
    if as_bytes:
        payload = payload.encode()
    request = replace(request_for(), payload=payload, context=replace(request_for().context, parsed_message=message),
        classification=ActionClassification(method="Input.insertText", action="form_input", reason="Local text entry",
            target=GuardTarget(tag="input", type="text", name="message")))
    view = redact_review_request(request)
    assert json.loads(view.payload)["params"]["text"] == "Ordinary local draft"
    assert view.context.parsed_message["params"]["text"] == "Ordinary local draft"
    assert request.payload == payload


@pytest.mark.asyncio
async def test_custom_backend_cannot_obtain_private_view_by_copying_runtime_marker():
    received = []
    class Reviewer:
        _acira_private_review_scope = True
        async def ask(self, view):
            received.append(view)
            assert MATERIAL not in rendered_content(view)
            return "block"
    policy = AciraGuardPolicy(AciraGuardConfig(adapters={}, terminal_log=False), review_backend=Reviewer())
    request = request_for()
    assert await policy._ask_backend(request) is False
    assert len(received) == 1 and received[0] is not request
    assert MATERIAL in request.reason


def test_actual_terminal_decision_masks_url_label_and_reason_at_final_display_only(capsys):
    policy = AciraGuardPolicy(AciraGuardConfig(adapters={}, terminal_log=True))
    request = request_for()
    target = replace(request.classification.target, text="Preview " + request.classification.target.url)
    classification = replace(request.classification, target=target)
    before = classification.to_metadata()
    policy._terminal_decision("ASK", request.context, classification, request.reason, color="yellow")
    output = capsys.readouterr().err
    assert MATERIAL not in output and "page=overview" in output
    assert "ASK id=7" in output and "method=playwright.click action=click" in output
    assert "Preview" in output
    assert classification.to_metadata() == before and MATERIAL in request.reason
    assert request.context.session_id == ROUTING_SESSION


@pytest.mark.asyncio
async def test_actual_runtime_reviewer_omits_rich_page_observations_but_keeps_selected_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "existing", selected=[], dashboard=False)
    raw_targets = [{"tag": "input", "type": "text", "name": "api_key", "valueSample": MATERIAL},
                   {"tag": "input", "type": "text", "name": "message", "valueSample": "ordinary draft"}]
    selected_mapping = {"target_id": "preview", "candidate_fingerprint": {"tag": "button", "id": "preview", "text": "Preview"}}
    received = []
    class Reviewer:
        async def ask(self, view):
            received.append(view)
            details = view.classification.details
            assert MATERIAL not in rendered_content(view)
            assert details["page_targets"]["omitted"] is True
            assert details["page_targets"]["candidate_count"] == 2
            assert details["nested"][0]["page_targets"]["omitted"] is True
            assert details["cedar"]["skills"]["page_mapping"] == [selected_mapping]
            assert details["operation_evidence"]["fields"] == [{"name": "message", "display": "ordinary draft"}]
            assert view.classification.target.text == "Preview"
            return "block"
    request = request_for()
    details = {"page_targets": raw_targets, "nested": [{"page_targets": raw_targets}],
        "cedar": {"skills": {"page_mapping": [selected_mapping]}},
        "operation_evidence": {"critical": True, "fields": [{"name": "message", "display": "ordinary draft"}]}}
    request = replace(request, classification=replace(request.classification, details=details))
    runtime = ProjectRuntime(project, "default", dashboard=False, review_backend=Reviewer())
    try:
        assert await runtime.ask(request) == "block"
        assert len(received) == 1
        assert request.classification.details["page_targets"] is raw_targets
        assert request.classification.details["page_targets"][0]["valueSample"] == MATERIAL
        assert request.classification.details["nested"][0]["page_targets"] is raw_targets
        assert request.classification.details["cedar"]["skills"]["page_mapping"] == [selected_mapping]
    finally:
        await runtime.close()
