"""CI setup paths stay exact; ordinary headless launches retain their sandbox."""
from pathlib import Path, PurePosixPath
import runpy
import sys
import types

import pytest


tool = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools" / "configure_ci_chromium.py"))


def installed(tmp_path):
    full = tmp_path / "chromium-1243" / "chrome-linux64" / "chrome"
    headless = tmp_path / "chromium_headless_shell-1243" / "chrome-headless-shell-linux64" / "chrome-headless-shell"
    for path in (full, headless):
        path.parent.mkdir(parents=True)
        path.write_text("fake test browser executable")
        path.chmod(0o700)
    output = (f"Chrome for Testing 153.0.8010.12 (playwright chromium v1243)\n"
              f"  Install location:    {full.parents[1]}\n"
              "FFmpeg (playwright ffmpeg v1011)\n"
              f"  Install location:    {tmp_path / 'ffmpeg-1011'}\n"
              "Chrome Headless Shell 153.0.8010.12 (playwright chromium-headless-shell v1243)\n"
              f"  Install location:    {headless.parents[1]}\n")
    return full, headless, output


def test_cli_declared_full_and_headless_paths(tmp_path):
    full, headless, output = installed(tmp_path)
    assert tool["installed_executables"](output, full) == {"chromium": full, "chromium-headless-shell": headless}
    for kind, path in {"chromium": full, "chromium-headless-shell": headless}.items():
        profile = tool["profile_for"](path, tool["PROFILE_NAMES"][kind])
        assert f'"{path.as_posix()}"' in profile and "  userns,\n" in profile
        assert "*" not in profile and "?" not in profile


@pytest.mark.parametrize("change", ["missing", "duplicate", "relative", "wrong_name", "different_cache"])
def test_rejects_invalid_cli_metadata(tmp_path, change):
    full, headless, output = installed(tmp_path)
    if change == "missing":
        output = output.split("Chrome Headless Shell")[0]
    elif change == "duplicate":
        output += output
    elif change == "relative":
        output = output.replace(str(full.parents[1]), "chromium-1243")
    elif change == "wrong_name":
        output = output.replace("chromium_headless_shell-1243", "unrelated")
    else:
        elsewhere = tmp_path / "different-cache"
        elsewhere.mkdir()
        output = output.replace(str(headless.parents[1]), str(elsewhere / "chromium_headless_shell-1243"))
    with pytest.raises(ValueError):
        tool["installed_executables"](output, full)


@pytest.mark.parametrize("kind", ["full", "headless"])
def test_rejects_out_of_root_symlink(tmp_path, kind):
    full, headless, output = installed(tmp_path)
    outside = tmp_path / "outside-browser"
    outside.write_text("unrelated file")
    outside.chmod(0o700)
    target = full if kind == "full" else headless
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        tool["installed_executables"](output, full)


@pytest.mark.parametrize("kind", ["full", "headless"])
def test_rejects_browser_root_symlink_escape(tmp_path, kind):
    full, headless, output = installed(tmp_path)
    target = full.parents[1] if kind == "full" else headless.parents[1]
    outside = tmp_path / ("outside-" + kind)
    target.rename(outside)
    target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        tool["installed_executables"](output, full)


@pytest.mark.parametrize("change", ["missing", "ambiguous", "directory", "not_executable"])
def test_rejects_unusable_headless_result(tmp_path, monkeypatch, change):
    full, headless, output = installed(tmp_path)
    if change in {"missing", "directory"}:
        headless.unlink()
        if change == "directory":
            headless.mkdir()
    elif change == "ambiguous":
        duplicate = headless.parents[1] / "other" / "chrome-headless-shell"
        duplicate.parent.mkdir()
        duplicate.write_text("duplicate")
        duplicate.chmod(0o700)
    else:
        original = tool["installed_executables"].__globals__["os"].access
        monkeypatch.setattr(tool["installed_executables"].__globals__["os"], "access", lambda path, mode: False if path == headless else original(path, mode))
    with pytest.raises(ValueError):
        tool["installed_executables"](output, full)


@pytest.mark.parametrize("bad", ['*', '?', '[', ']', '{', '}', '"', '\\', '\n', '\r', '\t', '\x00'])
def test_rejects_literal_profile_path_injection(tmp_path, bad):
    with pytest.raises(ValueError):
        tool["literal_path"](PurePosixPath("/literal-app-armor") / ("bad" + bad + "browser"))


def test_profile_name_cannot_address_an_unrelated_profile(tmp_path):
    full, _, _ = installed(tmp_path)
    with pytest.raises(ValueError):
        tool["profile_for"](full, "unrelated-global-profile")


@pytest.mark.asyncio
async def test_ci_loads_both_exact_profiles_and_probes_both_sandboxed_launches(tmp_path, monkeypatch):
    full, _, output = installed(tmp_path)
    restriction = tmp_path / "kernel-restriction"
    restriction.write_text("1")
    calls, launch_options = [], []

    async def evaluate(expression):
        assert expression == "1 + 1"
        return 2

    async def goto(url):
        assert url == "about:blank"

    page = types.SimpleNamespace(evaluate=evaluate, goto=goto)

    class Browser:
        async def new_page(self):
            return page
        async def close(self):
            pass

    async def launch(**options):
        launch_options.append(options)
        return Browser()

    class Context:
        async def __aenter__(self):
            return types.SimpleNamespace(chromium=types.SimpleNamespace(executable_path=str(full), launch=launch))
        async def __aexit__(self, *exc):
            pass

    module = types.ModuleType("playwright.async_api")
    module.async_playwright = Context
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    globals_ = tool["main"].__globals__
    monkeypatch.setitem(globals_, "Path", lambda value: restriction if str(value) == "/proc/sys/kernel/apparmor_restrict_unprivileged_userns" else Path(value))

    def run(command, **kwargs):
        if command[:3] == [sys.executable, "-m", "playwright"]:
            assert command[3:] == ["install", "--dry-run", "chromium"]
            return types.SimpleNamespace(stdout=output)
        assert command[:4] == ["sudo", "--non-interactive", "apparmor_parser", "--replace"]
        calls.append(Path(command[4]).read_text())
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(globals_["subprocess"], "run", run)
    await tool["main"]()
    assert len(calls) == 2
    assert all(f"profile {name} " in calls[index] for index, name in enumerate(tool["PROFILE_NAMES"].values()))
    assert launch_options == [
        {"headless": True, "chromium_sandbox": True, "timeout": 15000},
        {"headless": True, "chromium_sandbox": True, "timeout": 15000, "executable_path": str(full)},
    ]
    assert restriction.read_text() == "1"
