"""Direct attachment validates supported public API identity before changing objects."""
import os
import runpy
from pathlib import Path
from types import SimpleNamespace
from importlib import metadata

import pytest
pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright
from acira.adapters.playwright import PlaywrightCDPProxy
from acira_cdp_proxy.action_gateway import AciraActionBlocked



def test_direct_sync_page_is_rejected_without_changes(monkeypatch):
    from playwright.sync_api import Page
    page = Page.__new__(Page)
    protected = PlaywrightCDPProxy(config={"terminal_log": False})
    def unexpected(*args):
        raise AssertionError("Unsupported direct attachment must reject before context mutation")
    monkeypatch.setattr(protected, "_patch_context", unexpected)
    with pytest.raises(ValueError, match="asynchronous API"):
        protected.patch_page(page)
    assert protected._patched_attrs == []
    assert protected._contexts == []


@pytest.mark.parametrize("family", ["firefox", "webkit", None])
def test_direct_unverified_browser_family_is_rejected_before_changes(monkeypatch, family):
    class PublicPageMock:
        async def goto(self, *args):
            raise AssertionError("Identity verification must never navigate")
    monkeypatch.setattr("playwright.async_api.Page", PublicPageMock)
    browser = SimpleNamespace(browser_type=SimpleNamespace(name=family)) if family else None
    context = SimpleNamespace(browser=browser)
    page = PublicPageMock()
    page.context = context
    protected = PlaywrightCDPProxy(config={"terminal_log": False})
    def unexpected(*args):
        raise AssertionError("Unsupported direct attachment must reject before context mutation")
    monkeypatch.setattr(protected, "_patch_context", unexpected)
    with pytest.raises(ValueError, match="asynchronous Chromium"):
        protected.patch_page(page)
    assert protected._patched_attrs == []
    assert protected._contexts == []
    assert not hasattr(context, "_acira_registered")


@pytest.mark.asyncio
async def test_direct_persistent_chromium_page_remains_supported(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(tmp_path / "empty-profile", headless=True, chromium_sandbox=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            assert context.browser is not None
            assert context.browser.browser_type.name == "chromium"
            async with PlaywrightCDPProxy(config={"terminal_log": False}) as protected:
                protected.patch_page(page)
                assert context._acira_registered is protected
                with pytest.raises(AciraActionBlocked, match="Context script"):
                    await context.add_init_script("window.unreviewed=1")
                if hasattr(page, "local_storage"):
                    with pytest.raises(AciraActionBlocked, match="storage"):
                        await page.local_storage.set_item("unreviewed", "fixture")
                else:
                    assert metadata.version("playwright") == "1.60.0"
        finally:
            await context.close()
