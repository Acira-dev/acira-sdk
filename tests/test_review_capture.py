"""Observe pixels and coordinates when review pages change during capture."""
import asyncio
import base64
import io

import pytest
from PIL import Image

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright
from acira_cdp_proxy.screenshots import capture_masked_cdp
from acira_cdp_proxy.redaction import SECRET_REDACTION_REGISTRY


@pytest.mark.asyncio
@pytest.mark.parametrize("moving", [False, True], ids=["transient-layout", "unstable-layout"])
async def test_capture_recovers_only_after_mask_and_target_geometry_stabilize(moving):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 800, "height": 600})
        await page.set_content('<input value="private-test-field"><p id="status">Preparing</p><button>Review</button>')
        session = await page.context.new_cdp_session(page)
        captures = 0

        async def probe(method, params, session_id):
            nonlocal captures
            if method == "Page.captureScreenshot":
                captures += 1
                if moving or captures == 1:
                    await page.evaluate("n => {const s=document.querySelector('#status');s.textContent='Updated '+n;s.style.marginTop=(n*20)+'px'}", captures)
            return await session.send(method, params)

        artifact = await capture_masked_cdp(probe, target_probe=page.locator("button").bounding_box)
        if moving:
            assert artifact["capture_status"] == "unavailable"
            assert artifact["reason_code"] == "page_unstable"
            assert "image" not in artifact and captures == 3
        else:
            assert artifact["capture_status"] == "ready" and captures == 2
            assert artifact["bounding_box"] == await page.locator("button").bounding_box()
            pixels = Image.open(io.BytesIO(base64.b64decode(artifact["image"])))
            field = await page.locator("input").bounding_box()
            assert pixels.getpixel((int(field["x"] + 2), int(field["y"] + 2))) == (0, 0, 0)
        await browser.close()


@pytest.mark.asyncio
async def test_capture_does_not_reuse_stale_static_target_box_after_layout_change():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content('<p id="status">Preparing</p><button>Review</button>')
        session = await page.context.new_cdp_session(page)
        box = await page.locator("button").bounding_box()
        captures = 0

        async def probe(method, params, session_id):
            nonlocal captures
            if method == "Page.captureScreenshot":
                captures += 1
                await page.evaluate("document.querySelector('#status').style.marginTop='100px'")
            return await session.send(method, params)

        artifact = await capture_masked_cdp(probe, bounding_box=box)
        assert artifact["capture_status"] == "unavailable" and "image" not in artifact
        assert captures == 1
        await browser.close()


@pytest.mark.asyncio
async def test_capture_unresponsive_transport_has_one_total_time_budget():
    async def unavailable(*args):
        await asyncio.Event().wait()
    artifact = await asyncio.wait_for(capture_masked_cdp(unavailable), 6)
    assert artifact["capture_status"] == "unavailable" and "image" not in artifact
    assert artifact["reason_code"] == "capture_timeout"


@pytest.mark.asyncio
async def test_capture_omits_an_image_with_a_target_outside_its_visual_viewport():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content('<div style="height:2000px"></div><button>Review</button>')
        session = await page.context.new_cdp_session(page)
        async def probe(method,params,sid):
            return await session.send(method,params)
        artifact = await capture_masked_cdp(probe,target_probe=page.locator("button").bounding_box)
        assert artifact["capture_status"] == "unavailable" and "image" not in artifact
        assert artifact["reason_code"] == "target_unavailable"
        await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["pseudo-text", "closed-shadow-text", "closed-shadow-input", "forged-page-scan", "sensitive-shadow", "sensitive-filter", "sensitive-stroke", "sensitive-reflection"])
async def test_native_capture_masks_private_rendering_that_page_javascript_cannot_scan(case):
    secret = "only-for-review-image-privacy-test"
    SECRET_REDACTION_REGISTRY.register_secret(secret_id="native-image-test", label="Fake secret", value=secret)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width": 800, "height": 600})
            await page.set_content('<div id="holder" style="display:block;width:400px;height:100px"></div><button>Review</button>')
            if case == "pseudo-text":
                await page.evaluate("s=>{const style=document.createElement('style');style.textContent='#holder::before {content:'+JSON.stringify(s)+'}';document.head.append(style)}", secret)
            elif case.startswith("closed-shadow"):
                await page.evaluate("data=>{const root=document.querySelector('#holder').attachShadow({mode:'closed'});root.innerHTML=data.input?'<input value=\"'+data.secret+'\">':'<p>'+data.secret+'</p>'}", {"input": case.endswith("input"), "secret": secret})
            else:
                await page.evaluate("s=>document.querySelector('#holder').textContent=s", secret)
                if case == "forged-page-scan":
                    await page.evaluate("Document.prototype.createTreeWalker=()=>({nextNode:()=>null});Element.prototype.getBoundingClientRect=()=>({x:0,y:0,width:0,height:0})")
                elif case == "sensitive-shadow":
                    await page.evaluate("document.querySelector('#holder').style.textShadow='60px 60px black'")
                elif case == "sensitive-filter":
                    await page.evaluate("document.body.style.filter='drop-shadow(60px 60px black)'")
                elif case == "sensitive-stroke":
                    await page.evaluate("document.querySelector('#holder').style.webkitTextStroke='20px black'")
                else:
                    await page.evaluate("document.querySelector('#holder').style.webkitBoxReflect='below 60px'")
            session = await page.context.new_cdp_session(page)
            async def probe(method, params, session_id):
                return await session.send(method, params)
            artifact = await capture_masked_cdp(probe)
            if case.startswith("sensitive-"):
                assert artifact["capture_status"] == "unavailable" and "image" not in artifact
                assert artifact["reason_code"] == "masking_effects_unbounded"
            else:
                assert artifact["capture_status"] == "ready"
                pixels = Image.open(io.BytesIO(base64.b64decode(artifact["image"])))
                # The fake secret starts at (8,8). Sample the interior of its
                # native text/control rectangle, not a glyph's black pixels.
                assert all(pixels.getpixel((x, y)) == (0, 0, 0) for x in range(10, 80) for y in range(10, 22))
            await browser.close()
    finally:
        SECRET_REDACTION_REGISTRY.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("device_scale", [1, 1.25, 2, 3])
@pytest.mark.parametrize("visual_scale", [1, 1.5])
async def test_native_masks_use_css_coordinates_on_scaled_scrolled_viewports(device_scale, visual_scale):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width":800,"height":600}, device_scale_factor=device_scale)
        await page.set_content('<style>body{zoom:1.25}input{width:250px;height:40px}</style><div style="height:900px"></div><input value="private-review-only-field"><div style="height:1000px"></div>')
        session = await page.context.new_cdp_session(page)
        await session.send("Emulation.setPageScaleFactor", {"pageScaleFactor":visual_scale})
        await page.locator("input").scroll_into_view_if_needed()
        async def probe(method,params,sid):
            return await session.send(method,params)
        artifact = await capture_masked_cdp(probe)
        assert artifact["capture_status"] == "ready"
        pixels = Image.open(io.BytesIO(base64.b64decode(artifact["image"])))
        viewport = (await session.send("Page.getLayoutMetrics"))["cssVisualViewport"]
        box = await page.locator("input").bounding_box()
        sx,sy = pixels.width/viewport["clientWidth"],pixels.height/viewport["clientHeight"]
        left,top = max(0,(box["x"]-viewport["offsetX"])*sx),max(0,(box["y"]-viewport["offsetY"])*sy)
        right,bottom = min(pixels.width,(box["x"]-viewport["offsetX"]+box["width"])*sx),min(pixels.height,(box["y"]-viewport["offsetY"]+box["height"])*sy)
        assert right-left > 10 and bottom-top > 10
        x,y = int((left+right)/2-5),int((top+bottom)/2-5)
        assert all(pixels.getpixel((x+dx,y+dy)) == (0,0,0) for dx in range(10) for dy in range(10))
        await browser.close()


@pytest.mark.asyncio
async def test_capture_diagnostics_never_emit_browser_exception_payloads():
    async def secret_failure(*args):
        raise ValueError("fixture-private-token-cookie-card-value")
    artifact = await capture_masked_cdp(secret_failure)
    assert artifact["reason_code"] == "capture_unavailable"
    assert "fixture-private" not in str(artifact) and "image" not in artifact


def test_capture_reason_enum_survives_durable_journal_redaction(tmp_path):
    from acira.journal import Journal
    from acira_cdp_proxy.screenshots import CAPTURE_REASONS,unavailable_capture
    journal=Journal(tmp_path)
    for code in CAPTURE_REASONS:
        journal.event("capture_fixture",artifact=unavailable_capture(code))
    assert [event["artifact"]["reason_code"] for event in journal.events()] == list(CAPTURE_REASONS)


def test_capture_reason_restoration_never_preserves_arbitrary_access_codes(tmp_path):
    from acira.journal import Journal
    journal=Journal(tmp_path)
    journal.event("capture_fixture",artifact={"capture_status":"unavailable",
        "reason_code":"654321","code":"987654","reason":"Safe fixed fixture message"})
    event=journal.events()[0]
    assert event["artifact"]["reason_code"] == event["artifact"]["code"] == "[REDACTED]"
    journal.event("capture_fixture",artifact={"capture_status":"ready","reason_code":"target_unavailable"})
    assert journal.events()[1]["artifact"]["reason_code"] == "[REDACTED]"


@pytest.mark.asyncio
@pytest.mark.parametrize("device_scale", [1, 2])
async def test_playwright_review_scrolls_exact_target_into_masked_image_before_rejection(tmp_path,monkeypatch,device_scale):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from acira import protect
    from acira.project import initialize
    from acira.local import atomic_json
    from acira.journal import Journal
    from acira_cdp_proxy.action_gateway import AciraActionBlocked
    monkeypatch.setenv("ACIRA_HOME",str(tmp_path/"private"))
    project = initialize(tmp_path/"project",adapter="playwright",selected=[],dashboard=False)
    document = project.document()
    document["agents"]["default"]["guard"] = {"terminal_log":False}
    atomic_json(project.path,document)
    hits = []
    async def index(request):
        return web.Response(text='<div style="height:1200px"></div><form><input value="private-review-only-field"><button type="button" id="pay" onclick="fetch(\'/payment\',{method:\'POST\',headers:{\'content-type\':\'application/json\'},body:JSON.stringify({amount:\'10\'})})">Pay</button></form>',content_type="text/html")
    async def payment(request):
        hits.append(await request.json())
        return web.json_response({"received":True})
    app = web.Application()
    app.router.add_get("/",index)
    app.router.add_post("/payment",payment)
    class Reviewer:
        async def ask(self,request):
            artifact = Journal(project.root).reviews()[0]["artifact"]
            assert artifact["capture_status"] == "ready"
            box,viewport = artifact["bounding_box"],artifact["viewport"]
            assert 0 <= box["y"] < box["y"]+box["height"] <= viewport["height"]
            pixels = Image.open(project.root/".acira/artifacts"/artifact["id"])
            field = await page.locator("input").bounding_box()
            sx,sy = pixels.width/viewport["width"],pixels.height/viewport["height"]
            assert pixels.getpixel((int((field["x"]+10)*sx),int((field["y"]+10)*sy))) == (0,0,0)
            return "block"
    async with TestServer(app,host="127.0.0.1") as server,async_playwright() as p:
        async with protect(project=project.root,dashboard=False,review_backend=Reviewer()) as guard:
            guard.patch_playwright(p)
            browser = await p.chromium.launch()
            page = await browser.new_page(viewport={"width":800,"height":600},device_scale_factor=device_scale)
            await page.goto(str(server.make_url("/")))
            assert (await page.locator("#pay").bounding_box())["y"] > 600
            with pytest.raises(AciraActionBlocked):
                await guard.submit(page.locator("#pay"),destination=str(server.make_url("/payment")),values={"amount":"10"})
            assert hits == []
            await browser.close()
