"""One-attempt HTTP submission contracts, bound to explicitly reviewed values."""
from __future__ import annotations

import asyncio
import contextlib
from contextvars import ContextVar
from typing import Any

from acira_cdp_proxy.action_gateway import AciraActionBlocked
from .playwright_boundary import OWNERS, WORKER_HISTORY, trusted

SUBMISSION: ContextVar[Any] = ContextVar("acira_submission", default=None)


from ._submission_contract import RequestContract


class Submission(RequestContract):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.routes = set()
        self.tasks = set()
        self.frame = None

    async def bind_initiator(self, element: Any) -> None:
        frame = await element.owner_frame()
        if frame is None or frame.page is not self.page or self.frame is not None and self.frame is not frame:
            raise AciraActionBlocked("The submission initiator could not be bound to its reviewed frame")
        self.frame = frame

    def worker_free(self) -> bool:
        # Playwright assigns a dedicated worker's requests to its owner Frame.
        # Such a context cannot establish an exact document request initiator.
        pages = self.page.context.pages
        return self.page in pages and all(page in WORKER_HISTORY and not WORKER_HISTORY[page] and not page.workers for page in pages)

    async def route(self, route: Any) -> None:
        task = asyncio.current_task()
        self.routes.add(route)
        self.tasks.add(task)
        try:
            await self._route(route)
        except BaseException:
            with contextlib.suppress(Exception):
                await route.abort("blockedbyclient")
            if not self.outcome.done():
                self.outcome.set_result({"blocked": True})
        finally:
            self.routes.discard(route)
            self.tasks.discard(task)

    async def close(self) -> None:
        self.active = False
        self.finished = True
        if not self.outcome.done():
            self.outcome.set_result({"blocked": True})
        for route in tuple(self.routes):
            try:
                await route.abort("blockedbyclient")
            except Exception:
                if not route.request.frame.page.is_closed():
                    raise
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=1)
            if pending:
                raise RuntimeError("Submission callbacks did not stop; interception remains installed")

    async def _route(self, route: Any) -> None:
        request = route.request
        # After the attempt, retain a lock on this endpoint. A new explicit
        # submission installs a newer route; ordinary application retries cannot.
        relevant = request.url == self.destination or self.active and (request.method not in {"GET", "HEAD", "OPTIONS"} or request.resource_type in {"xhr", "fetch", "document"})
        if not relevant:
            await route.fallback()
            return
        try:
            initiator_matches = self.frame is not None and request.frame is self.frame and request.frame.page is self.page
        except Exception:
            initiator_matches = False
        if self.finished or self.used or not self.worker_free() or not initiator_matches or not self.matches(request):
            await route.abort("blockedbyclient")
            if (request.url == self.destination or request.method not in {"GET", "HEAD", "OPTIONS"}) and not self.outcome.done():
                self.outcome.set_result({"blocked": True})
            elif self.active and request.method in {"GET", "HEAD", "OPTIONS"}:
                event = getattr(self.manager.action_gateway.checkpoint_store, "event", lambda *args, **kwargs: None)
                event("submission_background_read_blocked", method=request.method)
            return
        if self.manager.guard_policy.before_decision is not None:
            await self.manager.guard_policy.before_decision()
        if self.used or self.finished or not self.worker_free() or getattr(self.manager, "_closing", False) or self.manager.guard_policy.policy_revision != self.authorized_revision:
            await route.abort("blockedbyclient")
            if not self.outcome.done():
                self.outcome.set_result({"blocked": True})
            return
        self.used = True
        event = getattr(self.manager.action_gateway.checkpoint_store, "event", lambda *args, **kwargs: None)
        try:
            event("submission_dispatch_attempt", destination=self.destination, method=self.method)
            # Chromium follows redirects without invoking Playwright's routing
            # handler again. Fetch the one reviewed request with redirects and
            # transport retries disabled, then fulfill only a direct response.
            response = await route.fetch(max_redirects=0, max_retries=0, timeout=self.timeout * 1000)
            try:
                status = response.status
                event("submission_response_observed", status=status, business_outcome="unverified")
                if 300 <= status < 400:
                    await route.abort("blockedbyclient")
                    event("submission_redirect_blocked", status=status, business_outcome="unknown")
                    if not self.outcome.done():
                        self.outcome.set_result({"redirect": True, "status": status})
                else:
                    await route.fulfill(response=response)
                    if not self.outcome.done():
                        self.outcome.set_result({"status": status})
            finally:
                await response.dispose()
        except BaseException:
            with contextlib.suppress(Exception):
                await route.abort("blockedbyclient")
            if not self.outcome.done():
                self.outcome.set_result({"blocked": True})
            raise

    async def execute(self, callback: Any) -> Any:
        if self.frame is None:
            raise AciraActionBlocked("Submission contracts require an exact reviewed frame")
        if not self.worker_free():
            raise AciraActionBlocked("Critical submissions require a worker-free Playwright context; use a fresh context without workers")
        if not getattr(self.page.context, "_acira_service_workers_blocked", False):
            raise AciraActionBlocked("Submission contracts require a guard-created context with service_workers='block'")
        context = self.page.context
        if any(getattr(page._impl_obj, "_routes", []) for page in context.pages):
            raise AciraActionBlocked("Remove page-level routing before using a critical submission contract")
        if any(OWNERS.get(page) is not self.manager for page in context.pages):
            raise AciraActionBlocked("Use a browser context owned exclusively by this Acira run for submission contracts")
        if getattr(context, "_acira_submission_active", False):
            raise AciraActionBlocked("Only one submission operation may run in a browser context at a time")
        if len(self.manager._submission_routes) >= 1000:
            raise AciraActionBlocked("Submission endpoint budget exhausted; start a fresh Acira run")
        context._acira_submission_locked = self.manager
        await trusted(context, "route", "**/*", self.route)
        self.manager._submission_routes.append((context, self.route))
        context._acira_submission_active = True
        self.authorized_revision = self.manager.guard_policy.policy_revision
        self.active = True
        try:
            result = await callback()
            outcome = await asyncio.wait_for(asyncio.shield(self.outcome), self.timeout)
            if outcome.get("redirect"):
                raise AciraActionBlocked("The reviewed request returned a redirect; the unreviewed destination was blocked. Its first attempt may have taken effect; do not retry automatically")
            if outcome.get("blocked"):
                if self.used:
                    raise AciraActionBlocked("Submission outcome is unknown after its dispatch attempt; do not retry automatically")
                raise AciraActionBlocked("The actual submission differed from the reviewed request; it was not sent")
            return result
        except asyncio.TimeoutError as exc:
            raise AciraActionBlocked("Submission outcome is unknown; its endpoint remains blocked against automatic retries") from exc
        finally:
            context._acira_submission_active = False
            self.active = False
            self.finished = True


async def submit(manager: Any, locator: Any, *, destination: str, values: dict[str, Any], encoding: str = "json",
                 method: str = "POST", facts: dict[str, str] | None = None, timeout: float = 5) -> None:
    if OWNERS.get(locator.page) is not manager or await locator.count() != 1:
        raise ValueError("Resolve one submission control on a page protected by this run")
    contract = Submission(manager, locator.page, destination, values, encoding, method.upper(), facts or {}, timeout)
    token = SUBMISSION.set(contract)
    try:
        await locator.click()
    finally:
        SUBMISSION.reset(token)
