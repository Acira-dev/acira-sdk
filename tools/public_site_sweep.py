"""Opt-in, signed-out public-site compatibility sweep; never runs in ordinary CI.

Live cases use installed Acira, a fresh context for every case, bundled policies,
and an independent read-only network fence. Test reviewers cover harmless public
links only. No account, payment, form, download, or CAPTCHA workflow is attempted.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import hashlib
import html
import importlib.metadata
import ipaddress
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from aiohttp import ClientSession, ClientTimeout
from playwright.async_api import async_playwright
from acira import protect
from acira.journal import Journal
from acira.local import atomic_json
from acira.project import initialize
from acira_cdp_proxy.action_gateway import AciraActionBlocked

USER_AGENT = "AciraCompatibilityTest/0.1 (+https://github.com/Acira-dev/acira-sdk)"
UNSAFE = re.compile(r"login|log-in|sign.?in|sign.?up|logout|log-out|account|checkout|payment|purchase|buy(?:now)?|cart|basket|order|subscribe|unsubscribe|donat|delete|remove|destroy|accept|consent|authorize|oauth|auth/|register|download|export|upload|submit|reset|token|password|credential", re.I)
CONTENT = re.compile(r"docs?|documentation|guide|tutorial|learn|about|overview|features|research|science|academics|department|library|libraries|collection|exhibition|mission|history|news|article|resources|publications|reference|introduction|explore|discover|projects|community|education", re.I)
CHALLENGE = re.compile(r"just a moment|verify (?:that )?you are human|checking your browser|access denied|unusual traffic|captcha|enable javascript and cookies to continue|attention required|robot or human", re.I)
NON_PAGE = re.compile(r"\.(?:pdf|zip|gz|bz2|xz|exe|msi|dmg|apk|csv|json|xml|txt|png|jpg|jpeg|gif|svg|webp|mp3|mp4|webm|woff2?)(?:$|/)", re.I)

def public_url(url: str, *, document: bool = False) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            return False
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            ipaddress.ip_address(host)
            return False  # The curated sweep uses named public websites only.
        except ValueError:
            pass
        decoded = unquote(unquote(parsed.path + "?" + parsed.query))
        return not UNSAFE.search(decoded) and (not document or (not parsed.query and not NON_PAGE.search(parsed.path)))
    except ValueError:
        return False

def clean_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", parsed.fragment))

def document_key(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))

def destination_matches(actual: str, expected: str) -> bool:
    return (public_url(actual, document=True) and public_url(expected, document=True)
            and document_key(actual) == document_key(expected)
            and urlsplit(actual).fragment == urlsplit(expected).fragment)

class DocumentPacer:
    def __init__(self, interval):
        self.interval = interval
        self.next = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            await asyncio.sleep(max(0, self.next - time.monotonic()))
            self.next = time.monotonic() + self.interval

class Fence:
    def __init__(self, url: str, pacer=None):
        self.start = clean_url(url)
        host = urlsplit(url).hostname
        self.hosts = {host, host[4:] if host.startswith("www.") else "www." + host}
        self.documents = None
        self.allowed = 0
        self.denied = Counter()
        self.navigations = []
        self.redirects = {}
        self.limit = 400
        self.pacer = pacer
        self.active_routes = set()

    def permits(self, url: str, method: str, navigation: bool) -> bool:
        if method not in {"GET", "HEAD"} or not public_url(url, document=navigation):
            return False
        if navigation:
            if urlsplit(url).hostname not in self.hosts:
                return False
            if self.documents is not None and document_key(url) not in self.documents:
                return False
        return True

    async def route(self, route):
        # Retain route tasks explicitly while responses are being fetched.
        # Playwright's handler completion futures alone do not retain tasks
        # through cancellation/garbage collection during context teardown.
        task = asyncio.current_task()
        self.active_routes.add(task)
        try:
            await self._route(route)
        finally:
            self.active_routes.discard(task)

    async def _route(self, route):
        request = route.request
        navigation = request.is_navigation_request()
        if self.allowed >= self.limit:
            self.denied["request_budget"] += 1
            await route.abort("blockedbyclient")
        elif not self.permits(request.url, request.method, navigation):
            self.denied["method" if request.method not in {"GET", "HEAD"} else "url"] += 1
            await route.abort("blockedbyclient")
        else:
            self.allowed += 1
            if navigation:
                self.navigations.append(clean_url(request.url))
                if self.pacer is not None:
                    await self.pacer.wait()
            # Native HTTP redirect continuations are not re-routed reliably by
            # Playwright. Fetch exactly one response and never release that
            # original browser request. The driver follows permitted document
            # redirects as new guarded navigations; resource redirects stop.
            response = None
            try:
                response = await route.fetch(max_redirects=0, max_retries=0, timeout=12000)
                if response.status in {301, 302, 303, 307, 308}:
                    destination = urljoin(request.url, response.headers.get("location", ""))
                    if navigation and self.permits(destination, "GET", True):
                        self.redirects[document_key(request.url)] = destination
                        self.denied["redirect_held_for_guarded_navigation"] += 1
                        # Complete the original request with an inert test page.
                        # Aborting it leaves a Chrome error-page navigation racing
                        # the next explicit guarded goto on some real websites.
                        await route.fulfill(status=200, content_type="text/html",
                            body="<!doctype html><title>Redirect held by test boundary</title><p>Awaiting explicit guarded navigation.</p>")
                    else:
                        self.denied["redirect_not_permitted"] += 1
                        await route.abort("blockedbyclient")
                else:
                    await route.fulfill(response=response)
            except Exception:
                self.denied["transport_error"] += 1
                with contextlib.suppress(Exception):
                    await route.abort("failed")
            finally:
                if response is not None:
                    # A page may close while background GETs finish. Response
                    # cleanup must not poison the next isolated browser case.
                    with contextlib.suppress(Exception):
                        await response.dispose()

    async def websocket(self, socket):
        self.denied["websocket"] += 1
        await socket.close()

class TestReviewer:
    def __init__(self):
        self.decision = "block"
        self.expected = None
        self.requests = 0

    async def ask(self, request):
        self.requests += 1
        classification = request.classification
        target = classification.target
        href = target.href if target else None
        base = target.url if target else None
        resolved = urljoin(base or self.expected or "", href or "")
        if self.decision == "allow" and classification.action == "click" and clean_url(resolved) == self.expected and public_url(resolved, document=True):
            return "allow"
        return "block"

async def new_context(browser, fence):
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900}, permissions=[],
        storage_state={"cookies": [], "origins": []}, accept_downloads=False,
        service_workers="block", user_agent=USER_AGENT,
    )
    assert await context.cookies() == []
    await context.route("**/*", fence.route)
    await context.route_web_socket("**/*", fence.websocket)
    async def dismiss(dialog):
        await dialog.dismiss()
    context.on("dialog", lambda dialog: asyncio.create_task(dismiss(dialog)))
    return context

async def observe(page):
    title = (await page.title())[:180]
    body = (await page.locator("body").inner_text(timeout=7000))[:6000]
    return {"title": title, "text_length": len(body), "challenge": bool(CHALLENGE.search(title + "\n" + body))}

async def find_link(page, expected=None, robots=None):
    links = page.locator("a[href]")
    for index in range(min(await links.count(), 120)):
        item = links.nth(index)
        try:
            href, label, download, onclick, role, target = await asyncio.gather(
                item.get_attribute("href"), item.inner_text(timeout=1500),
                item.get_attribute("download"), item.get_attribute("onclick"),
                item.get_attribute("role"), item.get_attribute("target"),
            )
            resolved = urljoin(page.url, href or "")
            if expected and clean_url(resolved) != expected:
                continue
            if not expected and not CONTENT.search((label or "") + " " + (href or "")):
                continue
            if (href or "").startswith("#") or re.search(r"skip .*(?:content|navigation)|back to top", label, re.I):
                continue
            if not label.strip() or len(label) > 180 or download is not None or onclick or role == "button" or target not in (None, "", "_self"):
                continue
            if UNSAFE.search(label) or not public_url(resolved, document=True) or urlsplit(resolved).hostname != urlsplit(page.url).hostname:
                continue
            if clean_url(resolved) == clean_url(page.url) or not await item.is_visible():
                continue
            if robots is not None and (not robots.can_fetch(USER_AGENT, resolved) or not robots.can_fetch("*", resolved)):
                continue
            return item, clean_url(resolved), label.strip()
        except Exception:
            continue
    return None

async def robots_check(session, url):
    origin = urlsplit(url)
    address = urlunsplit((origin.scheme, origin.netloc, "/robots.txt", "", ""))
    try:
        for _ in range(4):
            async with session.get(address, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    destination = urljoin(address, response.headers.get("Location", ""))
                    if not public_url(destination) or urlsplit(destination).query or urlsplit(destination).hostname not in Fence(url).hosts or urlsplit(destination).path != "/robots.txt":
                        return False, "robots_redirect_outside_scope"
                    address = destination
                    continue
                if response.status == 404:
                    return None, "not_present"
                if response.status != 200:
                    return False, "robots_http_" + str(response.status)
                text = await response.content.read(512_001)
                if len(text) > 512_000:
                    return False, "robots_too_large"
                if re.search(br"<!doctype\s+html|<html", text[:2000], re.I):
                    return False, "robots_not_text"
                parser = RobotFileParser()
                parser.parse(text.decode("utf-8", errors="replace").splitlines())
                if not parser.can_fetch(USER_AGENT, url) or not parser.can_fetch("*", url):
                    return False, "robots_disallowed"
                return parser, "allowed"
        return False, "robots_redirect_limit"
    except Exception as exc:
        return False, "robots_unavailable_" + type(exc).__name__

async def load_page(page, url, timeout, fence, robots=None):
    chain = []
    for _ in range(4):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception:
            if document_key(url) not in fence.redirects:
                raise
        destination = fence.redirects.pop(document_key(url), None)
        if destination is None:
            break
        if robots is not None and (not robots.can_fetch(USER_AGENT, destination) or not robots.can_fetch("*", destination)):
            return {"status": "redirect_robots_disallowed"}
        chain.append({"from": clean_url(url), "to": clean_url(destination)})
        url = destination
    else:
        return {"status": "redirect_limit"}
    if response is None or response.status >= 400:
        return {"status": "http_error", "http_status": response.status if response else None}
    if "text/html" not in response.headers.get("content-type", ""):
        return {"status": "not_html", "http_status": response.status}
    details = await observe(page)
    return {"status": "bot_challenge" if details["challenge"] else "loaded", "http_status": response.status, "explicit_redirects": chain, **details}

def event_slice(journal, run, previous):
    return journal.events(run=run, after=previous, limit=5000)

async def case(browser, site, *, timeout, expected=None, mode="baseline", guard=None, journal=None, reviewer=None, robots=None, pacer=None):
    fence = Fence(site["url"], pacer)
    context = await new_context(browser, fence)
    page = await context.new_page()
    page.set_default_timeout(6000)
    result = {"mode": mode, "fresh_cookie_count": 0}
    run = guard.runtime_control.run_id if guard else None
    try:
        result.update(await load_page(page, site["url"], timeout, fence, robots))
        result["final_url"] = clean_url(page.url)
        if result["status"] != "loaded":
            return result
        try:
            async with asyncio.timeout(15):
                link = await find_link(page, expected, robots)
        except TimeoutError:
            result["status"] = "link_scan_timeout"
            return result
        if not link:
            result["status"] = "no_safe_link"
            return result
        locator, destination, label = link
        result.update(target_url=destination, target_text=label)
        # Restrict all document navigation during the operation to the observed
        # start and selected public content page, including a fragment route.
        fence.documents = {document_key(page.url), document_key(destination)}
        initial, before_requests = page.url, len(fence.navigations)
        destination_responses = []
        page.on("response", lambda response: destination_responses.append(response.status)
            if response.request.is_navigation_request() and destination_matches(response.url, destination) else None)
        events = journal.events(run=run, limit=1, descending=True) if journal else []
        previous = events[-1]["sequence"] if events else 0
        prior_reviews = {r["review_id"] for r in journal.reviews() if r["run_id"] == run} if journal else set()
        if reviewer:
            reviewer.expected = destination
            reviewer.decision = "allow" if mode == "approved" else "block"
        stopped = None
        try:
            await asyncio.wait_for(locator.click(), timeout=max(25, timeout / 1000 + 10))
        except AciraActionBlocked as exc:
            stopped = str(exc)[:500]
        except Exception:
            if mode in {"blocked", "rejected"} or document_key(destination) not in fence.redirects:
                raise
        redirect = fence.redirects.pop(document_key(destination), None)
        if redirect and not stopped:
            if mode in {"blocked", "rejected"}:
                raise RuntimeError("A blocked operation initiated a redirect")
            if robots is not None and (not robots.can_fetch(USER_AGENT, redirect) or not robots.can_fetch("*", redirect)):
                result["status"] = "redirect_robots_disallowed"
                return result
            followed = await load_page(page, redirect, timeout, fence, robots)
            result["click_redirect"] = {"from": destination, "to": clean_url(redirect), "landing_status": followed["status"]}
            if followed["status"] != "loaded":
                result["status"] = followed["status"]
                return result
        if mode in {"blocked", "rejected"}:
            result["status"] = "verified" if stopped and page.url == initial and len(fence.navigations) == before_requests else "protection_failure"
        elif stopped:
            result["status"] = "sdk_block"
        else:
            try:
                await page.wait_for_url(lambda url: destination_matches(str(url), destination), wait_until="domcontentloaded", timeout=timeout)
                observation = await observe(page)
                result["status"] = "verified" if destination_matches(page.url, destination) and observation["text_length"] > 0 and not observation["challenge"] and all(status < 400 for status in destination_responses) else "destination_failure"
                result["destination"] = observation
                result["destination_http_statuses"] = destination_responses
            except Exception as exc:
                result.update(status="destination_failure", error=type(exc).__name__ + ": " + str(exc)[:500])
        if stopped:
            result["reason"] = stopped
        result["final_url"] = clean_url(page.url)
        result["document_requests_after_click"] = len(fence.navigations) - before_requests
        if journal:
            changed = event_slice(journal, run, previous)
            operations = {e["operation_id"] for e in changed if e["type"] == "action_proposed" and e.get("action") == "click"}
            result["click_dispatches"] = sum(e["type"] == "action_dispatched" and e.get("operation_id") in operations for e in changed)
            reviews = [r for r in journal.reviews() if r["run_id"] == run and r["review_id"] not in prior_reviews]
            result["reviews"] = [{"state": r["state"], "capture": r.get("artifact", {}).get("capture_status"), "capture_reason": r.get("artifact", {}).get("reason"),
                                  "bounding_box": r.get("artifact", {}).get("bounding_box"), "viewport": r.get("artifact", {}).get("viewport"),
                                  "artifact_id": r.get("artifact", {}).get("id")} for r in reviews]
            if result["status"] == "verified":
                if result["click_dispatches"] != (0 if mode in {"blocked", "rejected"} else 1):
                    result["status"] = "audit_failure"
                if mode in {"rejected", "approved"} and (len(reviews) != 1 or reviews[0]["state"] != ("rejected" if mode == "rejected" else "approved")):
                    result["status"] = "review_failure"
    except Exception as exc:
        result.update(status="error", error=type(exc).__name__ + ": " + str(exc)[:600])
    finally:
        result["read_requests"] = fence.allowed
        result["requests_stopped"] = dict(fence.denied)
        result["final_url"] = clean_url(page.url)
        # The context is being destroyed; suppress only abandoned route errors,
        # after all action/effect evidence has been recorded.
        await context.unroute_all(behavior="ignoreErrors")
        if fence.active_routes:
            await asyncio.wait(tuple(fence.active_routes), timeout=14)
        await asyncio.wait_for(context.close(), timeout=12)
        if fence.active_routes:
            await asyncio.wait(tuple(fence.active_routes), timeout=2)
    return result

async def worker(args):
    root = args.output.resolve()
    os.environ["ACIRA_HOME"] = str(root / "private" / f"worker-{args.shard}")
    root.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).read_bytes()
    import acira
    metadata = {"started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": sys.version, "sdk": importlib.metadata.version("acira"),
        "playwright": importlib.metadata.version("playwright"), "sdk_module": str(Path(acira.__file__).resolve()),
        "harness_sha256": hashlib.sha256(source).hexdigest(),
        "catalog_sha256": hashlib.sha256(args.catalog.read_bytes()).hexdigest(),
        "shard": args.shard, "shards": args.shards, "timeout_ms": args.timeout,
        "signed_out": "New empty context for every case; no profile, credentials, permissions or storage imports.",
        "network": "GET/HEAD HTTPS only; service workers, WebSockets, accounts, mutations and downloads blocked."}
    existing_metadata = root / f"metadata-{args.shard}.json"
    if args.resume and existing_metadata.exists():
        previous = json.loads(existing_metadata.read_text())
        for key in ("harness_sha256", "catalog_sha256", "shard", "shards", "timeout_ms", "sdk", "playwright"):
            if previous.get(key) != metadata[key]:
                raise ValueError(f"Cannot mix runs with different {key}; choose a new output folder")
        metadata["started_at_utc"] = previous["started_at_utc"]
    (root / f"metadata-{args.shard}.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (root / f"harness-{args.shard}.py").write_bytes(source)
    (root / "catalog.json").write_bytes(args.catalog.read_bytes())
    sites = json.loads(args.catalog.read_text())["sites"]
    sites = sites[:args.limit] if args.limit else sites
    sites = [site for index, site in enumerate(sites) if index % args.shards == args.shard]
    path = root / f"results-{args.shard}.jsonl"
    done = {json.loads(line)["url"] for line in path.read_text().splitlines()} if path.exists() and args.resume else set()
    if not args.resume:
        path.write_text("")
    project = initialize(root / f"project-{args.shard}", agent="normal", selected=["payment", "credentials", "destructive-actions", "downloads"], dashboard=False)
    for name, extra in (("blocked", {"blocked_actions": ["click"]}), ("review", {"review_actions": ["click"]})):
        initialize(project.root, agent=name, selected=[], dashboard=False)
        document = project.document()
        document["agents"][name]["guard"] = {**extra, "terminal_review": False, "terminal_log": False}
        atomic_json(project.path, document)
    document = project.document()
    document["agents"]["normal"]["guard"] = {"terminal_review": False, "terminal_log": False}
    atomic_json(project.path, document)
    reviewer = TestReviewer()
    journal = Journal(project.root)
    async with contextlib.AsyncExitStack() as stack:
        p = await stack.enter_async_context(async_playwright())
        baseline = await p.chromium.launch(headless=not args.headed)
        stack.push_async_callback(baseline.close)
        browsers, guards = {}, {}
        for name in ("normal", "blocked", "review"):
            guard = await stack.enter_async_context(protect(project=project.root, agent=name, dashboard=False, review_backend=reviewer))
            runtime = await stack.enter_async_context(async_playwright())
            guard.patch_playwright(runtime)
            browsers[name] = await runtime.chromium.launch(headless=not args.headed)
            guards[name] = guard
        session = await stack.enter_async_context(ClientSession(timeout=ClientTimeout(total=12), headers={"User-Agent": USER_AGENT}))
        for site in sites:
            if site["url"] in done:
                continue
            began = time.monotonic()
            result = {**site, "cases": [], "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            robots, result["robots"] = await robots_check(session, site["url"])
            interval = .5
            if robots:
                delay = robots.crawl_delay(USER_AGENT) or robots.crawl_delay("*") or .5
                rate = robots.request_rate(USER_AGENT) or robots.request_rate("*")
                interval = max(delay, rate.seconds / rate.requests if rate else .5)
                if interval > 5:
                    robots, result["robots"] = False, "robots_rate_exceeds_sweep_budget"
            result["minimum_document_interval_seconds"] = interval
            pacer = DocumentPacer(interval)
            if robots is not False:
                first = await case(baseline, site, timeout=args.timeout, robots=robots, pacer=pacer)
                result["cases"].append(first)
                if first.get("status") == "verified":
                    target = first["target_url"]
                    if robots is None or (robots.can_fetch(USER_AGENT, target) and robots.can_fetch("*", target)):
                        for mode, profile in (("normal", "normal"), ("blocked", "blocked"), ("rejected", "review"), ("approved", "review")):
                            result["cases"].append(await case(browsers[profile], site, timeout=args.timeout, expected=target,
                                mode=mode, guard=guards[profile], journal=journal, reviewer=reviewer, robots=robots, pacer=pacer))
                    else:
                        result["target_robots_disallowed"] = True
                elif first.get("status") == "no_safe_link":
                    # Count a real guarded landing-page observation without
                    # pretending an action/review workflow was covered.
                    result["cases"].append(await case(browsers["normal"], site, timeout=args.timeout,
                        mode="normal", guard=guards["normal"], journal=journal, reviewer=reviewer, robots=robots, pacer=pacer))
            statuses = [c["status"] for c in result["cases"]]
            result["status"] = "complete" if statuses == ["verified"] * 5 else "partial" if any(s in {"verified", "no_safe_link"} for s in statuses) else "unavailable"
            result["elapsed_seconds"] = round(time.monotonic() - began, 2)
            with path.open("a") as output:
                output.write(json.dumps(result, ensure_ascii=False) + "\n")
            print(json.dumps({"site": site["url"], "status": result["status"], "cases": statuses, "robots": result["robots"]}), flush=True)
            if "protection_failure" in statuses or any(c.get("click_dispatches", 0) > 0 for c in result["cases"] if c["mode"] in {"blocked", "rejected"}):
                raise RuntimeError("Safety verification failed; worker stopped for investigation")
            await asyncio.sleep(.5)
    print("SWEEP WORKER FINISHED", args.shard, flush=True)

def summary(output):
    rows = [json.loads(line) for path in sorted(output.glob("results-*.jsonl")) for line in path.read_text().splitlines() if line]
    counts = Counter(row["status"] for row in rows)
    cases = Counter(f"{case['mode']}:{case['status']}" for row in rows for case in row["cases"])
    metadata = {"sites_attempted": len(rows), "unique_hosts": len({urlsplit(r["url"]).hostname for r in rows}),
                "sites_with_observed_html": sum(any(c.get("http_status", 999) < 400 and c.get("title") for c in r["cases"]) for r in rows),
                "sites_with_guarded_html": sum(any(c["mode"] != "baseline" and c.get("http_status", 999) < 400 and c.get("title") for c in r["cases"]) for r in rows),
                "full_workflow_passes": counts["complete"],
                "site_status": dict(counts), "cases": dict(cases),
                "limits": ["Signed-out asynchronous Chromium/Playwright public-page workflows only.",
                           "Independent GET/HEAD fence changes pages requiring other network methods.",
                           "HTTP redirects are followed explicitly as new guarded navigations; resource redirects stop.",
                           "No authenticated, payment, destructive, upload, or download workflow coverage.",
                           "Unavailable sites and partial workflows are not protection passes."]}
    (output / "summary.json").write_text(json.dumps(metadata, indent=2) + "\n")
    fields = ["url", "category", "status", "robots", "baseline", "normal", "blocked", "rejected", "approved", "elapsed_seconds"]
    with (output / "results.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({**{field: row.get(field, "") for field in fields},
                             **{case["mode"]: case["status"] for case in row["cases"]}})
    render_report(output, rows, metadata)
    return metadata

def render_report(output, rows, metadata):
    escape = html.escape
    table = []
    for row in sorted(rows, key=lambda r: (r["status"] == "complete", r["category"], r["url"])):
        cases = {case["mode"]: case for case in row["cases"]}
        cells = "".join(f"<td>{escape(cases.get(mode, {}).get('status', '—'))}</td>" for mode in ("baseline", "normal", "blocked", "rejected", "approved"))
        # Detail JSON deliberately excludes runtime cookies and raw page bodies.
        detail = escape(json.dumps(row, ensure_ascii=False, indent=2))
        table.append(f"<tr data-status='{escape(row['status'])}'><td><details><summary>{escape(urlsplit(row['url']).hostname)}</summary><p>{escape(row['url'])}</p><pre>{detail}</pre></details></td><td>{escape(row['category'])}</td><td>{escape(row['status'])}</td>{cells}</tr>")
    limits = "".join(f"<li>{escape(item)}</li>" for item in metadata["limits"])
    document = """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Acira · Signed-out website coverage</title><style>
body{font:15px/1.5 system-ui,sans-serif;color:#182032;background:#fafbfc;margin:0;padding:36px;max-width:1500px}h1{font-size:30px;margin:0 0 12px}p{max-width:1000px;color:#536078}.metrics{display:flex;gap:16px;flex-wrap:wrap;margin:28px 0}.metric{border:1px solid #dce1e8;background:white;border-radius:12px;padding:20px;min-width:150px}.metric strong{display:block;font-size:32px;color:#182032}table{border-collapse:collapse;background:white;width:100%;margin:20px 0}th,td{text-align:left;vertical-align:top;padding:12px;border-bottom:1px solid #e6e9ee}th{background:#f0f3f7}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;max-height:380px;overflow:auto;max-width:650px}select,input{font:inherit;padding:10px;border:1px solid #aab4c4;border-radius:6px;background:white;margin-right:12px}tr[data-status=complete]>td:nth-child(3){color:#10734d}tr[data-status=partial]>td:nth-child(3){color:#865100}tr[data-status=unavailable]>td:nth-child(3){color:#9e3341}label{display:inline-block}a{color:#225dd7}.scroll{overflow-x:auto}:focus-visible{outline:2px solid #225dd7;outline-offset:3px}
</style><h1>Signed-out website coverage</h1><p>Installed Acira SDK · Chromium / asynchronous Playwright · fresh empty browser session for every case. Only public content navigation is tested. A full pass requires baseline, ordinary policies, block, rejected review and approved review to match their expected browser effects.</p>
<div class="metrics">METRICS</div><label>Show <select id="status"><option value="">All results</option><option>complete</option><option>partial</option><option>unavailable</option></select></label><label>Find site <input id="search" type="search" placeholder="Domain or category"></label><div class="scroll"><table><thead><tr><th>Website / evidence</th><th>Category</th><th>Result</th><th>Baseline</th><th>Policies</th><th>Block</th><th>Reject</th><th>Approve</th></tr></thead><tbody>ROWS</tbody></table></div><h2>Interpretation</h2><ul>LIMITS</ul><p>Unavailable results include robots restrictions, bot checks, HTTP errors and pages without a suitable safe action. They are retained and never counted as full protection passes. This report covers real public-page behavior; it does not establish compatibility with every workflow or browser integration.</p><script>
const status=document.getElementById('status'),search=document.getElementById('search');function filter(){for(const row of document.querySelectorAll('tbody tr')){row.hidden=!!(status.value&&row.dataset.status!==status.value)||!row.textContent.toLowerCase().includes(search.value.toLowerCase());}}status.addEventListener('change',filter);search.addEventListener('input',filter);
</script></html>"""
    metrics = "".join(f"<div class='metric'><strong>{metadata[key]}</strong>{label}</div>" for key, label in (("sites_attempted", "Sites attempted"), ("sites_with_observed_html", "Public pages observed"), ("sites_with_guarded_html", "Pages with Acira"), ("full_workflow_passes", "Complete workflow passes")))
    document = document.replace("METRICS", metrics).replace("ROWS", "".join(table)).replace("LIMITS", limits)
    (output / "report.html").write_text(document)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=Path(__file__).with_name("public-sites.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=int, default=20000)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    if args.shards < 1 or not 0 <= args.shard < args.shards:
        parser.error("Choose a shard within the shard count")
    if args.summary:
        print(json.dumps(summary(args.output.resolve()), indent=2))
    else:
        asyncio.run(worker(args))

if __name__ == "__main__":
    main()
