"""First-run errors stay visible, actionable and safe to retry."""
import json
from pathlib import Path

import pytest

from acira.cli import run
from acira.extension import install_extension
from acira.project import Project, initialize
from tests.test_terminal_policies import capture


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))


@pytest.mark.parametrize("adapter", ["browser-use", "BROWSER_USE", "Playwright"])
def test_init_accepts_framework_spelling_and_case(tmp_path, adapter):
    root = tmp_path / "project"
    run(["init", "--project", str(root), "--yes", "--adapter", adapter, "--policies", "none"])
    assert Project(root).snapshot().profile["adapter"] == adapter.lower().replace("-", "_")


def test_repeated_init_does_not_prompt_or_change_existing_settings(tmp_path, monkeypatch, capsys):
    project = initialize(tmp_path / "project", adapter="browser_use", selected=[])
    before = project.path.read_bytes()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Existing agents must not prompt then ignore the answers"))
    run(["init", "--project", str(project.root)])
    assert project.path.read_bytes() == before
    assert "Already initialized" in capsys.readouterr().out


def test_wizard_reasks_invalid_choices_before_creating_files(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    answers = iter(["typo", "browser-use", "", "", "paymant", "none", "perhaps", "no"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    def answer(_):
        assert not root.exists(), "The wizard must finish before creating project files"
        return next(answers)
    monkeypatch.setattr("builtins.input", answer)
    run(["init", "--project", str(root)])
    assert Project(root).snapshot().profile["adapter"] == "browser_use"
    assert Project(root).policies("default") == []
    assert Project(root).document()["dashboard"] == {"connect": False}
    text = capsys.readouterr()
    assert text.err.count("Try again") == 3
    assert "No starter policies selected" in text.out


@pytest.mark.parametrize("exception", [KeyboardInterrupt, EOFError])
def test_cancelled_wizard_has_no_traceback_or_partial_project(tmp_path, monkeypatch, capsys, exception):
    root = tmp_path / "project"
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    def cancel(_):
        raise exception()
    monkeypatch.setattr("builtins.input", cancel)
    with pytest.raises(SystemExit) as error:
        run(["init", "--project", str(root)])
    assert error.value.code == 130 and not root.exists()
    stderr = capsys.readouterr().err
    assert "Cancelled" in stderr and "Traceback" not in stderr


@pytest.mark.parametrize("selected", [["payment", "paymant"], ["missing"]])
def test_invalid_starter_choices_do_not_partially_initialize(tmp_path, selected):
    root = tmp_path / "project"
    with pytest.raises(ValueError, match="Unknown policy"):
        initialize(root, selected=selected)
    assert not root.exists()


@pytest.mark.parametrize("name", ["payment", "checkout", "acira.general.checkout"])
def test_catalog_alias_can_be_added_from_terminal(tmp_path, name):
    project = initialize(tmp_path / "project", selected=[])
    run(["policy", "add", name, "--project", str(project.root)])
    assert [p["id"] for p in project.policies("default")] == ["acira.general.checkout"]


@pytest.mark.parametrize("payload,hint", [('{' , "line 1, column 2"), ('[]', "Expected a JSON object"), ('{"anything": true}', "Acira Capture")])
def test_import_errors_name_source_and_keep_project_unchanged(tmp_path, capsys, payload, hint):
    project = initialize(tmp_path / "project", selected=[])
    source = tmp_path / "bad capture.json"
    source.write_text(payload)
    before = project.snapshot().revision
    with pytest.raises(SystemExit) as error:
        run(["policy", "import", str(source), "--project", str(project.root)])
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert str(source) in stderr and hint in stderr
    assert project.snapshot().revision == before


def test_updated_capture_import_requires_explicit_replace_and_gives_repair(tmp_path, capsys):
    project = initialize(tmp_path / "project", selected=[])
    source = tmp_path / "acira-skill-example.test.json"
    value = capture()
    source.write_text(json.dumps(value))
    argv = ["policy", "import", str(source), "--project", str(project.root)]
    run(argv)
    before = project.snapshot().revision
    value["targets"][0]["decision"] = "ask"
    source.write_text(json.dumps(value))
    with pytest.raises(SystemExit):
        run(argv)
    assert "--replace" in capsys.readouterr().err
    assert project.snapshot().revision == before
    run([*argv, "--replace"])
    assert project.policies("default")[0]["document"]["targets"][0]["decision"] == "ask"


def test_existing_empty_extension_folder_is_a_valid_destination(tmp_path):
    destination = tmp_path / "Chrome Capture"
    destination.mkdir()
    assert install_extension(destination) == destination
    assert (destination / "manifest.json").is_file()


def test_unmodified_receipt_install_updates_without_manual_replace(tmp_path, monkeypatch):
    import acira.extension as extension
    destination = install_extension(tmp_path / "capture")
    source = extension.extension_files
    updated = source()
    updated["README.md"] = updated.get("README.md", b"") + b"\nUpdated instructions\n"
    monkeypatch.setattr(extension, "extension_files", lambda: updated)
    install_extension(destination)
    assert (destination / "README.md").read_bytes() == updated["README.md"]
    (destination / "README.md").write_text("User edits")
    with pytest.raises(ValueError, match="--replace"):
        install_extension(destination)
    assert (destination / "README.md").read_text() == "User edits"


@pytest.mark.parametrize("legacy_aliases", [False, True])
def test_capture_parser_accepted_formats_remain_importable(tmp_path, legacy_aliases):
    from acira_cdp_proxy.skills import SpecificSkill
    project = initialize(tmp_path / "project", selected=[])
    value = capture()
    value["schema"] = "  acira.specific_skill.v2  "
    if legacy_aliases:
        value["scope"] = {"urls": value["scope"]["url_patterns"]}
        value["targets"][0]["match"] = value["targets"][0].pop("fingerprint")
    assert SpecificSkill.from_dict(value).id == value["id"]
    source = tmp_path / "legacy-capture.json"
    source.write_text(json.dumps(value))
    run(["policy", "import", str(source), "--project", str(project.root)])
    assert project.policies("default")[0]["id"] == value["id"]


def test_malformed_capture_field_types_are_path_aware_cli_errors(tmp_path, capsys):
    project = initialize(tmp_path / "project", selected=[])
    value = capture()
    value["scope"] = ["not", "an", "object"]
    source = tmp_path / "bad-field.json"
    source.write_text(json.dumps(value))
    before = project.snapshot().revision
    with pytest.raises(SystemExit) as error:
        run(["policy", "import", str(source), "--project", str(project.root)])
    text = capsys.readouterr().err
    assert error.value.code == 2 and str(source) in text
    assert "scope must be an object" in text and "Traceback" not in text
    assert project.snapshot().revision == before
