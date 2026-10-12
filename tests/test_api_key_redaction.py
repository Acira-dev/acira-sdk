"""API-key aliases are private in persistence, with unchanged browser transport."""
from __future__ import annotations

import copy
import json
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from acira.journal import Journal
from acira_cdp_proxy.logger import CDPLogger
from acira_cdp_proxy.redaction import redact_payload, redact_value


ALIASES = ("api_key", "api-key", "apiKey", "X-API-Key", "client_api_key")
MATERIAL = "local-fixture-key"


def identity_url(alias: str) -> str:
    return "https://example.invalid/send?" + urlencode([(alias, MATERIAL), ("page", "overview"), ("key", "navigation")])


def assert_private_url(url: str, alias: str) -> None:
    assert dict(parse_qsl(urlsplit(url).query)) == {
        alias: "[REDACTED]", "page": "overview", "key": "navigation"}
    assert MATERIAL not in url


@pytest.mark.parametrize("alias", ALIASES)
def test_api_key_dictionary_and_identity_query_are_masked_without_changing_source(alias):
    value = {"headers": {alias: MATERIAL}, "target": {"href": identity_url(alias)}, "key": "navigation"}
    original = copy.deepcopy(value)
    masked = redact_value(value)
    assert masked["headers"][alias] == "[REDACTED]"
    assert_private_url(masked["target"]["href"], alias)
    assert masked["key"] == "navigation"
    assert value == original


@pytest.mark.parametrize("alias", ALIASES)
@pytest.mark.parametrize("binary", [False, True])
def test_persisted_cdp_payload_masks_alias_but_raw_browser_wire_is_exactly_unchanged(alias, binary):
    request = {"id": 7, "method": "Network.setExtraHTTPHeaders",
        "params": {"headers": {alias: MATERIAL, "Content-Type": "application/json"},
                   "target": {"href": identity_url(alias)}}}
    text = json.dumps(request, indent=2)
    wire = text.encode() if binary else text
    masked = json.loads(redact_payload(wire))
    assert masked["params"]["headers"][alias] == "[REDACTED]"
    assert masked["params"]["headers"]["Content-Type"] == "application/json"
    assert_private_url(masked["params"]["target"]["href"], alias)
    assert redact_payload(wire, redact_generic=False) == wire
    assert type(redact_payload(wire, redact_generic=False)) is type(wire)


@pytest.mark.parametrize("alias", ALIASES)
def test_actual_journal_masks_api_key_aliases_and_href_before_sqlite_persistence(tmp_path, alias):
    journal = Journal(tmp_path)
    event_id = journal.event("action_proposed", target={"href": identity_url(alias)}, headers={alias: MATERIAL})
    record = next(item for item in journal.events() if item["event_id"] == event_id)
    assert record["headers"][alias] == "[REDACTED]"
    assert_private_url(record["target"]["href"], alias)
    with journal.db() as database:
        stored = database.execute("SELECT payload FROM events WHERE id=?", (event_id,)).fetchone()[0]
    assert MATERIAL not in stored


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ALIASES)
async def test_actual_cdp_diagnostics_and_decision_metadata_mask_api_key_aliases(tmp_path, alias):
    logger = CDPLogger(tmp_path / "diagnostics.log")
    payload = json.dumps({"id": 7, "method": "Network.setExtraHTTPHeaders",
                          "params": {"headers": {alias: MATERIAL}, "url": identity_url(alias)}})
    await logger.log(direction="client", connection_id="local", message_number=1,
                     websocket_path="/devtools/browser/local", payload=payload)
    await logger.log_decision(connection_id="local", message_number=1,
        websocket_path="/devtools/browser/local", payload=payload,
        metadata={"headers": {alias: MATERIAL}, "target": {"href": identity_url(alias)}})
    lines = logger.path.read_text().splitlines()
    assert MATERIAL not in "\n".join(lines)
    recorded_payload = json.loads(lines[1])
    decision = json.loads(lines[3])
    assert recorded_payload["params"]["headers"][alias] == "[REDACTED]"
    assert decision["metadata"]["headers"][alias] == "[REDACTED]"
    assert_private_url(decision["metadata"]["target"]["href"], alias)
    assert json.loads(decision["payload"])["params"]["headers"][alias] == "[REDACTED]"


@pytest.mark.parametrize("ordinary", ["key", "monkey", "apikeyboard", "api-keyboard", "rapid_api", "public_key"])
def test_unrelated_key_names_and_normal_queries_remain_readable(ordinary):
    value = {ordinary: "navigation", "href": "https://example.invalid/docs?" + urlencode({ordinary: "navigation", "page": "intro"})}
    assert redact_value(value) == value
    payload = json.dumps({"method": "Fixture.describe", "params": value}, indent=2)
    assert redact_payload(payload) == payload
    assert redact_payload(payload, redact_generic=False) == payload
