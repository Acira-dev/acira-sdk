import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright
from acira.adapters.playwright import PlaywrightCDPProxy
from acira.adapters.playwright_boundary import trusted
from acira_cdp_proxy.action_gateway import AciraActionBlocked


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_amount,decision,expected_count", [("10", "allow", 1), ("900", "allow", 0), ("10", "block", 0)])
@pytest.mark.parametrize("background", [False, True], ids=["direct", "background-read"])
async def test_submission_binds_actual_request_and_denies_retry(actual_amount, decision, expected_count, background):
    received = []
    async def index(request):
        payment = "fetch('/payment',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({amount:'"+actual_amount+"',currency:'USD',account:'account-123'})})"
        code = "fetch('/status').catch(()=>{}).finally(()=>" + payment + ")" if background else payment
        return web.Response(text='<button type="button" id="pay" onclick="'+code+'">Pay</button>', content_type="text/html")
    async def payment(request):
        received.append(await request.json())
        return web.json_response({"received": True})
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/payment", payment)
    class Reviewer:
        requests = []
        async def ask(self, request):
            self.requests.append(request)
            return decision
    reviewer = Reviewer()
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as p:
        async with PlaywrightCDPProxy(config={"terminal_log": False}, review_backend=reviewer) as guard:
            guard.patch_playwright(p)
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(str(server.make_url("/")))
            args = {"destination": str(server.make_url("/payment")), "values": {"amount":"10", "currency":"USD", "account":"account-123"},
                    "facts": {"amount":"amount", "currency":"currency", "account":"account"}}
            if expected_count:
                await guard.submit(page.locator("#pay"), **args)
                # Application-level retries through this page cannot reuse approval.
                await trusted(page, "evaluate", "document.querySelector('#pay').click()")
                await asyncio.sleep(.1)
            else:
                with pytest.raises(AciraActionBlocked):
                    await guard.submit(page.locator("#pay"), **args)
            assert len(received) == expected_count
            assert len(reviewer.requests) == 1
            evidence = reviewer.requests[0].classification.details["operation_evidence"]
            assert evidence["verified_fields"]["amount"] == "10"
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["popup", "worker", "page-routing"])
async def test_submission_covers_new_targets_and_refuses_page_routing(case):
    received = []
    async def index(request):
        code = "window.open('/popup')" if case == "popup" else "new Worker('/worker.js')" if case == "worker" else "fetch('/payment',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({amount:'10'})})"
        return web.Response(text='<button id="pay" onclick="'+code+'">Pay</button>', content_type="text/html")
    async def payment(request):
        received.append(await request.json())
        return web.json_response({"received": True})
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/payment", payment)
    async def popup(request):
        return web.Response(text="<script>fetch('/payment',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({amount:'10'})})</script>", content_type="text/html")
    async def worker(request):
        return web.Response(text="fetch('/payment',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({amount:'10'})})", content_type="application/javascript")
    app.router.add_get("/popup", popup)
    app.router.add_get("/worker.js", worker)
    class Reviewer:
        async def ask(self, request):
            return "allow"
    async with TestServer(app, host="127.0.0.1") as server, async_playwright() as p:
        async with PlaywrightCDPProxy(config={"terminal_log": False}, review_backend=Reviewer()) as guard:
            guard.patch_playwright(p)
            browser = await p.chromium.launch()
            page = await browser.new_page()
            await page.goto(str(server.make_url("/")))
            if case == "page-routing":
                await page.route("**/*", lambda route: route.continue_())
            args = {"destination": str(server.make_url("/payment")), "values": {"amount":"10"}}
            if case == "worker":
                await guard.submit(page.locator("#pay"), **args)
                with pytest.raises(AciraActionBlocked, match="Network routing"):
                    await page.context.unroute_all()
            else:
                with pytest.raises(AciraActionBlocked):
                    await guard.submit(page.locator("#pay"), **args)
            await asyncio.sleep(.1)
            assert received == ([{"amount":"10"}] if case == "worker" else [])
            await browser.close()
