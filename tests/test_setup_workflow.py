import textwrap
import pytest

from acira.cli import run
from acira.local import Registry
from acira.project import Project, initialize


@pytest.mark.parametrize("adapter", ["playwright", "browser_use"])
def test_init_preserves_existing_application_and_starts_no_demo(tmp_path, monkeypatch, capsys, adapter):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    root = tmp_path / "my existing project"
    root.mkdir()
    application = root / "agent.py"
    application.write_text("# My existing browser agent\n", encoding="utf-8")
    requirements = root / "requirements.txt"
    requirements.write_text("# Existing dependencies\n", encoding="utf-8")
    ignore = root / ".gitignore"
    ignore.write_text(".venv/\n", encoding="utf-8")

    def unexpected_launch(*args, **kwargs):
        pytest.fail("Project initialization must not launch a demo, browser, or model service")

    monkeypatch.setattr("webbrowser.open", unexpected_launch)
    monkeypatch.setattr("subprocess.Popen", unexpected_launch)
    run(["init", "--project", str(root), "--agent", "checkout", "--adapter", adapter,
         "--yes", "--no-dashboard", "--policies", "credentials"])
    output = capsys.readouterr().out

    assert application.read_text() == "# My existing browser agent\n"
    assert requirements.read_text() == "# Existing dependencies\n"
    assert ignore.read_text().startswith(".venv/\n")
    assert Project(root).snapshot("checkout").profile["adapter"] == adapter
    assert len(Registry().projects()) == 1
    assert not (tmp_path / "private/server.json").exists()
    assert "acira doctor --agent checkout" in output
    assert "acira policy validate --agent checkout" in output
    assert "Start your agent using its usual command" in output
    assert "automatic dashboard connection disabled" in output
    snippet = output.split("\nfrom acira import protect\n", 1)[1].split("\n2. ", 1)[0]
    snippet = "from acira import protect\n" + snippet
    compile("async def entry_point():\n" + textwrap.indent(snippet, "    "), "printed integration snippet", "exec")
    assert ("guard.patch_playwright(p)" in snippet) == (adapter == "playwright")


def test_reinitialization_prints_the_preserved_agent_integration(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "existing", agent="research", adapter="browser_use", selected=[], dashboard=False)
    before = project.path.read_bytes()
    run(["init", "--project", str(project.root), "--agent", "research", "--adapter", "playwright", "--yes"])
    output = capsys.readouterr().out
    assert project.path.read_bytes() == before
    assert "entry point (browser_use)" in output
    assert "guard.patch_playwright" not in output
    assert "Create BrowserSession and Agent" in output
    assert "acira doctor --agent research" in output
