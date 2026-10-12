"""Native modal text is private even in explicitly enabled CDP diagnostics."""
from copy import deepcopy
import json

import pytest

from acira_cdp_proxy.logger import CDPLogger
from acira_cdp_proxy import redaction


NARRATIVE = "Narrative for a private local fixture"
DRAFT = "A draft from a private local fixture"


def message(method):
    if method == "Page.javascriptDialogOpening":
        params = {"type": "confirm", "message": NARRATIVE, "defaultPrompt": DRAFT}
    elif method == "Page.javascriptDialogClosed":
        params = {"result": False, "userInput": DRAFT, "frameId": "owned-frame"}
    else:
        params = {"accept": False, "promptText": DRAFT}
    return {"method": method, "params": params, "id": 2, "sessionId": "owned-session"}


METHODS = ("Page.javascriptDialogOpening", "Page.handleJavaScriptDialog", "Page.javascriptDialogClosed")


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("binary", (False, True), ids=("text", "bytes"))
def test_persistence_masks_exact_modal_content_and_preserves_routing(method, binary):
    original = message(method)
    text = json.dumps(original)
    payload = text.encode() if binary else text
    stored = redaction.redact_payload(payload)
    assert isinstance(stored, bytes if binary else str)
    result = json.loads(stored)
    assert NARRATIVE not in json.dumps(result) and DRAFT not in json.dumps(result)
    assert result["id"] == 2 and result["sessionId"] == "owned-session"
    assert result["method"] == method
    if method == METHODS[0]:
        assert result["params"]["type"] == "confirm"
    elif method == "Page.javascriptDialogClosed":
        assert result["params"]["result"] is False and result["params"]["frameId"] == "owned-frame"
    else:
        assert result["params"]["accept"] is False
    assert original == message(method)


@pytest.mark.parametrize("method", METHODS)
def test_browser_wire_projection_retains_unregistered_modal_content(method):
    # _decide_inner uses generic=False for browser-bound observation forwarding.
    # Diagnostics must not alter what the browser or framework actually sees.
    text = json.dumps(message(method), indent=2)
    assert redaction.redact_payload(text, redact_generic=False) == text
    assert redaction.redact_payload(text.encode(), redact_generic=False) == text.encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("kind", ("message", "decision"))
async def test_optional_cdp_logs_never_persist_modal_text(tmp_path, method, kind):
    path = tmp_path / "explicit-cdp-diagnostics.log"
    logger = CDPLogger(path)
    payload = json.dumps(message(method)).encode()
    context = {"parsed_message": message(method), "normal": {"message": "Public status"}}
    before = deepcopy(context)
    if kind == "message":
        await logger.log(direction="BROWSER_TO_AGENT", connection_id="owned",
            message_number=2, websocket_path="/devtools/browser/owned", payload=payload)
    else:
        await logger.log_decision(connection_id="owned", message_number=2,
            websocket_path="/devtools/browser/owned", payload=payload,
            metadata={"context": context}, reason="Native modal acceptance was blocked")
    stored = path.read_text()
    assert NARRATIVE not in stored and DRAFT not in stored
    assert "[REDACTED:dialog]" in stored
    assert "owned-session" in stored and method in stored
    assert context == before
    if kind == "decision":
        assert "Public status" in stored and "Native modal acceptance was blocked" in stored


def test_unrelated_protocol_fields_and_known_secret_masking_are_preserved(monkeypatch):
    registry = redaction.SecretRedactionRegistry()
    monkeypatch.setattr(redaction, "SECRET_REDACTION_REGISTRY", registry)
    known = "Known private fixture value"
    assert registry.register_secret(secret_id="fixture", label="Fixture", value=known)
    other = {"method": "Runtime.evaluate", "params": {"message": NARRATIVE,
        "defaultPrompt": DRAFT, "promptText": "Public status", "userInput": "Ordinary public field", "text": known}}
    result = json.loads(redaction.redact_payload(json.dumps(other)))
    assert result["params"]["message"] == NARRATIVE
    assert result["params"]["defaultPrompt"] == DRAFT
    assert result["params"]["promptText"] == "Public status"
    assert result["params"]["userInput"] == "Ordinary public field"
    assert result["params"]["text"] == "[REDACTED:secret]"
    # Existing secret masking remains on the browser-wire projection too.
    wire = json.loads(redaction.redact_payload(json.dumps(other), redact_generic=False))
    assert wire["params"]["text"] == "[REDACTED:secret]"
    assert wire["params"]["message"] == NARRATIVE


def test_nested_protocol_context_is_masked_without_mutating_input():
    original = {"context": [{"parsed_message": message(METHODS[0])},
                {"parsed_message": message(METHODS[1])}, {"parsed_message": message(METHODS[2])}],
                "normal": {"message": "Public status", "promptText": "Ordinary public value"}}
    before = deepcopy(original)
    result = redaction.redact_value(original)
    assert NARRATIVE not in json.dumps(result) and DRAFT not in json.dumps(result)
    assert result["normal"] == original["normal"]
    assert original == before
