"""Current form values and requested selections share credential-hint masking."""
import asyncio
import html
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright

from acira.adapters.playwright import PlaywrightCDPProxy


@pytest.mark.asyncio
@pytest.mark.parametrize("hint,label,sensitive", [
    ("aria-label", "API key", True),
    ("title", "Credential choice", True),
    ("autocomplete", "cc-number", True),
    ("aria-label", "Preferred display", False),
])
async def test_in_form_selection_masks_current_and_requested_credential_values(hint, label, sensitive):
    previous = "privatevalue" if sensitive else "publicchoice"
    hits, reviews = [], []
    async def index(request):
        return web.Response(text=f"""<form id="preferences">
            <select id="choice" name="choice" {hint}="{html.escape(label, quote=True)}"
              onchange="fetch('/effect',{{method:'POST',body:this.value}})">
            <option value="{previous}">Previously selected</option>
            <option value="next">Next</option></select></form>""", content_type="text/html")
    async def effect(request):
        hits.append(await request.text())
        return web.Response(text="Observed local selection")
    class Reviewer:
        async def ask(self, request):
            reviews.append(request)
            assert hits == []
            return "allow"
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/effect", effect)
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as pw:
        async with PlaywrightCDPProxy(config={"review_actions": ["select"], "terminal_log": False}, review_backend=Reviewer()) as guard:
            guard.patch_playwright(pw)
            browser = await pw.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = await browser.new_page()
                await page.goto(str(server.make_url("/")))
                assert await page.locator("#choice").input_value() == previous
                assert await page.locator("#choice").select_option(value=["next"]) == ["next"]
                async with asyncio.timeout(5):
                    while not hits:
                        await asyncio.sleep(.01)
                assert hits == ["next"] and len(reviews) == 1
                evidence = reviews[0].classification.details["operation_evidence"]
                assert evidence["fields"] == [{"name": "choice", "display": "[REDACTED]" if sensitive else previous, "checked": None}]
                if sensitive:
                    assert previous not in json.dumps(evidence)
                    assert evidence["requested_selection"] == "[REDACTED]"
                    assert evidence["critical"] is True and "grant_scope" not in evidence
                else:
                    assert evidence["requested_selection"] == {"byValue": ["next"]}
                    assert previous in json.dumps(evidence)
            finally:
                await browser.close()
