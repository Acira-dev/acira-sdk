"""Visible, free browser examples using the installed Acira SDK.

All effects use fake values and a loopback test server. Playwright is scripted;
the optional browser-use path plans with a downloaded local model. No paid API is used.
"""
import asyncio
import contextlib
import webbrowser
import os
from html import escape
from pathlib import Path

ROOT: Path | None = None
RESEARCH_ROOT: Path | None = None

from aiohttp import web
from playwright.async_api import async_playwright
from acira import protect
from acira.journal import Journal
from acira.local import Registry, atomic_json, state_dir
from acira.project import Project, initialize
from acira.service import open_dashboard
from acira_cdp_proxy.action_gateway import AciraActionBlocked

VALUES = {"amount": "10.00", "currency": "USD", "account": "test-account", "recipient": "Acira test shop"}
DRAFT = {"note": "Acira local test note."}
COUNTS = {"payment_requests": 0, "delete_requests": 0, "draft_requests": 0}
RUNS = {name: {"status": "Ready.", "run": None, "phase": "ready", "kind": None,
              "outcome": None, "keep_active": False, "run_state": "not_started"}
        for name in ("checkout", "research")}
TASKS = {}
RELEASES = {}
RUN_LOCKS = {name: asyncio.Lock() for name in RUNS}
BASE = None
ADAPTER = "playwright"
MODEL = "qwen3:4b-instruct"
OLLAMA_HOST = "http://127.0.0.1:11434"

STYLE = """
*{box-sizing:border-box}body{font:16px system-ui;line-height:1.55;margin:0;color:#18223b;background:#f3f6fc}
main{max-width:1080px;margin:38px auto;padding:0 24px}h1{font-size:32px;letter-spacing:-.8px;margin:8px 0}
h2{font-size:22px;margin:8px 0}p{margin:10px 0}.eyebrow{color:#4a5acd;font-size:13px;font-weight:750;letter-spacing:1px}
.intro,.card,.guide{background:#fff;border:1px solid #dce3f1;border-radius:15px;padding:24px;margin:18px 0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.grid .card{margin:0}.tag{display:inline-block;background:#eef1ff;color:#3749bb;padding:3px 9px;border-radius:6px;font-size:12px}
button{padding:11px 15px;margin:6px 6px 6px 0;border:0;border-radius:8px;background:#324bd7;color:white;font:inherit;font-weight:600;cursor:pointer}
button.secondary{background:#e9edf6;color:#273653}button.danger{background:#982f49}button:disabled{opacity:.45;cursor:default}
button:focus-visible,a:focus-visible{outline:3px solid #9b60df;outline-offset:3px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f4fb;padding:15px;border-radius:9px;font:14px/1.6 ui-monospace,monospace}
code{background:#eef2f9;padding:2px 5px;border-radius:4px}label{display:block;margin:14px 0}input,textarea{font:inherit;padding:10px;border:1px solid #c5cee2;border-radius:7px;width:100%}
.checkbox{display:flex;align-items:flex-start;gap:9px;font-size:14px;color:#53627e}.checkbox input{width:auto;margin-top:5px}
.counts{display:flex;gap:24px;flex-wrap:wrap;background:#edf3ff;padding:16px;border-radius:10px}.counts strong{font-size:25px;display:block}
.hint,small{color:#53627e}.success{color:#216444}a{color:#314bd9}li{margin:7px 0}#outcome{font-weight:650}
.run-feedback{background:#eef2ff;border:2px solid #6477dc;border-radius:12px;padding:16px;margin:20px 0}.run-feedback h2{font-size:20px}.run-feedback pre{margin:10px 0}.review-link{display:inline-block;font-weight:700;margin:8px 0}.review-next{background:#fff1cd;border:1px solid #d8ad41;border-radius:9px;padding:14px;margin:14px 0}.review-next[hidden]{display:none}
@media(max-width:750px){.grid{grid-template-columns:1fr}main{padding:0 16px}h1{font-size:27px}}
"""
PLAYGROUND = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Acira · Playwright test drive</title><style>""" + STYLE + r"""</style></head><body><main>
<section class="intro"><div class="eyebrow">ACIRA / LOCAL TEST DRIVE</div><h1>Watch the browser. Make the decision.</h1>
<p>The installed Acira SDK protects two real <strong>Playwright / Chromium</strong> workflows. Chromium opens as a visible window. Your decision and the execution result update here automatically.</p>
<p><strong>Free. No API key. Fake local data only.</strong> This is a repeatable browser workflow, not an AI model demonstration.</p>
<ol><li>Start an action below and watch the separate Chromium window.</li><li>For a review, switch to <a href="__DASHBOARD__" target="_blank" rel="noopener">the Acira dashboard</a> → <strong>Approvals → Review operation</strong>.</li><li>Inspect the screenshot, highlight and values. Choose <strong>Approve once</strong> or <strong>Reject</strong>.</li><li>Return here: the server counters show whether the action actually happened.</li></ol></section>
<div class="grid"><section class="card"><span class="tag">PROJECT 1 · CHECKOUT</span><h2>Payment and destructive actions</h2>
<p>Payment asks for review. The bundled destructive policy blocks deletion.</p>
<label class="checkbox"><input type="checkbox" id="checkout-keep">Keep this run active after its result for policy testing</label>
<button data-project="checkout" data-kind="payment">Start payment review</button><button class="danger" data-project="checkout" data-kind="delete">Try blocked delete</button>
<pre id="checkout-status" role="status" aria-live="polite" aria-atomic="true">Ready.</pre><div class="review-next" id="checkout-next" hidden><strong>Next step: your approval is needed.</strong><br>Chromium is waiting before it clicks Pay. <a href="__DASHBOARD__" target="_blank" rel="noopener">Open dashboard</a> → <strong>Approvals → Review operation</strong> → <strong>Approve once</strong> or <strong>Reject</strong>.<br><small>Cancel run stops the pending action. Resolved runs finish automatically.</small></div><button class="secondary" data-finish="checkout">Cancel checkout run</button></section>
<section class="card"><span class="tag">PROJECT 2 · RESEARCH</span><h2>Write and save a draft</h2>
<p>Watch Playwright type a test note. This project's separate configuration requires review before saving it.</p>
<label class="checkbox"><input type="checkbox" id="research-keep">Keep this run active after its result for policy testing</label>
<button data-project="research" data-kind="draft">Start draft review</button>
<pre id="research-status" role="status" aria-live="polite" aria-atomic="true">Ready.</pre><div class="review-next" id="research-next" hidden><strong>Next step: review the draft.</strong><br><a href="__DASHBOARD__" target="_blank" rel="noopener">Open dashboard</a> → <strong>Approvals → Review operation</strong>. Cancel run stops its pending save.</div><button class="secondary" data-finish="research">Cancel research run</button></section></div>
<section class="card"><h2>Observed local effects</h2><p class="hint">These counters increase only when a matching request reaches this test server. Approval alone does not increase them.</p>
<div class="counts"><div><strong id="payments">0</strong>Fake payment requests</div><div><strong id="deletes">0</strong>Fake delete requests</div><div><strong id="drafts">0</strong>Draft saves</div></div>
<p id="notice" role="status"></p></section>
<section class="guide"><h2>Then try the rest of Acira</h2><ul>
<li><strong>Agents:</strong> start both projects to see their independent runs in one dashboard.</li>
<li><strong>Policies:</strong> select “Keep this run active” before starting if you want to test live policy updates. Edit a policy and save. Watch its run report the new active revision. A change invalidates an open review; start a fresh attempt.</li>
<li><strong>Activity:</strong> select the run and inspect the decision and browser outcome.</li>
<li><strong>Backtesting:</strong> evaluate recorded actions or compare policy revisions.</li>
<li><strong>Settings:</strong> pair the capture extension or inspect project locations and diagnostics.</li></ul>
<p class="hint">Each review expires after five minutes. After the result, normal runs finish automatically and Chromium closes after three seconds. The optional policy-testing mode keeps a run active for up to ten minutes. Stopping the dashboard does not disable SDK enforcement.</p>
<p><a href="/guide" target="_blank" rel="noopener">Open the setup and testing guide</a></p></section></main>
<script>
let refreshing=false;
function text(node,value){if(node.textContent!==value)node.textContent=value;}
async function refresh(){
 if(refreshing)return;refreshing=true;
 try{const s=await(await fetch('/status')).json();
  for(const [name,r] of Object.entries(s.runs)){
   text(document.querySelector('#'+name+'-status'),r.status+'\n\nRun: '+(r.run||'—')+'\nState: '+r.phase+' · SDK run: '+r.run_state+(r.model_calls!==undefined?'\nLocal model replies: '+(r.model_responses||0)+' / '+r.model_calls+' requests':''));
   document.querySelector('#'+name+'-next').hidden=r.phase!=='review'||r.decision&&r.decision!=='pending';
   document.querySelectorAll('[data-project="'+name+'"]').forEach(b=>b.disabled=r.busy&&!['result','error'].includes(r.phase));
   const finish=document.querySelector('[data-finish="'+name+'"]');finish.disabled=!r.busy;
   text(finish,(r.phase==='result'&&r.keep_active?'End active ':'Cancel ')+name+' run');
   document.querySelector('#'+name+'-keep').disabled=r.busy;
  }
  text(document.querySelector('#payments'),String(s.payment_requests));text(document.querySelector('#deletes'),String(s.delete_requests));text(document.querySelector('#drafts'),String(s.draft_requests));
 }catch(e){text(document.querySelector('#notice'),'Test server unavailable. Restart start.sh to continue.');}finally{refreshing=false;}}
async function send(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw Error(await r.text());await refresh();}
document.querySelectorAll('[data-kind]').forEach(b=>b.onclick=async()=>{try{await send('/run',{project:b.dataset.project,kind:b.dataset.kind,keep_active:document.querySelector('#'+b.dataset.project+'-keep').checked});document.querySelector('#notice').textContent='Chromium opens in a separate window. Decisions and results update automatically.';}catch(e){document.querySelector('#notice').textContent=e.message;}});
document.querySelectorAll('[data-finish]').forEach(b=>b.onclick=async()=>{try{await send('/finish',{project:b.dataset.finish});}catch(e){document.querySelector('#notice').textContent=e.message;}});
setInterval(refresh,1000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});window.addEventListener('focus',refresh);refresh();
</script></body></html>"""

CHECKOUT = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Acira test · Checkout payment</title><style>""" + STYLE + """</style></head><body><main><section class="card">
<div class="eyebrow">PLAYWRIGHT / PROJECT 1</div><h1>Checkout payment</h1><p><strong>Local test. No real payment or deletion.</strong></p>
<section class="run-feedback" aria-label="Automated workflow"><h2 id="workflow-title">Preparing the automated workflow</h2><pre id="outcome" aria-live="polite">Opening checkout…</pre><a class="review-link" href="__DASHBOARD__" target="_blank" rel="noopener">Open Acira dashboard</a><p id="next-step">Acira checks the requested action before Playwright clicks it.</p></section><p id="received" class="success" aria-live="polite"></p><form>
<label>Amount <input name="amount" value="10.00" readonly></label><label>Currency <input name="currency" value="USD" readonly></label>
<label>Account <input name="account" value="test-account" readonly></label><label>Recipient <input name="recipient" value="Acira test shop" readonly></label>
<button type="button" id="pay" onclick="sendPayment()">Pay $10.00</button></form>
<section><h2>Account records</h2><button id="delete" type="button" class="danger" onclick="sendDelete()">Delete all records permanently</button></section>
<p class="hint">The automated workflow waits for Acira. Use the dashboard to decide; manually clicking page controls is outside SDK enforcement.</p>
</section></main><script>
async function sendPayment(){const r=await fetch('/payment',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({amount:'10.00',currency:'USD',account:'test-account',recipient:'Acira test shop'})});if(r.ok)document.querySelector('#received').textContent='Test server received the exact payment request. No real money moved.';}
async function sendDelete(){const r=await fetch('/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(r.ok)document.querySelector('#received').textContent='Test server received a fake delete request.';}
async function refreshRun(){try{const s=await(await fetch('/status')).json(),r=s.runs.checkout;document.querySelector('#outcome').textContent=r.status;document.querySelector('#workflow-title').textContent=r.phase==='review'?'Waiting for your approval':r.phase==='finished'?'Run finished':r.phase==='result'?'Action resolved':'Automated workflow';document.querySelector('#next-step').textContent=r.phase==='review'?'Open the dashboard → Approvals → Review operation. Choose Approve once or Reject. Waiting here does not approve the payment.':r.phase==='finished'?'Return to the test drive to start a fresh action. Finish run cancels a pending review.':r.phase==='result'?'Check the result and the test-drive counters.':'Acira checks the requested action before Playwright clicks it.';}catch(e){document.querySelector('#outcome').textContent='Test server disconnected. Return to the test drive.';}}setInterval(refreshRun,700);refreshRun();
</script></body></html>"""

RESEARCH = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Acira test · Research draft</title><style>""" + STYLE + """</style></head><body><main><section class="card">
<div class="eyebrow">PLAYWRIGHT / PROJECT 2</div><h1>Research draft</h1><p>Playwright writes a fake note. Acira asks before this project's save action.</p>
<section class="run-feedback" aria-label="Automated workflow"><h2 id="workflow-title">Preparing the automated workflow</h2><pre id="outcome" aria-live="polite">Opening the research page…</pre><a class="review-link" href="__DASHBOARD__" target="_blank" rel="noopener">Open Acira dashboard</a><p id="next-step">Playwright types a note, then Acira waits for your review before saving.</p></section><p id="received" class="success" aria-live="polite"></p><form>
<label>Note <textarea name="note" id="note" rows="4"></textarea></label>
<button type="button" id="save" onclick="saveDraft()">Save draft</button></form>
<p class="hint">Use Acira Approvals to decide. Manually clicking page controls is outside SDK enforcement.</p>
</section></main><script>
async function saveDraft(){const r=await fetch('/draft',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note:document.querySelector('#note').value})});if(r.ok)document.querySelector('#received').textContent='Test server received the reviewed draft.';}
async function refreshRun(){try{const s=await(await fetch('/status')).json(),r=s.runs.research;document.querySelector('#outcome').textContent=r.status;document.querySelector('#workflow-title').textContent=r.phase==='review'?'Waiting for your approval':r.phase==='finished'?'Run finished':r.phase==='result'?'Action resolved':'Automated workflow';document.querySelector('#next-step').textContent=r.phase==='review'?'Open the dashboard → Approvals → Review operation. Choose Approve once or Reject to resolve the draft save.':r.phase==='finished'?'Return to the test drive to start a fresh action.':r.phase==='result'?'Check the result and the test-drive counters.':'Playwright types a note, then Acira waits for your review before saving.';}catch(e){document.querySelector('#outcome').textContent='Test server disconnected. Return to the test drive.';}}setInterval(refreshRun,700);refreshRun();
</script></body></html>"""

def setup_checkout():
    if (ROOT / "acira.json").exists():
        if Project(ROOT).snapshot().profile["adapter"] != ADAPTER:
            raise ValueError("Choose a separate demo directory configured for " + ADAPTER)
        return
    project = initialize(ROOT, adapter=ADAPTER, selected=["payment", "credentials", "destructive-actions", "downloads"])
    document = project.document()
    document["name"] = "Acira Playground" if ADAPTER == "playwright" else "Acira Browser-use Playground"
    atomic_json(project.path, document)
    Registry().register(project.root, document["name"])


def setup_research():
    if (RESEARCH_ROOT / "acira.json").exists():
        if Project(RESEARCH_ROOT).snapshot().profile["adapter"] != ADAPTER:
            raise ValueError("Choose a separate research demo directory configured for " + ADAPTER)
        return
    project = initialize(RESEARCH_ROOT, adapter=ADAPTER, selected=["destructive-actions"])
    document = project.document()
    document["name"] = "Acira Research Playground" if ADAPTER == "playwright" else "Acira Browser-use Research"
    document["agents"]["default"]["guard"] = {"review_actions": ["click"], "terminal_log": False}
    atomic_json(project.path, document)
    Registry().register(project.root, document["name"])

async def wait_for_effect(name, before):
    # The SDK dispatch and the application's request handler complete asynchronously.
    for _ in range(40):
        if COUNTS[name] > before:
            return COUNTS[name] == before + 1
        await asyncio.sleep(.05)
    return False

async def workflow(project_name, kind, *, keep_active=False):
    state = RUNS[project_name]
    state.update(status="Opening visible Chromium…", run=None, phase="starting", kind=kind,
                 keep_active=keep_active, outcome=None, run_state="starting")
    root = ROOT if project_name == "checkout" else RESEARCH_ROOT
    try:
        async with protect(project=root) as guard:
            state["run"] = guard.runtime_control.run_id
            state["run_state"] = "running"
            async with async_playwright() as p:
                guard.patch_playwright(p)
                browser = await p.chromium.launch(headless=False, slow_mo=200)
                try:
                    page = await browser.new_page(viewport={"width": 1000, "height": 800})
                    await page.goto(BASE + ("/checkout" if project_name == "checkout" else "/research"))
                    await asyncio.sleep(1)
                    try:
                        if kind == "payment":
                            state.update(status="Waiting for review: Acira dashboard → Approvals → Review operation.", phase="review")
                            await asyncio.sleep(1)
                            before = COUNTS["payment_requests"]
                            await guard.submit(page.locator("#pay"), destination=BASE + "/payment", values=VALUES,
                                               facts={name: name for name in VALUES})
                            received = await wait_for_effect("payment_requests", before)
                            state.update(status=("APPROVED AND VERIFIED: exactly one matching fake payment request reached the test server."
                                if received else "OUTCOME UNKNOWN: no confirmed matching request. The SDK will not retry."), phase="result", outcome="completed" if received else "unknown")
                        elif kind == "draft":
                            state.update(status="Watch Chromium: Playwright is typing the test note…", phase="preparing")
                            await page.locator("#note").fill(DRAFT["note"])
                            await asyncio.sleep(1)
                            state.update(status="Draft typed. Waiting for review in Acira → Approvals.", phase="review")
                            await asyncio.sleep(1)
                            before = COUNTS["draft_requests"]
                            await guard.submit(page.locator("#save"), destination=BASE + "/draft", values=DRAFT, facts={"note": "note"})
                            received = await wait_for_effect("draft_requests", before)
                            state.update(status=("APPROVED AND VERIFIED: exactly one reviewed draft reached the test server."
                                if received else "OUTCOME UNKNOWN: no confirmed draft save. It will not be retried."), phase="result", outcome="completed" if received else "unknown")
                        else:
                            state.update(status="Checking the destructive-action policy…", phase="preparing")
                            before = COUNTS["delete_requests"]
                            await page.locator("#delete").click()
                            received = await wait_for_effect("delete_requests", before)
                            state.update(status=("CURRENT POLICY ALLOWED the fake delete. Inspect Activity and your policy configuration."
                                if received else "OUTCOME UNKNOWN: the browser call returned, but no fake delete was observed."), phase="result", outcome="completed" if received else "unknown")
                    except AciraActionBlocked as error:
                        reviews = [item for item in Journal(root).reviews() if item["run_id"] == state["run"]]
                        outcome = reviews[0]["state"] if reviews else "policy_block"
                        if guard.runtime_control.last_operation and guard.runtime_control.last_operation.get("state") == "outcome unknown":
                            state.update(status="OUTCOME UNKNOWN. An attempt was dispatched; it will not be retried. Inspect Activity before another attempt.", phase="result", outcome="unknown")
                        else:
                            labels = {"rejected": "REJECTED", "expired": "REVIEW EXPIRED", "invalidated": "REVIEW INVALIDATED", "policy_block": "BLOCKED BY POLICY", "approved": "BLOCKED AFTER REVALIDATION"}
                            state.update(status=labels.get(outcome, "BLOCKED") + ": Acira stopped the automated action. Inspect Activity for the decision and reason.", phase="result", outcome="invalidated" if outcome == "approved" else outcome)
                    state["status"] += ("\n\nAction resolved. Run kept active for policy testing; use End active run when done."
                                         if keep_active else "\n\nAction resolved. The run is finishing automatically…")
                    # Normal one-operation demos end automatically. Keeping a run
                    # alive for policy activation is an explicit, bounded choice.
                    deadline = asyncio.get_running_loop().time() + (600 if keep_active else 3)
                    while browser.is_connected() and not RELEASES[project_name].is_set() and asyncio.get_running_loop().time() < deadline:
                        try:
                            await asyncio.wait_for(RELEASES[project_name].wait(), 1)
                        except asyncio.TimeoutError:
                            pass
                finally:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(browser.close(), 10)
        state["phase"] = "finished"
        state["run_state"] = "ended"
        state["status"] = state["status"].split("\n\n", 1)[0] + "\n\nRUN ENDED. Ready for another action."
    except asyncio.CancelledError:
        state.update(status="RUN CANCELLED. Any pending review was invalidated; no cancelled action was retried.", phase="finished", outcome="cancelled", run_state="ended")
        raise
    except Exception as error:
        state.update(status="Demo error: " + str(error), phase="error", outcome="error", run_state="ended")

@web.middleware
async def local_boundary(request, handler):
    if request.host != BASE.removeprefix("http://"):
        raise web.HTTPForbidden(text="Use the local playground address.")
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin != BASE:
            raise web.HTTPForbidden(text="Cross-origin demo requests are not allowed.")
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType()
    return await handler(request)

def render_page(source, dashboard_url):
    source = source.replace("Test server disconnected. Return to the test drive.",
        "Status temporarily unavailable. Check the test drive for the current outcome.")
    if ADAPTER == "browser_use":
        source = source.replace("Playwright", "browser-use").replace("PLAYWRIGHT", "BROWSER-USE")
        source = source.replace("This is a repeatable browser workflow, not an AI model demonstration.",
            "An AI agent plans with " + escape(MODEL) + " running locally on this computer.")
    return source.replace("__DASHBOARD__", dashboard_url)

async def index(request):
    from acira.service import verified_descriptor
    descriptor = await asyncio.to_thread(verified_descriptor)
    dashboard_url = descriptor["url"] if descriptor else "#"
    return web.Response(text=render_page(PLAYGROUND, dashboard_url), content_type="text/html")

async def checkout(request):
    from acira.service import verified_descriptor
    descriptor = await asyncio.to_thread(verified_descriptor)
    return web.Response(text=render_page(CHECKOUT, descriptor["url"] if descriptor else "#"), content_type="text/html")

async def research(request):
    from acira.service import verified_descriptor
    descriptor = await asyncio.to_thread(verified_descriptor)
    return web.Response(text=render_page(RESEARCH, descriptor["url"] if descriptor else "#"), content_type="text/html")

async def guide(request):
    # Serve documentation as text, with no project credentials or executable markup.
    document = GUIDE
    if ADAPTER == "browser_use":
        from .browser_use_demo import GUIDE as document
    return web.Response(text=document, content_type="text/plain")

async def status(request):
    runs = {}
    for name, state in RUNS.items():
        item = {**state, "busy": name in TASKS and not TASKS[name].done()}
        if item["run"]:
            root = ROOT if name == "checkout" else RESEARCH_ROOT
            journal = Journal(root)
            reviews = [r for r in journal.reviews() if r["run_id"] == item["run"]]
            item["decision"] = reviews[0]["state"] if reviews else None
            if item["phase"] == "review" and item["decision"] not in {None, "pending"}:
                item["phase"] = "executing" if item["decision"] == "approved" else "resolving"
                item["status"] = ("APPROVAL RECORDED. Acira is rechecking and executing the operation…"
                                  if item["decision"] == "approved" else "REVIEW " + item["decision"].upper() + ". Acira is stopping the operation…")
            current = next((r for r in journal.runs() if r["run_id"] == item["run"]), None)
            if current:
                item["run_state"] = current["state"]
        runs[name] = item
    return web.json_response({**COUNTS, "runs": runs}, headers={"Cache-Control": "no-store"})

async def finish_task(name):
    task = TASKS.get(name)
    if task is None or task.done():
        return
    RELEASES[name].set()
    if RUNS[name]["phase"] not in {"result", "error"}:
        task.cancel()
    try:
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)
    except asyncio.TimeoutError:
        raise web.HTTPServiceUnavailable(text="Browser cleanup is still running. Check the terminal before starting another run.")

async def start(request):
    data = await request.json()
    name, kind = data.get("project"), data.get("kind")
    keep_active = data.get("keep_active", False)
    if not isinstance(keep_active, bool):
        raise web.HTTPBadRequest(text="Keep active must be true or false.")
    if (name, kind) not in {("checkout", "payment"), ("checkout", "delete"), ("research", "draft")}:
        raise web.HTTPBadRequest(text="Choose one of the displayed actions.")
    async with RUN_LOCKS[name]:
        if name in TASKS and not TASKS[name].done():
            if RUNS[name]["phase"] not in {"result", "error"}:
                raise web.HTTPConflict(text="Resolve this project's current review or finish its run first.")
            await finish_task(name)
        RELEASES[name] = asyncio.Event()
        RUNS[name].update(status="Starting…", phase="starting", kind=kind, run=None,
                          outcome=None, keep_active=keep_active, run_state="starting")
        handler = workflow
        if ADAPTER == "browser_use":
            from .browser_use_demo import workflow as handler
        TASKS[name] = asyncio.create_task(handler(name, kind, keep_active=keep_active))
    return web.json_response({"started": True})

async def finish(request):
    name = (await request.json()).get("project")
    if name not in RUNS:
        raise web.HTTPBadRequest(text="Unknown demo project.")
    async with RUN_LOCKS[name]:
        await finish_task(name)
    return web.json_response({"finished": True})

async def payment(request):
    if await request.json() != VALUES:
        raise web.HTTPBadRequest(text="Unexpected fake payment values.")
    COUNTS["payment_requests"] += 1
    return web.json_response({"local_test_received": True})

async def delete(request):
    await request.json()
    COUNTS["delete_requests"] += 1
    return web.json_response({"local_test_received": True})

async def draft(request):
    if await request.json() != DRAFT:
        raise web.HTTPBadRequest(text="Unexpected test note.")
    COUNTS["draft_requests"] += 1
    return web.json_response({"local_test_received": True})

GUIDE = """Acira: free Playwright test drive

1. Start payment review. Chromium opens and waits before clicking Pay.
2. In Acira's dashboard, choose Approvals > Review operation.
3. Check the fake 10.00 USD, test-account, Acira test shop and local destination.
4. Approve once or Reject. Both the test drive and Agents update automatically.
5. Approved requests increase the corresponding counter exactly once. Rejected
   or blocked actions leave the counter unchanged. Cancel run stops a pending action.

A normal demo run finishes automatically after the result; Chromium closes after
three seconds. Select Keep this run active BEFORE starting to test live policy
changes for up to ten minutes. End active run stops that explicit testing mode.

Start both projects to test independent policies and reviews in one dashboard.
Policies supports import, edit, assignment, Saved/Active, revision and rollback.
Activity records decisions and browser outcomes. Backtesting compares revisions.
Settings contains extension pairing, secret references, retention and diagnostics.
Manual human clicks on Chromium controls are outside the SDK's automation boundary.

For your own project: install acira[playwright], install Chromium with
python -m playwright install chromium, then acira init and acira doctor. Wrap your
existing async workflow in protect(), call guard.patch_playwright(p), and continue
using Playwright normally. acira open reuses the same per-user local dashboard.
No API key, paid model or cloud account is needed for this test drive.
Ctrl-C stops the demo. acira server stop stops the dashboard separately.
"""


def create_app(directory: Path | str | None = None, *, adapter="playwright", model="qwen3:4b-instruct", ollama_host="http://127.0.0.1:11434"):
    global ROOT, RESEARCH_ROOT, RUNS, TASKS, RELEASES, RUN_LOCKS, ADAPTER, MODEL, OLLAMA_HOST
    if any(not task.done() for task in TASKS.values()):
        raise RuntimeError("Finish the current demo before starting another in this process")
    if adapter not in {"playwright", "browser_use"}:
        raise ValueError("Choose playwright or browser_use")
    ADAPTER, MODEL, OLLAMA_HOST = adapter, model, ollama_host
    ROOT = Path(directory).expanduser().resolve() if directory is not None else state_dir() / ("playwright-demo" if adapter == "playwright" else "browser-use-demo")
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    RESEARCH_ROOT = ROOT / "research-project"
    COUNTS.update(payment_requests=0, delete_requests=0, draft_requests=0)
    RUNS = {name: {"status": "Ready.", "run": None, "phase": "ready", "kind": None,
                  "outcome": None, "keep_active": False, "run_state": "not_started"}
            for name in ("checkout", "research")}
    TASKS, RELEASES = {}, {}
    RUN_LOCKS = {name: asyncio.Lock() for name in RUNS}
    setup_checkout()
    setup_research()
    app = web.Application(middlewares=[local_boundary], client_max_size=4096)
    app.add_routes([web.get("/", index), web.get("/checkout", checkout), web.get("/research", research),
                    web.get("/guide", guide), web.get("/status", status), web.post("/run", start),
                    web.post("/finish", finish), web.post("/payment", payment), web.post("/delete", delete), web.post("/draft", draft)])
    app.on_cleanup.append(cleanup_tasks)
    return app


async def cleanup_tasks(app):
    for task in TASKS.values():
        if not task.done():
            task.cancel()
    if TASKS:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.gather(*TASKS.values(), return_exceptions=True), 20)


async def main(auto_open=True, port=0, directory=None, *, adapter="playwright", model="qwen3:4b-instruct", ollama_host="http://127.0.0.1:11434"):
    global BASE
    if adapter == "browser_use":
        os.environ["ANONYMIZED_TELEMETRY"] = "false"
        os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"
        model_root = Path(directory).expanduser().resolve() if directory is not None else state_dir() / "browser-use-demo"
        os.environ.setdefault("BROWSER_USE_CONFIG_DIR", str(model_root / ".acira/browser-use"))
        from .browser_use_demo import check_local_model
        await check_local_model(ollama_host, model)
    app = create_app(directory, adapter=adapter, model=model, ollama_host=ollama_host)
    if adapter == "browser_use":
        os.environ.setdefault("BROWSER_USE_CONFIG_DIR", str(ROOT / ".acira/browser-use"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    BASE = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
    try:
        dashboard = open_dashboard(launch_browser=auto_open)
        atomic_json(ROOT / ".acira/demo-links.json", {"playground": BASE, "dashboard": dashboard}, private=True)
        print("Acira " + ("Playwright" if adapter == "playwright" else "browser-use") + " test drive:", BASE, flush=True)
        print("Free local browser automation. No API keys. Start an action to open visible Chromium.", flush=True)
        if auto_open:
            webbrowser.open(BASE)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
