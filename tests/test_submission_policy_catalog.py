import json
from importlib.resources import files
from pathlib import Path

import pytest

from acira.cli import run
from acira.project import CATALOG, Project, catalog_policy, initialize, validate_policy
from acira.service import Service
from acira_cdp_proxy.general_skills import GeneralSkill, evaluate_general_skills

POLICY_ID = "acira.general.form_submission"
RISK = "submission.outbound_attempt"


def facts(action="click", *, button=True, submit=False, native=False, label="Send", tag="button", input_type="", nearby=()):
    return {"action": {"action": action}, "target": {"is_button": button,
        "is_submit": submit, "is_native_submit": native, "tag": tag, "type": input_type,
        "identity_tokens": [label.lower()] if label else [],
        "nearby_tokens": list(nearby), "form_tokens": [], "section_tokens": []}}


def detected(value):
    skill = GeneralSkill.from_dict(catalog_policy("submissions"))
    return evaluate_general_skills((skill,), value).detections


@pytest.mark.parametrize("label", ["Submit", "Send", "Post", "Publish", "Apply", "Finalize"])
def test_submission_catalog_covers_direct_non_form_commit_buttons(label):
    result = detected(facts(label=label))
    assert len(result) == 1
    assert result[0].risk == RISK and result[0].context == "action"
    assert result[0].severity == "high"


@pytest.mark.parametrize("label", ["Search", "Continue", ""])
def test_submission_catalog_gates_native_forms_without_guessing_label_or_method(label):
    assert detected(facts(submit=True, native=True, label=label))[0].risk == RISK


@pytest.mark.parametrize("label", ["Preview", "Search", "Continue", "Save"])
def test_submission_catalog_does_not_confuse_outside_form_buttons_with_native_submit(label):
    # Legacy is_submit is intentionally heuristic; only the precise native flag
    # establishes native form association for this new policy.
    assert not detected(facts(submit=True, native=False, label=label))


@pytest.mark.parametrize("tag,input_type,button", [
    ("button", "", True),
    ("button", "submit", True),
    ("input", "submit", True),
    ("input", "image", False),
])
def test_submission_catalog_gates_form_associated_native_controls(tag, input_type, button):
    result = detected(facts(native=True, label="Preview", tag=tag,
                            input_type=input_type, button=button))
    assert len(result) == 1 and result[0].risk == RISK


@pytest.mark.parametrize("missing", ["absent", "null"])
def test_submission_catalog_gates_buttons_with_unknown_native_status(missing):
    value = facts(label="Preview", native=None)
    if missing == "absent":
        value["target"].pop("is_native_submit")
    assert detected(value)[0].risk == RISK


@pytest.mark.parametrize("action", ["page_preflight", "form_input", "navigation"])
def test_submission_catalog_unknown_native_status_does_not_gate_noncommit_actions(action):
    assert not detected(facts(action, label="Preview", native=None))


def test_submission_catalog_unknown_native_status_does_not_gate_ordinary_anchor():
    assert not detected(facts(label="Preview", button=False, tag="a", native=None))


def test_submission_catalog_covers_explicit_submit_classification_on_form():
    assert detected(facts("submit", button=False, label="", tag="form"))[0].risk == RISK


@pytest.mark.parametrize("value", [
    facts("page_preflight", submit=True, native=True),
    facts("form_input", button=False, label="Send", tag="input"),
    facts("select", label="Submit"),
    facts(button=False, label="Post", tag="a"),
    facts(label="Preview", nearby=["send", "submit", "post"]),
    facts(label="Search"),
    facts(label="Postage"),
])
def test_submission_catalog_does_not_trigger_from_prose_links_or_noncommit_actions(value):
    assert not detected(value)


def test_submission_catalog_has_request_binding_without_invented_payment_fields():
    policy = catalog_policy("submissions")
    assert validate_policy(policy) == ("general", POLICY_ID)
    assert policy["acira_decisions"] == {RISK: "ask"}
    assert policy["acira_review"] == {RISK: {"request_binding": True}}
    source = Path(__file__).resolve().parents[1] / "skills/general/form_submission.json"
    packaged = files("acira").joinpath("resources/policies/form_submission.json")
    assert source.read_bytes() == packaged.read_bytes()
    for name in ("configs/acira.default.json", "configs/acira.dashboard.json"):
        config = json.loads((source.parents[2] / name).read_text())
        assert config["general_skills"][POLICY_ID]["risks"] == {RISK: "ask"}


@pytest.fixture
def private_project(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    return initialize(tmp_path / "existing", selected=[], dashboard=False)


def test_submission_terminal_add_configure_validate_without_dashboard(private_project, capsys):
    common = ["--project", str(private_project.root)]
    run(["policy", "add", "submissions", *common])
    snapshot = private_project.snapshot("default")
    assert snapshot.config.skill_policies[POLICY_ID][RISK] == "ask"
    assert snapshot.config.general_skills[0].review_requirements == {RISK: {"request_binding": True}}
    run(["policy", "configure", "submissions", "--risk", RISK, "--decision", "block", *common])
    assert private_project.snapshot("default").config.skill_policies[POLICY_ID][RISK] == "block"
    assert "GET/search submissions block while enabled" in capsys.readouterr().out
    run(["policy", "validate", *common])
    validation = json.loads(capsys.readouterr().out)
    assert validation == {"valid": True, "revision": private_project.snapshot("default").revision, "policy_count": 1}
    assert not (private_project.root.parent / "private/server.json").exists()


def test_submission_numbered_terminal_choice_is_available(private_project, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: str(list(CATALOG).index("submissions") + 1))
    run(["policy", "add", "--project", str(private_project.root)])
    assert private_project.policies("default")[0]["id"] == POLICY_ID


def test_submission_init_explains_conservative_default_and_preserves_existing_project(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    root = tmp_path / "new"
    run(["init", "--project", str(root), "--yes", "--no-dashboard"])
    output = capsys.readouterr().out
    assert "including GET/search" in output and "guard.submit(...)" in output
    assert "Verified GET requests are unsupported" in output
    assert "GET/search submissions block while enabled" in output
    assert Project(root).snapshot("default").config.skill_policies[POLICY_ID][RISK] == "ask"
    old = initialize(tmp_path / "old", selected=["credentials"], dashboard=False)
    before = old.path.read_bytes()
    run(["init", "--project", str(old.root), "--yes", "--no-dashboard"])
    assert old.path.read_bytes() == before
    assert all(policy["id"] != POLICY_ID for policy in old.policies("default"))


@pytest.mark.asyncio
async def test_submission_dashboard_catalog_uses_the_same_packaged_document():
    response = await Service.catalog(object(), None)
    policies = json.loads(response.body)["policies"]
    item = next(policy for policy in policies if policy["name"] == "submissions")
    assert item["filename"] == "form_submission.json"
    assert "GET/search" in item["document"]["description"]
    assert "unsupported and block" in item["document"]["description"]
    assert item["document"] == catalog_policy("submissions")
