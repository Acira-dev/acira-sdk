"""Known native form association stays distinct from legacy heuristics and replay."""
from copy import deepcopy
from dataclasses import replace

import pytest

from acira.project import initialize
from acira.replay import evaluate
from acira_cdp_proxy.config import AciraGuardRule
from acira_cdp_proxy.facts import build_browser_event_facts
from acira_cdp_proxy.guard import (
    ActionClassification, AciraGuardPolicy, GuardTarget, _target_from_probe_value,
)
from acira_cdp_proxy.policy import CDPMessageContext


def event(native):
    classification = ActionClassification(method="playwright.click", action="click",
        reason="resolved local control", meaningful=True, confidence=1,
        target=GuardTarget(tag="button", text="Preview", id="preview", is_button=True,
                           is_submit=True, is_native_submit=native, confidence=1))
    return {"evaluation_contract": "acira.action_facts.v1",
        "classification": classification.to_metadata(),
        "facts": build_browser_event_facts(classification).to_dict(),
        "specific_facts": {}, "metadata": {}}


@pytest.mark.parametrize("native", [None, False, True])
def test_replay_uses_known_association_and_never_invents_it_from_legacy_heuristic(tmp_path, monkeypatch, native):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    config = initialize(tmp_path / "project", selected=["submissions"], dashboard=False).snapshot().config
    frozen = event(native)
    before = deepcopy(frozen)
    result = evaluate(config, frozen)
    if native is None:
        assert result == {"decision": "indeterminate", "reason": "Missing recorded native form-association evidence"}
    elif native is False:
        assert result["decision"] == "allow"
    else:
        assert result["decision"] == "indeterminate"
        assert "verified request binding" in result["reason"]
    assert frozen == before


def test_historical_mutating_event_without_new_flag_remains_replayable_for_old_policies(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", selected=[], dashboard=False)
    frozen = event(None)
    del frozen["facts"]["target"]["is_native_submit"]
    del frozen["classification"]["target"]["is_native_submit"]
    assert evaluate(project.snapshot().config, frozen)["decision"] == "allow"
    # A candidate with the new policy must report insufficient historical data.
    from acira.cli import run
    run(["policy", "add", "submissions", "--project", str(project.root)])
    assert evaluate(project.snapshot().config, frozen)["decision"] == "indeterminate"


@pytest.mark.asyncio
@pytest.mark.parametrize("native,expected", [(False, "form_control_activation"), (True, "submit"), (None, "submit")])
async def test_cdp_pointer_classification_uses_precise_association_without_destroying_legacy_fact(tmp_path, monkeypatch, native, expected):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    config = initialize(tmp_path / "project", selected=[], dashboard=False).snapshot().config
    guard = AciraGuardPolicy(replace(config, adapters={}, terminal_log=False))
    target = _target_from_probe_value({"tag": "button", "text": "Preview", "isButton": True,
        "isSubmit": True, "isNativeSubmit": native, "confidence": 1})
    async def probe(*args):
        return target
    monkeypatch.setattr(guard, "_probe_point", probe)
    item = await guard._classify_pointer_event("Input.dispatchMouseEvent", {"type": "mousePressed", "x": 1, "y": 1},
        CDPMessageContext(direction="AGENT_TO_BROWSER", connection_id="local", message_number=1,
                          websocket_path="/devtools/page/local", parsed_message={}), pointer_kind="mouse")
    assert item.action == expected and item.target.is_submit is True
    assert build_browser_event_facts(item).target.is_native_submit is native


@pytest.mark.parametrize("action", ["click", "dialog_confirmation"])
def test_missing_native_history_does_not_obscure_a_conclusive_core_block(tmp_path, monkeypatch, action):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    config = initialize(tmp_path / "project", selected=["submissions"], dashboard=False).snapshot().config
    frozen = event(None)
    frozen["classification"]["action"] = action
    frozen["facts"]["action"]["action"] = action
    if action == "click":
        config = replace(config, rules=(AciraGuardRule(id="block-local-click", actions=("click",), decision="block", reason="Known local restriction"),))
    assert evaluate(config, frozen)["decision"] == "block"
