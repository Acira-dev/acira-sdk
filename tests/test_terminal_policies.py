import json
from pathlib import Path

import pytest

from acira.cli import run
from acira.project import initialize, digest, read_json
from acira.local import atomic_json
from acira.extension import install_extension


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    return initialize(tmp_path / "existing", selected=[], dashboard=False)


def capture():
    return {"schema": "acira.specific_skill.v2", "id": "captured-guide", "name": "Public guide",
            "scope": {"url_patterns": ["https://example.test/*"]}, "targets": [
                {"id": "guide", "decision": "block", "reason": "Block activation of captured target.",
                 "fingerprint": {"tag": "a", "id": "guide", "text": "Public guide", "href": "/guide"}}]}


def test_terminal_selects_general_policy_and_edits_risk_without_dashboard(project, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda label: print(label) or "1")
    run(["policy", "add", "--project", str(project.root)])
    assert project.policies("default")[0]["id"] == "acira.general.checkout"
    run(["policy", "configure", "payment", "--project", str(project.root), "--risk", "checkout.page_detected", "--decision", "block"])
    assert project.snapshot("default").config.skill_policies["acira.general.checkout"]["checkout.page_detected"] == "block"
    assert "Choose a general protection" in capsys.readouterr().out
    assert not (project.root.parent / "private/server.json").exists()


def test_terminal_import_configure_and_toggle_specific_are_agent_isolated(project, tmp_path, capsys):
    initialize(project.root, agent="other", general="policies/other/general", specific="policies/other/specific", selected=[], dashboard=False)
    source = tmp_path / "capture.json"
    source.write_text(json.dumps(capture()))
    common = ["--project", str(project.root)]
    run(["policy", "import", str(source), *common])
    run(["policy", "import", str(source), *common])
    assert len(project.policies("default")) == 1
    assert project.policies("other") == []
    run(["policy", "configure", "captured-guide", "--decision", "ask", *common])
    policy = project.policies("default")[0]
    assert policy["document"]["targets"][0]["decision"] == "ask"
    assert policy["document"]["targets"][0]["reason"] == "Ask before activation of captured target."
    assert len(project.snapshot("default").config.specific_skills) == 1
    run(["policy", "disable", "captured-guide", *common])
    assert not project.snapshot("default").config.specific_skills
    run(["policy", "enable", "captured-guide", *common])
    assert len(project.snapshot("default").config.specific_skills) == 1
    run(["policy", "list", *common])
    text = capsys.readouterr().out
    assert "Specific policies" in text and "acira extension install" in text
    run(["policy", "list", "--json", *common])
    assert json.loads(capsys.readouterr().out)["policies"][0]["kind"] == "specific"


def test_specific_cannot_be_configured_as_allow(project):
    project.save_policy("default", "guide.json", capture(), expected=None)
    before = project.snapshot("default").revision
    with pytest.raises(SystemExit) as error:
        run(["policy", "configure", "captured-guide", "--project", str(project.root), "--decision", "allow"])
    assert error.value.code == 2
    assert project.snapshot("default").revision == before


def test_noninteractive_configuration_never_guesses_a_decision(project, monkeypatch):
    project.save_policy("default", "guide.json", capture(), expected=None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    before = project.snapshot("default").revision
    with pytest.raises(SystemExit):
        run(["policy", "configure", "captured-guide", "--project", str(project.root)])
    assert project.snapshot("default").revision == before


def test_configure_updates_existing_agent_override_instead_of_ineffective_file_default(project):
    run(["policy", "add", "credentials", "--project", str(project.root)])
    policy = project.policies("default")[0]
    risk = next(iter(policy["document"]["acira_decisions"]))
    document = project.document()
    document["agents"]["default"]["decisions"] = {policy["id"]: {risk: "block"}}
    atomic_json(project.path, document)
    file_hash = digest(read_json(Path(policy["path"])))
    run(["policy", "configure", "credentials", "--risk", risk, "--decision", "ask", "--project", str(project.root)])
    assert project.snapshot("default").config.skill_policies[policy["id"]][risk] == "ask"
    assert digest(read_json(Path(policy["path"]))) == file_hash


def test_extension_export_is_idempotent_and_never_replaces_unrelated_files(tmp_path, monkeypatch):
    directory = install_extension(tmp_path / "Chrome extension")
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["manifest_version"] == 3
    assert "debugger" not in manifest["permissions"]
    assert "host_permissions" not in manifest
    assert (directory / "content.js").is_file()
    assert (directory / "assets/acira-logo.svg").is_file()
    assert install_extension(directory) == directory
    (directory / "my notes.txt").write_text("keep")
    with pytest.raises(ValueError, match="additional files"):
        install_extension(directory, replace=True)
    assert (directory / "my notes.txt").read_text() == "keep"
    symlink = tmp_path / "shortcut"
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == symlink or original(path))
    with pytest.raises(ValueError, match="symbolic link"):
        install_extension(symlink)
