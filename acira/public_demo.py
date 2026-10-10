"""Visible, read-only public documentation workflows through the real SDK."""
from __future__ import annotations

import asyncio
import contextlib
import re
import webbrowser
from html import escape
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from aiohttp import web
from playwright.async_api import async_playwright

from acira import protect
from acira_cdp_proxy.action_gateway import AciraActionBlocked

from .journal import Journal
from .local import Registry, atomic_json, state_dir
from .project import Project, initialize
from .service import open_dashboard


SITES = {
    "python": {"name": "Python documentation", "url": "https://docs.python.org/3/tutorial/",
               "href": "interpreter.html", "prefix": "/3/", "heading": "2. Using the Python Interpreter"},
    "mdn": {"name": "MDN JavaScript guide", "url": "https://developer.mozilla.org/en-US/docs/Web/JavaScript/Guide",
            "href": "/en-US/docs/Web/JavaScript/Guide/Control_flow_and_error_handling", "prefix": "/en-US/docs/", "heading": "Control flow and error handling"},
    "playwright": {"name": "Playwright documentation", "url": "https://playwright.dev/python/docs/intro",
                   "href": "/python/docs/writing-tests", "prefix": "/python/docs/", "heading": "Writing tests"},
}
MODES = {"browse": {}, "review": {"review_actions": ["click"]}, "blocked": {"blocked_actions": ["click"]}}


def request_allowed(site: dict, url: str, method: str, *, navigation: bool) -> bool:
    """Constrain this test drive, independently of editable SDK policy decisions."""
    target, origin = urlsplit(url), urlsplit(site["url"])
    path = unquote(target.path).lower()
    versioned_asset = (not navigation and target.path.startswith(site["prefix"] + "_static/")
                       and target.path.endswith((".css", ".js")) and bool(re.fullmatch(r"v=[0-9a-fA-F]{1,64}", target.query)))
    return (method in {"GET", "HEAD"} and target.scheme == "https"
            and target.netloc == origin.netloc and not target.username and not target.password
            and (not target.query or versioned_asset)
            and not any(word in path for word in ("login", "signin", "sign-in", "signup", "account", "checkout", "payment", "logout"))
            and (not navigation or target.path.startswith(site["prefix"])))


PAGE = r"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Acira — public websites</title><style>
body{font:17px system-ui;background:#f4f6fa;color:#182337;max-width:860px;margin:36px auto;padding:0 22px}
section{background:white;padding:24px;margin:20px 0;border:1px solid #d7dfeb;border-radius:14px}
button,select,a{font:inherit}button,select{padding:10px 14px;margin:6px 5px 6px 0}
button{cursor:pointer}button:disabled{cursor:default}a{color:#174aba}pre{white-space:pre-wrap;overflow-wrap:anywhere}
:focus-visible{outline:3px solid #2864e5;outline-offset:3px}.hint{color:#465671;line-height:1.5}
</style><h1>Try Acira on real websites</h1>
<p>Watch Playwright open public documentation in Chromium. No accounts, purchases, form submissions or paid APIs.</p>
<section><label for="site">Choose a website</label><br><select id="site">__OPTIONS__</select>
<p class="hint">Each run starts in a fresh browser session. The link leads to another documentation page on the same website.</p>
<button data-mode="browse">Browse normally</button><button data-mode="review">Try a review</button><button data-mode="blocked">Try a blocked click</button>
<button id="cancel" disabled>Cancel run</button></section>
<section><h2>Live result</h2><pre id="status" role="status" aria-live="polite">Ready.</pre>
<p id="next" hidden>Chromium is waiting. Open <a href="/dashboard" target="_blank" rel="noopener">the Acira dashboard</a>,
select <strong>Approvals → Review operation</strong>, inspect the screenshot and highlighted link, then choose <strong>Approve once</strong> or <strong>Reject</strong>.</p>
<p><a href="/dashboard" target="_blank" rel="noopener">Open dashboard</a> · <a href="/guide">Step-by-step instructions</a></p></section>
<p class="hint">Use the Acira review buttons; manually clicking Chromium is outside SDK enforcement. These test policies deliberately review or block harmless links to make protection visible. They do not label documentation as dangerous.</p>
<script>
async function refresh(){try{const r=await fetch('/status',{cache:'no-store'});if(!r.ok)throw Error();const s=await r.json();
const message=s.message+'\n\n'+(s.url?'Browser URL: '+s.url+'\n':'')+(s.run?'Run: '+s.run+'\n':'')+'State: '+s.phase+'\nRead-only requests: '+s.read_requests+' | Requests stopped by test-drive restrictions: '+s.stopped_requests;
if(document.querySelector('#status').textContent!==message)document.querySelector('#status').textContent=message;
document.querySelectorAll('[data-mode],#site').forEach(e=>e.disabled=s.busy);document.querySelector('#cancel').disabled=!s.busy;document.querySelector('#next').hidden=s.phase!=='review';
}catch(e){document.querySelector('#status').textContent='Test drive unavailable. Check its terminal.';document.querySelectorAll('button').forEach(e=>e.disabled=true)}}
async function act(path,data){try{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});if(!r.ok)throw Error(await r.text());await refresh()}catch(e){alert(e.message)}}
document.querySelectorAll('[data-mode]').forEach(e=>e.onclick=()=>act('/run',{site:document.querySelector('#site').value,mode:e.dataset.mode}));
document.querySelector('#cancel').onclick=()=>act('/cancel',{});setInterval(refresh,1000);refresh();
</script></html>"""


class PublicDemo:
    def __init__(self, directory: Path | str | None = None):
        self.root = Path(directory).expanduser().resolve() if directory is not None else state_dir() / "public-websites-demo"
        self.projects = {}
        for key, site in SITES.items():
            root = self.root / key
            for mode in MODES:
                existing = (root / "acira.json").exists()
                existing_agent = existing and mode in Project(root).document()["agents"]
                project = initialize(root, agent=mode, general=f"policies/{mode}/general", specific=f"policies/{mode}/specific", selected=[])
                if project.snapshot(mode).profile["adapter"] != "playwright":
                    raise ValueError("Choose a separate public test-drive directory configured for Playwright")
                # Existing user policy edits are retained. Configure only new agents.
                document = project.document()
                if not existing_agent:
                    document["agents"][mode]["guard"] = {**MODES[mode], "terminal_log": False, "terminal_review": False}
                    if not existing:
                        document["name"] = "Public docs — " + site["name"]
                    atomic_json(project.path, document)
                    Registry().register(root, document["name"])
            self.projects[key] = project
        self.task = None
        self.lock = asyncio.Lock()
        self.base = ""
        self.state = {"message": "Ready.", "phase": "ready", "run": None, "url": None,
                      "read_requests": 0, "stopped_requests": 0, "link_requests": 0, "navigation_kind": None,
                      "outcome": None, "site": None, "mode": None}

    async def workflow(self, key: str, mode: str):
        site, project = SITES[key], self.projects[key]
        expected = urljoin(site["url"], site["href"])
        self.state.update(message="Opening visible Chromium…", phase="starting", site=key, mode=mode,
                          run=None, url=None, outcome=None, navigation_kind=None, read_requests=0, stopped_requests=0, link_requests=0)
        try:
            async with protect(project=project.root, agent=mode) as guard:
                self.state["run"] = guard.runtime_control.run_id
                async with async_playwright() as p:
                    guard.patch_playwright(p)
                    browser = await p.chromium.launch(headless=False, slow_mo=150)
                    try:
                        context = await browser.new_context(viewport={"width": 1100, "height": 800}, permissions=[], accept_downloads=False, service_workers="block")

                        async def read_only(route):
                            request = route.request
                            allowed = request_allowed(site, request.url, request.method, navigation=request.is_navigation_request())
                            self.state["read_requests" if allowed else "stopped_requests"] += 1
                            if allowed and request.is_navigation_request() and request.url == expected:
                                self.state["link_requests"] += 1
                            if allowed:
                                await route.fallback()
                            else:
                                await route.abort("blockedbyclient")

                        async def block_socket(socket):
                            await socket.close()

                        await context.route("**/*", read_only)
                        await context.route_web_socket("**/*", block_socket)
                        page = await context.new_page()
                        page.set_default_timeout(20000)
                        destination_responses = []
                        page.on("response", lambda response: destination_responses.append(response)
                                if response.url == expected and response.request.is_navigation_request() else None)
                        response = await page.goto(site["url"], wait_until="domcontentloaded", timeout=45000)
                        if response is None or not response.ok:
                            raise RuntimeError("The documentation page returned an unsuccessful response")
                        initial = page.url
                        self.state["url"] = initial
                        link = page.locator('a[href="' + site["href"] + '"]:visible').first
                        await link.wait_for(state="visible")
                        destination = urljoin(initial, await link.get_attribute("href"))
                        if destination != expected or not request_allowed(site, destination, "GET", navigation=True):
                            raise ValueError("The documentation link changed. No click was attempted.")
                        self.state.update(phase="review" if mode == "review" else "running",
                                          message="Waiting for your decision in Acira → Approvals." if mode == "review" else "Checking policy and following the documentation link…")
                        try:
                            await link.click()
                            await page.wait_for_url(expected, wait_until="domcontentloaded", timeout=20000)
                            if destination_responses and not destination_responses[-1].ok:
                                raise RuntimeError("The documentation destination returned an unsuccessful response")
                            heading = page.get_by_role("heading", name=site["heading"], level=1).first
                            await heading.wait_for(state="visible")
                            self.state.update(outcome="navigated", navigation_kind="document" if destination_responses else "client",
                                              message="NAVIGATION VERIFIED: Chromium reached the expected documentation URL and page heading.")
                        except AciraActionBlocked:
                            review = next((r for r in Journal(project.root).reviews() if r["run_id"] == self.state["run"]), None)
                            outcome = review["state"] if review else "policy_block"
                            if page.url != initial or self.state["link_requests"]:
                                raise RuntimeError("The page changed unexpectedly; no successful block is claimed.")
                            self.state.update(outcome=outcome, message=outcome.upper().replace("_", " ") + ": Acira stopped the click. Chromium remained on the original page.")
                        self.state.update(phase="result", url=page.url)
                        self.state["message"] += "\nBrowser closes in five seconds; the result stays here and in Activity."
                        await asyncio.sleep(5)
                    finally:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(browser.close(), 10)
            self.state["phase"] = "ended"
            self.state["message"] += "\nRUN ENDED. Choose another test."
        except asyncio.CancelledError:
            self.state.update(phase="ended", outcome="cancelled", message="RUN CANCELLED. The pending review was invalidated; no cancelled click will be retried.")
            raise
        except Exception as error:
            self.state.update(phase="error", outcome="error", message="Test could not complete: " + str(error) + "\nNo success is claimed. Check connectivity or use another documentation site.")

    async def cancel(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.wait_for(asyncio.gather(self.task, return_exceptions=True), 20)

    def app(self):
        @web.middleware
        async def local_boundary(request, handler):
            if request.host != self.base.removeprefix("http://"):
                raise web.HTTPForbidden(text="Use the printed local test-drive address.")
            if request.method == "POST":
                if request.headers.get("Origin") not in {None, self.base}:
                    raise web.HTTPForbidden(text="Cross-origin requests are not allowed.")
                if request.content_type != "application/json":
                    raise web.HTTPUnsupportedMediaType()
            return await handler(request)

        async def index(request):
            options = "".join('<option value="' + key + '">' + escape(site["name"]) + '</option>' for key, site in SITES.items())
            return web.Response(text=PAGE.replace("__OPTIONS__", options), content_type="text/html")

        async def dashboard(request):
            raise web.HTTPFound(open_dashboard(launch_browser=False))

        async def status(request):
            return web.json_response({**self.state, "busy": self.task is not None and not self.task.done()}, headers={"Cache-Control": "no-store"})

        async def start(request):
            data = await request.json()
            if (not isinstance(data, dict) or not isinstance(data.get("site"), str)
                    or not isinstance(data.get("mode"), str) or data["site"] not in SITES or data["mode"] not in MODES):
                raise web.HTTPBadRequest(text="Choose a displayed website and test.")
            async with self.lock:
                if self.task is not None and not self.task.done():
                    raise web.HTTPConflict(text="Resolve or cancel the current run first.")
                self.task = asyncio.create_task(self.workflow(data["site"], data["mode"]))
            return web.json_response({"started": True})

        async def cancel(request):
            async with self.lock:
                await self.cancel()
            return web.json_response({"cancelled": True})

        async def guide(request):
            return web.Response(text="""<!doctype html><html lang="en"><meta charset="utf-8"><title>Acira public test instructions</title>
<main style="font:18px system-ui;max-width:720px;margin:40px auto;padding:20px"><h1>Your first public website test</h1><ol>
<li>Return to the <a href="/">test drive</a> and choose Python, MDN or Playwright documentation.</li>
<li>Choose <strong>Browse normally</strong>. Watch Chromium open the real website and follow a documentation link.</li>
<li>Choose <strong>Try a review</strong>. Chromium stops before clicking. Open <a href="/dashboard">Acira</a>, select Approvals, then Review operation for the matching Public docs project and review agent.</li>
<li>Inspect the screenshot and highlighted link. Approve once follows the link; Reject leaves the original page unchanged.</li>
<li>Choose <strong>Try a blocked click</strong>. Acira stops the same harmless link using this agent's test policy.</li>
<li>The run ends automatically after the result. Inspect Agents and Activity in the dashboard, or Cancel run while waiting.</li></ol>
<p>These are scripted Playwright workflows through the real Acira SDK. They require no model, account or paid API. Other sites are intentionally unavailable in this test drive. Existing local/browser-use demos remain available.</p></main></html>""", content_type="text/html")

        async def cleanup(app):
            await self.cancel()

        app = web.Application(middlewares=[local_boundary], client_max_size=4096)
        app.add_routes([web.get("/", index), web.get("/guide", guide), web.get("/dashboard", dashboard),
                        web.get("/status", status), web.post("/run", start), web.post("/cancel", cancel)])
        app.on_cleanup.append(cleanup)
        return app


async def main(auto_open=True, port=0, directory=None, **kwargs):
    demo = PublicDemo(directory)
    runner = web.AppRunner(demo.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    demo.base = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
    try:
        dashboard = open_dashboard(launch_browser=auto_open)
        atomic_json(demo.root / ".acira/demo-links.json", {"playground": demo.base, "dashboard": dashboard}, private=True)
        print("Acira public-websites test drive:", demo.base, flush=True)
        print("Read-only public documentation. Visible Playwright; no accounts or paid APIs.", flush=True)
        if auto_open:
            webbrowser.open(demo.base)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
