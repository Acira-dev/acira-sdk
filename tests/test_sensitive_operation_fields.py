"""Real local form evidence masks API aliases without changing browser values."""
import asyncio
from contextlib import asynccontextmanager
import html
import json
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright

from acira import protect
from acira.adapters.playwright_boundary import trusted
from acira.project import initialize
from acira_cdp_proxy.action_gateway import AciraActionBlocked
from tests.test_captured_outbound_grants import (
    RejectReusableOutboundApproval,
    assert_two_independent_rejections,
    capture_and_import,
)


PRIVATE_FIELDS = {
    "api_key": "local fixture credential alpha",
    "api-key": "local fixture credential beta",
    "apiKey": "local fixture credential gamma",
    "X-API-Key": "local fixture credential delta",
    "client_api_key": "local fixture credential epsilon",
}
ORDINARY_FIELDS = {"message": "ordinary local draft", "key": "ordinary key choice"}


@asynccontextmanager
async def sensitive_form_site():
    received = []

    async def index(request):
        controls = "".join(
            '<input type="text" name="' + name + '" value="' + html.escape(value, quote=True) + '">'
            for name, value in {**PRIVATE_FIELDS, **ORDINARY_FIELDS}.items()
        )
        code = "window.attempts++;document.querySelector('#attempts').textContent=String(window.attempts);fetch('/outbound',{method:'POST',body:'local fixture only'})"
        markup = (
            '<title>Local private-field review</title><script>window.attempts=0</script>'
            '<form id="preferences" action="/outbound" method="post">' + controls +
            '<button id="preview" type="button" onclick="' + html.escape(code, quote=True) + '">Preview</button>'
            '</form><output id="attempts">0</output>'
        )
        return web.Response(text=markup, content_type="text/html")

    async def outbound(request):
        received.append({"method": request.method, "path": request.path})
        return web.Response(text="Local fixture only")

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/outbound", outbound)
    async with TestServer(app, host="127.0.0.1") as server:
        yield server, received


def assert_projected_fields(fields):
    values = {field["name"]: field["display"] for field in fields}
    assert {name: values[name] for name in PRIVATE_FIELDS} == {name: "[REDACTED]" for name in PRIVATE_FIELDS}
    assert {name: values[name] for name in ORDINARY_FIELDS} == ORDINARY_FIELDS
    serialized = json.dumps(fields)
    assert all(value not in serialized for value in PRIVATE_FIELDS.values())


class RejectSensitiveFormApproval(RejectReusableOutboundApproval):
    async def ask(self, request):
        evidence = request.classification.details["operation_evidence"]
        assert_projected_fields(evidence["fields"])
        pending = [review for review in self.journal.reviews() if review["state"] == "pending"]
        assert len(pending) == 1
        assert_projected_fields(pending[0]["evidence"]["fields"])
        return await super().ask(request)


async def close_owned_preserving_failure(close, label):
    primary = sys.exc_info()[1]
    try:
        await asyncio.wait_for(close(), 10)
    except BaseException as error:
        if primary is None:
            raise
        primary.add_note(label + " cleanup failed: " + type(error).__name__)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["playwright", "browser_use"])
async def test_captured_only_api_form_masks_review_and_denies_routine_grants(
    tmp_path, monkeypatch, select_capture_target, adapter,
):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    monkeypatch.setenv("BROWSER_USE_CLOUD_SYNC", "false")
    monkeypatch.setenv("BROWSER_USE_CONFIG_DIR", str(tmp_path / "browser-use"))
    project = initialize(tmp_path / "existing", adapter=adapter, selected=[], dashboard=False)
    async with sensitive_form_site() as (server, received):
        url = str(server.make_url("/"))
        captured = await capture_and_import(project, tmp_path, url, "preview", select_capture_target)
        assert all(value not in json.dumps(captured) for value in PRIVATE_FIELDS.values())
        reviewer = RejectSensitiveFormApproval(project)
        async with protect(project=project.root, dashboard=False, review_backend=reviewer) as guard, async_playwright() as p:
            if adapter == "playwright":
                guard.patch_playwright(p)
                browser = await p.chromium.launch(chromium_sandbox=True)
                try:
                    page = await browser.new_page()
                    await page.goto(url)
                    for _ in range(2):
                        with pytest.raises(AciraActionBlocked):
                            await page.locator("#preview").click()
                        assert await trusted(page, "evaluate", "window.attempts") == 0
                        # Readonly fixture observations prove masking never rewrites
                        # the actual browser field or its trusted wire result.
                        for name, value in {**PRIVATE_FIELDS, **ORDINARY_FIELDS}.items():
                            expression = "document.querySelector(" + json.dumps('[name="' + name + '"]') + ").value"
                            assert await trusted(page, "evaluate", expression) == value
                        assert received == []
                finally:
                    await close_owned_preserving_failure(browser.close, "Owned Playwright browser")
            else:
                pytest.importorskip("browser_use")
                from browser_use import BrowserSession
                from browser_use.browser.events import ClickElementEvent, NavigateToUrlEvent
                session = BrowserSession(
                    headless=True, executable_path=p.chromium.executable_path, chromium_sandbox=True,
                    user_data_dir=tmp_path / "profile", downloads_path=tmp_path / "downloads",
                    enable_default_extensions=False, permissions=[], keep_alive=False,
                )
                try:
                    await asyncio.wait_for(session.start(), 30)
                    navigation = session.event_bus.dispatch(NavigateToUrlEvent(url=url))
                    await asyncio.wait_for(navigation.event_result(raise_if_any=True, raise_if_none=False), 30)
                    for _ in range(2):
                        summary = await asyncio.wait_for(session.get_browser_state_summary(include_screenshot=False), 30)
                        node = next(value for value in summary.dom_state.selector_map.values() if value.attributes.get("id") == "preview")
                        clicked = session.event_bus.dispatch(ClickElementEvent(node=node))
                        result = await asyncio.wait_for(clicked.event_result(raise_if_any=True), 10)
                        assert result["acira"]["decision"].startswith("block") and "validation_error" in result
                        for name, value in {**PRIVATE_FIELDS, **ORDINARY_FIELDS}.items():
                            expression = "document.querySelector(" + json.dumps('[name="' + name + '"]') + ").value"
                            wire = await guard._send_browser_use_cdp(session, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
                            assert wire["result"]["value"] == value
                        attempts = await guard._send_browser_use_cdp(session, "Runtime.evaluate", {
                            "expression": "document.querySelector('#attempts').textContent", "returnByValue": True,
                        })
                        assert attempts["result"]["value"] == "0" and received == []
                finally:
                    await close_owned_preserving_failure(session.kill, "Owned browser-use session")
        assert_two_independent_rejections(reviewer, received, "preview")
        serialized = json.dumps({"reviews": reviewer.journal.reviews(), "events": reviewer.journal.events(limit=1000)})
        assert all(value not in serialized for value in PRIVATE_FIELDS.values())
        with reviewer.journal.db() as database:
            persisted = "\n".join(row[0] for table in ("events", "reviews", "runs")
                for row in database.execute("SELECT payload FROM " + table))
        assert all(value not in persisted for value in PRIVATE_FIELDS.values())
