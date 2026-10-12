"""Diagnose version gaps without pretending that unknown browser APIs are guarded."""
from importlib import metadata
from types import SimpleNamespace

import pytest

from acira.framework_compatibility import framework_compatibility, require_verified_framework


@pytest.mark.parametrize("adapter,version", [("playwright", "1.60.0"), ("playwright", "1.61.0"), ("playwright", "1.62.0"), ("playwright", "1.63.0"), ("browser_use", "0.11.12"), ("browser_use", "0.11.13")])
def test_exact_tested_framework_version_is_reported(monkeypatch, adapter, version):
    monkeypatch.setattr(metadata, "version", lambda name: version)
    assert framework_compatibility(adapter)["status"] == "verified"
    require_verified_framework(adapter)


@pytest.mark.parametrize("version", ["1.59.0", "1.64.0", "1.63.1", "1.63.0rc1"])
def test_unverified_version_does_not_inherit_a_range_claim(monkeypatch, version):
    monkeypatch.setattr(metadata, "version", lambda name: version)
    status = framework_compatibility("playwright")
    assert status["status"] == "unverified"
    assert status["installed_version"] == version
    with pytest.raises(ValueError, match="will not downgrade or modify"):
        require_verified_framework("playwright")


def test_missing_framework_has_explicit_diagnostics(monkeypatch):
    def missing(name):
        raise metadata.PackageNotFoundError(name)
    monkeypatch.setattr(metadata, "version", missing)
    assert framework_compatibility("playwright")["status"] == "missing"
    with pytest.raises(ValueError, match="not installed"):
        require_verified_framework("playwright")


def test_playwright_attach_rejects_unverified_version_before_patching(monkeypatch):
    from acira.adapters.playwright import PlaywrightCDPProxy
    async def launch(**kwargs):
        raise AssertionError("Version rejection must precede browser launch")
    browser_type = SimpleNamespace(name="chromium", launch=launch)
    playwright = SimpleNamespace(chromium=browser_type)
    monkeypatch.setattr(metadata, "version", lambda name: "1.64.0")
    manager = PlaywrightCDPProxy(config={"terminal_log": False})
    with pytest.raises(ValueError, match="1.64.0"):
        manager.patch_playwright(playwright)
    assert browser_type.launch is launch
    assert manager._patched_attrs == []


@pytest.mark.asyncio
async def test_browser_use_rejects_unverified_version_before_patching(monkeypatch):
    from acira.adapters.browser_use import BrowserUseCDPProxy
    monkeypatch.setattr(metadata, "version", lambda name: "0.13.11")
    manager = BrowserUseCDPProxy(config={"terminal_log": False})
    with pytest.raises(ValueError, match="0.13.11"):
        await manager.__aenter__()
    assert manager._browser_session_class is None
    assert manager._original_connect is None
