"""Policy loading must never announce protection before actual attachment."""
import pytest

from acira import protect
from acira.project import initialize
pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright


@pytest.mark.asyncio
async def test_missing_attachment_has_truthful_start_and_exit_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", selected=["destructive-actions"], dashboard=False)
    async with protect(project=project.root):
        startup = capsys.readouterr().err
        assert "policies loaded; waiting for guard.patch_playwright" in startup
        assert "interception ON" not in startup
    assert "No Chromium context was attached" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_late_patch_only_arms_future_launches_and_warns_at_exit(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", selected=[], dashboard=False)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, chromium_sandbox=True)
        try:
            await browser.new_page()
            async with protect(project=project.root) as guard:
                guard.patch_playwright(p)
                text = capsys.readouterr().err
                assert "armed for future Chromium" in text and "Existing browsers are not attached" in text
                assert "Chromium context attached" not in text
            assert "No Chromium context was attached" in capsys.readouterr().err
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_correct_attachment_announces_context_and_restores_methods(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", selected=[], dashboard=False)
    async with async_playwright() as p:
        launch = p.chromium.launch
        async with protect(project=project.root) as guard:
            guard.patch_playwright(p)
            browser = await p.chromium.launch(headless=True, chromium_sandbox=True)
            await browser.new_page()
            text = capsys.readouterr().err
            assert text.count("Chromium context attached") == 1
        assert p.chromium.launch == launch
        assert "No Chromium context was attached" not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_quiet_attachment_notices_follow_terminal_log_setting(tmp_path, monkeypatch, capsys):
    from acira.local import atomic_json
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", selected=[], dashboard=False)
    config = project.document()
    config["agents"]["default"]["guard"] = {"terminal_log": False}
    atomic_json(project.path, config)
    async with protect(project=project.root):
        assert not capsys.readouterr().err
    assert not capsys.readouterr().err


@pytest.mark.asyncio
async def test_failed_context_wrapper_install_never_claims_attached(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", selected=[], dashboard=False)
    # A context can fail halfway through wrapper installation. Merely recording
    # ownership must not announce success before all supported wrappers install.
    context = SimpleNamespace(on=lambda *args: None, remove_listener=lambda *args: None)
    async with protect(project=project.root) as guard:
        capsys.readouterr()
        with pytest.raises(AttributeError):
            guard._patch_context(context)
        assert "Chromium context attached" not in capsys.readouterr().err
