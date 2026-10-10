from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Callable
from .playwright_boundary import trusted

from acira_cdp_proxy.action_gateway import ActionGateway, ActionRequest
from acira_cdp_proxy.checkpoint import CheckpointStore
from acira_cdp_proxy.config import AciraGuardConfig
from acira_cdp_proxy.framework_adapters import ActionCapability, FrameworkAdapterSpec
from acira_cdp_proxy.guard import (
    AciraGuardPolicy,
    GuardTarget,
    PAGE_TARGETS_PROBE_JS,
    RUNTIME_OBJECT_PROBE_JS,
    _target_from_probe_value,
)
from acira_cdp_proxy.logger import CDPLogger, NullCDPLogger
from acira_cdp_proxy.policy import AGENT_TO_BROWSER, CDPMessageContext, CDPPolicy
from acira_cdp_proxy.server import AciraCDPProxy
from acira_cdp_proxy.startup import print_skill_activation_summary


PLAYWRIGHT_FRAMEWORK_ID = "playwright"
PLAYWRIGHT_CDP_FOLLOWUP_ACTIONS = {
    "click",
    "form_input",
    "select",
    "submit",
    "check",
    "file_upload",
}
PLAYWRIGHT_GENERAL_PREFLIGHT_CACHE_TTL_SECONDS = 300.0
PLAYWRIGHT_PAGE_TARGET_PROBE_CACHE_TTL_SECONDS = 1.5
PLAYWRIGHT_MAPPING_PREFLIGHT_CACHE_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class PlaywrightAdapter:
    """Declarative spec contribution for the Playwright framework adapter."""

    def spec(self) -> FrameworkAdapterSpec:
        return FrameworkAdapterSpec(
            id=PLAYWRIGHT_FRAMEWORK_ID,
            display_name="Playwright",
            ignored_selectors=(),
            runtime_helpers=(),
            default_trusted_helpers=(),
            action_capabilities={
                action: ActionCapability(
                    opens_cdp_followup_transaction=action in PLAYWRIGHT_CDP_FOLLOWUP_ACTIONS,
                    release_pause_on_review_exit=True,
                    supervised_runtime=True,
                )
                for action in {
                    "click",
                    "form_input",
                    "select",
                    "submit",
                    "check",
                    "file_upload",
                    "keyboard",
                    "navigation",
                    "mapping_preflight",
                    "meaningful_action",
                }
            },
        )


@dataclass(frozen=True)
class _PageTargetProbeCacheEntry:
    targets: list[dict[str, Any]]
    metadata: dict[str, Any]
    expires_at: float


@dataclass(frozen=True)
class _PagePreflightCacheEntry:
    expires_at: float


_ACTIVE_PLAYWRIGHT_PROXIES: list["PlaywrightCDPProxy"] = []


class PlaywrightGuardBlockedError(RuntimeError):
    """Raised when Acira blocks a Playwright action."""

    def __init__(self, reason: str, *, metadata: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.metadata = metadata or {}


class PlaywrightCDPProxy:
    """Async context manager that patches Playwright BrowserType and Page for HITL protection.

    Usage::

        async with async_playwright() as p:
            async with acira.protect(config="configs/acira.default.json") as guard:
                guard.patch_playwright(p)
                browser = await p.chromium.launch(headless=False)
                page = await browser.new_page()
                await page.goto("https://example.com")
                await page.click("#submit")  # <- paused for review

    Local and connect_over_cdp browsers use the same public action boundaries.
    Agent-created raw CDP sessions permit only the explicit observation subset.
    """

    def __init__(
        self,
        *,
        log_file: Path | str | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        config: AciraGuardConfig | dict[str, Any] | None = None,
        policy: CDPPolicy | None = None,
        action_gateway: ActionGateway | None = None,
        checkpoint_store: CheckpointStore | None = None,
        review_backend: Any | None = None,
        runtime_control: Any | None = None,
        adapter: PlaywrightAdapter | None = None,
    ) -> None:
        self.adapter = adapter or PlaywrightAdapter()
        self.adapter_spec = self.adapter.spec()
        adapter_specs = (self.adapter_spec,)
        self.logger = CDPLogger(log_file) if log_file is not None else NullCDPLogger()
        self.host = host
        self.port = port
        gateway_policy = getattr(action_gateway, "policy", None) if action_gateway is not None else None
        # Strip adapter configs not registered with this proxy (e.g. browser_use entries
        # in acira.default.json) so FrameworkAdapterRegistry validation passes.
        _policy_config = _filter_adapter_config(config, adapter_specs)
        self.guard_policy = (
            policy
            if isinstance(policy, AciraGuardPolicy)
            else gateway_policy
            if isinstance(gateway_policy, AciraGuardPolicy)
            else AciraGuardPolicy(_policy_config, review_backend=review_backend, adapters=adapter_specs)
        )
        self.policy = policy or self.guard_policy
        self.action_gateway = action_gateway or ActionGateway(
            _policy_config,
            policy=self.guard_policy,
            checkpoint_store=checkpoint_store,
            adapters=adapter_specs,
        )
        if review_backend is not None and hasattr(review_backend, "bind_run_id"):
            review_backend.bind_run_id(self.action_gateway.run_id)
        if runtime_control is not None and hasattr(runtime_control, "bind_run_id"):
            runtime_control.bind_run_id(self.action_gateway.run_id)
        self.runtime_control = runtime_control
        # (object, attr_name, original_value) — for cleanup
        self._patched_attrs: list[tuple[Any, str, Any]] = []
        self._cdp_proxy_servers: list[AciraCDPProxy] = []
        # Launched browsers whose lifecycle we manage (from launch() wrappers)
        self._launched_browsers: list[Any] = []
        self._page_target_probe_cache: dict[int, _PageTargetProbeCacheEntry] = {}
        self._page_preflight_cache: dict[int, _PagePreflightCacheEntry] = {}
        self._page_mapping_preflight_cache: dict[int, _PagePreflightCacheEntry] = {}
        self._cdp_session_cache: dict[int, Any] = {}
        self._contexts: list[Any] = []
        self._submission_routes: list[Any] = []
        self._closing = False

    async def __aenter__(self) -> "PlaywrightCDPProxy":
        from acira_cdp_proxy.redaction import SECRET_REDACTION_REGISTRY
        if self in _ACTIVE_PLAYWRIGHT_PROXIES:
            return self
        self._redaction_lease = SECRET_REDACTION_REGISTRY.begin_run()
        _ACTIVE_PLAYWRIGHT_PROXIES.append(self)
        try:
            print_skill_activation_summary(self.guard_policy.config, framework=PLAYWRIGHT_FRAMEWORK_ID)
            if self.runtime_control is not None and hasattr(self.runtime_control, "start"):
                result = self.runtime_control.start()
                if inspect.isawaitable(result):
                    await result
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        from acira_cdp_proxy.redaction import SECRET_REDACTION_REGISTRY
        try:
            await self._close_context(exc_type, exc, traceback)
        finally:
            SECRET_REDACTION_REGISTRY.end_run(getattr(self, "_redaction_lease", None))
            self._redaction_lease = None

    async def _close_context(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._closing = True
        if self in _ACTIVE_PLAYWRIGHT_PROXIES:
            _ACTIVE_PLAYWRIGHT_PROXIES.remove(self)

        for context in self._contexts:
            with contextlib.suppress(Exception):
                context.remove_listener("page", self._patch_page)
                del context._acira_registered
        self._contexts.clear()
        for page, handler in self._submission_routes:
            # Unrouting first can release the browser's original request while
            # the old callback also performs its fetch. Abort and retire that
            # contract before removing interception.
            await handler.__self__.close()
            with contextlib.suppress(Exception):
                from .playwright_boundary import trusted
                await trusted(page, "unroute", "**/*", handler)
            if getattr(page, "_acira_submission_locked", None) is self:
                del page._acira_submission_locked
        self._submission_routes.clear()

        for obj, attr, original in reversed(self._patched_attrs):
            with contextlib.suppress(Exception):
                setattr(obj, attr, original)
        self._patched_attrs.clear()

        for server in self._cdp_proxy_servers:
            with contextlib.suppress(Exception):
                await server.stop()
        self._cdp_proxy_servers.clear()

        for browser in self._launched_browsers:
            with contextlib.suppress(Exception):
                await browser.close()
        self._launched_browsers.clear()

        self._page_target_probe_cache.clear()
        self._page_preflight_cache.clear()
        self._page_mapping_preflight_cache.clear()
        self._cdp_session_cache.clear()
        from .playwright_boundary import detach
        detach(self)

        if self.runtime_control is not None and hasattr(self.runtime_control, "close"):
            result = self.runtime_control.close(reason="Playwright Acira context closed")
            if inspect.isawaitable(result):
                await result

    # ------------------------------------------------------------------ #
    # Public patching helpers
    # ------------------------------------------------------------------ #

    def patch_playwright(self, playwright: Any) -> None:
        """Patch all browser types (chromium, firefox, webkit) on a Playwright instance.

        Call this after entering the context manager but before launching/connecting.
        """
        if not inspect.iscoroutinefunction(playwright.chromium.launch):
            raise ValueError("Acira requires Playwright's asynchronous API")
        for bt_name in ("chromium", "firefox", "webkit"):
            bt = getattr(playwright, bt_name, None)
            if bt is not None:
                self._patch_browser_type(bt)

    def patch_page(self, page: Any) -> None:
        """Directly patch an already-created Page object.

        Use when you bypass launch()/connect_over_cdp() and create pages
        through your own factory.
        """
        self._patch_page(page)

    async def fill_fields(self, fields: list[tuple[Any, str]], *, purpose: str = "Fill form fields") -> None:
        """Review a bounded set of text-field writes together; never submits."""
        from .playwright_operations import fill_fields
        await fill_fields(self, fields, purpose=purpose)

    async def submit(self, locator: Any, **contract: Any) -> None:
        """Review and permit one exact HTTP request caused by this control."""
        from .playwright_submission import submit
        await submit(self, locator, **contract)

    # ------------------------------------------------------------------ #
    # BrowserType patching
    # ------------------------------------------------------------------ #

    def _patch_browser_type(self, bt: Any) -> None:
        manager = self

        if getattr(bt, "name", "chromium") != "chromium":
            async def unsupported(**kwargs: Any) -> Any:
                raise ValueError("Acira supports asynchronous Chromium integrations; this browser type is unsupported")
            for attr in ("launch", "launch_persistent_context", "connect"):
                if hasattr(bt, attr):
                    self._patched_attrs.append((bt, attr, getattr(bt, attr)))
                    setattr(bt, attr, unsupported)
            return

        original_launch = bt.launch

        async def launch_with_acira(**kwargs: Any) -> Any:
            return await manager._launch_with_proxy(original_launch, **kwargs)

        bt.launch = launch_with_acira
        self._patched_attrs.append((bt, "launch", original_launch))

        original_persistent = getattr(bt, "launch_persistent_context", None)
        if original_persistent is not None:
            async def persistent_with_acira(user_data_dir: Any, **kwargs: Any) -> Any:
                kwargs.setdefault("service_workers", "block")
                context = await original_persistent(user_data_dir, **kwargs)
                context._acira_service_workers_blocked = kwargs["service_workers"] == "block"
                manager._patch_context(context)
                return context
            bt.launch_persistent_context = persistent_with_acira
            self._patched_attrs.append((bt, "launch_persistent_context", original_persistent))

        original_connect = getattr(bt, "connect", None)
        if original_connect is not None:
            async def connect_with_acira(ws_endpoint: str, **kwargs: Any) -> Any:
                browser = await original_connect(ws_endpoint, **kwargs)
                manager._patch_browser(browser)
                return browser
            bt.connect = connect_with_acira
            self._patched_attrs.append((bt, "connect", original_connect))

        original_connect_cdp = getattr(bt, "connect_over_cdp", None)
        if original_connect_cdp is not None:

            async def connect_over_cdp_with_acira(endpoint_url: str, **kwargs: Any) -> Any:
                return await manager._connect_over_cdp_with_proxy(original_connect_cdp, endpoint_url, **kwargs)

            bt.connect_over_cdp = connect_over_cdp_with_acira
            self._patched_attrs.append((bt, "connect_over_cdp", original_connect_cdp))

    async def _launch_with_proxy(self, original_launch: Any, **kwargs: Any) -> Any:
        """Launch the browser and patch returned browser/pages for HITL.

        For locally launched browsers, the CDP network proxy is not injected
        (we can't obtain the raw CDP HTTP endpoint from Playwright's abstraction).
        Supported Page/Locator/Frame actions are guarded by public API boundaries.
        Unsupported mutation paths are rejected; this is not a browser sandbox.
        """
        browser = await original_launch(**kwargs)
        self._patch_browser(browser)
        return browser

    async def _connect_over_cdp_with_proxy(
        self,
        original_connect: Any,
        endpoint_url: str,
        **kwargs: Any,
    ) -> Any:
        """Guard the connected browser through the same boundary as local launches.

        Playwright's internal selector engine uses arbitrary Runtime scripts.
        Running those through the generic proxy either blocks observations or
        requires an overly broad script exception. Public mutations instead
        resolve and authorize exact targets; exposed raw CDP mutations are denied.
        """
        browser = await original_connect(endpoint_url, **kwargs)
        self._patch_browser(browser)
        return browser

    # ------------------------------------------------------------------ #
    # Browser / Context / Page patching
    # ------------------------------------------------------------------ #

    def _patch_browser(self, browser: Any) -> None:
        manager = self
        from .playwright_boundary import patch_cdp_factory
        patch_cdp_factory(self, browser, "new_browser_cdp_session")
        for context in browser.contexts:
            manager._patch_context(context)

        original_new_context = getattr(browser, "new_context", None)
        if original_new_context is not None:
            async def new_context_with_acira(**kwargs: Any) -> Any:
                kwargs.setdefault("service_workers", "block")
                ctx = await original_new_context(**kwargs)
                ctx._acira_service_workers_blocked = kwargs["service_workers"] == "block"
                manager._patch_context(ctx)
                return ctx
            self._patched_attrs.append((browser, "new_context", original_new_context))
            browser.new_context = new_context_with_acira

        original_new_page = getattr(browser, "new_page", None)
        if original_new_page is not None:
            async def new_page_with_acira(**kwargs: Any) -> Any:
                kwargs.setdefault("service_workers", "block")
                page = await original_new_page(**kwargs)
                page.context._acira_service_workers_blocked = kwargs["service_workers"] == "block"
                manager._patch_context(page.context)
                manager._patch_page(page)
                return page
            self._patched_attrs.append((browser, "new_page", original_new_page))
            browser.new_page = new_page_with_acira

    def _patch_context(self, context: Any) -> None:
        manager = self
        registered = getattr(context, "_acira_registered", None)
        if registered is not None:
            if registered is not self:
                raise ValueError("This browser context belongs to another Acira run")
            return
        context._acira_registered = self
        self._contexts.append(context)
        context.on("page", manager._patch_page)
        from .playwright_boundary import patch_cdp_factory
        patch_cdp_factory(self, context, "new_cdp_session")
        # Context-wide injected JavaScript otherwise bypasses every page wrapper.
        for name in ("add_init_script",):
            saved = getattr(context, name)
            async def unsupported(*args: Any, **kwargs: Any) -> Any:
                from acira_cdp_proxy.action_gateway import AciraActionBlocked
                raise AciraActionBlocked("Context script injection has no verified action contract")
            self._patched_attrs.append((context, name, saved))
            setattr(context, name, unsupported)
        for existing_page in context.pages:
            manager._patch_page(existing_page)
        original_new_page = context.new_page

        async def new_page_with_acira(**kwargs: Any) -> Any:
            page = await original_new_page(**kwargs)
            manager._patch_page(page)
            return page

        self._patched_attrs.append((context, "new_page", original_new_page))
        context.new_page = new_page_with_acira

    def _patch_page(self, page: Any) -> None:
        from .playwright_boundary import attach
        attach(self, page)

    # ------------------------------------------------------------------ #
    # Core guard logic
    # ------------------------------------------------------------------ #

    async def _guard_page_action(
        self,
        page: Any,
        action: str,
        selector: str | None,
        execute_fn: Callable[[], Any],
        *,
        details: dict[str, Any],
        reason: str,
        read_only: bool = False,
    ) -> Any:
        if action != "navigation":
            await self._ensure_page_general_preflight(page, trigger="before_action")
            await self._ensure_page_mapping_preflight(page)

        target = None
        target_resolved: bool | None = None
        target_resolution_source: str | None = None
        target_resolution_confidence: float | None = None

        if selector is not None and action != "navigation":
            target, target_resolved, target_resolution_source, target_resolution_confidence = (
                await self._resolve_target(page, selector, action=action)
            )

        cap = self.adapter_spec.action_capabilities.get(action)
        request = ActionRequest(
            action=action,
            framework=PLAYWRIGHT_FRAMEWORK_ID,
            target=target,
            details=details,
            reason=reason,
            confidence=0.82,
            read_only=read_only,
            meaningful=True,
            opens_cdp_followup_transaction=cap.opens_cdp_followup_transaction if cap else False,
            release_pause_on_review_exit=cap.release_pause_on_review_exit if cap else False,
            supervised_runtime=cap.supervised_runtime if cap else False,
            target_resolved=target_resolved,
            target_resolution_source=target_resolution_source,
            target_resolution_confidence=target_resolution_confidence,
            adapter_handles_page_preflight=True,
            send_probe=await self._make_page_probe(page),
        )

        result = await self.action_gateway.guard_execute(
            request,
            execute_fn,
            blocked_result=_playwright_blocked_result,
        )

        if action == "navigation":
            self._invalidate_page_probe_cache(page)
            await self._ensure_page_general_preflight(page, trigger="after_navigation")
            await self._ensure_page_mapping_preflight(page)

        return result

    # ------------------------------------------------------------------ #
    # Target resolution via CDP
    # ------------------------------------------------------------------ #

    async def _resolve_target(
        self,
        page: Any,
        selector: str,
        *,
        action: str,
    ) -> tuple[GuardTarget | None, bool | None, str | None, float | None]:
        timeout = float(getattr(self.guard_policy.config, "probe_timeout", 1.0) or 1.0)
        try:
            cdp_session = await page.context.new_cdp_session(page)
        except Exception:
            return _fallback_target(selector), False, "playwright.selector", 0.35

        try:
            doc = await asyncio.wait_for(
                trusted(cdp_session, "send", "DOM.getDocument", {"depth": 0}),
                timeout=timeout,
            )
            root_id = doc["root"]["nodeId"]

            query = await asyncio.wait_for(
                trusted(cdp_session, "send", "DOM.querySelector", {"nodeId": root_id, "selector": selector}),
                timeout=timeout,
            )
            node_id = query.get("nodeId") or 0
            if not node_id:
                return _fallback_target(selector), False, "playwright.selector", 0.35

            resolved = await asyncio.wait_for(
                trusted(cdp_session, "send", "DOM.resolveNode", {"nodeId": node_id}),
                timeout=timeout,
            )
            object_id = (resolved.get("object") or {}).get("objectId")
            if not object_id:
                return _fallback_target(selector), False, "playwright.selector.node", 0.4

            probe_result = await asyncio.wait_for(
                trusted(cdp_session, "send", "Runtime.callFunctionOn", {
                    "functionDeclaration": RUNTIME_OBJECT_PROBE_JS,
                    "objectId": object_id,
                    "returnByValue": True,
                    "awaitPromise": False,
                }),
                timeout=timeout,
            )
            probe_value = (probe_result.get("result") or {}).get("value")
            target = _target_from_probe_value(probe_value)
            if target is not None:
                return target, _target_is_specific(target), "playwright.cdp.probe", target.confidence

            return _fallback_target(selector), False, "playwright.selector", 0.35

        except Exception:
            return _fallback_target(selector), False, "playwright.selector.error", 0.2
        finally:
            with contextlib.suppress(Exception):
                await cdp_session.detach()

    # ------------------------------------------------------------------ #
    # Page target inventory (preflight)
    # ------------------------------------------------------------------ #

    def _invalidate_page_probe_cache(self, page: Any) -> None:
        page_id = id(page)
        self._page_target_probe_cache.pop(page_id, None)
        self._page_preflight_cache.pop(page_id, None)
        self._page_mapping_preflight_cache.pop(page_id, None)
        self._cdp_session_cache.pop(page_id, None)

    async def _get_or_create_cdp_session(self, page: Any) -> Any | None:
        page_id = id(page)
        cached = self._cdp_session_cache.get(page_id)
        if cached is not None:
            return cached
        try:
            session = await page.context.new_cdp_session(page)
            self._cdp_session_cache[page_id] = session
            return session
        except Exception:
            return None

    async def _make_page_probe(self, page: Any) -> Any:
        session = await self._get_or_create_cdp_session(page)
        if session is None:
            return None

        async def _probe(method: str, params: dict[str, Any] | None = None, session_id: str | None = None) -> dict[str, Any] | None:
            try:
                return await trusted(session, "send", method, params or {})
            except Exception:
                return None

        return _probe

    async def _probe_page_targets(self, page: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        page_id = id(page)
        now = time.monotonic()
        cached = self._page_target_probe_cache.get(page_id)
        if cached is not None and cached.expires_at > now:
            return cached.targets, cached.metadata

        try:
            raw = await asyncio.wait_for(trusted(page, "evaluate", PAGE_TARGETS_PROBE_JS), timeout=2.0)
            if isinstance(raw, dict):
                targets: list[dict[str, Any]] = raw.get("targets") or []
                metadata: dict[str, Any] = {k: v for k, v in raw.items() if k != "targets"}
            elif isinstance(raw, list):
                targets = raw
                metadata = {"status": "ok"}
            else:
                targets = []
                metadata = {"status": "unexpected_result", "raw_type": type(raw).__name__}
        except Exception as exc:
            targets = []
            metadata = {"status": "error", "error": str(exc)}

        self._page_target_probe_cache[page_id] = _PageTargetProbeCacheEntry(
            targets=targets,
            metadata=metadata,
            expires_at=now + PLAYWRIGHT_PAGE_TARGET_PROBE_CACHE_TTL_SECONDS,
        )
        return targets, metadata

    async def _ensure_page_mapping_preflight(self, page: Any) -> None:
        config = self.guard_policy.config
        if not config.specific_skills or not config.has_page_policy:
            return

        page_id = id(page)
        now = time.monotonic()
        cached = self._page_mapping_preflight_cache.get(page_id)
        if cached is not None and cached.expires_at > now:
            return
        self._page_mapping_preflight_cache.pop(page_id, None)

        targets, metadata = await self._probe_page_targets(page)
        if metadata.get("status") != "ok":
            return

        details: dict[str, Any] = {
            "event_type": "PageMappingPreflight",
            "page_targets": targets,
            "page_target_probe": metadata,
            "mapping_preflight": "page",
        }
        await self.action_gateway.guard_execute(
            ActionRequest(
                action="mapping_preflight",
                framework=PLAYWRIGHT_FRAMEWORK_ID,
                target=None,
                details=details,
                reason="Playwright page mapping preflight",
                confidence=0.99,
                meaningful=True,
                release_pause_on_review_exit=True,
                supervised_runtime=True,
            ),
            lambda: {"mapping_preflight": "ok"},
            blocked_result=_playwright_blocked_result,
        )
        self._page_mapping_preflight_cache[page_id] = _PagePreflightCacheEntry(
            expires_at=now + PLAYWRIGHT_MAPPING_PREFLIGHT_CACHE_TTL_SECONDS,
        )

    async def _ensure_page_general_preflight(self, page: Any, *, trigger: str) -> None:
        config = self.guard_policy.config
        if not config.general_skill_preflight.enabled or not config.general_skills or not config.has_page_policy:
            return

        page_id = id(page)
        now = time.monotonic()
        cached = self._page_preflight_cache.get(page_id)
        if cached is not None and cached.expires_at > now:
            return
        self._page_preflight_cache.pop(page_id, None)

        targets, metadata = await self._probe_page_targets(page)
        if metadata.get("status") != "ok":
            if self.guard_policy.config.terminal_log:
                self.guard_policy._terminal_preflight_status(  # noqa: SLF001
                    scope_key=f"playwright:{id(page)}",
                    trigger=f"playwright_{trigger}",
                    status=str(metadata.get("status") or "probe_failed"),
                )
            return

        try:
            url = page.url
        except Exception:
            url = ""

        snapshot = self.guard_policy.evaluate_page_preflight_targets(
            targets,
            dict(metadata),
            scope_key=f"playwright:{id(page)}",
            readiness={
                "strategy": "playwright_page_probe",
                "trigger": trigger,
                "url": url,
                "status": metadata.get("status"),
                "candidate_count": metadata.get("candidate_count"),
            },
        )
        context = CDPMessageContext(
            direction=AGENT_TO_BROWSER,
            connection_id="playwright-page-preflight",
            message_number=0,
            websocket_path=f"/acira/action/{PLAYWRIGHT_FRAMEWORK_ID}",
            parsed_message={"method": "Acira.preflight", "params": snapshot.to_metadata()},
            send_probe=await self._make_page_probe(page),
        )
        decision = await self.guard_policy.enforce_page_preflight_snapshot(
            snapshot,
            context,
            trigger=f"playwright_{trigger}",
        )
        if not decision.allow:
            reason = decision.reason or "Blocked by Acira page preflight."
            metadata_out = decision.metadata or {}
            if self.guard_policy.config.terminal_log:
                print(
                    f"[Acira Guard] PLAYWRIGHT_PAGE_PREFLIGHT_BLOCKED trigger={trigger} reason={reason}",
                    file=sys.stderr,
                    flush=True,
                )
            raise PlaywrightGuardBlockedError(reason, metadata=metadata_out)

        self._page_preflight_cache[page_id] = _PagePreflightCacheEntry(
            expires_at=now + PLAYWRIGHT_GENERAL_PREFLIGHT_CACHE_TTL_SECONDS,
        )


# ------------------------------------------------------------------ #
# Module-level helpers
# ------------------------------------------------------------------ #

def _playwright_blocked_result(reason: str, metadata: dict[str, Any]) -> Any:
    raise PlaywrightGuardBlockedError(reason, metadata=metadata)


def _target_is_specific(target: GuardTarget) -> bool:
    return bool(
        target.id
        or target.text
        or target.aria_label
        or target.name
        or target.href
    )


def _fallback_target(selector: str) -> GuardTarget:
    sel_id = _extract_id_from_selector(selector)
    sel_text = _extract_text_from_selector(selector)
    return GuardTarget(
        id=sel_id,
        text=sel_text,
        confidence=0.35,
        source="playwright.selector",
    )


def _extract_id_from_selector(selector: str) -> str | None:
    m = re.search(r"#([\w-]+)", selector)
    if m:
        return m.group(1)
    m = re.search(r'\[id=["\']?([\w-]+)["\']?\]', selector)
    if m:
        return m.group(1)
    return None


def _extract_text_from_selector(selector: str) -> str | None:
    if selector.startswith("text="):
        return selector[5:].strip("'\"")
    m = re.search(r':(?:text|has-text)\(["\']([^"\']+)["\']', selector)
    if m:
        return m.group(1)
    return None


def _filter_adapter_config(
    config: AciraGuardConfig | dict[str, Any] | None,
    adapter_specs: tuple[FrameworkAdapterSpec, ...],
) -> AciraGuardConfig | dict[str, Any] | None:
    """Return config with adapter entries restricted to registered specs only."""
    if config is None:
        return None
    registered = {spec.id for spec in adapter_specs}
    if isinstance(config, AciraGuardConfig):
        unknown = set(config.adapters) - registered
        if not unknown:
            return config
        return dataclasses.replace(
            config,
            adapters={k: v for k, v in config.adapters.items() if k in registered},
        )
    if isinstance(config, dict):
        adapters = config.get("adapters")
        if not isinstance(adapters, dict):
            return config
        unknown = set(adapters) - registered
        if not unknown:
            return config
        return {**config, "adapters": {k: v for k, v in adapters.items() if k in registered}}
    return config


def _safe_details(kwargs: dict[str, Any]) -> dict[str, Any]:
    SKIP = {"timeout", "trial", "no_wait_after", "strict", "force"}
    return {
        k: v
        for k, v in kwargs.items()
        if k not in SKIP and isinstance(v, (str, int, float, bool, type(None)))
    }


# ------------------------------------------------------------------ #
# Public factory functions
# ------------------------------------------------------------------ #

def playwright(
    *,
    config: AciraGuardConfig | dict[str, Any] | str | Path | None = None,
    log_file: str | Path | None = None,
    checkpoint_store: CheckpointStore | None = None,
    review_backend: Any | None = None,
    runtime_control: Any | None = None,
) -> PlaywrightCDPProxy:
    resolved_config = AciraGuardConfig.from_file(config) if isinstance(config, (str, Path)) else config
    return PlaywrightCDPProxy(
        config=resolved_config,
        log_file=log_file,
        checkpoint_store=checkpoint_store,
        review_backend=review_backend,
        runtime_control=runtime_control,
    )


def protect(
    *,
    config: AciraGuardConfig | dict[str, Any] | str | Path | None = None,
    log_file: str | Path | None = None,
    checkpoint_store: CheckpointStore | None = None,
    review_backend: Any | None = None,
    runtime_control: Any | None = None,
) -> PlaywrightCDPProxy:
    return playwright(
        config=config,
        log_file=log_file,
        checkpoint_store=checkpoint_store,
        review_backend=review_backend,
        runtime_control=runtime_control,
    )


# Adapter contract factory: the core discovers adapters by importing the module and
# calling ``create`` (see acira/adapters/contract.py). The developer never calls this
# directly — they use ``acira.protect(config=...)``.
create = protect
