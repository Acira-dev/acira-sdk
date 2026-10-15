"""Capture UI stays reachable across viewport, zoom, and website styles."""
from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright, expect

from acira.extension import install_extension


HOSTILE_CSS = """
html { font-size: 250%; }
section { margin: 36px; padding: 48px; width: 900px; }
button { min-width: 480px !important; width: 720px !important;
  padding: 24px; margin: 20px; font: italic 42px/2 serif !important; }
span { white-space: nowrap !important; }
pre { white-space: pre; font: 36px/2 monospace; }
* { box-sizing: content-box; letter-spacing: 4px; }
"""


async def panel_geometry(panel):
    return await panel.evaluate("""panel => {
      const r=panel.getBoundingClientRect(), v=window.visualViewport;
      return {left:r.left,top:r.top,right:r.right,bottom:r.bottom,width:r.width,
        viewportWidth:v?.width??innerWidth,viewportHeight:v?.height??innerHeight,
        scrollWidth:panel.scrollWidth,clientWidth:panel.clientWidth,
        scrollHeight:panel.scrollHeight,clientHeight:panel.clientHeight};
    }""")


async def assert_icons_render(panel):
    icons = await panel.evaluate("""panel => Array.from(panel.querySelectorAll('svg')).map(svg => {
      const r=svg.getBoundingClientRect(), b=svg.getBBox();
      return {action:svg.closest('button')?.dataset.action??null,width:r.width,height:r.height,
        drawingWidth:b.width,drawingHeight:b.height,
        pathData:Array.from(svg.querySelectorAll('path')).map(path=>getComputedStyle(path).d)};
    })""")
    assert icons and any(icon["action"] == "close" for icon in icons)
    for icon in icons:
        assert icon["width"] > 0 and icon["height"] > 0, icon
        assert icon["drawingWidth"] > 0 and icon["drawingHeight"] > 0, icon
        assert icon["pathData"] and all(d and d != "none" for d in icon["pathData"]), icon


@pytest.mark.asyncio
@pytest.mark.parametrize("width,height,zoom,hostile", [
    (320, 480, 1, False),
    (800, 320, 1, False),
    (800, 600, 2, False),
    (390, 500, 1, True),
], ids=["narrow", "short", "browser-zoom-200", "host-css"])
async def test_packaged_capture_panel_fits_viewport_and_keeps_controls_reachable(tmp_path, width, height, zoom, hostile):
    # Both source and installed-wheel runs exercise the production export path.
    # The fixture grants only localhost; production extension permissions stay unchanged.
    extension = install_extension(tmp_path / "exported-extension")
    manifest = json.loads((extension / "manifest.json").read_text())
    manifest["host_permissions"] = ["http://127.0.0.1/*"]
    (extension / "manifest.json").write_text(json.dumps(manifest))
    hits = []

    async def fixture(request):
        hits.append(request.path)
        return web.Response(text="<link rel='icon' href='data:,'><title>Capture layout fixture</title>"
            + "<style>" + (HOSTILE_CSS if hostile else "") + "</style>"
            + "<main><button id='website-control'>Inert website button</button></main>", content_type="text/html")

    app = web.Application()
    app.router.add_get("/", fixture)
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(tmp_path / "anonymous-profile",
            channel="chromium", headless=True, chromium_sandbox=True,
            viewport={"width": width, "height": height}, args=["--password-store=basic", "--use-mock-keychain",
                "--disable-extensions-except=" + str(extension), "--load-extension=" + str(extension)])
        primary = None
        try:
            worker = context.service_workers[0] if context.service_workers else await context.wait_for_event("serviceworker", timeout=10000)
            page = await context.new_page()
            url = str(server.make_url("/"))
            await page.goto(url)
            host_before = await page.locator("#website-control").evaluate("el => ({font:getComputedStyle(el).font,minWidth:getComputedStyle(el).minWidth})")
            tab = await worker.evaluate("""async ({url,zoom}) => {
              const tab=(await chrome.tabs.query({})).find(t=>t.url===url);
              if (!tab) throw new Error('Fixture tab is missing');
              await chrome.tabs.setZoom(tab.id,zoom);
              const actual=await chrome.tabs.getZoom(tab.id);
              await chrome.scripting.insertCSS({target:{tabId:tab.id},files:['capture.css']});
              await chrome.scripting.executeScript({target:{tabId:tab.id},files:['content.js']});
              return {id:tab.id,zoom:actual};
            }""", {"url": url, "zoom": zoom})
            assert tab["zoom"] == zoom
            panel = page.get_by_role("region", name="Acira Capture", exact=True)
            await expect(panel).to_be_visible()
            geometry = await panel_geometry(panel)
            assert geometry["left"] >= 0 and geometry["top"] >= 0, geometry
            assert geometry["right"] <= geometry["viewportWidth"] + 1, geometry
            assert geometry["bottom"] <= geometry["viewportHeight"] + 1, geometry
            assert geometry["width"] <= 361, geometry
            assert geometry["scrollWidth"] <= geometry["clientWidth"] + 1, geometry
            if height <= 320 or zoom == 2:
                assert geometry["scrollHeight"] > geometry["clientHeight"], geometry
            await assert_icons_render(panel)
            await page.screenshot(path=str(tmp_path / "panel-header.png"))
            for name in ("Copy JSON", "Download", "Save to dashboard", "Connection settings"):
                button = panel.get_by_role("button", name=name, exact=True)
                await button.scroll_into_view_if_needed()
                usable = await button.evaluate("""button => {
                  const r=button.getBoundingClientRect(),p=button.closest('#acira-capture-panel').getBoundingClientRect();
                  const x=r.left+r.width/2,y=r.top+r.height/2;
                  const first=document.elementsFromPoint(x,y).find(el=>
                    !el.matches('.acira-capture-hover,.acira-capture-selected,.acira-capture-label'));
                  return {inside:r.left>=p.left&&r.right<=p.right&&r.top>=p.top&&r.bottom<=p.bottom,
                    centerHit:first===button||button.contains(first),
                    fontSize:getComputedStyle(button).fontSize};
                }""")
                assert usable["inside"] and usable["centerHit"], (name, usable)
                assert usable["fontSize"] == await panel.evaluate("el => getComputedStyle(el).fontSize"), (name, usable)
                await button.click(trial=True)
            await assert_icons_render(panel)
            await page.screenshot(path=str(tmp_path / "panel-footer.png"))
            await panel.get_by_role("button", name="Pause capture", exact=True).click()
            await expect(panel.locator("#acira-capture-pill")).to_have_text("Paused")
            await panel.get_by_role("button", name="Start capture", exact=True).click()
            await expect(panel.locator("#acira-capture-pill")).to_have_text("Capturing")
            close = panel.get_by_role("button", name="Close Acira Capture", exact=True)
            await close.focus()
            await expect(close).to_be_focused()
            await page.keyboard.press("Enter")
            await expect(panel).to_be_hidden()
            assert await page.locator("#website-control").evaluate("el => ({font:getComputedStyle(el).font,minWidth:getComputedStyle(el).minWidth})") == host_before
            assert page.url == url and hits == ["/"], "Layout interaction must not activate the website"
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                await asyncio.wait_for(context.close(), 15)
            except BaseException as cleanup:
                if primary is None:
                    raise
                primary.add_note("Capture layout context cleanup: " + type(cleanup).__name__)
