"""Opt-in signed-out capture-extension/CLI/specific-policy compatibility campaign.

Uses real exported fingerprints, not generic blocked_actions/review_actions.
The independent read-only fence and robots rules remain in place for every case.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

import public_site_sweep as safe
from acira import protect
from acira.extension import install_extension
from acira.journal import Journal
from acira.project import initialize


class CaptureReviewer(safe.TestReviewer):
    """Simulated reviewer for one captured, read-only public link only."""
    def __init__(self, policy, expected):
        super().__init__()
        self.policy = policy
        self.expected = expected
        self.mapping_reviews = 0

    async def ask(self, request):
        if request.classification.action == "mapping_preflight":
            mappings = (request.classification.details or {}).get("cedar", {}).get("skills", {}).get("page_mapping", [])
            if len(mappings) != 1:
                return "block"
            mapping = mappings[0]
            candidate = mapping.get("candidate_fingerprint", {})
            original = self.policy["targets"][0]["fingerprint"]
            from urllib.parse import urljoin
            exact_destination = safe.clean_url(urljoin(candidate.get("url") or self.expected, candidate.get("href") or "")) == self.expected
            # Browser-visible text can differ from the captured accessible name
            # (aria-label) or a hover tooltip. Compare exact recorded label facts,
            # not loose similarity, while retaining the exact destination gate.
            def labels(value):
                facts = value.get("labels") or {}
                values = [value.get(key) for key in ("text", "aria_label", "nearby_text", "title")]
                values += [facts.get(key) for key in ("accessible", "associated")]
                return {" ".join(item.split()).casefold() for item in values
                        if isinstance(item, str) and item.strip() and "[REDACTED]" not in item}
            exact_label = bool(labels(candidate).intersection(labels(original)))
            if (mapping.get("skill_id") == self.policy["id"] and mapping.get("target_id") == self.policy["targets"][0]["id"]
                    and exact_destination and exact_label and mapping.get("evidence_gate", {}).get("concrete")
                    and safe.public_url(self.expected, document=True)):
                self.mapping_reviews += 1
                return "allow"
            return "block"
        return await super().ask(request)


class CaptureCleanupError(RuntimeError):
    """A capture worker cannot claim success after uncertain browser teardown."""


async def capture(p, site, expected, root, *, timeout, robots, pacer, headed, expected_control=None, progress=None):
    progress = progress if progress is not None else {}
    began=time.monotonic()
    def stage(name):
        progress["last_operation_stage"]=name
        progress.setdefault("stages",[]).append({"stage":name,"elapsed_seconds":round(time.monotonic()-began,2)})
    fence = safe.Fence(site["url"], pacer)
    with tempfile.TemporaryDirectory(prefix="capture-", dir=root) as temporary:
        directory = Path(temporary)
        extension = install_extension(directory / "extension")
        manifest = json.loads((extension / "manifest.json").read_text())
        # Narrow test fixture grants: production stays activeTab-only. Native
        # Chrome toolbar/permission UX is a separate, explicitly recorded check.
        manifest["host_permissions"] = ["https://"+host+"/*" for host in sorted(fence.hosts)]
        (extension / "manifest.json").write_text(json.dumps(manifest))
        stage("launch_browser")
        context = await p.chromium.launch_persistent_context(directory / "profile", channel="chromium",
            headless=not headed, viewport={"width":1280,"height":900}, permissions=[],
            service_workers="block", user_agent=safe.USER_AGENT,
            args=["--disable-extensions-except="+str(extension), "--load-extension="+str(extension)])
        try:
            assert await context.cookies() == []
            await context.route("**/*", fence.route)
            await context.route_web_socket("**/*", fence.websocket)
            worker = context.service_workers[0] if context.service_workers else await context.wait_for_event("serviceworker", timeout=10000)
            # These two installed extension resources are local files, not
            # public network destinations. Keep the public fence on all other
            # requests, including the page and every external resource.
            extension_origin = worker.url.rsplit("/", 1)[0]
            for name in ("selection.html", "selection.js"):
                await context.route(extension_origin + "/" + name, lambda route: route.continue_())
            page = await context.new_page()
            stage("load_source")
            loaded = await safe.load_page(page, site["url"], timeout, fence, robots)
            if loaded["status"] != "loaded":
                return {"status": loaded["status"]}, None
            stage("resolve_same_control")
            found, diagnostics = await safe.ready_link(page, expected, robots, expected_control=expected_control)
            if not found:
                return {"status":"target_unavailable","selection":diagnostics}, None
            target, destination, label = found
            control = await safe.link_control_identity(target)
            if expected_control and control["sha256"] != expected_control:
                return {"status":"target_identity_unavailable"}, None
            before, initial = len(fence.navigations), page.url
            stage("open_capture")
            await worker.evaluate("""async url=>{const tab=(await chrome.tabs.query({})).find(t=>t.url===url);
              await chrome.scripting.insertCSS({target:{tabId:tab.id},files:['capture.css']});
              await chrome.scripting.executeScript({target:{tabId:tab.id},files:['content.js']});}""", page.url)
            await page.locator("#acira-capture-panel").wait_for(state="visible")
            panel = page.locator("#acira-capture-panel")
            stage("wait_for_selector")
            try:
                await page.locator(".acira-capture-shield[data-ready='true']").wait_for(state="visible", timeout=6000)
            except PlaywrightTimeoutError:
                return {"status":"capture_selector_unavailable",
                    "selector_status":await panel.locator("#acira-capture-status").inner_text()}, None
            for selection_attempt in range(3):
                stage("select_control")
                # Fonts/hydration can shift a control while the selector loads.
                # Reobserve the same DOM node; never mark a different control.
                await target.wait_for_element_state("stable", timeout=2000)
                if (await safe.link_control_identity(target))["sha256"] != control["sha256"]:
                    return {"status":"target_identity_changed_before_capture"}, None
                if not await page.locator(".acira-capture-shield[data-ready='true']").is_visible():
                    return {"status":"capture_selector_unavailable"}, None
                box = await target.bounding_box()
                if not box or box["width"] <= 0 or box["height"] <= 0:
                    return {"status":"target_unavailable"}, None
                await page.mouse.click(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
                try:
                    await page.wait_for_function("""({destination,label,path}) => {
                      const raw=document.querySelector('#acira-capture-facts')?.textContent;
                      if (!raw) return false;
                      const fp=JSON.parse(raw), actual=new URL(fp.href,location.href), expected=new URL(destination);
                      const normalize=s=>typeof s==='string' ? s.replace(/\\s+/g,' ').trim().toLowerCase() : '';
                      // Captures intentionally omit URL query/fragment data.
                      // Verify the exact document plus the observed control's
                      // label; actual action reviews still bind the full link.
                      return fp.dom_path===path && fp.tag==='a' && actual.origin===expected.origin && actual.pathname===expected.pathname
                        && [fp.text,fp.aria_label,fp.title,fp.labels?.accessible,fp.labels?.associated]
                          .some(value=>normalize(value)===normalize(label));
                    }""", arg={"destination":destination,"label":label,"path":control["capture_path"]}, timeout=2000)
                    break
                except PlaywrightTimeoutError:
                    if page.url != initial or len(fence.navigations) != before:
                        return {"status":"capture_selection_activated_target"}, None
                    if selection_attempt == 2:
                        return {"status":"target_moved_during_selection",
                            "selector_status":await panel.locator("#acira-capture-status").inner_text()}, None
            if page.url != initial or len(fence.navigations) != before:
                return {"status":"capture_selection_activated_target"}, None
            stage("mark_control")
            await panel.get_by_role("button",name="Block target",exact=True).click()
            if page.url != initial or len(fence.navigations) != before:
                return {"status":"capture_selection_activated_target"}, None
            stage("download_export")
            async with page.expect_download(timeout=10000) as download:
                await panel.get_by_role("button",name="Download",exact=True).click()
            output = directory / "capture.json"
            await (await download.value).save_as(output)
            value = json.loads(output.read_text())
            assert value["schema"] == "acira.specific_skill.v2"
            assert len(value["targets"]) == 1 and value["targets"][0]["decision"] == "block"
            source = urlsplit(initial)
            expected_scope = source.scheme + "://" + source.netloc + (source.path or "/")
            if value["scope"]["url_patterns"] != [expected_scope]:
                return {"status":"capture_scope_changed"}, None
            stage("repeat_export")
            async with page.expect_download(timeout=10000) as again:
                await panel.get_by_role("button",name="Download",exact=True).click()
            second = json.loads(Path(await (await again.value).path()).read_text())
            assert second["id"] == value["id"] and second["targets"] == value["targets"]
            stage("exports_verified")
            return {"status":"verified","target_text":label,"target_url":destination,"target_control_sha256":control["sha256"],
                    "capture_selection_document_requests":len(fence.navigations)-before,
                    "fresh_cookie_count":0,"repeat_export_identity":"stable",
                    "extension_permission_fixture":"selected origins only; production manifest unchanged"}, value
        finally:
            progress["cleanup_status"]="started"
            try:
                await safe.close_context(context, fence)
                progress["cleanup_status"]="complete"
            except (Exception,asyncio.CancelledError) as error:
                progress["cleanup_status"]="failed"
                progress["cleanup_error"]=type(error).__name__
                browser=context.browser
                if browser is not None:
                    try:
                        await asyncio.wait_for(browser.close(),timeout=10)
                        progress["emergency_browser_close"]="complete" if not browser.is_connected() else "unconfirmed"
                    except (Exception,asyncio.CancelledError):
                        progress["emergency_browser_close"]="unconfirmed"
                raise CaptureCleanupError("Capture teardown failed; worker must stop without claiming a completed journey") from error


def cli(*arguments):
    result = subprocess.run([sys.executable,"-m","acira",*arguments], text=True,
                            capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError("Terminal policy operation failed: "+result.stderr[-600:])
    return result.stdout


async def campaign(args):
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ["ACIRA_HOME"] = str(root / "private")
    for name in ("ANONYMIZED_TELEMETRY","BROWSER_USE_CLOUD_SYNC","BROWSER_USE_VERSION_CHECK"):
        os.environ[name] = "false"
    import acira
    if "site-packages" not in Path(acira.__file__).parts:
        raise RuntimeError("Live campaign must use a pip-installed SDK, not source-checkout imports")
    metadata = {"started_at_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        "sdk":importlib.metadata.version("acira"),"sdk_module":str(Path(acira.__file__).resolve()),
        "playwright":importlib.metadata.version("playwright"),"duration_budget_minutes":args.minutes,
        "harness_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "catalog_sha256":hashlib.sha256(args.catalog.read_bytes()).hexdigest(),
        "signed_out":"Fresh empty browser context/profile for every baseline, capture and policy case.",
        "network":"Read-only GET/HEAD, robots rules, no accounts/forms/purchases/download links/WebSockets; no challenge bypass.",
        "approval":"Test reviewer permits only the selected public content link, and explicit page mappings with an exact recorded control-label fact and destination; every other request is rejected.",
        "extension":"Bundled ManifestV3 extension in Chromium; narrowly pregranted site permissions for automation, separate native Chrome UI check."}
    (root / "metadata.json").write_text(json.dumps(metadata,indent=2)+"\n")
    results = root / "results.jsonl"
    if results.exists():
        raise ValueError("Use a new output folder; evidence must not be overwritten")
    began = time.monotonic()
    sites = json.loads(args.catalog.read_text())["sites"][:args.limit]
    async with contextlib.AsyncExitStack() as stack:
        p = await stack.enter_async_context(async_playwright())
        baseline = await p.chromium.launch(headless=not args.headed)
        stack.push_async_callback(baseline.close)
        session = await stack.enter_async_context(ClientSession(timeout=ClientTimeout(total=12),headers={"User-Agent":safe.USER_AGENT}))
        for site in sites:
            if time.monotonic()-began >= args.minutes*60:
                break
            row = {**site,"cases":[],"started_at_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
            tick = time.monotonic()
            robots, row["robots"] = await safe.robots_check(session, site["url"])
            interval = .5
            if robots:
                rate = robots.request_rate(safe.USER_AGENT) or robots.request_rate("*")
                interval = max(robots.crawl_delay(safe.USER_AGENT) or robots.crawl_delay("*") or .5,
                               rate.seconds/rate.requests if rate else .5)
                if interval>5:
                    robots, row["robots"] = False,"robots_rate_exceeds_campaign_budget"
            pacer = safe.DocumentPacer(interval)
            row["minimum_document_interval_seconds"] = interval
            if robots is not False:
                first = await safe.bounded_case(baseline,site,timeout=args.timeout,robots=robots,pacer=pacer)
                row["cases"].append(first)
                if first["status"] == "verified":
                    try:
                        row["capture_progress"]={}
                        captured, policy = await asyncio.wait_for(capture(p,site,first["target_url"],root,
                            timeout=args.timeout,robots=robots,pacer=pacer,headed=args.headed,expected_control=first["target_control_sha256"],progress=row["capture_progress"]),120)
                        row["capture"] = captured
                        if policy:
                            project = initialize(root / ("project-"+hashlib.sha256(site["url"].encode()).hexdigest()[:12]),
                                selected=["payment","credentials","destructive-actions","downloads"],dashboard=False)
                            export = project.root / "capture.json"
                            export.write_text(json.dumps(policy,indent=2)+"\n")
                            options = ["--project",str(project.root)]
                            cli("policy","import",str(export),*options)
                            cli("policy","import",str(export),*options)
                            assert sum(entry["kind"] == "specific" for entry in project.policies("default")) == 1
                            row["policy_id"] = policy["id"]
                            row["repeat_import"] = "idempotent"
                            reviewer = CaptureReviewer(policy, first["target_url"])
                            async with protect(project=project.root,dashboard=False,review_backend=reviewer) as guard, async_playwright() as runtime:
                                guard.patch_playwright(runtime)
                                browser = await runtime.chromium.launch(headless=not args.headed)
                                try:
                                    for mode, decision in (("blocked","block"),("rejected","ask"),("approved","ask")):
                                        cli("policy","configure",policy["id"],"--target",str(policy["targets"][0]["id"]),"--decision",decision,*options)
                                        tested = await safe.bounded_case(browser,site,timeout=args.timeout,expected=first["target_url"],
                                            expected_landing=first["landing_url"],expected_control=captured["target_control_sha256"],mode=mode,guard=guard,journal=Journal(project.root),
                                            reviewer=reviewer,robots=robots,pacer=pacer)
                                        tested["policy_revision"] = guard.guard_policy.policy_revision
                                        tested["control"] = "captured specific policy; no generic blocked/review action settings"
                                        tested["mapping_reviews_so_far"] = reviewer.mapping_reviews
                                        row["cases"].append(tested)
                                        if tested["status"] in {"protection_failure","audit_failure","review_failure"}:
                                            break
                                finally:
                                    await browser.close()
                    except Exception as error:
                        row["error"] = type(error).__name__+": "+str(error)[:600]
                        if isinstance(error,CaptureCleanupError):
                            row["worker_failure"]="capture_cleanup_failure"
            statuses = [case["status"] for case in row["cases"]]
            row["status"] = "complete" if statuses == ["verified"]*4 and row.get("capture",{}).get("status")=="verified" else "partial" if "verified" in statuses else "unavailable"
            row["elapsed_seconds"] = round(time.monotonic()-tick,2)
            with results.open("a") as output:
                output.write(json.dumps(row,ensure_ascii=False)+"\n")
            print(json.dumps({"site":site["url"],"status":row["status"],"capture":row.get("capture",{}).get("status"),"cases":statuses,"error":row.get("error")}),flush=True)
            if row.get("worker_failure") or any(status in {"protection_failure","audit_failure","review_failure"} for status in statuses) or row.get("capture",{}).get("status") in {"capture_selection_activated_target", "capture_scope_changed"}:
                raise RuntimeError("Safety/evidence failure; campaign stopped without weakening protections")
            await asyncio.sleep(.5)
    summary(root, time.monotonic()-began)


def summary(root, seconds=None):
    rows = [json.loads(line) for line in (root/"results.jsonl").read_text().splitlines() if line]
    report = {"sites_attempted":len(rows),"unique_hosts":len({urlsplit(row["url"]).hostname for row in rows}),
        "full_specific_workflow_passes":sum(row["status"]=="complete" for row in rows),
        "site_status":dict(Counter(row["status"] for row in rows)),
        "case_status":dict(Counter(case["mode"]+":"+case["status"] for row in rows for case in row["cases"])),
        "captures_verified":sum(row.get("capture",{}).get("status")=="verified" for row in rows),
        "elapsed_seconds":seconds,
        "limitations":["Signed-out public content links; not payment/account or authenticated workflow coverage.",
            "Chromium extension tests use narrowly pregranted host permissions; native Chrome setup is recorded separately.",
            "Blocked/robots-disallowed/challenged/unavailable websites are not protection passes."]}
    (root/"summary.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report,indent=2),flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog",type=Path,default=Path(__file__).with_name("public-sites.json"))
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--minutes",type=float,default=60)
    parser.add_argument("--limit",type=int,default=300)
    parser.add_argument("--timeout",type=int,default=18000)
    parser.add_argument("--headed",action="store_true")
    args = parser.parse_args()
    if args.minutes<=0 or args.limit<1 or not 1000<=args.timeout<=30000:
        parser.error("Use a positive duration/site limit and a 1000–30000ms navigation timeout")
    try:
        asyncio.run(campaign(args))
    except BaseException:
        # Preserve coverage accounting even when a safety failure stops the
        # worker. Keep the failure exit status; never turn it into a pass.
        root = args.output.resolve()
        if (root / "results.jsonl").exists():
            summary(root)
        raise
