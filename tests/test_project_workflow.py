import json
from pathlib import Path

import pytest

from acira.journal import Journal
from acira.local import Registry
from acira.project import Project, PolicyConflict, catalog_policy, digest, initialize


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))


def test_init_is_idempotent_and_roots_are_distinct(tmp_path, monkeypatch):
    first = initialize(tmp_path / "one", selected=["payment"])
    before = first.path.read_bytes()
    initialize(first.root, selected=["credentials"])
    assert first.path.read_bytes() == before
    second = initialize(tmp_path / "two", selected=["credentials"])
    assert first.snapshot().config.general_skills[0].id != second.snapshot().config.general_skills[0].id
    assert len(Registry().projects()) == 2
    (first.root / "nested").mkdir()
    monkeypatch.chdir(first.root / "nested")
    assert Project.find().root == first.root


def test_policy_save_detects_conflicts_and_external_invalid_changes(tmp_path):
    project = initialize(tmp_path / "one", selected=["payment"])
    policy = project.policies("default")[0]
    payload = policy["document"]
    payload["acira_decisions"]["checkout.card_data_entry"] = "block"
    result = project.save_policy("default", "checkout.json", payload, expected=policy["hash"])
    assert result["state"] == "Saved"
    assert project.snapshot().config.skill_policies[payload["id"]]["checkout.card_data_entry"] == "block"
    with pytest.raises(PolicyConflict):
        project.save_policy("default", "checkout.json", payload, expected=policy["hash"])
    Path(policy["path"]).write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        project.snapshot()


def test_shared_folder_affected_agents_and_path_escape(tmp_path):
    project = initialize(tmp_path / "one", selected=["payment"])
    initialize(project.root, agent="second")
    payload = catalog_policy("credentials")
    result = project.save_policy("default", "credentials.json", payload, expected=None)
    assert result["affected_agents"] == ["default", "second"]
    with pytest.raises(ValueError):
        project.save_policy("default", "../outside.json", payload, expected=None)


def test_durable_review_first_response_wins_and_redacts(tmp_path):
    journal = Journal(tmp_path)
    review = journal.create_review("run-a", {"password": "do-not-persist", "grant_eligible": False})
    assert journal.decide(review["review_id"], "rejected")
    assert not journal.decide(review["review_id"], "approved")
    assert Journal(tmp_path).review(review["review_id"])["state"] == "rejected"
    assert "do-not-persist" not in json.dumps(journal.events())
    second = journal.create_review("run-b", {"grant_eligible": False})
    with pytest.raises(ValueError):
        journal.decide(second["review_id"], "approved", grant=True)


def test_invalid_policy_does_not_replace_existing_file(tmp_path):
    project = initialize(tmp_path / "one", selected=["payment"])
    policy = project.policies("default")[0]
    before = Path(policy["path"]).read_bytes()
    payload = policy["document"]
    payload["acira_decisions"]["checkout.card_data_entry"] = "invalid"
    with pytest.raises(ValueError):
        project.save_policy("default", "checkout.json", payload, expected=policy["hash"])
    assert Path(policy["path"]).read_bytes() == before


def test_large_aggregate_revision_remains_readable_replayable_and_restorable(tmp_path):
    from acira.local import atomic_json
    from acira.project import SNAPSHOT_JSON_LIMIT, read_json
    from acira.replay import revision_config
    from acira.revisions import recover, rollback

    project = initialize(tmp_path / "many-policies", selected=[])
    for name in ("payment", "credentials"):
        policy = catalog_policy(name)
        policy["description"] = "Policy explanation. " * 30000
        project.save_policy("default", name + ".json", policy, expected=None)
    original = project.snapshot()
    revision_path = project.root / ".acira/revisions/default" / (original.revision + ".json")
    assert revision_path.stat().st_size > 1024 * 1024
    assert read_json(revision_path, max_bytes=SNAPSHOT_JSON_LIMIT)["revision"] == original.revision
    assert len(revision_config(project, "default", original.revision).general_skills) == 2
    policy = project.policies("default")[0]
    policy["document"]["description"] = "New explanation"
    project.save_policy("default", Path(policy["path"]).name, policy["document"], expected=policy["hash"])
    result = rollback(project, "default", original.revision, expected=project.snapshot().revision)
    assert result["revision"] != original.revision
    assert project.snapshot().documents == original.documents
    marker = project.root / ".acira/policy-transaction.json"
    atomic_json(marker, {"configuration": project.document(), "documents": original.documents, "created": []})
    assert recover(project)
    assert not marker.exists()


def test_oversized_policy_is_rejected_before_writing(tmp_path):
    project = initialize(tmp_path / "one", selected=[])
    policy = catalog_policy("credentials")
    policy["description"] = "x" * (1024 * 1024)
    with pytest.raises(ValueError, match="1 MiB"):
        project.save_policy("default", "large.json", policy, expected=None)
    assert not (project.root / "policies/general/large.json").exists()
