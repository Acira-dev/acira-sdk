from __future__ import annotations

from contextvars import ContextVar

import asyncio
import contextlib
import contextvars
import inspect
import json
import os
import re
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import TracebackType
from typing import Any, Callable
from urllib.parse import urlparse

from acira_cdp_proxy.action_gateway import ActionGateway, ActionRequest
from acira_cdp_proxy.checkpoint import CheckpointStore
from acira_cdp_proxy.config import AciraGuardConfig
from acira_cdp_proxy.framework_adapters import ActionCapability, FrameworkAdapterSpec, RuntimeHelperContext, RuntimeHelperSpec
from acira_cdp_proxy.guard import (
    AciraGuardPolicy,
    ActionClassification,
    GuardTarget,
    OVERLAY_DISALLOWED_PATTERNS,
    PAGE_TARGETS_PROBE_JS,
    RUNTIME_OBJECT_PROBE_JS,
    SENSITIVE_JS_PATTERNS,
    _normalize_js,
    _point_probe_js,
    _runtime_js_details,
    _runtime_target_hint_from_target,
    _safe_native_select_payload,
    _page_target_signature,
    _target_from_probe_value,
)
from acira_cdp_proxy.logger import CDPLogger, NullCDPLogger
from acira_cdp_proxy.pause import ACIRA_REVIEW_PAUSE, sleep_with_review_pause, wait_until_review_unpaused, wait_with_review_pause
from acira_cdp_proxy.policy import CDPMessageContext, CDPPolicy
from acira_cdp_proxy.redaction import SECRET_REDACTION_REGISTRY, redact_value
from acira_cdp_proxy.server import AciraCDPProxy, CDPProxyServer
from acira_cdp_proxy.startup import print_skill_activation_summary
from acira_cdp_proxy.secrets import SecretBroker, SecretBrokerError, SecretUseMetadata


_BROWSER_USE_AGENT_PATCH_REFCOUNT = 0
_BROWSER_USE_WATCHDOG_PATCH_REFCOUNT = 0
_ORIGINAL_BROWSER_USE_STEP: Callable[..., Any] | None = None
_ORIGINAL_BROWSER_USE_EXECUTE_STEP: Callable[..., Any] | None = None
_ORIGINAL_BROWSER_USE_GET_MODEL_OUTPUT: Callable[..., Any] | None = None
_ORIGINAL_BROWSER_USE_CHECK_STOP_OR_PAUSE: Callable[..., Any] | None = None
_ORIGINAL_BROWSER_USE_EXECUTE_ACTIONS: Callable[..., Any] | None = None
_ORIGINAL_BROWSER_USE_HANDLE_POST_LLM_PROCESSING: Callable[..., Any] | None = None
_ORIGINAL_BROWSER_USE_BROADCAST_MODEL_STATE: Callable[..., Any] | None = None
_ORIGINAL_BROWSER_USE_POST_PROCESS: Callable[..., Any] | None = None
_ORIGINAL_BROWSER_USE_FINALIZE: Callable[..., Any] | None = None
_ORIGINAL_BROWSER_USE_BROWSER_STATE_SUMMARY: Callable[..., Any] | None = None
_ORIGINAL_BROWSER_USE_RECONNECT: Callable[..., Any] | None = None
_ORIGINAL_BROWSER_USE_AUTO_RECONNECT: Callable[..., Any] | None = None
_ORIGINAL_BROWSER_USE_INPUT_TEXT_ELEMENT_NODE_IMPL: Callable[..., Any] | None = None
_ORIGINAL_BROWSER_USE_TRIGGER_FRAMEWORK_EVENTS: Callable[..., Any] | None = None
_ORIGINAL_BROWSER_USE_AGENT_ASYNCIO: Any | None = None
_BROWSER_USE_FREEZE_PATCH_REFCOUNT = 0
_ORIGINAL_BUBUS_EXECUTE_HANDLER: Callable[..., Any] | None = None
_ORIGINAL_BUBUS_EVENT_RESULTS_FILTERED: Callable[..., Any] | None = None
_ORIGINAL_BROWSER_USE_TOOLS_ACT: Callable[..., Any] | None = None
_ACTIVE_BROWSER_USE_PROXIES: list["BrowserUseCDPProxy"] = []
_CURRENT_BROWSER_USE_PROXY: ContextVar[Any] = ContextVar("acira_browser_use_proxy", default=None)
_BASE_BROWSER_SESSION_CONNECT: Any = None
_BASE_BROWSER_SESSION_KILL: Any = None
_BASE_BROWSER_SESSION_STOP: Any = None
_ACIRA_INTERNAL_SECRET_FILL: contextvars.ContextVar["_InternalSecretFillContext | None"] = contextvars.ContextVar(
    "acira_internal_secret_fill",
    default=None,
)
PAGE_TARGET_PROBE_CACHE_TTL_SECONDS = 1.5
BROWSER_USE_GENERAL_PREFLIGHT_CACHE_TTL_SECONDS = 300.0
BROWSER_USE_FRAMEWORK_ID = "browser_use"
ACIRA_SECRET_ACTION_NAME = "acira_secret_fill"
ACIRA_SECRET_CATALOG_ACTION_NAME = "acira_list_secrets"
BROWSER_USE_IGNORED_SELECTORS = (
    "[data-browser-use-highlight]",
    "[data-browser-use-interaction-highlight]",
    "[data-browser-use-coordinate-highlight]",
    "#browser-use-debug-highlights",
    ".browser-use-debug-highlights",
)
BROWSER_USE_OVERLAY_MARKERS = (
    "data-browser-use-highlight",
    "data-browser-use-interaction-highlight",
    "data-browser-use-coordinate-highlight",
    "browser-use-debug-highlights",
)


@dataclass(frozen=True)
class _InternalSecretFillContext:
    proxy_id: int
    browser_session_id: int
    event_id: int
    node_identity: tuple[tuple[str, str], ...]
    secret_id: str
    element_index: int


def _browser_use_node_identity(node: Any) -> tuple[tuple[str, str], ...]:
    if node is None:
        return ()

    parts: list[tuple[str, str]] = []
    for name in ("backend_node_id", "node_id", "target_id", "session_id", "frame_id", "xpath", "x_path"):
        value = getattr(node, name, None)
        if value is not None and str(value):
            parts.append((name, str(value)))

    attributes = getattr(node, "attributes", None)
    if isinstance(attributes, dict):
        for name in ("id", "name", "type", "placeholder", "aria-label"):
            value = attributes.get(name)
            if value is not None and str(value):
                parts.append((f"attr:{name}", str(value)))

    return tuple(parts)
BROWSER_USE_CDP_FOLLOWUP_ACTIONS = {
    "click",
    "form_input",
    "select",
    "submit",
    "check",
    "form_control_activation",
    "file_upload",
}
ACIRA_GUARD_OUTCOME_BLOCKED_CONTINUE = "ACIRA_GUARD_OUTCOME_BLOCKED_CONTINUE"


@dataclass
class _ProxyBinding:
    real_cdp_url: str
    server: CDPProxyServer


@dataclass(frozen=True)
class _PageTargetProbeResult:
    targets: list[dict[str, Any]]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class _PageTargetProbeCacheEntry:
    result: _PageTargetProbeResult
    expires_at: float


@dataclass(frozen=True)
class _PageMappingPreflightCacheEntry:
    expires_at: float


@dataclass(frozen=True)
class _GeneralPagePreflightCacheEntry:
    expires_at: float


@dataclass(frozen=True)
class BrowserUseAdapter:
    """Trusted first-party adapter contribution for browser-use."""

    def spec(self) -> FrameworkAdapterSpec:
        return FrameworkAdapterSpec(
            id=BROWSER_USE_FRAMEWORK_ID,
            display_name="BrowserUse",
            ignored_selectors=BROWSER_USE_IGNORED_SELECTORS,
            runtime_helpers=(
                RuntimeHelperSpec("overlay", _classify_browser_use_overlay_helper, requires_trust=True),
                RuntimeHelperSpec("setup_overlay", _classify_browser_use_setup_overlay_helper, requires_trust=True),
                RuntimeHelperSpec("safe_select", _classify_browser_use_safe_select_helper),
                RuntimeHelperSpec("safe_range", _classify_browser_use_safe_range_helper),
                RuntimeHelperSpec("text_mutation", _classify_browser_use_text_mutation_helper),
            ),
            default_trusted_helpers=("overlay", "setup_overlay"),
            action_capabilities={
                action: ActionCapability(
                    opens_cdp_followup_transaction=action in BROWSER_USE_CDP_FOLLOWUP_ACTIONS,
                    release_pause_on_review_exit=True,
                    supervised_runtime=True,
                )
                for action in {
                    "click",
                    "form_input",
                    "select",
                    "submit",
                    "check",
                    "form_control_activation",
                    "file_upload",
                    "keyboard",
                    "navigation",
                    "mapping_preflight",
                    "meaningful_action",
                }
            },
        )


class BrowserUseCDPProxy:
    """Patch browser-use's CDP connection and high-level actions for a context."""

    GUARDED_EVENT_TYPES = {
        "ClickElementEvent",
        "ClickCoordinateEvent",
        "TypeTextEvent",
        "SelectDropdownOptionEvent",
        "NavigateToUrlEvent",
        "SendKeysEvent",
        "UploadFileEvent",
    }

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
        hitl_event_timeout: float | None = 3600.0,
        hitl_freeze_mode: str | None = None,
        adapter: BrowserUseAdapter | None = None,
    ) -> None:
        self.adapter = adapter or BrowserUseAdapter()
        self.adapter_spec = self.adapter.spec()
        adapter_specs = (self.adapter_spec,)
        self.logger = CDPLogger(log_file) if log_file is not None else NullCDPLogger()
        self.host = host
        self.port = port
        gateway_policy = getattr(action_gateway, "policy", None) if action_gateway is not None else None
        self.guard_policy = (
            policy
            if isinstance(policy, AciraGuardPolicy)
            else gateway_policy
            if isinstance(gateway_policy, AciraGuardPolicy)
            else AciraGuardPolicy(config, review_backend=review_backend, adapters=adapter_specs)
        )
        self.policy = policy or self.guard_policy
        self.action_gateway = action_gateway or ActionGateway(
            config,
            policy=self.guard_policy,
            checkpoint_store=checkpoint_store,
            adapters=adapter_specs,
        )
        if review_backend is not None and hasattr(review_backend, "bind_run_id"):
            review_backend.bind_run_id(self.action_gateway.run_id)
        if runtime_control is not None and hasattr(runtime_control, "bind_run_id"):
            runtime_control.bind_run_id(self.action_gateway.run_id)
        self.runtime_control = runtime_control
        self.secret_broker = SecretBroker(self.guard_policy.config.secrets)
        self.hitl_event_timeout = hitl_event_timeout
        adapter_config = self.guard_policy.framework_adapters.adapter_config(self.adapter_spec.id)
        self.hitl_freeze_mode = _normalize_hitl_freeze_mode(
            hitl_freeze_mode or adapter_config.hitl_freeze_mode or self.guard_policy.config.hitl_freeze_mode
        )
        self._browser_session_class: Any | None = None
        self._original_connect: Callable[..., Any] | None = None
        self._watchdog_patch_active = False
        self._agent_step_patch_active = False
        self._freeze_patch_active = False
        self._bindings: dict[int, _ProxyBinding] = {}
        self._patched_event_buses: dict[int, tuple[Any, Callable[..., Any], dict[str, list[Callable[..., Any]]]]] = {}
        self._page_target_probe_cache: dict[tuple[int, str, str], _PageTargetProbeCacheEntry] = {}
        self._page_mapping_preflight_cache: dict[tuple[int, str, str, str], _PageMappingPreflightCacheEntry] = {}
        self._general_page_preflight_cache: dict[tuple[int, str], _GeneralPagePreflightCacheEntry] = {}
        self._upload_staging = contextlib.ExitStack()
        self._upload_bytes = 0
        self._upload_count = 0
        self._operation_channels = {}
        self._network_gates = {}
        self._session_operations: dict[int, set[asyncio.Task[Any]]] = {}
        self._stopping_sessions: set[int] = set()

    async def __aenter__(self) -> "BrowserUseCDPProxy":
        from acira.framework_compatibility import require_verified_framework
        require_verified_framework("browser_use")
        import os
        os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
        os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")
        from browser_use.browser.session import BrowserSession

        if self._original_connect is not None:
            return self

        global _BASE_BROWSER_SESSION_CONNECT, _BASE_BROWSER_SESSION_KILL, _BASE_BROWSER_SESSION_STOP
        from acira_cdp_proxy.pause import CURRENT_REVIEW_STATE
        self._context_token = _CURRENT_BROWSER_USE_PROXY.set(self)
        self._pause_context_token = CURRENT_REVIEW_STATE.set(self.guard_policy.review_pause)
        try:
            self._redaction_lease = SECRET_REDACTION_REGISTRY.begin_run()
            print_skill_activation_summary(self.guard_policy.config, framework=BROWSER_USE_FRAMEWORK_ID)
            if self.runtime_control is not None and hasattr(self.runtime_control, "start"):
                result = self.runtime_control.start()
                if inspect.isawaitable(result):
                    await result

            if self not in _ACTIVE_BROWSER_USE_PROXIES:
                _ACTIVE_BROWSER_USE_PROXIES.append(self)
            self._patch_browser_use_watchdog_handoff()
            self._patch_browser_use_hitl_freeze()
            self._browser_session_class = BrowserSession
            if _BASE_BROWSER_SESSION_CONNECT is None:
                _BASE_BROWSER_SESSION_CONNECT = BrowserSession.connect
            self._original_connect = _BASE_BROWSER_SESSION_CONNECT

            async def connect_with_acira_proxy(browser_session: Any, cdp_url: str | None = None) -> Any:
                manager = _active_browser_use_proxy()
                if manager is None:
                    raise RuntimeError("Create the browser-use session inside its Acira context; its owning run is ambiguous")
                return await manager._connect_with_proxy(browser_session, cdp_url)

            BrowserSession.connect = connect_with_acira_proxy  # type: ignore[method-assign]
            if _BASE_BROWSER_SESSION_KILL is None:
                _BASE_BROWSER_SESSION_KILL = BrowserSession.kill
                _BASE_BROWSER_SESSION_STOP = BrowserSession.stop

            async def stop_session(browser_session: Any, *, force: bool) -> Any:
                owner = next((proxy for proxy in _ACTIVE_BROWSER_USE_PROXIES
                              if id(browser_session) in proxy._bindings), None)
                if owner is not None:
                    await owner._cancel_session_operations(browser_session)
                original = _BASE_BROWSER_SESSION_KILL if force else _BASE_BROWSER_SESSION_STOP
                return await original(browser_session)

            async def kill_with_acira(browser_session: Any) -> Any:
                return await stop_session(browser_session, force=True)

            async def stop_with_acira(browser_session: Any) -> Any:
                return await stop_session(browser_session, force=False)

            BrowserSession.kill = kill_with_acira  # type: ignore[method-assign]
            BrowserSession.stop = stop_with_acira  # type: ignore[method-assign]
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
        self._stopping_sessions.update(self._bindings)
        operations = {task for tasks in self._session_operations.values() for task in tasks
                      if task is not asyncio.current_task()}
        for task in operations:
            task.cancel()
        if operations:
            await asyncio.wait(operations, timeout=1)
        if self in _ACTIVE_BROWSER_USE_PROXIES:
            _ACTIVE_BROWSER_USE_PROXIES.remove(self)
        self._restore_browser_use_hitl_freeze()
        self._restore_browser_use_watchdog_handoff()

        global _BASE_BROWSER_SESSION_CONNECT, _BASE_BROWSER_SESSION_KILL, _BASE_BROWSER_SESSION_STOP
        if self._browser_session_class is not None and self._original_connect is not None and not _ACTIVE_BROWSER_USE_PROXIES:
            self._browser_session_class.connect = _BASE_BROWSER_SESSION_CONNECT
            _BASE_BROWSER_SESSION_CONNECT = None
            self._browser_session_class.kill = _BASE_BROWSER_SESSION_KILL
            self._browser_session_class.stop = _BASE_BROWSER_SESSION_STOP
            _BASE_BROWSER_SESSION_KILL = _BASE_BROWSER_SESSION_STOP = None
        from acira_cdp_proxy.pause import CURRENT_REVIEW_STATE
        if getattr(self, "_context_token", None) is not None:
            _CURRENT_BROWSER_USE_PROXY.reset(self._context_token)
            self._context_token = None
        if getattr(self, "_pause_context_token", None) is not None:
            CURRENT_REVIEW_STATE.reset(self._pause_context_token)
            self._pause_context_token = None

        self._browser_session_class = None
        self._original_connect = None

        for event_bus, original_dispatch, original_handlers in self._patched_event_buses.values():
            event_bus.dispatch = original_dispatch
            for event_type, handlers in original_handlers.items():
                event_bus.handlers[event_type] = handlers
        self._patched_event_buses.clear()
        self._page_target_probe_cache.clear()
        self._page_mapping_preflight_cache.clear()
        self._general_page_preflight_cache.clear()
        for channel in self._operation_channels.values():
            await channel.close()
        self._operation_channels.clear()
        self._network_gates.clear()
        self._upload_staging.close()
        self._upload_bytes = self._upload_count = 0

        bindings = list(self._bindings.values())
        self._bindings.clear()

        for binding in bindings:
            await binding.server.stop()
        if self.runtime_control is not None and hasattr(self.runtime_control, "close"):
            result = self.runtime_control.close(reason="BrowserUse Acira context closed")
            if inspect.isawaitable(result):
                await result

    async def submit(self, browser_session: Any, node: Any, **contract: Any) -> None:
        """Review and send one exact JSON/form HTTP request from this DOM control."""
        from .browser_use_operations import submit
        await self._run_session_operation(browser_session, lambda: submit(self, browser_session, node, **contract))

    async def fill_fields(self, browser_session: Any, fields: list[tuple[Any, str]], *, purpose: str = "Fill form fields") -> None:
        from .browser_use_operations import fill_fields
        await self._run_session_operation(browser_session, lambda: fill_fields(self, browser_session, fields, purpose=purpose))

    async def _run_session_operation(self, browser_session: Any, operation: Callable[..., Any]) -> Any:
        from acira_cdp_proxy.action_gateway import AciraActionBlocked
        key = id(browser_session)
        if key in self._stopping_sessions:
            raise AciraActionBlocked("The browser session is stopping; no new operation was dispatched")
        task = asyncio.create_task(operation())
        pending = self._session_operations.setdefault(key, set())
        pending.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            if key in self._stopping_sessions and not asyncio.current_task().cancelling():
                raise AciraActionBlocked("Browser session stopped; the pending operation was cancelled without retry") from None
            raise
        finally:
            pending.discard(task)
            if not pending:
                self._session_operations.pop(key, None)

    async def _cancel_session_operations(self, browser_session: Any) -> None:
        # Clear reviews before browser-use tries to save state through its event
        # bus. Otherwise that lifecycle work waits on the review it must cancel.
        key = id(browser_session)
        self._stopping_sessions.add(key)
        tasks = tuple(self._session_operations.get(key, ()))
        for task in tasks:
            if task is not asyncio.current_task():
                task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=1)

    async def _browser_use_action_request_for_exact_element(self, browser_session: Any, element: Any, *, action: str) -> ActionRequest:
        return ActionRequest(action=action, framework=self.adapter_spec.id, target=element.target,
            reason="browser-use exact element operation", source="browser_use.operation", meaningful=True,
            target_resolved=True, target_resolution_source="browser_use.exact_backend_node", target_resolution_confidence=1.0,
            adapter_handles_page_preflight=True,
            send_probe=lambda method, params=None, session_id=None: element.channel.send(method, params))

    def _patch_browser_use_watchdog_handoff(self) -> None:
        global _BROWSER_USE_WATCHDOG_PATCH_REFCOUNT
        global _ORIGINAL_BROWSER_USE_INPUT_TEXT_ELEMENT_NODE_IMPL, _ORIGINAL_BROWSER_USE_TRIGGER_FRAMEWORK_EVENTS

        from browser_use.browser.watchdogs.default_action_watchdog import DefaultActionWatchdog

        if _BROWSER_USE_WATCHDOG_PATCH_REFCOUNT == 0:
            _ORIGINAL_BROWSER_USE_INPUT_TEXT_ELEMENT_NODE_IMPL = DefaultActionWatchdog._input_text_element_node_impl
            _ORIGINAL_BROWSER_USE_TRIGGER_FRAMEWORK_EVENTS = DefaultActionWatchdog._trigger_framework_events
            DefaultActionWatchdog._input_text_element_node_impl = _browser_use_input_text_impl_with_acira_live_node  # type: ignore[method-assign]
            DefaultActionWatchdog._trigger_framework_events = _browser_use_trigger_framework_events_with_acira_live_object  # type: ignore[method-assign]
        _BROWSER_USE_WATCHDOG_PATCH_REFCOUNT += 1
        self._watchdog_patch_active = True

    def _restore_browser_use_watchdog_handoff(self) -> None:
        global _BROWSER_USE_WATCHDOG_PATCH_REFCOUNT
        global _ORIGINAL_BROWSER_USE_INPUT_TEXT_ELEMENT_NODE_IMPL, _ORIGINAL_BROWSER_USE_TRIGGER_FRAMEWORK_EVENTS

        if not self._watchdog_patch_active:
            return
        self._watchdog_patch_active = False
        _BROWSER_USE_WATCHDOG_PATCH_REFCOUNT = max(0, _BROWSER_USE_WATCHDOG_PATCH_REFCOUNT - 1)
        if _BROWSER_USE_WATCHDOG_PATCH_REFCOUNT != 0:
            return

        from browser_use.browser.watchdogs.default_action_watchdog import DefaultActionWatchdog

        if _ORIGINAL_BROWSER_USE_INPUT_TEXT_ELEMENT_NODE_IMPL is not None:
            DefaultActionWatchdog._input_text_element_node_impl = _ORIGINAL_BROWSER_USE_INPUT_TEXT_ELEMENT_NODE_IMPL  # type: ignore[method-assign]
            _ORIGINAL_BROWSER_USE_INPUT_TEXT_ELEMENT_NODE_IMPL = None
        if _ORIGINAL_BROWSER_USE_TRIGGER_FRAMEWORK_EVENTS is not None:
            DefaultActionWatchdog._trigger_framework_events = _ORIGINAL_BROWSER_USE_TRIGGER_FRAMEWORK_EVENTS  # type: ignore[method-assign]
            _ORIGINAL_BROWSER_USE_TRIGGER_FRAMEWORK_EVENTS = None

    def _patch_browser_use_hitl_freeze(self) -> None:
        global _BROWSER_USE_AGENT_PATCH_REFCOUNT, _BROWSER_USE_FREEZE_PATCH_REFCOUNT
        global _ORIGINAL_BROWSER_USE_STEP, _ORIGINAL_BROWSER_USE_EXECUTE_STEP, _ORIGINAL_BROWSER_USE_GET_MODEL_OUTPUT
        global _ORIGINAL_BROWSER_USE_CHECK_STOP_OR_PAUSE
        global _ORIGINAL_BROWSER_USE_EXECUTE_ACTIONS, _ORIGINAL_BROWSER_USE_HANDLE_POST_LLM_PROCESSING
        global _ORIGINAL_BROWSER_USE_BROADCAST_MODEL_STATE, _ORIGINAL_BROWSER_USE_POST_PROCESS, _ORIGINAL_BROWSER_USE_FINALIZE
        global _ORIGINAL_BROWSER_USE_BROWSER_STATE_SUMMARY, _ORIGINAL_BROWSER_USE_RECONNECT, _ORIGINAL_BROWSER_USE_AUTO_RECONNECT
        global _ORIGINAL_BROWSER_USE_AGENT_ASYNCIO, _ORIGINAL_BUBUS_EXECUTE_HANDLER
        global _ORIGINAL_BUBUS_EVENT_RESULTS_FILTERED, _ORIGINAL_BROWSER_USE_TOOLS_ACT

        if self.hitl_freeze_mode == "off":
            return

        import browser_use.agent.service as agent_service
        from browser_use.agent.service import Agent
        from browser_use.browser.session import BrowserSession

        if _BROWSER_USE_AGENT_PATCH_REFCOUNT == 0:
            _ORIGINAL_BROWSER_USE_STEP = Agent.step
            _ORIGINAL_BROWSER_USE_EXECUTE_STEP = Agent._execute_step
            _ORIGINAL_BROWSER_USE_GET_MODEL_OUTPUT = Agent.get_model_output
            _ORIGINAL_BROWSER_USE_CHECK_STOP_OR_PAUSE = Agent._check_stop_or_pause
            _ORIGINAL_BROWSER_USE_EXECUTE_ACTIONS = Agent._execute_actions
            _ORIGINAL_BROWSER_USE_HANDLE_POST_LLM_PROCESSING = Agent._handle_post_llm_processing
            _ORIGINAL_BROWSER_USE_BROADCAST_MODEL_STATE = Agent._broadcast_model_state
            _ORIGINAL_BROWSER_USE_POST_PROCESS = Agent._post_process
            _ORIGINAL_BROWSER_USE_FINALIZE = Agent._finalize
            _ORIGINAL_BROWSER_USE_BROWSER_STATE_SUMMARY = BrowserSession.get_browser_state_summary
            # Some supported browser-use versions reconnect through connect()
            # directly rather than exposing separate recovery entry points.
            _ORIGINAL_BROWSER_USE_RECONNECT = getattr(BrowserSession, "reconnect", None)
            _ORIGINAL_BROWSER_USE_AUTO_RECONNECT = getattr(BrowserSession, "_auto_reconnect", None)
            _ORIGINAL_BROWSER_USE_AGENT_ASYNCIO = agent_service.asyncio
            Agent.step = _agent_step_with_acira_runtime_freeze  # type: ignore[method-assign]
            Agent._execute_step = _execute_step_with_acira_hitl_pause  # type: ignore[method-assign]
            Agent.get_model_output = _get_model_output_with_acira_runtime_freeze  # type: ignore[method-assign]
            Agent._check_stop_or_pause = _check_stop_or_pause_with_acira_runtime_freeze  # type: ignore[method-assign]
            Agent._execute_actions = _execute_actions_with_acira_runtime_freeze  # type: ignore[method-assign]
            Agent._handle_post_llm_processing = _handle_post_llm_processing_with_acira_runtime_freeze  # type: ignore[method-assign]
            Agent._broadcast_model_state = _broadcast_model_state_with_acira_runtime_freeze  # type: ignore[method-assign]
            Agent._post_process = _post_process_with_acira_runtime_freeze  # type: ignore[method-assign]
            Agent._finalize = _finalize_with_acira_runtime_freeze  # type: ignore[method-assign]
            BrowserSession.get_browser_state_summary = _browser_state_summary_with_acira_runtime_freeze  # type: ignore[method-assign]
            if _ORIGINAL_BROWSER_USE_RECONNECT is not None:
                BrowserSession.reconnect = _browser_session_reconnect_with_acira_runtime_freeze  # type: ignore[method-assign]
            if _ORIGINAL_BROWSER_USE_AUTO_RECONNECT is not None:
                BrowserSession._auto_reconnect = _browser_session_auto_reconnect_with_acira_runtime_freeze  # type: ignore[method-assign]
            agent_service.asyncio = _AsyncioReviewPauseProxy(agent_service.asyncio)
        _BROWSER_USE_AGENT_PATCH_REFCOUNT += 1
        self._agent_step_patch_active = True

        if self.hitl_freeze_mode != "strict":
            return

        from bubus import BaseEvent, EventBus
        from browser_use.tools.service import Tools

        if _BROWSER_USE_FREEZE_PATCH_REFCOUNT == 0:
            _ORIGINAL_BUBUS_EXECUTE_HANDLER = EventBus.execute_handler
            _ORIGINAL_BUBUS_EVENT_RESULTS_FILTERED = BaseEvent.event_results_filtered
            _ORIGINAL_BROWSER_USE_TOOLS_ACT = Tools.act
            EventBus.execute_handler = _bubus_execute_handler_with_acira_pause  # type: ignore[method-assign]
            BaseEvent.event_results_filtered = _bubus_event_results_filtered_with_acira_pause  # type: ignore[method-assign]
            Tools.act = _browser_use_tools_act_with_acira_pause  # type: ignore[method-assign]
        _BROWSER_USE_FREEZE_PATCH_REFCOUNT += 1
        self._freeze_patch_active = True

    def _restore_browser_use_hitl_freeze(self) -> None:
        global _BROWSER_USE_AGENT_PATCH_REFCOUNT, _BROWSER_USE_FREEZE_PATCH_REFCOUNT
        global _ORIGINAL_BROWSER_USE_STEP, _ORIGINAL_BROWSER_USE_EXECUTE_STEP, _ORIGINAL_BROWSER_USE_GET_MODEL_OUTPUT
        global _ORIGINAL_BROWSER_USE_CHECK_STOP_OR_PAUSE
        global _ORIGINAL_BROWSER_USE_EXECUTE_ACTIONS, _ORIGINAL_BROWSER_USE_HANDLE_POST_LLM_PROCESSING
        global _ORIGINAL_BROWSER_USE_BROADCAST_MODEL_STATE, _ORIGINAL_BROWSER_USE_POST_PROCESS, _ORIGINAL_BROWSER_USE_FINALIZE
        global _ORIGINAL_BROWSER_USE_BROWSER_STATE_SUMMARY, _ORIGINAL_BROWSER_USE_RECONNECT, _ORIGINAL_BROWSER_USE_AUTO_RECONNECT
        global _ORIGINAL_BROWSER_USE_AGENT_ASYNCIO, _ORIGINAL_BUBUS_EXECUTE_HANDLER
        global _ORIGINAL_BUBUS_EVENT_RESULTS_FILTERED, _ORIGINAL_BROWSER_USE_TOOLS_ACT

        if not self._agent_step_patch_active:
            pass
        else:
            self._agent_step_patch_active = False
            _BROWSER_USE_AGENT_PATCH_REFCOUNT = max(0, _BROWSER_USE_AGENT_PATCH_REFCOUNT - 1)
            if _BROWSER_USE_AGENT_PATCH_REFCOUNT == 0 and _ORIGINAL_BROWSER_USE_EXECUTE_STEP is not None:
                import browser_use.agent.service as agent_service
                from browser_use.agent.service import Agent
                from browser_use.browser.session import BrowserSession

                if _ORIGINAL_BROWSER_USE_STEP is not None:
                    Agent.step = _ORIGINAL_BROWSER_USE_STEP  # type: ignore[method-assign]
                    _ORIGINAL_BROWSER_USE_STEP = None
                Agent._execute_step = _ORIGINAL_BROWSER_USE_EXECUTE_STEP  # type: ignore[method-assign]
                _ORIGINAL_BROWSER_USE_EXECUTE_STEP = None
                if _ORIGINAL_BROWSER_USE_GET_MODEL_OUTPUT is not None:
                    Agent.get_model_output = _ORIGINAL_BROWSER_USE_GET_MODEL_OUTPUT  # type: ignore[method-assign]
                    _ORIGINAL_BROWSER_USE_GET_MODEL_OUTPUT = None
                if _ORIGINAL_BROWSER_USE_CHECK_STOP_OR_PAUSE is not None:
                    Agent._check_stop_or_pause = _ORIGINAL_BROWSER_USE_CHECK_STOP_OR_PAUSE  # type: ignore[method-assign]
                    _ORIGINAL_BROWSER_USE_CHECK_STOP_OR_PAUSE = None
                if _ORIGINAL_BROWSER_USE_EXECUTE_ACTIONS is not None:
                    Agent._execute_actions = _ORIGINAL_BROWSER_USE_EXECUTE_ACTIONS  # type: ignore[method-assign]
                    _ORIGINAL_BROWSER_USE_EXECUTE_ACTIONS = None
                if _ORIGINAL_BROWSER_USE_HANDLE_POST_LLM_PROCESSING is not None:
                    Agent._handle_post_llm_processing = _ORIGINAL_BROWSER_USE_HANDLE_POST_LLM_PROCESSING  # type: ignore[method-assign]
                    _ORIGINAL_BROWSER_USE_HANDLE_POST_LLM_PROCESSING = None
                if _ORIGINAL_BROWSER_USE_BROADCAST_MODEL_STATE is not None:
                    Agent._broadcast_model_state = _ORIGINAL_BROWSER_USE_BROADCAST_MODEL_STATE  # type: ignore[method-assign]
                    _ORIGINAL_BROWSER_USE_BROADCAST_MODEL_STATE = None
                if _ORIGINAL_BROWSER_USE_POST_PROCESS is not None:
                    Agent._post_process = _ORIGINAL_BROWSER_USE_POST_PROCESS  # type: ignore[method-assign]
                    _ORIGINAL_BROWSER_USE_POST_PROCESS = None
                if _ORIGINAL_BROWSER_USE_FINALIZE is not None:
                    Agent._finalize = _ORIGINAL_BROWSER_USE_FINALIZE  # type: ignore[method-assign]
                    _ORIGINAL_BROWSER_USE_FINALIZE = None
                if _ORIGINAL_BROWSER_USE_BROWSER_STATE_SUMMARY is not None:
                    BrowserSession.get_browser_state_summary = _ORIGINAL_BROWSER_USE_BROWSER_STATE_SUMMARY  # type: ignore[method-assign]
                    _ORIGINAL_BROWSER_USE_BROWSER_STATE_SUMMARY = None
                if _ORIGINAL_BROWSER_USE_RECONNECT is not None:
                    BrowserSession.reconnect = _ORIGINAL_BROWSER_USE_RECONNECT  # type: ignore[method-assign]
                    _ORIGINAL_BROWSER_USE_RECONNECT = None
                if _ORIGINAL_BROWSER_USE_AUTO_RECONNECT is not None:
                    BrowserSession._auto_reconnect = _ORIGINAL_BROWSER_USE_AUTO_RECONNECT  # type: ignore[method-assign]
                    _ORIGINAL_BROWSER_USE_AUTO_RECONNECT = None
                if _ORIGINAL_BROWSER_USE_AGENT_ASYNCIO is not None:
                    agent_service.asyncio = _ORIGINAL_BROWSER_USE_AGENT_ASYNCIO
                    _ORIGINAL_BROWSER_USE_AGENT_ASYNCIO = None

        if not self._freeze_patch_active:
            return
        self._freeze_patch_active = False
        _BROWSER_USE_FREEZE_PATCH_REFCOUNT = max(0, _BROWSER_USE_FREEZE_PATCH_REFCOUNT - 1)
        if _BROWSER_USE_FREEZE_PATCH_REFCOUNT == 0:
            from bubus import BaseEvent, EventBus
            from browser_use.tools.service import Tools

            if _ORIGINAL_BUBUS_EXECUTE_HANDLER is not None:
                EventBus.execute_handler = _ORIGINAL_BUBUS_EXECUTE_HANDLER  # type: ignore[method-assign]
                _ORIGINAL_BUBUS_EXECUTE_HANDLER = None
            if _ORIGINAL_BUBUS_EVENT_RESULTS_FILTERED is not None:
                BaseEvent.event_results_filtered = _ORIGINAL_BUBUS_EVENT_RESULTS_FILTERED  # type: ignore[method-assign]
                _ORIGINAL_BUBUS_EVENT_RESULTS_FILTERED = None
            if _ORIGINAL_BROWSER_USE_TOOLS_ACT is not None:
                Tools.act = _ORIGINAL_BROWSER_USE_TOOLS_ACT  # type: ignore[method-assign]
                _ORIGINAL_BROWSER_USE_TOOLS_ACT = None

    async def _connect_with_proxy(self, browser_session: Any, cdp_url: str | None) -> Any:
        if self._original_connect is None:
            raise RuntimeError("BrowserUseCDPProxy has not been entered")
        if (getattr(browser_session, "_intentional_stop", False)
                or id(browser_session) in self._stopping_sessions):
            raise ConnectionError("A deliberately stopped browser cannot reconnect during cleanup")

        real_cdp_url = cdp_url or getattr(browser_session, "cdp_url", None)
        if not real_cdp_url:
            return await self._original_connect(browser_session, cdp_url=cdp_url)

        binding_key = id(browser_session)
        existing = self._bindings.get(binding_key)
        if existing and existing.server.running:
            if existing.server.owns_proxy_url(real_cdp_url):
                return await self._original_connect(browser_session, cdp_url=real_cdp_url)

            if existing.real_cdp_url == real_cdp_url:
                proxy_url = existing.server.proxy_cdp_url(real_cdp_url)
                return await self._original_connect(browser_session, cdp_url=proxy_url)

            await existing.server.stop()
            self._bindings.pop(binding_key, None)

        from .browser_use_operations import TransportPolicy
        server = AciraCDPProxy(
            real_cdp_url,
            host=self.host,
            port=self.port,
            logger=self.logger,
            policy=TransportPolicy(self),
        )
        await server.start()
        self._bindings[binding_key] = _ProxyBinding(real_cdp_url=real_cdp_url, server=server)
        self._protect_browser_use_event_bus(browser_session)

        try:
            return await self._original_connect(browser_session, cdp_url=server.proxy_cdp_url(real_cdp_url))
        except Exception:
            self._bindings.pop(binding_key, None)
            await server.stop()
            raise

    def _protect_browser_use_event_bus(self, browser_session: Any) -> None:
        event_bus = getattr(browser_session, "event_bus", None)
        if event_bus is None or not hasattr(event_bus, "handlers"):
            return

        key = id(event_bus)
        if key in self._patched_event_buses:
            return

        original_dispatch = event_bus.dispatch
        original_handlers: dict[str, list[Callable[..., Any]]] = {}

        def dispatch_with_acira_timeout(event: Any) -> Any:
            event_type = getattr(event, "event_type", event.__class__.__name__)
            if event_type == "NavigateToUrlEvent":
                _apply_navigation_speed_defaults(event)
            if self.hitl_event_timeout is not None and event_type in self.GUARDED_EVENT_TYPES:
                current_timeout = getattr(event, "event_timeout", None)
                if current_timeout is None or current_timeout < self.hitl_event_timeout:
                    event.event_timeout = self.hitl_event_timeout
            return original_dispatch(event)

        event_bus.dispatch = dispatch_with_acira_timeout

        for event_type in self.GUARDED_EVENT_TYPES:
            handlers = list(event_bus.handlers.get(event_type, []))
            if not handlers:
                continue
            original_handlers[event_type] = handlers
            event_bus.handlers[event_type] = [
                self._guard_browser_use_handler(handler, browser_session, event_type) for handler in handlers
            ]

        self._patched_event_buses[key] = (event_bus, original_dispatch, original_handlers)

    def _guard_browser_use_handler(self, handler: Callable[..., Any], browser_session: Any, event_type: str) -> Callable[..., Any]:
        if getattr(handler, "_acira_guarded", False):
            return handler

        async def guarded_handler(event: Any) -> Any:
            internal_secret_fill = self._internal_secret_fill_context_for_event(event, browser_session, event_type)
            if internal_secret_fill is not None:
                return await handler(event)

            if event_type != "NavigateToUrlEvent":
                await self._ensure_browser_use_general_page_preflight(browser_session, trigger="before_action")
                await self._ensure_browser_use_page_mapping_preflight(browser_session)

            if (event_type == "ClickElementEvent" and getattr(self.guard_policy, "require_bound_review", False)):
                # The native watchdog may retry a pointer click as JavaScript.
                # A protected project performs one exact DOM activation instead.
                from .browser_use_operations import activate
                if getattr(event, "button", "left") != "left":
                    from acira_cdp_proxy.action_gateway import AciraActionBlocked
                    raise AciraActionBlocked("Exact element activation supports the primary button only; no action was dispatched")
                return await activate(self, browser_session, event.node)

            request = await self._browser_use_action_request(event, browser_session)
            if request is None:
                result = await handler(event)
                if event_type == "NavigateToUrlEvent":
                    await self._ensure_browser_use_general_page_preflight(browser_session, trigger="after_navigation")
                    await self._ensure_browser_use_page_mapping_preflight(browser_session)
                return result

            operation_binding = None
            frozen_upload = None
            if getattr(self.guard_policy, "require_bound_review", False) and request.action in BROWSER_USE_CDP_FOLLOWUP_ACTIONS:
                from acira_cdp_proxy.operation import bind_cdp
                from dataclasses import replace
                node = getattr(event, "node", None)
                backend_id = _positive_int_or_none(getattr(node, "backend_node_id", None) or getattr(node, "backendNodeId", None))
                try:
                    operation_binding = await bind_cdp(request.send_probe, request.target, request.action,
                        node={"backendNodeId": backend_id} if backend_id else None,
                        session_id=_browser_use_node_session_id(node))
                except Exception as exc:
                    from acira_cdp_proxy.action_gateway import AciraActionBlocked
                    raise AciraActionBlocked("Cannot establish exact operation evidence; no browser action was sent") from exc
                # Keep values and routing private while the review is pending.
                # Browser-use events are mutable; another task must not substitute
                # text, keys, a destination, or a node after authorization.
                def operation_arguments():
                    import copy
                    values = {name: copy.deepcopy(getattr(event, name)) for name in
                              ("text", "keys", "file_path", "clear", "button", "url",
                               "coordinate_x", "coordinate_y", "force", "direction", "amount")
                              if hasattr(event, name)}
                    selected_node = getattr(event, "node", None)
                    values["node"] = tuple(getattr(selected_node, name, None) for name in
                                           ("backend_node_id", "node_id", "session_id", "frame_id", "target_id"))
                    return values
                frozen_arguments = operation_arguments()
                upload_unchanged = None
                if event_type == "UploadFileEvent":
                    from ._uploads import freeze_uploads
                    try:
                        uploads, upload_evidence, upload_unchanged = freeze_uploads(event.file_path)
                        frozen_upload = uploads[0]
                        operation_binding.evidence["files"] = upload_evidence
                        operation_binding.evidence["critical"] = True
                    except BaseException:
                        await operation_binding.release()
                        raise
                async def revalidate_operation():
                    return (operation_arguments() == frozen_arguments
                            and (upload_unchanged is None or upload_unchanged())
                            and await operation_binding.revalidate())
                request = replace(request, revalidate=revalidate_operation, operation_evidence=operation_binding.evidence)
            async def execute_operation():
                if frozen_upload is not None:
                    from ._uploads import private_upload_path
                    size = len(frozen_upload["buffer"])
                    if self._upload_bytes + size > 256 * 1024 * 1024 or self._upload_count >= 128:
                        from acira_cdp_proxy.action_gateway import AciraActionBlocked
                        raise AciraActionBlocked("This run reached its private upload staging limit; finish it before uploading more files")
                    # Chromium can read the selected file later during submission.
                    # Keep a private copy until this run ends, with a bounded budget.
                    path = self._upload_staging.enter_context(private_upload_path(frozen_upload))
                    self._upload_bytes += size
                    self._upload_count += 1
                    bound_event = event.model_copy(update={"file_path": path})
                    return await self._execute_browser_use_handler(handler, bound_event, browser_session, request)
                return await self._execute_browser_use_handler(handler, event, browser_session, request)
            try:
                result = await self.action_gateway.guard_execute(
                    request,
                    execute_operation,
                    blocked_result=self._browser_use_blocked_result_factory(event_type),
                )
                if event_type == "NavigateToUrlEvent":
                    await self._ensure_browser_use_general_page_preflight(browser_session, trigger="after_navigation")
                    await self._ensure_browser_use_page_mapping_preflight(browser_session)
                return result
            except Exception as exc:
                normalized = _browser_use_error_from_acira_exception(exc)
                if normalized is not None:
                    raise normalized from exc
                raise
            finally:
                if operation_binding is not None:
                    await operation_binding.release()

        async def tracked_handler(event: Any) -> Any:
            return await self._run_session_operation(browser_session, lambda: guarded_handler(event))

        tracked_handler.__name__ = getattr(handler, "__name__", f"acira_guarded_{event_type}")
        tracked_handler._acira_guarded = True  # type: ignore[attr-defined]
        return tracked_handler

    async def _execute_browser_use_handler(
        self,
        handler: Callable[..., Any],
        event: Any,
        browser_session: Any,
        request: ActionRequest,
    ) -> Any:
        # Guard approval is authorization only. The actual browser-use handler must
        # perform the browser action so dashboard/HITL never substitutes its own
        # typing path for native browser-use behavior. If the review wait made the
        # browser-use node stale, Acira refreshes the event node to the approved
        # live target and restores focus before browser-use's native handler runs.
        if request.revalidate is None:
            await self._focus_browser_use_approved_text_target(browser_session, event, request)
        return await handler(event)

    async def _focus_browser_use_approved_text_target(
        self,
        browser_session: Any,
        event: Any,
        request: ActionRequest,
    ) -> None:
        if request.target is None or request.action != "form_input":
            return
        if not (request.target.is_editable or request.target.is_form_control):
            return

        event_type = getattr(event, "event_type", event.__class__.__name__)
        if event_type != "TypeTextEvent":
            return

        selectors = _browser_use_approved_target_selectors(request.target)
        if not selectors:
            return

        node = getattr(event, "node", None)
        target_id_hint = _browser_use_node_target_id(node)
        session_id_hint = _browser_use_node_session_id(node)
        try:
            timeout = float(getattr(self.guard_policy.config, "probe_timeout", 1.0) or 1.0)
        except (TypeError, ValueError):
            timeout = 1.0
        timeout = max(0.1, min(timeout, 1.0))

        for target_id, client, session_id, session_error in await self._browser_use_cdp_clients(
            browser_session,
            include_fallback=True,
            target_id_hint=target_id_hint,
            session_id_hint=session_id_hint,
        ):
            if session_error or client is None:
                continue
            effective_session_id = session_id if isinstance(session_id, str) and session_id else None
            try:
                document = await asyncio.wait_for(
                    _browser_use_send_cdp_command(
                        client,
                        "DOM.getDocument",
                        {"depth": 1, "pierce": True},
                        session_id=effective_session_id,
                    ),
                    timeout=timeout,
                )
            except Exception:
                continue
            root_node_id = _dom_root_node_id_from_response(document)
            if root_node_id is None:
                continue

            for selector in selectors:
                try:
                    query = await asyncio.wait_for(
                        _browser_use_send_cdp_command(
                            client,
                            "DOM.querySelector",
                            {"nodeId": root_node_id, "selector": selector},
                            session_id=effective_session_id,
                        ),
                        timeout=timeout,
                    )
                except Exception:
                    continue
                node_id = _dom_node_id_from_response(query)
                if node_id is None:
                    continue
                try:
                    described = await asyncio.wait_for(
                        _browser_use_send_cdp_command(
                            client,
                            "DOM.describeNode",
                            {"nodeId": node_id},
                            session_id=effective_session_id,
                        ),
                        timeout=timeout,
                    )
                except Exception:
                    continue
                described_node = _dom_described_node_from_response(described)
                backend_node_id = _dom_backend_node_id_from_described_node(described_node)
                if backend_node_id is None:
                    continue
                if not _browser_use_described_node_matches_target(described_node, request.target):
                    continue
                _refresh_browser_use_event_node_identity(
                    node,
                    node_id=node_id,
                    backend_node_id=backend_node_id,
                    target_id=target_id,
                    session_id=effective_session_id,
                )

                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        _browser_use_send_cdp_command(
                            client,
                            "DOM.scrollIntoViewIfNeeded",
                            {"nodeId": node_id},
                            session_id=effective_session_id,
                        ),
                        timeout=timeout,
                    )
                try:
                    await asyncio.wait_for(
                        _browser_use_send_cdp_command(
                            client,
                            "DOM.focus",
                            {"nodeId": node_id},
                            session_id=effective_session_id,
                        ),
                        timeout=timeout,
                    )
                except Exception:
                    continue
                if self.guard_policy.config.terminal_log:
                    label = _target_display_label(request.target) or "approved target"
                    print(
                        "[Acira Guard] FOCUSED_APPROVED_BROWSER_USE_TARGET"
                        f" action={request.action}"
                        f" target={label}"
                        f" selector={selector}"
                        f" backend_node_id={backend_node_id}"
                        f" target_id={target_id}",
                        file=sys.stderr,
                        flush=True,
                    )
                return

    def _internal_secret_fill_context_for_event(
        self,
        event: Any,
        browser_session: Any,
        event_type: str,
    ) -> _InternalSecretFillContext | None:
        context = getattr(event, "_acira_internal_secret_fill", None) or _ACIRA_INTERNAL_SECRET_FILL.get()
        if context is None:
            return None
        if not isinstance(context, _InternalSecretFillContext):
            return None
        if event_type != "TypeTextEvent":
            return None

        mismatches: list[str] = []
        if context.proxy_id != id(self):
            mismatches.append("proxy")
        if context.browser_session_id != id(browser_session):
            mismatches.append("browser_session")
        if context.event_id != id(event):
            mismatches.append("event")
        if not context.node_identity or context.node_identity != _browser_use_node_identity(getattr(event, "node", None)):
            mismatches.append("node")

        if mismatches:
            reason = "Acira internal secret fill target mismatch; the action was not sent."
            _raise_browser_use_blocked_error(
                reason,
                {
                    "guard": "acira",
                    "decision": "block_internal_secret_fill_mismatch",
                    "secret_id": context.secret_id,
                    "element_index": context.element_index,
                    "mismatches": mismatches,
                },
            )
        return context

    async def _ensure_browser_use_page_mapping_preflight(self, browser_session: Any) -> None:
        if not self.guard_policy.config.specific_skills:
            return

        url = await self._current_browser_use_url(browser_session)
        if not self.guard_policy.has_specific_skill_for_url(url):
            return

        probe_result = await self._probe_browser_use_page_targets_with_retries(browser_session, expected_url=url)
        preflight_key = (
            id(browser_session),
            _normalize_probe_url(url),
        )
        now = time.monotonic()
        cached = self._page_mapping_preflight_cache.get(preflight_key)
        if cached is not None and cached.expires_at > now:
            return
        if cached is not None:
            self._page_mapping_preflight_cache.pop(preflight_key, None)

        details = {
            "event_type": "PageMappingPreflight",
            "page_targets": probe_result.targets,
            "page_target_probe": probe_result.metadata,
            "mapping_preflight": "page",
        }
        await self.action_gateway.guard_execute(
            ActionRequest(
                action="mapping_preflight",
                framework=self.adapter_spec.id,
                target=None,
                details=details,
                reason="BrowserUse page mapping preflight",
                confidence=0.99,
                meaningful=True,
                source="browser_use.event",
                display_name="browser action",
                release_pause_on_review_exit=True,
                supervised_runtime=True,
            ),
            lambda: {"mapping_preflight": "ok"},
            blocked_result=_raise_browser_use_blocked_error,
        )
        self._page_mapping_preflight_cache[preflight_key] = _PageMappingPreflightCacheEntry(
            expires_at=time.monotonic() + 30.0,
        )

    async def _ensure_browser_use_general_page_preflight(self, browser_session: Any, *, trigger: str) -> None:
        config = self.guard_policy.config
        if not config.general_skill_preflight.enabled or not config.general_skills or not config.has_page_policy:
            return

        url = await self._current_browser_use_url(browser_session)
        if not url:
            return

        preflight_key = (id(browser_session), _normalize_probe_url(url))
        now = time.monotonic()
        cached = self._general_page_preflight_cache.get(preflight_key)
        if cached is not None and cached.expires_at > now:
            return
        if cached is not None:
            self._general_page_preflight_cache.pop(preflight_key, None)

        probe_result = (
            await self._probe_browser_use_stable_page_targets(browser_session, expected_url=url)
            if trigger == "after_navigation"
            else await self._probe_browser_use_page_targets_with_retries(browser_session, expected_url=url)
        )

        if probe_result.metadata.get("status") != "ok":
            self.guard_policy._terminal_preflight_status(  # noqa: SLF001
                scope_key=f"browser_use:{id(browser_session)}",
                trigger=f"browser_use_{trigger}",
                status=str(probe_result.metadata.get("status") or "probe_failed"),
            )
            return

        snapshot = self.guard_policy.evaluate_page_preflight_targets(
            probe_result.targets,
            dict(probe_result.metadata),
            scope_key=f"browser_use:{id(browser_session)}",
            readiness={
                "strategy": "browser_use_page_probe",
                "trigger": trigger,
                "status": probe_result.metadata.get("status"),
                "candidate_count": probe_result.metadata.get("candidate_count"),
                "probe_readiness": probe_result.metadata.get("readiness"),
            },
        )
        context = CDPMessageContext(
            direction="AGENT -> BROWSER",
            connection_id="browser-use-page-preflight",
            message_number=0,
            websocket_path=f"/acira/page/browser_use/{id(browser_session)}",
            parsed_message={"method": "Acira.preflight", "params": snapshot.to_metadata()},
            send_probe=lambda method, params=None, session_id=None: self._send_browser_use_cdp(
                browser_session,
                method,
                params,
                session_id=session_id,
            ),
        )
        decision = await self.guard_policy.enforce_page_preflight_snapshot(
            snapshot,
            context,
            trigger=f"browser_use_{trigger}",
        )
        if not decision.allow:
            _raise_browser_use_blocked_error(decision.reason or "Blocked by Acira page preflight.", decision.metadata or {})
        self._general_page_preflight_cache[preflight_key] = _GeneralPagePreflightCacheEntry(
            expires_at=time.monotonic() + BROWSER_USE_GENERAL_PREFLIGHT_CACHE_TTL_SECONDS,
        )

    async def _browser_use_action_request(self, event: Any, browser_session: Any) -> ActionRequest | None:
        event_type = getattr(event, "event_type", event.__class__.__name__)
        if event_type not in self.GUARDED_EVENT_TYPES:
            return None

        url = await self._current_browser_use_url(browser_session)
        node = getattr(event, "node", None)
        target = guard_target_from_browser_use_node(node, url=url)
        node_target = target
        action = "meaningful_action"
        reason = f"browser-use {event_type}"

        if event_type in {"ClickElementEvent", "ClickCoordinateEvent"}:
            action = "click"
            reason = "browser-use click action"
        elif event_type == "TypeTextEvent":
            action = "form_input"
            reason = "browser-use text input action"
        elif event_type == "SelectDropdownOptionEvent":
            action = "select"
            reason = "browser-use dropdown selection action"
        elif event_type == "NavigateToUrlEvent":
            action = "navigation"
            reason = "browser-use navigation action"
            target = None
        elif event_type == "SendKeysEvent":
            keys = str(getattr(event, "keys", "") or "")
            action = "submit" if keys.lower() in {"enter", "return", "numenter", "numpadenter"} else "keyboard"
            reason = "browser-use keyboard action"
        elif event_type == "UploadFileEvent":
            action = "file_upload"
            reason = "browser-use file upload action"

        if action == "navigation":
            self._invalidate_browser_use_page_probe_cache(browser_session)

        resolution_metadata: dict[str, Any] | None = None
        target_resolved = _browser_use_target_is_specific(target)
        target_resolution_source = "browser_use.node" if target is not None else None
        target_resolution_confidence = target.confidence if target is not None else None
        target_resolution_unsafe = False
        node_target_id = _browser_use_node_target_id(node)
        node_session_id = _browser_use_node_session_id(node)
        if action != "navigation" and node is not None:
            exact_target, resolution_metadata = await self._probe_browser_use_event_node_target(
                browser_session,
                node,
                action=action,
                expected_url=url,
                target_id_hint=node_target_id,
                session_id_hint=node_session_id,
            )
            if exact_target is not None:
                target = exact_target
                target_resolved = _browser_use_target_is_specific(target)
                target_resolution_source = _metadata_source(resolution_metadata) or target.source
                target_resolution_confidence = target.confidence
                if _browser_use_resolution_is_unsafe(resolution_metadata):
                    target_resolution_unsafe = True
                    target_resolved = False
                    target_resolution_confidence = min(float(target_resolution_confidence or 0.0), 0.4)

        probe_result = (
            await self._probe_browser_use_page_targets(
                browser_session,
                expected_url=url,
                target_id_hint=node_target_id,
                session_id_hint=node_session_id,
            )
            if action != "navigation"
            and (
                self.guard_policy.config.specific_skills
                or self.guard_policy.config.general_skills
                or _browser_use_target_needs_probe_enrichment(target)
            )
            else None
        )
        page_targets = probe_result.targets if probe_result is not None else None
        target, enrichment_metadata = self._enrich_browser_use_target(target, page_targets, action=action)
        if enrichment_metadata is not None:
            target_resolved = _browser_use_target_is_specific(target) and not target_resolution_unsafe
            target_resolution_source = str(enrichment_metadata.get("source") or target_resolution_source or "")
            target_resolution_confidence = target.confidence if target is not None else target_resolution_confidence
            if action == "click" and _browser_use_resolution_is_unsafe(enrichment_metadata):
                target_resolution_unsafe = True
                target_resolved = False
                target_resolution_confidence = min(float(target_resolution_confidence or target.confidence or 0.0), 0.4)
        if (
            action == "click"
            and node_target is not None
            and _guard_target_is_interactive_click(node_target)
            and target is not None
            and not _guard_target_is_interactive_click(target)
        ):
            target_resolution_unsafe = True
            target_resolved = False
            target_resolution_confidence = min(float(target_resolution_confidence or target.confidence or 0.0), 0.4)
            if enrichment_metadata is None:
                enrichment_metadata = {}
            enrichment_metadata.update(
                {
                    "source": target_resolution_source or target.source,
                    "status": "noninteractive_probe_target",
                    "node_target": _target_display_label(node_target),
                    "probe_target": _target_display_label(target),
                }
            )
        details: dict[str, Any] = {"event_type": event_type}
        if probe_result is not None:
            details["page_target_probe"] = probe_result.metadata
            details["page_targets"] = page_targets or []
            details["mapping_preflight"] = "page"
        elif page_targets:
            details["page_targets"] = page_targets
        if resolution_metadata is not None:
            details["target_resolution_probe"] = resolution_metadata
        if enrichment_metadata is not None:
            details["target_enrichment"] = enrichment_metadata
        details["target_resolution"] = {
            "resolved": target_resolved,
            "source": target_resolution_source,
            "confidence": target_resolution_confidence,
            "label": _target_display_label(target) if target is not None else "",
        }

        if event_type == "ClickCoordinateEvent":
            details.update(
                {
                    "x": getattr(event, "coordinate_x", None),
                    "y": getattr(event, "coordinate_y", None),
                    "button": getattr(event, "button", None),
                }
            )
        elif event_type == "TypeTextEvent":
            details["text_length"] = len(str(getattr(event, "text", "") or ""))
            details["is_sensitive"] = bool(getattr(event, "is_sensitive", False))
        elif event_type == "SelectDropdownOptionEvent":
            details["option_text"] = str(getattr(event, "text", "") or "")
        elif event_type == "NavigateToUrlEvent":
            details["url"] = str(getattr(event, "url", "") or "")
        elif event_type == "SendKeysEvent":
            details["keys"] = str(getattr(event, "keys", "") or "")
        elif event_type == "UploadFileEvent":
            details["file_path"] = str(getattr(event, "file_path", "") or "")

        if target is not None:
            details["defer_missing_target_review"] = "cdp_backstop"
        binding = self._bindings.get(id(browser_session))
        if binding is not None and binding.real_cdp_url:
            details["cdp_capture"] = {"url": binding.real_cdp_url}

        return ActionRequest(
            action=action,
            framework=self.adapter_spec.id,
            target=target,
            details=details,
            reason=reason,
            confidence=0.88 if target is not None else 0.72,
            meaningful=True,
            source="browser_use.event",
            display_name="browser action",
            opens_cdp_followup_transaction=action in BROWSER_USE_CDP_FOLLOWUP_ACTIONS,
            release_pause_on_review_exit=True,
            supervised_runtime=True,
            target_resolved=target_resolved,
            target_resolution_source=target_resolution_source,
            target_resolution_confidence=target_resolution_confidence,
            send_probe=lambda method, params=None, session_id=None: self._send_browser_use_cdp(
                browser_session,
                method,
                params,
                session_id=session_id,
            ),
        )

    def _enrich_browser_use_target(
        self,
        target: GuardTarget | None,
        page_targets: list[dict[str, Any]] | None,
        *,
        action: str,
    ) -> tuple[GuardTarget | None, dict[str, Any] | None]:
        if target is None or not page_targets:
            return target, None

        best: dict[str, Any] | None = None
        best_score = 0
        second_score = 0
        target_metadata = target.to_metadata()
        for candidate in page_targets:
            if not isinstance(candidate, dict):
                continue
            if action == "click" and _click_candidate_is_less_precise(target_metadata, candidate):
                continue
            score = _browser_use_target_candidate_score(target_metadata, candidate)
            if action == "click" and _is_interactive_click_candidate(candidate):
                score += 35
            if score > best_score:
                second_score = best_score
                best = candidate
                best_score = score
            elif score > second_score:
                second_score = score

        if best is None or best_score < 70:
            return target, {
                "source": "browser_use.page_inventory",
                "status": "no_strong_match",
                "score": best_score,
                "second_score": second_score,
            }

        if second_score and best_score - second_score < 15:
            return target, {
                "source": "browser_use.page_inventory",
                "status": "ambiguous_match",
                "score": best_score,
                "second_score": second_score,
            }

        enriched = _target_from_probe_value(best)
        return enriched or target, {
            "source": "browser_use.page_inventory",
            "status": "ok" if enriched is not None else "invalid_candidate",
            "score": best_score,
            "label": _target_display_label(enriched) if enriched is not None else "",
        }

    async def _probe_browser_use_event_node_target(
        self,
        browser_session: Any,
        node: Any,
        *,
        action: str | None = None,
        expected_url: str | None = None,
        target_id_hint: str | None = None,
        session_id_hint: str | None = None,
    ) -> tuple[GuardTarget | None, dict[str, Any] | None]:
        exact, exact_metadata = await self._probe_browser_use_node_id_target(
            browser_session,
            node,
            action=action,
            expected_url=expected_url,
            target_id_hint=target_id_hint,
            session_id_hint=session_id_hint,
        )
        point, point_metadata = await self._probe_browser_use_node_point_target(
            browser_session,
            node,
            expected_url=expected_url,
            target_id_hint=target_id_hint,
            session_id_hint=session_id_hint,
        )
        if action == "click":
            selected, metadata = _resolve_browser_use_click_probe_target(exact, exact_metadata, point, point_metadata)
            if selected is not None:
                return _target_with_browser_use_node_rect(selected, node), metadata
            return None, metadata
        if exact is not None:
            return _target_with_browser_use_node_rect(exact, node), exact_metadata
        if point is not None:
            return _target_with_browser_use_node_rect(point, node), point_metadata
        return None, exact_metadata or point_metadata

    async def _probe_browser_use_node_id_target(
        self,
        browser_session: Any,
        node: Any,
        *,
        action: str | None = None,
        expected_url: str | None = None,
        target_id_hint: str | None = None,
        session_id_hint: str | None = None,
    ) -> tuple[GuardTarget | None, dict[str, Any] | None]:
        backend_node_id = _positive_int_or_none(getattr(node, "backend_node_id", None) or getattr(node, "backendNodeId", None))
        node_id = _positive_int_or_none(getattr(node, "node_id", None) or getattr(node, "nodeId", None))
        if backend_node_id is None and node_id is None:
            return None, None

        resolve_params: dict[str, Any] = {}
        if backend_node_id is not None:
            resolve_params["backendNodeId"] = backend_node_id
        elif node_id is not None:
            resolve_params["nodeId"] = node_id

        attempts: list[dict[str, Any]] = []
        for target_id, client, session_id, session_error in await self._browser_use_cdp_clients(
            browser_session,
            include_fallback=True,
            target_id_hint=target_id_hint,
            session_id_hint=session_id_hint,
        ):
            attempt: dict[str, Any] = {
                "source": "browser_use.cdp_node",
                "target_id": target_id,
                "session_id": session_id,
                "backend_node_id": backend_node_id,
                "node_id": node_id,
                "status": "pending",
            }
            if session_error:
                attempt["session_error"] = session_error
            if client is None:
                attempt["status"] = "unavailable"
                attempts.append(attempt)
                continue
            try:
                resolved = await asyncio.wait_for(
                    _browser_use_send_cdp_command(
                        client,
                        "DOM.resolveNode",
                        resolve_params,
                        session_id=session_id if isinstance(session_id, str) and session_id else None,
                    ),
                    timeout=self.guard_policy.config.probe_timeout,
                )
                object_id = _remote_object_id_from_response(resolved)
                if not object_id:
                    attempt["status"] = "resolve_failed"
                    attempts.append(attempt)
                    continue
                probed = await asyncio.wait_for(
                    _browser_use_send_cdp_command(
                        client,
                        "Runtime.callFunctionOn",
                        {
                            "objectId": object_id,
                            "functionDeclaration": RUNTIME_OBJECT_PROBE_JS,
                            "returnByValue": True,
                            "silent": True,
                        },
                        session_id=session_id if isinstance(session_id, str) and session_id else None,
                    ),
                    timeout=self.guard_policy.config.probe_timeout,
                )
            except Exception as exc:
                attempt["status"] = "failed"
                attempt["error"] = f"{exc.__class__.__name__}: {exc}"
                attempts.append(attempt)
                continue

            target = _target_from_probe_value(_runtime_response_value(probed))
            if target is None:
                attempt["status"] = "invalid_response"
                attempts.append(attempt)
                continue
            attempt["status"] = "ok"
            attempt["label"] = target.text or target.aria_label or target.name or target.id or ""
            attempt["tag"] = target.tag
            attempt["url_matches_expected"] = _browser_use_probe_url_matches(expected_url, target.url)
            attempts.append(attempt)
            if not attempt["url_matches_expected"]:
                if _browser_use_frame_document_node_probe_is_safe(
                    action=action,
                    target=target,
                    target_id=target_id,
                    session_id=session_id,
                    target_id_hint=target_id_hint,
                    session_id_hint=session_id_hint,
                ):
                    attempt["url_matches_expected"] = True
                    attempt["url_relation"] = "frame_document"
                else:
                    continue
            return target, {"source": "browser_use.cdp_node", "attempts": attempts}

        return None, {"source": "browser_use.cdp_node", "attempts": attempts, "status": "unavailable"}

    async def _probe_browser_use_node_point_target(
        self,
        browser_session: Any,
        node: Any,
        *,
        expected_url: str | None = None,
        target_id_hint: str | None = None,
        session_id_hint: str | None = None,
    ) -> tuple[GuardTarget | None, dict[str, Any] | None]:
        rect = _browser_use_node_rect(node)
        if rect is None:
            return None, None
        attempts: list[dict[str, Any]] = []
        point_expressions = _browser_use_point_probe_expressions(rect, BROWSER_USE_IGNORED_SELECTORS)
        for target_id, client, session_id, session_error in await self._browser_use_cdp_clients(
            browser_session,
            include_fallback=True,
            target_id_hint=target_id_hint,
            session_id_hint=session_id_hint,
        ):
            attempt: dict[str, Any] = {
                "source": "browser_use.point_probe",
                "target_id": target_id,
                "session_id": session_id,
                "status": "pending",
            }
            if session_error:
                attempt["session_error"] = session_error
            if client is None:
                attempt["status"] = "unavailable"
                attempts.append(attempt)
                continue

            for coordinate_space, expression in point_expressions:
                point_attempt = dict(attempt)
                point_attempt["coordinate_space"] = coordinate_space
                try:
                    response = await asyncio.wait_for(
                        _browser_use_runtime_evaluate(
                            client,
                            {"expression": expression, "returnByValue": True, "silent": True},
                            session_id=session_id if isinstance(session_id, str) and session_id else None,
                        ),
                        timeout=self.guard_policy.config.probe_timeout,
                    )
                except Exception as exc:
                    point_attempt["status"] = "failed"
                    point_attempt["error"] = f"{exc.__class__.__name__}: {exc}"
                    attempts.append(point_attempt)
                    continue
                target = _target_from_probe_value(_runtime_response_value(response))
                if target is None:
                    point_attempt["status"] = "invalid_response"
                    attempts.append(point_attempt)
                    continue
                point_attempt["status"] = "ok"
                point_attempt["label"] = target.text or target.aria_label or target.name or target.id or ""
                point_attempt["tag"] = target.tag
                point_attempt["url_matches_expected"] = _browser_use_probe_url_matches(expected_url, target.url)
                attempts.append(point_attempt)
                if not point_attempt["url_matches_expected"]:
                    continue
                return target, {"source": "browser_use.point_probe", "attempts": attempts}
        return None, {"source": "browser_use.point_probe", "attempts": attempts, "status": "unavailable"}

    async def _browser_use_cdp_clients(
        self,
        browser_session: Any,
        *,
        include_fallback: bool = True,
        target_id_hint: str | None = None,
        session_id_hint: str | None = None,
    ) -> list[tuple[str | None, Any | None, str | None, str | None]]:
        getter = getattr(browser_session, "get_or_create_cdp_session", None)
        target_ids: list[str | None] = []
        if isinstance(target_id_hint, str) and target_id_hint:
            target_ids.append(target_id_hint)
        focus_target_id = getattr(browser_session, "agent_focus_target_id", None)
        if isinstance(focus_target_id, str) and focus_target_id:
            target_ids.append(focus_target_id)
        if include_fallback or not target_ids:
            target_ids.append(None)
        target_ids = list(dict.fromkeys(target_ids))

        sessions: list[tuple[str | None, Any | None, str | None]] = []
        if callable(getter):
            for target_id in target_ids:
                cdp_session = None
                error: str | None = None
                try:
                    cdp_session = await getter(target_id=target_id, focus=False)
                except TypeError:
                    if target_id is None:
                        try:
                            cdp_session = await getter()
                        except Exception as exc:
                            error = f"{exc.__class__.__name__}: {exc}"
                    else:
                        error = "getter_does_not_accept_target_id"
                except Exception as exc:
                    error = f"{exc.__class__.__name__}: {exc}"
                sessions.append((target_id, cdp_session, error))
        if not sessions:
            sessions.append((None, None, None))

        fallback_client = getattr(browser_session, "cdp_client", None)
        clients: list[tuple[str | None, Any | None, str | None, str | None]] = []
        for target_id, cdp_session, error in sessions:
            client = getattr(cdp_session, "cdp_client", None) if cdp_session is not None else fallback_client
            session_id = getattr(cdp_session, "session_id", None) if cdp_session is not None else None
            if not isinstance(session_id, str) and target_id == target_id_hint:
                session_id = session_id_hint
            clients.append((target_id, client, session_id if isinstance(session_id, str) else None, error))
        return clients

    async def _probe_browser_use_page_targets(
        self,
        browser_session: Any,
        *,
        expected_url: str | None = None,
        use_cache: bool = True,
        target_id_hint: str | None = None,
        session_id_hint: str | None = None,
    ) -> _PageTargetProbeResult:
        cache_key = self._browser_use_page_probe_cache_key(browser_session, expected_url, target_id_hint=target_id_hint)
        if use_cache:
            cached = self._page_target_probe_cache.get(cache_key)
            now = time.monotonic()
            if cached is not None and cached.expires_at > now:
                metadata = dict(cached.result.metadata)
                metadata["cached"] = True
                return _PageTargetProbeResult(targets=cached.result.targets, metadata=metadata)
            if cached is not None:
                self._page_target_probe_cache.pop(cache_key, None)

        params = {
            "expression": (
                "(() => {"
                f" const targets = ({PAGE_TARGETS_PROBE_JS})();"
                " return { url: location.href, title: document.title, readyState: document.readyState, targets };"
                "})()"
            ),
            "returnByValue": True,
            "silent": True,
        }

        attempts: list[dict[str, Any]] = []
        getter = getattr(browser_session, "get_or_create_cdp_session", None)
        target_ids: list[str | None] = []
        if isinstance(target_id_hint, str) and target_id_hint:
            target_ids.append(target_id_hint)
        focus_target_id = getattr(browser_session, "agent_focus_target_id", None)
        if isinstance(focus_target_id, str) and focus_target_id:
            target_ids.append(focus_target_id)
        target_ids.append(None)
        target_ids = list(dict.fromkeys(target_ids))

        sessions: list[tuple[str | None, Any | None, str | None]] = []
        if callable(getter):
            for target_id in target_ids:
                cdp_session = None
                error: str | None = None
                try:
                    cdp_session = await getter(target_id=target_id, focus=False)
                except TypeError:
                    if target_id is None:
                        try:
                            cdp_session = await getter()
                        except Exception as exc:
                            error = f"{exc.__class__.__name__}: {exc}"
                    else:
                        error = "getter_does_not_accept_target_id"
                except Exception as exc:
                    error = f"{exc.__class__.__name__}: {exc}"
                sessions.append((target_id, cdp_session, error))

        if not sessions:
            sessions.append((None, None, None))

        fallback_client = getattr(browser_session, "cdp_client", None)
        last_error: str | None = None
        empty_seen = False
        wrong_url_seen = False
        for target_id, cdp_session, session_error in sessions:
            client = getattr(cdp_session, "cdp_client", None) if cdp_session is not None else fallback_client
            session_id = getattr(cdp_session, "session_id", None) if cdp_session is not None else None
            if not isinstance(session_id, str) and target_id == target_id_hint:
                session_id = session_id_hint
            attempt: dict[str, Any] = {
                "target_id": target_id,
                "session_id": session_id,
                "status": "pending",
            }
            if session_error:
                attempt["session_error"] = session_error
            if client is None:
                attempt["status"] = "unavailable"
                attempts.append(attempt)
                last_error = session_error or "no_cdp_client"
                continue

            try:
                response = await asyncio.wait_for(
                    _browser_use_runtime_evaluate(
                        client,
                        params,
                        session_id=session_id if isinstance(session_id, str) and session_id else None,
                    ),
                    timeout=self.guard_policy.config.probe_timeout,
                )
            except Exception as exc:
                attempt["status"] = "failed"
                attempt["error"] = f"{exc.__class__.__name__}: {exc}"
                attempts.append(attempt)
                last_error = attempt["error"]
                continue

            value = _runtime_response_value(response)
            if isinstance(value, dict):
                probe_url = str(value.get("url") or "")
                probe_title = str(value.get("title") or "")
                ready_state = str(value.get("readyState") or value.get("ready_state") or "")
                raw_targets = value.get("targets")
            else:
                probe_url = ""
                probe_title = ""
                ready_state = ""
                raw_targets = value

            targets = [item for item in raw_targets if isinstance(item, dict)] if isinstance(raw_targets, list) else []
            attempt["url"] = probe_url
            attempt["title"] = probe_title
            attempt["candidate_count"] = len(targets)
            url_ok = _browser_use_probe_url_matches(expected_url, probe_url) if expected_url and probe_url else True
            attempt["url_matches_expected"] = url_ok
            if not isinstance(raw_targets, list):
                attempt["status"] = "invalid_response"
                attempts.append(attempt)
                last_error = "probe_returned_non_list_targets"
                continue
            if not url_ok:
                attempt["status"] = "wrong_target"
                attempts.append(attempt)
                wrong_url_seen = True
                continue
            if not targets:
                attempt["status"] = "empty"
                attempts.append(attempt)
                empty_seen = True
                continue
            attempt["status"] = "ok"
            attempts.append(attempt)
            metadata = {
                "status": "ok",
                "candidate_count": len(targets),
                "url": probe_url,
                "title": probe_title,
                "ready_state": ready_state,
                "attempts": attempts,
            }
            metadata["page_signature"] = _page_target_signature(targets, metadata)
            result = _PageTargetProbeResult(
                targets=targets,
                metadata=metadata,
            )
            self.guard_policy.remember_page_target_probe(
                url=probe_url or expected_url,
                targets=targets,
                metadata=metadata,
                ttl_seconds=5.0,
            )
            self._page_target_probe_cache[cache_key] = _PageTargetProbeCacheEntry(
                result=result,
                expires_at=time.monotonic() + PAGE_TARGET_PROBE_CACHE_TTL_SECONDS,
            )
            return result

        status = "wrong_target" if wrong_url_seen else "empty" if empty_seen else "unavailable"
        metadata = {
            "status": status,
            "candidate_count": 0,
            "url": expected_url or "",
            "title": "",
            "attempts": attempts,
            "error": last_error,
        }
        metadata["page_signature"] = _page_target_signature([], metadata)
        return _PageTargetProbeResult(
            targets=[],
            metadata=metadata,
        )

    async def _probe_browser_use_page_targets_with_retries(
        self,
        browser_session: Any,
        *,
        expected_url: str | None = None,
        use_cache: bool = True,
    ) -> _PageTargetProbeResult:
        last_result: _PageTargetProbeResult | None = None
        for attempt in range(3):
            result = await self._probe_browser_use_page_targets(
                browser_session,
                expected_url=expected_url,
                use_cache=use_cache,
            )
            last_result = result
            if result.metadata.get("status") == "ok":
                return result
            if attempt < 2:
                await asyncio.sleep(0.12 * (attempt + 1))
        return last_result or _PageTargetProbeResult(
            targets=[],
            metadata={
                "status": "unavailable",
                "candidate_count": 0,
                "url": expected_url or "",
                "title": "",
                "attempts": [],
                "error": "probe_not_attempted",
            },
        )

    async def _probe_browser_use_stable_page_targets(
        self,
        browser_session: Any,
        *,
        expected_url: str | None = None,
    ) -> _PageTargetProbeResult:
        config = self.guard_policy.config.general_skill_preflight
        quiet_seconds = max(0.05, config.quiet_ms / 1000.0)
        max_wait = max(0.2, config.max_wait_ms / 1000.0)
        started = time.monotonic()
        stable_since: float | None = None
        last_url = ""
        last_signature = ""
        last_result: _PageTargetProbeResult | None = None
        timeline: list[dict[str, Any]] = []

        while time.monotonic() - started <= max_wait:
            result = await self._probe_browser_use_page_targets_with_retries(
                browser_session,
                expected_url=expected_url,
                use_cache=False,
            )
            now = time.monotonic()
            metadata = result.metadata
            url = _normalize_probe_url(str(metadata.get("url") or ""))
            signature = str(metadata.get("page_signature") or "")
            ready_state = str(metadata.get("ready_state") or "").lower()
            same_page = bool(url and url == last_url and signature and signature == last_signature)
            if same_page:
                stable_since = stable_since or now
            else:
                stable_since = now
                last_url = url
                last_signature = signature
            quiet_for = now - (stable_since or now)
            timeline.append(
                {
                    "at_ms": round((now - started) * 1000, 1),
                    "status": metadata.get("status"),
                    "url": url,
                    "ready_state": ready_state,
                    "candidate_count": metadata.get("candidate_count"),
                    "signature": signature,
                    "quiet_for_ms": round(quiet_for * 1000, 1),
                }
            )
            last_result = result
            if metadata.get("status") == "ok" and ready_state == "complete" and quiet_for >= quiet_seconds:
                break
            await asyncio.sleep(min(0.2, max(0.05, quiet_seconds / 3)))

        if last_result is None:
            last_result = _PageTargetProbeResult(
                targets=[],
                metadata={
                    "status": "unavailable",
                    "candidate_count": 0,
                    "url": expected_url or "",
                    "title": "",
                    "attempts": [],
                    "error": "probe_not_attempted",
                },
            )
        metadata = dict(last_result.metadata)
        metadata["readiness"] = {
            "strategy": "browser_use_stability",
            "quiet_ms": config.quiet_ms,
            "max_wait_ms": config.max_wait_ms,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "timed_out": time.monotonic() - started > max_wait,
            "timeline": timeline[-8:],
        }
        return _PageTargetProbeResult(targets=last_result.targets, metadata=metadata)

    async def _send_browser_use_cdp(
        self,
        browser_session: Any,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> Any:
        getter = getattr(browser_session, "get_or_create_cdp_session", None)
        target_id = getattr(browser_session, "agent_focus_target_id", None)
        cdp_session = None
        if callable(getter):
            try:
                cdp_session = await getter(target_id=target_id, focus=False) if target_id else await getter()
            except TypeError:
                cdp_session = await getter()
            except Exception:
                cdp_session = None

        client = getattr(cdp_session, "cdp_client", None) if cdp_session is not None else getattr(browser_session, "cdp_client", None)
        if client is None:
            raise RuntimeError("browser-use CDP client is unavailable")
        if session_id is not None:
            effective_session_id = session_id if session_id else None
        else:
            effective_session_id = getattr(cdp_session, "session_id", None) if cdp_session is not None else None
        try:
            return await _browser_use_send_cdp_command(
                client,
                method,
                params or {},
                session_id=effective_session_id if isinstance(effective_session_id, str) and effective_session_id else None,
            )
        except Exception as exc:
            if not _cdp_active_page_retry_allowed(method, exc) or not callable(getter):
                raise
            self._invalidate_browser_use_page_probe_cache(browser_session)
            try:
                fresh_session = await getter(target_id=None, focus=True)
            except TypeError:
                fresh_session = await getter()
            fresh_client = getattr(fresh_session, "cdp_client", None)
            fresh_session_id = getattr(fresh_session, "session_id", None)
            if fresh_client is None:
                raise
            return await _browser_use_send_cdp_command(
                fresh_client,
                method,
                params or {},
                session_id=fresh_session_id if isinstance(fresh_session_id, str) and fresh_session_id else None,
            )

    def _browser_use_page_probe_cache_key(
        self,
        browser_session: Any,
        expected_url: str | None,
        *,
        target_id_hint: str | None = None,
    ) -> tuple[int, str, str]:
        focus_target_id = getattr(browser_session, "agent_focus_target_id", None)
        return (
            id(browser_session),
            _normalize_probe_url(expected_url),
            str(target_id_hint or focus_target_id or ""),
        )

    def _invalidate_browser_use_page_probe_cache(self, browser_session: Any) -> None:
        session_id = id(browser_session)
        self._page_target_probe_cache = {
            key: entry for key, entry in self._page_target_probe_cache.items() if key[0] != session_id
        }
        self._page_mapping_preflight_cache = {
            key: entry for key, entry in self._page_mapping_preflight_cache.items() if key[0] != session_id
        }
        self._general_page_preflight_cache = {
            key: entry for key, entry in self._general_page_preflight_cache.items() if key[0] != session_id
        }

    async def _current_browser_use_url(self, browser_session: Any) -> str | None:
        getter = getattr(browser_session, "get_current_page_url", None)
        if callable(getter):
            try:
                return str(await getter())
            except Exception:
                pass

        target_id = getattr(browser_session, "agent_focus_target_id", None)
        session_manager = getattr(browser_session, "session_manager", None)
        if target_id and session_manager is not None:
            try:
                target = session_manager.get_target(target_id)
                return str(getattr(target, "url", "") or "")
            except Exception:
                pass
        return None

    @staticmethod
    def _browser_use_blocked_result_factory(event_type: str) -> Callable[[str, dict[str, Any]], Any] | None:
        if event_type == "ClickElementEvent":
            return _browser_use_click_blocked_result

        if event_type in {"TypeTextEvent", "UploadFileEvent", "SendKeysEvent", "NavigateToUrlEvent"}:
            return _raise_browser_use_blocked_error

        if event_type == "SelectDropdownOptionEvent":
            return lambda reason, metadata: {
                "success": "false",
                "error": reason,
                "short_term_memory": reason,
                "long_term_memory": reason,
                "acira": metadata,
            }

        return None


def _browser_use_click_blocked_result(reason: str, metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "validation_error": f"{ACIRA_GUARD_OUTCOME_BLOCKED_CONTINUE}: {reason}",
        "acira": metadata,
    }


def browser_use(
    *,
    config: AciraGuardConfig | dict[str, Any] | str | Path | None = None,
    log_file: str | Path | None = None,
    checkpoint_store: CheckpointStore | None = None,
    review_backend: Any | None = None,
    runtime_control: Any | None = None,
    hitl_event_timeout: float | None = 3600.0,
    hitl_freeze_mode: str | None = None,
) -> BrowserUseCDPProxy:
    resolved_config = AciraGuardConfig.from_file(config) if isinstance(config, (str, Path)) else config
    return BrowserUseCDPProxy(
        config=resolved_config,
        log_file=log_file,
        checkpoint_store=checkpoint_store,
        review_backend=review_backend,
        runtime_control=runtime_control,
        hitl_event_timeout=hitl_event_timeout,
        hitl_freeze_mode=hitl_freeze_mode,
    )


def protect(
    *,
    config: AciraGuardConfig | dict[str, Any] | str | Path | None = None,
    log_file: str | Path | None = None,
    checkpoint_store: CheckpointStore | None = None,
    review_backend: Any | None = None,
    runtime_control: Any | None = None,
    hitl_event_timeout: float | None = 3600.0,
    hitl_freeze_mode: str | None = None,
) -> BrowserUseCDPProxy:
    return browser_use(
        config=config,
        log_file=log_file,
        checkpoint_store=checkpoint_store,
        review_backend=review_backend,
        runtime_control=runtime_control,
        hitl_event_timeout=hitl_event_timeout,
        hitl_freeze_mode=hitl_freeze_mode,
    )


# Adapter contract factory: the core discovers adapters by importing the module and
# calling ``create`` (see acira/adapters/contract.py). The developer never calls this
# directly — they use ``acira.protect(config=...)``.
create = protect


def guard_target_from_browser_use_node(node: Any, *, url: str | None = None) -> GuardTarget | None:
    if node is None:
        return None

    attributes = getattr(node, "attributes", None)
    if not isinstance(attributes, dict):
        attributes = {}
    normalized_attributes = {str(key): str(value) for key, value in attributes.items() if value is not None}
    tag = _string_or_none(getattr(node, "tag_name", None) or getattr(node, "node_name", None))
    tag = tag.lower() if tag else None
    input_type = (normalized_attributes.get("type") or "").lower()
    role = (normalized_attributes.get("role") or "").lower() or None
    classes = tuple(token for token in (normalized_attributes.get("class") or "").split() if token)
    accessible_name = _browser_use_node_accessible_name(node)
    text = _node_text(node, normalized_attributes, accessible_name=accessible_name, tag=tag, input_type=input_type, role=role)
    dom_path = _browser_use_node_dom_path(node)
    nearby_text = accessible_name or _truncate(_safe_call(node, "get_all_children_text", 180) or "", 180)

    return GuardTarget(
        tag=tag,
        type=input_type or None,
        role=role,
        text=text,
        id=normalized_attributes.get("id") or None,
        classes=classes,
        aria_label=normalized_attributes.get("aria-label") or None,
        title=normalized_attributes.get("title") or None,
        name=normalized_attributes.get("name") or None,
        href=normalized_attributes.get("href") or None,
        url=url,
        nearby_text=nearby_text,
        parent_label=accessible_name,
        dom_path=dom_path,
        structure_signature=_browser_use_node_structure_signature(node),
        attributes=normalized_attributes,
        is_button=_is_button_like(tag, input_type, role),
        is_checkbox=input_type in {"checkbox", "radio"} or role == "checkbox",
        is_editable=tag in {"textarea"}
        or (tag == "input" and input_type not in {"button", "checkbox", "hidden", "image", "radio", "reset", "submit"}),
        is_form_control=tag in {"button", "input", "select", "textarea"},
        is_select=tag == "select" or role in {"combobox", "listbox", "option"},
        is_submit=_is_submit_like(tag, input_type, role, text),
        confidence=0.86,
        source="browser_use.event",
    )


GENERIC_BROWSER_USE_LABELS = {
    "button",
    "click",
    "control",
    "field",
    "input",
    "link",
    "select",
    "text",
    "textarea",
}


def _node_text(
    node: Any,
    attributes: dict[str, str],
    *,
    accessible_name: str | None = None,
    tag: str | None = None,
    input_type: str = "",
    role: str | None = None,
) -> str | None:
    button_like = _is_button_like(tag, input_type, role)
    if accessible_name and button_like:
        return accessible_name
    for key in ("aria-label", "title", "placeholder", "alt"):
        value = attributes.get(key)
        if value:
            return _truncate(re.sub(r"\s+", " ", value).strip(), 160)
    value = attributes.get("value")
    if value and not button_like:
        return _truncate(re.sub(r"\s+", " ", value).strip(), 160)
    text = _safe_call(node, "get_meaningful_text_for_llm") or _safe_call(node, "get_all_children_text", 2)
    if text and accessible_name and _is_generic_browser_use_label(text):
        return accessible_name
    if accessible_name and not text:
        return accessible_name
    return _truncate(re.sub(r"\s+", " ", text).strip(), 160) if text else None


def _is_generic_browser_use_label(value: str | None) -> bool:
    normalized = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return normalized in GENERIC_BROWSER_USE_LABELS


def _browser_use_target_needs_probe_enrichment(target: GuardTarget | None) -> bool:
    if target is None:
        return False
    return _is_generic_browser_use_label(target.text)


def _browser_use_target_is_specific(target: GuardTarget | None) -> bool:
    if target is None:
        return False
    if not _guard_target_is_interactive_click(target) and not _guard_target_is_safe_informational_click(target):
        return False
    for value in (target.text, target.aria_label, target.title, target.name, target.id, target.nearby_text, target.section_heading):
        label = re.sub(r"\s+", " ", str(value or "")).strip()
        if label and not _is_generic_browser_use_label(label):
            return True
    return False


SAFE_INFORMATIONAL_KINDS = {"image_preview", "product_detail", "detail", "informational"}
INFORMATIONAL_RISK_TOKENS = {
    "add",
    "authorize",
    "buy",
    "capture",
    "card",
    "cart",
    "checkout",
    "continue",
    "credit",
    "debit",
    "delete",
    "funding",
    "order",
    "pay",
    "payment",
    "place",
    "purchase",
    "quantity",
    "qty",
    "remove",
    "submit",
    "update",
}


def _guard_target_is_safe_informational_click(target: GuardTarget | None) -> bool:
    if target is None:
        return False
    if target.is_button or target.is_checkbox or target.is_editable or target.is_form_control or target.is_select or target.is_submit:
        return False
    signature = target.structure_signature if isinstance(target.structure_signature, dict) else {}
    informational = signature.get("informational") if isinstance(signature.get("informational"), dict) else {}
    if informational.get("safe") is not True:
        return False
    kind = _normalize_match_token(informational.get("kind") or signature.get("target_kind"))
    if kind not in SAFE_INFORMATIONAL_KINDS:
        return False
    label = _normalize_label_text(
        " ".join(str(value or "") for value in (target.text, target.aria_label, target.title, target.name, target.id))
    )
    if not label or _is_generic_browser_use_label(label):
        return False
    risk_text = _normalize_label_text(
        " ".join(
            str(value or "")
            for value in (
                target.text,
                target.aria_label,
                target.title,
                target.name,
                target.id,
                target.href,
                target.form_action,
                target.attributes or {},
            )
        )
    )
    return not any(token in INFORMATIONAL_RISK_TOKENS for token in risk_text.split())


def _metadata_source(metadata: dict[str, Any] | None) -> str | None:
    if not isinstance(metadata, dict):
        return None
    source = metadata.get("source")
    return str(source) if isinstance(source, str) and source else None


UNSAFE_BROWSER_USE_RESOLUTION_STATUSES = {
    "ambiguous_click_target_mismatch",
    "ambiguous_match",
    "noninteractive_probe_target",
}


def _browser_use_resolution_is_unsafe(metadata: dict[str, Any] | None) -> bool:
    if not isinstance(metadata, dict):
        return False
    status = metadata.get("status")
    return isinstance(status, str) and status in UNSAFE_BROWSER_USE_RESOLUTION_STATUSES


def _resolve_browser_use_click_probe_target(
    exact: GuardTarget | None,
    exact_metadata: dict[str, Any] | None,
    point: GuardTarget | None,
    point_metadata: dict[str, Any] | None,
) -> tuple[GuardTarget | None, dict[str, Any] | None]:
    if exact is None and point is None:
        return None, exact_metadata or point_metadata
    if exact is None:
        return point, _browser_use_probe_choice_metadata(
            source="browser_use.point_probe",
            status="point_only",
            exact=exact,
            point=point,
            exact_metadata=exact_metadata,
            point_metadata=point_metadata,
        )
    if point is None:
        return exact, exact_metadata

    if _browser_use_targets_equivalent(exact, point):
        selected = point if _browser_use_target_is_specific(point) else exact
        return selected, _browser_use_probe_choice_metadata(
            source=_metadata_source(point_metadata) or _metadata_source(exact_metadata) or selected.source,
            status="consistent",
            exact=exact,
            point=point,
            exact_metadata=exact_metadata,
            point_metadata=point_metadata,
        )

    if _guard_target_is_interactive_click(point) and _browser_use_target_is_specific(point):
        return point, _browser_use_probe_choice_metadata(
            source=_metadata_source(point_metadata) or "browser_use.point_probe",
            status="point_overrode_node_mismatch",
            exact=exact,
            point=point,
            exact_metadata=exact_metadata,
            point_metadata=point_metadata,
        )

    fallback = exact if exact is not None else point
    return fallback, _browser_use_probe_choice_metadata(
        source="browser_use.target_resolution",
        status="ambiguous_click_target_mismatch",
        exact=exact,
        point=point,
        exact_metadata=exact_metadata,
        point_metadata=point_metadata,
    )


def _browser_use_probe_choice_metadata(
    *,
    source: str,
    status: str,
    exact: GuardTarget | None,
    point: GuardTarget | None,
    exact_metadata: dict[str, Any] | None,
    point_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "source": source,
        "status": status,
    }
    if exact is not None:
        metadata["node_target"] = _target_display_label(exact)
        metadata["node_interactive"] = _guard_target_is_interactive_click(exact)
        metadata["node_specific"] = _browser_use_target_is_specific(exact)
    if point is not None:
        metadata["point_target"] = _target_display_label(point)
        metadata["point_interactive"] = _guard_target_is_interactive_click(point)
        metadata["point_specific"] = _browser_use_target_is_specific(point)
    if isinstance(exact_metadata, dict):
        metadata["node_probe"] = exact_metadata
    if isinstance(point_metadata, dict):
        metadata["point_probe"] = point_metadata
    return metadata


def _browser_use_targets_equivalent(left: GuardTarget | None, right: GuardTarget | None) -> bool:
    if left is None or right is None:
        return False
    if _normalize_probe_url(left.url) and _normalize_probe_url(right.url) and _normalize_probe_url(left.url) != _normalize_probe_url(right.url):
        return False
    for attr in ("dom_path", "id"):
        left_value = _normalize_match_token(getattr(left, attr, None))
        right_value = _normalize_match_token(getattr(right, attr, None))
        if left_value and right_value and left_value == right_value:
            return True
    if (
        _normalize_match_token(left.name)
        and _normalize_match_token(left.name) == _normalize_match_token(right.name)
        and _normalize_match_token(left.form_id) == _normalize_match_token(right.form_id)
    ):
        return True
    if left.control_index is not None and right.control_index is not None and left.control_index == right.control_index:
        return True
    if _best_rect_match_score(_structure_rect_candidates(left.structure_signature), _structure_rect_candidates(right.structure_signature)) >= 75:
        return True

    left_label = _normalize_label_text(_target_display_label(left))
    right_label = _normalize_label_text(_target_display_label(right))
    if left_label and right_label and left_label == right_label and not _is_generic_browser_use_label(left_label):
        left_kind = (_normalize_match_token(left.tag), _normalize_match_token(left.type), _normalize_match_token(left.role))
        right_kind = (_normalize_match_token(right.tag), _normalize_match_token(right.type), _normalize_match_token(right.role))
        return left_kind == right_kind
    return False


def _browser_use_node_structure_signature(node: Any) -> dict[str, Any] | None:
    signature: dict[str, Any] = {}
    for key in ("node_id", "backend_node_id"):
        value = getattr(node, key, None)
        if value is not None:
            signature[key] = value
    target_id = _browser_use_node_target_id(node)
    session_id = _browser_use_node_session_id(node)
    if target_id:
        signature["target_id"] = target_id
    if session_id:
        signature["session_id"] = session_id
    dom_path = _browser_use_node_dom_path(node)
    if dom_path:
        signature["dom_path"] = dom_path
    rect_info = _browser_use_node_rect_with_source(node)
    if rect_info is not None:
        rect, source = rect_info
        signature["layout"] = {
            "rect": rect,
            "rect_source": source,
            "coordinate_space": _browser_use_rect_coordinate_space(source),
        }
    return signature or None


def _browser_use_node_rect(node: Any) -> dict[str, float] | None:
    rect_info = _browser_use_node_rect_with_source(node)
    return rect_info[0] if rect_info is not None else None


def _browser_use_node_rect_with_source(node: Any) -> tuple[dict[str, float], str] | None:
    snapshot = getattr(node, "snapshot_node", None)
    candidates = (
        ("bounds", getattr(node, "bounds", None)),
        ("absolute_position", getattr(node, "absolute_position", None)),
        ("snapshot.bounds", getattr(snapshot, "bounds", None) if snapshot is not None else None),
        ("snapshot.clientRects", getattr(snapshot, "clientRects", None) if snapshot is not None else None),
    )
    for source, raw in candidates:
        rect = _browser_use_rect_from_value(raw)
        if rect is not None:
            return rect, source
    return None


def _browser_use_rect_from_value(value: Any) -> dict[str, float] | None:
    if isinstance(value, (list, tuple)):
        for item in value:
            rect = _browser_use_rect_from_value(item)
            if rect is not None:
                return rect
        return None
    if value is None:
        return None
    values: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        raw = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return None
        values[key] = number
    if values["width"] < 0 or values["height"] < 0:
        return None
    return values


def _browser_use_rect_coordinate_space(source: str) -> str:
    if source in {"absolute_position", "snapshot.bounds"}:
        return "document"
    return "viewport"


def _browser_use_node_accessible_name(node: Any) -> str | None:
    ax_node = getattr(node, "ax_node", None)
    for value in (
        getattr(ax_node, "name", None) if ax_node is not None else None,
        ax_node.get("name") if isinstance(ax_node, dict) else None,
        getattr(node, "ax_name", None),
    ):
        normalized = _string_or_none(value)
        if normalized:
            return normalized
    return None


def _browser_use_node_dom_path(node: Any) -> str | None:
    for value in (getattr(node, "xpath", None), getattr(node, "x_path", None)):
        normalized = _string_or_none(value)
        if normalized:
            return normalized
    return None


def _browser_use_node_target_id(node: Any) -> str | None:
    return _string_or_none(getattr(node, "target_id", None))


def _browser_use_node_session_id(node: Any) -> str | None:
    return _string_or_none(getattr(node, "session_id", None))


def _safe_call(node: Any, method_name: str, *args: Any) -> str | None:
    method = getattr(node, method_name, None)
    if not callable(method):
        return None
    try:
        value = method(*args)
    except Exception:
        return None
    return str(value) if value is not None else None


def _is_button_like(tag: str | None, input_type: str, role: str | None) -> bool:
    return tag == "button" or role == "button" or input_type in {"button", "submit", "reset"}


def _is_submit_like(tag: str | None, input_type: str, role: str | None, text: str | None) -> bool:
    if input_type == "submit" or (tag == "button" and input_type in {"", "submit"}):
        return True
    if _is_button_like(tag, input_type, role) and text:
        return bool(re.search(r"\b(submit|send|continue|confirm|save|request)\b", text, re.IGNORECASE))
    return False


async def _classify_browser_use_overlay_helper(ctx: RuntimeHelperContext) -> ActionClassification | None:
    if not _is_browser_use_overlay_js(ctx.expression):
        return None
    rect = _browser_use_overlay_rect(ctx.expression)
    target = (
        await ctx.policy._probe_adapter_overlay_target(rect, ctx.cdp_context, BROWSER_USE_IGNORED_SELECTORS)  # noqa: SLF001
        if rect is not None
        else None
    )
    return ActionClassification(
        method=ctx.method,
        action="trusted_helper_js",
        reason="trusted framework visual overlay helper",
        confidence=0.92,
        meaningful=False,
        target=target,
        details=_runtime_js_details(
            ctx.expression,
            category=ctx.full_helper_id,
            intent="visual highlight overlay",
            source_field=ctx.source_field,
            target_hint=_runtime_target_hint_from_target(target) or ctx.target_hint,
            snippet_chars=ctx.policy.config.runtime_js_log_snippet_chars,
        ),
    )


def _classify_browser_use_setup_overlay_helper(ctx: RuntimeHelperContext) -> ActionClassification | None:
    normalized = _normalize_js(ctx.expression)
    if not normalized or not _looks_like_browser_use_setup_overlay(normalized):
        return None
    return ActionClassification(
        method=ctx.method,
        action="trusted_helper_js",
        reason="trusted framework setup overlay helper",
        confidence=0.94,
        meaningful=False,
        details=_runtime_js_details(
            ctx.expression,
            category=ctx.full_helper_id,
            intent="setup overlay",
            source_field=ctx.source_field,
            target_hint=ctx.target_hint,
            snippet_chars=ctx.policy.config.runtime_js_log_snippet_chars,
        ),
    )


async def _classify_browser_use_safe_select_helper(ctx: RuntimeHelperContext) -> ActionClassification | None:
    if ctx.method != "Runtime.callFunctionOn" or not _looks_like_browser_use_select_js(ctx.expression):
        return None
    target = await ctx.policy._probe_runtime_object_target(ctx.params, ctx.cdp_context)  # noqa: SLF001
    replacement_payload = _safe_native_select_payload(ctx.cdp_context, ctx.params)
    if target and target.is_select and replacement_payload is not None:
        return ActionClassification(
            method=ctx.method,
            action="select",
            reason="safe native select JavaScript selection",
            confidence=max(0.9, target.confidence),
            meaningful=True,
            target=target,
            details=_runtime_js_details(
                ctx.expression,
                category=ctx.full_helper_id,
                intent="native select option selection",
                source_field=ctx.source_field,
                target_hint=_runtime_target_hint_from_target(target) or ctx.target_hint,
                snippet_chars=ctx.policy.config.runtime_js_log_snippet_chars,
            ),
            replacement_payload=replacement_payload,
        )
    return None


async def _classify_browser_use_safe_range_helper(ctx: RuntimeHelperContext) -> ActionClassification | None:
    if ctx.method != "Runtime.callFunctionOn" or not _looks_like_browser_use_range_value_js(ctx.expression):
        return None
    target = await ctx.policy._probe_runtime_object_target(ctx.params, ctx.cdp_context)  # noqa: SLF001
    if target and target.tag == "input" and target.type == "range":
        return ActionClassification(
            method=ctx.method,
            action="form_input",
            reason="safe native range input value change",
            confidence=max(0.9, target.confidence),
            meaningful=True,
            target=target,
            details=_runtime_js_details(
                ctx.expression,
                category=ctx.full_helper_id,
                intent="native range slider value change",
                source_field=ctx.source_field,
                target_hint=_runtime_target_hint_from_target(target) or ctx.target_hint,
                snippet_chars=ctx.policy.config.runtime_js_log_snippet_chars,
            ),
        )
    return None


async def _classify_browser_use_text_mutation_helper(ctx: RuntimeHelperContext) -> ActionClassification | None:
    category = _browser_use_text_helper_category(ctx.expression)
    if ctx.method != "Runtime.callFunctionOn" or category is None:
        return None
    target = await ctx.policy._probe_runtime_object_target(ctx.params, ctx.cdp_context)  # noqa: SLF001
    return ActionClassification(
        method=ctx.method,
        action="dangerous_js",
        reason="JavaScript contains framework text helper mutation",
        confidence=0.9,
        meaningful=True,
        target=target,
        details=_runtime_js_details(
            ctx.expression,
            category=f"{ctx.full_helper_id}.{category}",
            intent="text input helper",
            source_field=ctx.source_field,
            target_hint=_runtime_target_hint_from_target(target) or ctx.target_hint,
            snippet_chars=ctx.policy.config.runtime_js_log_snippet_chars,
        ),
    )


def _is_browser_use_overlay_js(expression: str) -> bool:
    normalized = _normalize_js(expression)
    lowered = normalized.lower()
    if not lowered:
        return False
    if not any(marker in lowered for marker in BROWSER_USE_OVERLAY_MARKERS):
        return False
    if any(pattern.search(normalized) for pattern in OVERLAY_DISALLOWED_PATTERNS):
        return False
    for pattern, _reason in SENSITIVE_JS_PATTERNS:
        if pattern.search(normalized):
            return False
    is_removal = bool(re.search(r"\.\s*remove\s*\(", normalized, re.IGNORECASE))
    creates_overlay = bool(re.search(r"\bcreateElement\s*\(", normalized, re.IGNORECASE)) or bool(
        re.search(r"\bappendChild\s*\(", normalized, re.IGNORECASE)
    )
    pointer_events_none = "pointer-events: none" in lowered or bool(
        re.search(r"pointerEvents\s*=\s*['\"]none['\"]", normalized, re.IGNORECASE)
    )
    return (is_removal and not creates_overlay) or (creates_overlay and pointer_events_none)


def _looks_like_browser_use_select_js(expression: str) -> bool:
    normalized = _normalize_js(expression).lower()
    return (
        "attemptselection" in normalized
        and "targettext" in normalized
        and "array.from(element.options" in normalized
        and bool(re.search(r"tagname\s*\.\s*tolowercase\s*\(\s*\)\s*={2,3}\s*['\"]select['\"]", normalized))
    )


def _looks_like_browser_use_range_value_js(expression: str) -> bool:
    normalized = _normalize_js(expression).lower()
    if re.search(r"\.\s*(?:click|submit|requestsubmit)\s*\(", normalized):
        return False
    if any(token in normalized for token in ("mouseevent", "keyboardevent", "pointerevent", "submitevent")):
        return False
    if re.search(r"\b(?:setattribute|removeattribute|fetch|eval)\s*\(", normalized):
        return False
    if any(token in normalized for token in ("xmlhttprequest", "sendbeacon", "websocket")):
        return False
    return (
        "this.value" in normalized
        and "object.getownpropertydescriptor" in normalized
        and "dispatchevent" in normalized
        and bool(re.search(r"new\s+event\s*\(\s*['\"]input['\"]", normalized))
        and bool(re.search(r"new\s+event\s*\(\s*['\"]change['\"]", normalized))
    )


def _browser_use_text_helper_category(expression: str) -> str | None:
    normalized = _normalize_js(expression).lower()
    if not normalized:
        return None
    if re.search(r"\.\s*(?:click|submit|requestsubmit)\s*\(", normalized):
        return None
    if re.search(r"\b(?:requestsubmit|submit|fetch|eval|setattribute|removeattribute)\s*\(", normalized):
        return None
    if any(token in normalized for token in ("xmlhttprequest", "sendbeacon", "websocket", "localstorage", "sessionstorage")):
        return None
    if not normalized.startswith("function"):
        return None

    dom_property_assignment = bool(
        re.search(
            r"\.\s*(?:value|checked|selected|selectedindex|disabled|readonly|required|type|name|action|method)\s*=(?![=>])",
            normalized,
        )
    )
    literal_input_event = bool(re.search(r"new\s+inputevent\s*\(\s*['\"]input['\"]", normalized)) or bool(
        re.search(r"new\s+event\s*\(\s*['\"]input['\"]", normalized)
    )
    literal_change_event = bool(re.search(r"new\s+event\s*\(\s*['\"]change['\"]", normalized))
    config_event_dispatch = bool(re.search(r"new\s+event\s*\(\s*eventconfig\.type\b", normalized))
    configured_input_event = config_event_dispatch and bool(re.search(r"\btype\s*:\s*['\"]input['\"]", normalized))
    configured_change_event = config_event_dispatch and bool(re.search(r"\btype\s*:\s*['\"]change['\"]", normalized))
    has_text_events = (literal_input_event or configured_input_event) and (
        literal_change_event or configured_change_event
    )

    is_event_sync = (
        ("const element = this" in normalized or "element=this" in normalized or "element = this" in normalized)
        and "element.focus" in normalized
        and "dispatchevent" in normalized
        and not dom_property_assignment
    )
    if is_event_sync and has_text_events:
        return "event_helper_js"

    is_clear_helper = (
        "hascontenteditable" in normalized
        and "this.value" in normalized
        and "this.textcontent" in normalized
        and ("this.value = \"\"" in normalized or "this.value=''" in normalized or "this.value = ''" in normalized)
        and "dispatchevent" in normalized
        and has_text_events
    )
    if is_clear_helper:
        return "clear_helper_js"

    value_assignment = bool(re.search(r"\b(?:this|element)\s*\.\s*(?:value|textcontent)\s*=\s*newvalue\b", normalized))
    native_setter = "object.getownpropertydescriptor" in normalized and ".set.call(this, newvalue)" in normalized
    direct_native_setter = "nativeinputvaluesetter.call(this" in normalized and "object.getownpropertydescriptor" in normalized
    is_value_helper = (
        (("newvalue" in normalized and (value_assignment or native_setter)) or direct_native_setter)
        and "dispatchevent" in normalized
        and has_text_events
    )
    if is_value_helper:
        return "value_helper_js"
    return None


def _looks_like_browser_use_setup_overlay(expression: str) -> bool:
    normalized = _normalize_js(expression)
    lowered = normalized.lower()
    required_markers = (
        "__dvdanimationrunning",
        "pretty-loading-animation",
        "browser_session_label",
        "starting agent",
        "cf.browser-use.com/logo.svg",
    )
    if not all(marker in lowered for marker in required_markers):
        return False
    if "document.createelement('div')" not in lowered and 'document.createelement("div")' not in lowered:
        return False
    if "document.body.appendchild(loadingoverlay)" not in lowered:
        return False
    if any(token in lowered for token in ("queryselector('form", 'queryselector("form', ".click(", ".submit(", "requestsubmit(")):
        return False
    return True


def _browser_use_overlay_rect(expression: str) -> tuple[float, float, float, float] | None:
    rect_match = re.search(r"\brect\s*=\s*(\{[^{}]{1,700}\})", expression, re.IGNORECASE | re.DOTALL)
    if not rect_match:
        return None
    try:
        parsed = json.loads(rect_match.group(1))
    except json.JSONDecodeError:
        parsed = {}

    def read_number(name: str) -> float | None:
        value = parsed.get(name) if isinstance(parsed, dict) else None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return None
        return None

    x = read_number("x")
    y = read_number("y")
    width = read_number("width")
    height = read_number("height")
    if x is None or y is None or width is None or height is None:
        return None
    if width < 0 or height < 0:
        return None
    return (x, y, width, height)


def _browser_use_point_probe_expressions(
    rect: dict[str, float],
    ignored_selectors: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    x = rect["x"] + rect["width"] / 2
    y = rect["y"] + rect["height"] / 2
    point_probe = _point_probe_js(ignored_selectors)
    return (
        (
            "raw",
            f"({point_probe})({json.dumps(x)}, {json.dumps(y)})",
        ),
        (
            "document_to_viewport",
            (
                "(() => {"
                " const sx = Number(window.scrollX || window.pageXOffset || 0);"
                " const sy = Number(window.scrollY || window.pageYOffset || 0);"
                f" return ({point_probe})({json.dumps(x)} - sx, {json.dumps(y)} - sy);"
                "})()"
            ),
        ),
    )


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: max(0, limit - 1)]}..."


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _browser_use_target_candidate_score(target: dict[str, Any], candidate: dict[str, Any]) -> int:
    score = 0
    target_attrs = target.get("attributes") if isinstance(target.get("attributes"), dict) else {}
    candidate_attrs = candidate.get("attributes") if isinstance(candidate.get("attributes"), dict) else {}
    target_structure = target.get("structure_signature") if isinstance(target.get("structure_signature"), dict) else {}
    candidate_structure = (
        candidate.get("structureSignature")
        if isinstance(candidate.get("structureSignature"), dict)
        else candidate.get("structure_signature")
        if isinstance(candidate.get("structure_signature"), dict)
        else {}
    )

    rect_score = _best_rect_match_score(
        _structure_rect_candidates(target_structure),
        _structure_rect_candidates(candidate_structure),
    )
    score += rect_score

    for key, weight in (("id", 100), ("name", 80)):
        expected = _normalize_match_token(target.get(key) or target_attrs.get(key))
        observed = _normalize_match_token(candidate.get(key) or candidate_attrs.get(key))
        if expected and observed and expected == observed:
            score += weight

    target_dom_path = _normalize_dom_path(
        target.get("dom_path")
        or target.get("domPath")
        or target_structure.get("dom_path")
        or target_structure.get("domPath")
    )
    candidate_dom_path = _normalize_dom_path(
        candidate.get("domPath")
        or candidate.get("dom_path")
        or candidate_structure.get("dom_path")
        or candidate_structure.get("domPath")
    )
    if target_dom_path and candidate_dom_path and target_dom_path == candidate_dom_path:
        score += 95

    target_tag = _normalize_match_token(target.get("tag"))
    candidate_tag = _normalize_match_token(candidate.get("tag"))
    if target_tag and candidate_tag and target_tag == candidate_tag:
        score += 12

    target_type = _normalize_match_token(target.get("type") or target_attrs.get("type"))
    candidate_type = _normalize_match_token(candidate.get("type") or candidate_attrs.get("type"))
    if target_type and candidate_type and target_type == candidate_type:
        score += 10

    target_text = _normalize_label_text(
        " ".join(
            str(value or "")
            for value in (
                target.get("text"),
                target.get("parent_label"),
                target.get("nearby_text"),
                target.get("aria_label"),
                target.get("title"),
            )
        )
    )
    candidate_text = _normalize_label_text(
        " ".join(
            str(value or "")
            for value in (
                candidate.get("text"),
                candidate.get("parentLabel"),
                candidate.get("parent_label"),
                candidate.get("nearbyText"),
                candidate.get("nearby_text"),
                candidate.get("ariaLabel"),
                candidate.get("aria_label"),
                candidate.get("title"),
            )
        )
    )
    if target_text and candidate_text:
        if target_text == candidate_text:
            score += 45
        elif target_text in candidate_text or candidate_text in target_text:
            score += 28
            if _is_interactive_click_candidate(candidate) and not _is_interactive_click_candidate(target):
                score += 25
        else:
            overlap = set(target_text.split()) & set(candidate_text.split())
            if overlap:
                score += min(24, 8 * len(overlap))

    return score


CLICK_INTERACTIVE_TAGS = {"a", "button", "input", "option", "select", "summary", "textarea"}
CLICK_INTERACTIVE_ROLES = {
    "button",
    "checkbox",
    "combobox",
    "link",
    "menuitem",
    "option",
    "radio",
    "switch",
    "tab",
}
CLICK_INTERACTIVE_INPUT_TYPES = {"button", "checkbox", "radio", "reset", "submit"}


def _click_candidate_is_less_precise(target: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if _is_interactive_click_candidate(candidate):
        return False
    if _is_safe_informational_click_candidate(candidate):
        return False
    return _is_interactive_click_candidate(target) or _has_browser_use_rect(target)


def _is_interactive_click_candidate(value: dict[str, Any]) -> bool:
    tag = _normalize_match_token(value.get("tag"))
    role = _normalize_match_token(value.get("role") or _candidate_attributes(value).get("role"))
    input_type = _normalize_match_token(value.get("type") or _candidate_attributes(value).get("type"))
    if bool(value.get("isButton") or value.get("is_button")):
        return True
    if bool(value.get("isFormControl") or value.get("is_form_control")) and tag in CLICK_INTERACTIVE_TAGS:
        return True
    return tag in CLICK_INTERACTIVE_TAGS or role in CLICK_INTERACTIVE_ROLES or input_type in CLICK_INTERACTIVE_INPUT_TYPES


def _is_safe_informational_click_candidate(value: dict[str, Any]) -> bool:
    if bool(value.get("isButton") or value.get("is_button") or value.get("isFormControl") or value.get("is_form_control")):
        return False
    structure = value.get("structureSignature") if isinstance(value.get("structureSignature"), dict) else value.get("structure_signature")
    if not isinstance(structure, dict):
        return False
    informational = structure.get("informational") if isinstance(structure.get("informational"), dict) else {}
    if informational.get("safe") is not True:
        return False
    kind = _normalize_match_token(informational.get("kind") or structure.get("target_kind"))
    if kind not in SAFE_INFORMATIONAL_KINDS:
        return False
    label = _normalize_label_text(
        " ".join(
            str(value.get(key) or "")
            for key in ("text", "ariaLabel", "aria_label", "title", "name", "id")
        )
    )
    if not label or _is_generic_browser_use_label(label):
        return False
    risk_text = _normalize_label_text(
        " ".join(
            str(value.get(key) or "")
            for key in ("text", "ariaLabel", "aria_label", "title", "name", "id", "href")
        )
    )
    return not any(token in INFORMATIONAL_RISK_TOKENS for token in risk_text.split())


def _guard_target_is_interactive_click(target: GuardTarget | None) -> bool:
    if target is None:
        return False
    tag = _normalize_match_token(target.tag)
    role = _normalize_match_token(target.role)
    input_type = _normalize_match_token(target.type)
    return bool(
        target.is_button
        or target.is_checkbox
        or target.is_select
        or target.is_form_control and tag in CLICK_INTERACTIVE_TAGS
        or tag in CLICK_INTERACTIVE_TAGS
        or role in CLICK_INTERACTIVE_ROLES
        or input_type in CLICK_INTERACTIVE_INPUT_TYPES
    )


def _candidate_attributes(value: dict[str, Any]) -> dict[str, Any]:
    attrs = value.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _has_browser_use_rect(target: dict[str, Any]) -> bool:
    structure = target.get("structure_signature") if isinstance(target.get("structure_signature"), dict) else {}
    if not structure:
        structure = target.get("structureSignature") if isinstance(target.get("structureSignature"), dict) else {}
    layout = structure.get("layout") if isinstance(structure.get("layout"), dict) else {}
    return _browser_use_rect_from_value(layout.get("browser_use_rect")) is not None


def _structure_rect(structure: Any) -> dict[str, float] | None:
    rects = _structure_rect_candidates(structure)
    return rects[0] if rects else None


def _structure_rect_candidates(structure: Any) -> tuple[dict[str, float], ...]:
    if not isinstance(structure, dict):
        return ()
    layout = structure.get("layout") if isinstance(structure.get("layout"), dict) else {}
    rects: list[dict[str, float]] = []
    for key in ("rect", "viewport_rect", "document_rect", "browser_use_rect"):
        rect = layout.get(key)
        parsed = _browser_use_rect_from_value(rect)
        if parsed is not None and parsed not in rects:
            rects.append(parsed)
    return tuple(rects)


def _best_rect_match_score(left_rects: tuple[dict[str, float], ...], right_rects: tuple[dict[str, float], ...]) -> int:
    if not left_rects or not right_rects:
        return 0
    return max(_rect_match_score(left, right) for left in left_rects for right in right_rects)


def _rect_overlap_ratio(left: dict[str, float], right: dict[str, float]) -> float:
    left_x2 = left["x"] + left["width"]
    left_y2 = left["y"] + left["height"]
    right_x2 = right["x"] + right["width"]
    right_y2 = right["y"] + right["height"]
    overlap_width = max(0.0, min(left_x2, right_x2) - max(left["x"], right["x"]))
    overlap_height = max(0.0, min(left_y2, right_y2) - max(left["y"], right["y"]))
    overlap_area = overlap_width * overlap_height
    min_area = min(max(0.0, left["width"] * left["height"]), max(0.0, right["width"] * right["height"]))
    if min_area <= 0:
        return 0.0
    return overlap_area / min_area


def _rect_size_delta_ratio(left: dict[str, float], right: dict[str, float]) -> float:
    width_base = max(1.0, max(left["width"], right["width"]))
    height_base = max(1.0, max(left["height"], right["height"]))
    return max(abs(left["width"] - right["width"]) / width_base, abs(left["height"] - right["height"]) / height_base)


def _rect_center_delta(left: dict[str, float], right: dict[str, float]) -> tuple[float, float]:
    return (
        abs((left["x"] + left["width"] / 2) - (right["x"] + right["width"] / 2)),
        abs((left["y"] + left["height"] / 2) - (right["y"] + right["height"] / 2)),
    )


def _target_with_browser_use_node_rect(target: GuardTarget, node: Any) -> GuardTarget:
    node_rect = _browser_use_node_rect(node)
    if node_rect is None:
        return target
    signature = dict(target.structure_signature or {})
    layout = dict(signature.get("layout") if isinstance(signature.get("layout"), dict) else {})
    if _structure_rect(target.structure_signature) is None:
        layout["rect"] = node_rect
    layout["browser_use_rect"] = node_rect
    signature["layout"] = layout
    return replace(target, structure_signature=signature)


def _target_display_label(target: GuardTarget) -> str:
    for value in (target.text, target.aria_label, target.title, target.name, target.id, target.nearby_text):
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if text:
            return _truncate(text, 80)
    return target.tag or "target"


def _rect_match_score(left: dict[str, float], right: dict[str, float]) -> int:
    center_dx, center_dy = _rect_center_delta(left, right)
    size_dw = abs(left["width"] - right["width"])
    size_dh = abs(left["height"] - right["height"])
    overlap = _rect_overlap_ratio(left, right)
    size_delta = _rect_size_delta_ratio(left, right)
    if overlap >= 0.82 and size_delta <= 0.25:
        return 90
    if overlap >= 0.55 and size_delta <= 0.35:
        return 75
    if center_dx <= 4 and center_dy <= 4 and size_dw <= 6 and size_dh <= 6:
        return 90
    if center_dx <= 12 and center_dy <= 12:
        return 70
    if center_dx <= 24 and center_dy <= 24:
        return 45
    return 0


def _runtime_response_value(response: Any) -> Any:
    if not isinstance(response, dict):
        return None
    result = response.get("result")
    if not isinstance(result, dict):
        return None
    remote = result.get("result")
    if isinstance(remote, dict) and "value" in remote:
        return remote.get("value")
    if "value" in result:
        return result.get("value")
    return None


def _remote_object_id_from_response(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    result = response.get("result")
    if not isinstance(result, dict):
        result = {}
    candidates = []
    if isinstance(response.get("object"), dict):
        candidates.append(response["object"])
    if isinstance(result.get("object"), dict):
        candidates.append(result["object"])
    if isinstance(result.get("result"), dict):
        candidates.append(result["result"])
    for candidate in candidates:
        object_id = candidate.get("objectId")
        if isinstance(object_id, str) and object_id:
            return object_id
    return None


def _apply_navigation_speed_defaults(event: Any) -> None:
    wait_until = os.getenv("ACIRA_BROWSER_USE_NAV_WAIT_UNTIL", "").strip().lower()
    if wait_until in {"commit", "domcontentloaded", "load", "networkidle"} and hasattr(event, "wait_until"):
        event.wait_until = wait_until

    timeout_ms = _positive_int_from_env("ACIRA_BROWSER_USE_NAV_TIMEOUT_MS")
    if timeout_ms is not None and hasattr(event, "timeout_ms"):
        current = getattr(event, "timeout_ms", None)
        if current is None or current > timeout_ms:
            event.timeout_ms = timeout_ms


def _positive_int_from_env(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return None
    try:
        parsed = int(str(raw).strip())
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _positive_int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


async def _browser_use_runtime_evaluate(
    client: Any,
    params: dict[str, Any],
    *,
    session_id: str | None = None,
) -> Any:
    send = getattr(client, "send", None)
    runtime = getattr(send, "Runtime", None)
    evaluate = getattr(runtime, "evaluate", None)
    if callable(evaluate):
        if session_id:
            try:
                return await evaluate(params=params, session_id=session_id)
            except TypeError:
                pass
        return await evaluate(params=params)

    if callable(send):
        if session_id:
            for kwargs in (
                {"method": "Runtime.evaluate", "params": params, "session_id": session_id},
                {"params": params, "session_id": session_id},
            ):
                try:
                    return await send(**kwargs)
                except TypeError:
                    continue
        for args in (("Runtime.evaluate", params),):
            try:
                return await send(*args)
            except TypeError:
                continue
        return await send("Runtime.evaluate", params=params)

    raise RuntimeError("browser-use CDP client has no Runtime.evaluate entrypoint")


async def _browser_use_send_cdp_command(
    client: Any,
    method: str,
    params: dict[str, Any],
    *,
    session_id: str | None = None,
) -> Any:
    send = getattr(client, "send", None)
    domain_name, _, command_name = method.partition(".")
    domain = getattr(send, domain_name, None) if domain_name and command_name else None
    command = getattr(domain, command_name, None) if domain is not None else None
    if callable(command):
        if session_id:
            try:
                return await command(params=params, session_id=session_id)
            except TypeError:
                pass
        try:
            return await command(params=params)
        except TypeError:
            return await command(params)

    if callable(send):
        if session_id:
            for kwargs in (
                {"method": method, "params": params, "session_id": session_id},
                {"params": params, "session_id": session_id},
            ):
                try:
                    return await send(**kwargs)
                except TypeError:
                    continue
        for args in ((method, params),):
            try:
                return await send(*args)
            except TypeError:
                continue
        return await send(method, params=params)

    raise RuntimeError(f"browser-use CDP client has no {method} entrypoint")


def _cdp_active_page_retry_allowed(method: str, exc: BaseException) -> bool:
    if method not in {
        "Page.captureScreenshot",
        "Page.getLayoutMetrics",
        "Runtime.evaluate",
        "Overlay.highlightNode",
        "Overlay.highlightRect",
        "Overlay.hideHighlight",
    }:
        return False
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in ("not attached", "session closed", "connection closed", "target closed", "detached"))


def _browser_use_probe_url_matches(expected_url: str | None, observed_url: str | None) -> bool:
    if not expected_url or not observed_url:
        return True
    expected = urlparse(expected_url)
    observed = urlparse(observed_url)
    if expected.scheme and observed.scheme and expected.scheme != observed.scheme:
        return False
    if expected.netloc and observed.netloc and expected.netloc != observed.netloc:
        return False
    expected_path = expected.path or "/"
    observed_path = observed.path or "/"
    return expected_path == observed_path


def _browser_use_frame_document_node_probe_is_safe(
    *,
    action: str | None,
    target: GuardTarget,
    target_id: str | None,
    session_id: str | None,
    target_id_hint: str | None,
    session_id_hint: str | None,
) -> bool:
    if action not in {"form_input", "select"}:
        return False
    if not (target.is_form_control or target.is_editable):
        return False
    return bool(
        (target_id_hint and target_id and target_id == target_id_hint)
        or (session_id_hint and session_id and session_id == session_id_hint)
    )


def _normalize_probe_url(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    return f"{scheme}://{netloc}{path}" if scheme or netloc else path


def _normalize_match_token(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _normalize_dom_path(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def _normalize_label_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower()).strip()


def _raise_browser_use_blocked_error(reason: str, metadata: dict[str, Any]) -> None:
    from browser_use.browser.views import BrowserError

    raise BrowserError(
        message=reason,
        short_term_memory=reason,
        long_term_memory=reason,
        details={"acira": metadata},
    )


def _safe_secret_fill_dispatch_error(exc: Exception) -> str:
    return redact_value(f"Acira secret fill dispatch failed: {type(exc).__name__}")


def _browser_use_error_from_acira_exception(exc: Exception) -> Exception | None:
    from browser_use.browser.views import BrowserError

    if isinstance(exc, BrowserError) and getattr(exc, "long_term_memory", None):
        return None

    message = _extract_acira_error_text(exc)
    if message is None:
        return None
    return BrowserError(
        message=message,
        short_term_memory=message,
        long_term_memory=message,
        details={"acira": {"normalized_from": type(exc).__name__}},
    )


def _extract_acira_error_text(exc: BaseException) -> str | None:
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        for text in (str(current), repr(current)):
            extracted = _extract_acira_message_from_text(text)
            if extracted:
                return extracted
        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        if isinstance(cause, BaseException):
            stack.append(cause)
        if isinstance(context, BaseException):
            stack.append(context)
    return None


def _extract_acira_message_from_text(text: str) -> str | None:
    if "ACIRA_POLICY_BLOCKED" not in text:
        return None
    index = text.find("ACIRA_POLICY_BLOCKED")
    tail = text[index:]
    cut_points = [
        position
        for marker in ("', 'data':", '", "data":', "'},", '"},', "})", "}'", '"}')
        for position in [tail.find(marker)]
        if position > 0
    ]
    if cut_points:
        tail = tail[: min(cut_points)]
    return re.sub(r"\s+", " ", tail).strip()[:1200]


def _normalize_hitl_freeze_mode(value: str | None) -> str:
    normalized = str(value or "strict").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized not in {"strict", "timeout_only", "off"}:
        raise ValueError(f"Unsupported hitl_freeze_mode: {value!r}")
    return normalized


class _AsyncioReviewPauseProxy:
    """Module proxy that makes BrowserUse agent-service timeouts pause-aware."""

    def __init__(self, asyncio_module: Any) -> None:
        self._asyncio = asyncio_module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._asyncio, name)

    async def wait_for(self, fut: Any, timeout: float | None = None) -> Any:
        return await wait_with_review_pause(fut, timeout)


def _active_browser_use_proxy() -> BrowserUseCDPProxy | None:
    current = _CURRENT_BROWSER_USE_PROXY.get()
    if current in _ACTIVE_BROWSER_USE_PROXIES:
        return current
    return _ACTIVE_BROWSER_USE_PROXIES[0] if len(_ACTIVE_BROWSER_USE_PROXIES) == 1 else None


async def _acira_secret_fill_browser_use(
    proxy: BrowserUseCDPProxy,
    browser_session: Any,
    *,
    secret_id: str,
    index: int,
    clear: bool = True,
) -> Any:
    from browser_use.agent.views import ActionResult
    from browser_use.browser.events import TypeTextEvent

    broker = proxy.secret_broker
    try:
        secret_use = broker.metadata_for_fill(secret_id)
    except SecretBrokerError as exc:
        return ActionResult(error=f"Acira secret fill failed: {exc}")

    node = await browser_session.get_element_by_index(index)
    if node is None:
        return ActionResult(error=f"Element index {index} is not available; the secret was not fetched.")

    url = await _browser_use_current_page_url(browser_session)
    target = guard_target_from_browser_use_node(node, url=url)
    target_resolved = _browser_use_target_is_specific(target)
    details = {
        "event_type": "AciraSecretFillAction",
        "element_index": index,
        "clear": bool(clear),
        "secret_use": secret_use.to_metadata(),
    }
    request = ActionRequest(
        action="form_input",
        framework=BROWSER_USE_FRAMEWORK_ID,
        target=target,
        details=details,
        reason=f'Acira secret fill for "{secret_use.label}"',
        confidence=max(0.82, target.confidence if target else 0.0),
        source="acira.secrets",
        display_name="secret fill",
        opens_cdp_followup_transaction=True,
        release_pause_on_review_exit=True,
        supervised_runtime=True,
        target_resolved=target_resolved,
        target_resolution_source=target.source if target else None,
        target_resolution_confidence=target.confidence if target else None,
    )

    async def execute_fill() -> Any:
        try:
            plaintext = broker.resolve_for_fill(secret_use)
            broker.register_field_for_fill(
                secret_use,
                target.to_metadata() if target is not None else None,
            )
        except SecretBrokerError as exc:
            return ActionResult(error=f"Acira secret fill failed: {exc}")
        with contextlib.suppress(Exception):
            await browser_session.highlight_interaction_element(node)
        try:
            type_event = TypeTextEvent(
                node=node,
                text=plaintext,
                clear=clear,
                is_sensitive=True,
                sensitive_key_name=secret_use.label,
            )
            internal_context = _InternalSecretFillContext(
                proxy_id=id(proxy),
                browser_session_id=id(browser_session),
                event_id=id(type_event),
                node_identity=_browser_use_node_identity(getattr(type_event, "node", node)),
                secret_id=secret_use.id,
                element_index=index,
            )
            setattr(type_event, "_acira_internal_secret_fill", internal_context)
            context_token = _ACIRA_INTERNAL_SECRET_FILL.set(internal_context)
            try:
                event = browser_session.event_bus.dispatch(type_event)
                await event
                input_metadata = await event.event_result(raise_if_any=True, raise_if_none=False)
            finally:
                _ACIRA_INTERNAL_SECRET_FILL.reset(context_token)
        except Exception as exc:
            return ActionResult(error=_safe_secret_fill_dispatch_error(exc))
        message = f'Filled secret "{secret_use.label}" into element {index}'
        result_metadata = {
            "acira": {
                "secret_id": secret_use.id,
                "secret_label": secret_use.label,
                "secret_filled": True,
                "element_index": index,
            }
        }
        if isinstance(input_metadata, dict):
            result_metadata["input"] = redact_value(input_metadata)
        return ActionResult(
            extracted_content=message,
            long_term_memory=message,
            metadata=result_metadata,
        )

    def blocked_result(reason: str, metadata: dict[str, Any]) -> Any:
        return ActionResult(
            error=reason,
            metadata={"acira": redact_value(metadata)},
        )

    return await proxy.action_gateway.guard_execute(
        request,
        execute_fill,
        blocked_result=blocked_result,
    )


def _ensure_acira_secret_action_registered(agent: Any) -> None:
    proxy = _active_browser_use_proxy()
    if proxy is None or not proxy.guard_policy.config.secrets.enabled:
        return
    tools = getattr(agent, "tools", None)
    registry = getattr(tools, "registry", None)
    raw_registry = getattr(registry, "registry", None)
    actions = getattr(raw_registry, "actions", None)
    if not isinstance(actions, dict):
        return
    try:
        from pydantic import BaseModel, Field
        from browser_use.agent.views import ActionResult
        from browser_use.browser.session import BrowserSession
    except Exception:
        return

    class AciraSecretFillAction(BaseModel):
        secret_id: str = Field(description="Configured Acira secret id to fill.")
        index: int = Field(ge=0, description="Browser-use element index to fill.")
        clear: bool = Field(default=True, description="Clear the field before filling.")

    class AciraListSecretsAction(BaseModel):
        pass

    registered_any = False

    async def acira_list_secrets(params: AciraListSecretsAction) -> Any:
        active_proxy = _active_browser_use_proxy() or proxy
        catalog = active_proxy.secret_broker.list_catalog()
        payload = json.dumps({"secrets": catalog}, ensure_ascii=True, separators=(",", ":"))
        return ActionResult(
            extracted_content=payload,
            long_term_memory=payload,
            metadata={"acira": {"secrets": catalog}},
        )

    if ACIRA_SECRET_CATALOG_ACTION_NAME not in actions:
        registry.action(
            "List active Acira-managed secrets available for this run. Returns only secret ids and labels, never values.",
            param_model=AciraListSecretsAction,
        )(acira_list_secrets)
        registered_any = True

    async def acira_secret_fill(params: AciraSecretFillAction, browser_session: BrowserSession) -> Any:
        active_proxy = _active_browser_use_proxy() or proxy
        return await _acira_secret_fill_browser_use(
            active_proxy,
            browser_session,
            secret_id=params.secret_id,
            index=params.index,
            clear=params.clear,
        )

    if ACIRA_SECRET_ACTION_NAME not in actions:
        acira_secret_fill.__annotations__["browser_session"] = BrowserSession
        registry.action(
            "Fill an Acira-managed secret into an element by index. Use this instead of typing passwords, tokens, or other secrets.",
            param_model=AciraSecretFillAction,
        )(acira_secret_fill)
        registered_any = True

    if registered_any and hasattr(agent, "_setup_action_models"):
        _rebuild_agent_action_models_after_acira_registration(agent)


def _rebuild_agent_action_models_after_acira_registration(agent: Any) -> None:
    try:
        agent._setup_action_models()
        _reconvert_agent_initial_actions(agent)
    except Exception as exc:
        raise RuntimeError("Acira could not revalidate browser-use action models after registering secret tools.") from exc


def _reconvert_agent_initial_actions(agent: Any) -> None:
    initial_actions = getattr(agent, "initial_actions", None)
    if not initial_actions:
        return
    converter = getattr(agent, "_convert_initial_actions", None)
    if not callable(converter):
        raise RuntimeError("browser-use agent cannot reconvert initial actions after action model rebuild")

    initial_actions_dict: list[dict[str, Any]] = []
    for action in initial_actions:
        if hasattr(action, "model_dump"):
            action_dump = action.model_dump(exclude_unset=True)
        elif isinstance(action, dict):
            action_dump = dict(action)
        else:
            raise TypeError(f"Unsupported browser-use initial action type: {type(action).__name__}")
        if not isinstance(action_dump, dict):
            raise TypeError("browser-use initial action did not serialize to an object")
        initial_actions_dict.append(action_dump)
    agent.initial_actions = converter(initial_actions_dict)


async def _browser_use_current_page_url(browser_session: Any) -> str | None:
    with contextlib.suppress(Exception):
        url = await browser_session.get_current_page_url()
        return str(url) if url else None
    return None


async def _browser_use_input_text_impl_with_acira_live_node(watchdog: Any, *args: Any, **kwargs: Any) -> Any:
    if _ORIGINAL_BROWSER_USE_INPUT_TEXT_ELEMENT_NODE_IMPL is None:
        raise RuntimeError("Acira BrowserUse text input patch is missing its original method")
    element_node = args[0] if args else kwargs.get("element_node")
    current_input_node = _browser_use_input_node_tracking_metadata(element_node)
    sentinel = object()
    previous_input_node = getattr(watchdog, "_acira_current_input_node", sentinel)
    if current_input_node:
        with contextlib.suppress(Exception):
            setattr(watchdog, "_acira_current_input_node", current_input_node)
    try:
        result = _ORIGINAL_BROWSER_USE_INPUT_TEXT_ELEMENT_NODE_IMPL(watchdog, *args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result
    finally:
        with contextlib.suppress(Exception):
            if previous_input_node is sentinel:
                delattr(watchdog, "_acira_current_input_node")
            else:
                setattr(watchdog, "_acira_current_input_node", previous_input_node)


async def _browser_use_trigger_framework_events_with_acira_live_object(
    watchdog: Any,
    object_id: str,
    cdp_session: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    if _ORIGINAL_BROWSER_USE_TRIGGER_FRAMEWORK_EVENTS is None:
        raise RuntimeError("Acira BrowserUse framework event patch is missing its original method")
    event_object_id = await _browser_use_framework_event_object_id(watchdog, object_id, cdp_session)
    return await _ORIGINAL_BROWSER_USE_TRIGGER_FRAMEWORK_EVENTS(
        watchdog,
        event_object_id,
        cdp_session,
        *args,
        **kwargs,
    )


def _browser_use_input_node_tracking_metadata(node: Any) -> dict[str, Any]:
    if node is None:
        return {}
    metadata: dict[str, Any] = {}
    for key in ("backend_node_id", "node_id", "target_id", "session_id"):
        value = getattr(node, key, None)
        if value is not None and str(value):
            metadata[key] = value
    return metadata


async def _browser_use_resolve_current_input_object_id(watchdog: Any, cdp_session: Any) -> str | None:
    current_input_node = getattr(watchdog, "_acira_current_input_node", None)
    if not isinstance(current_input_node, dict):
        return None
    backend_node_id = _positive_int_or_none(current_input_node.get("backend_node_id"))
    node_id = _positive_int_or_none(current_input_node.get("node_id"))
    if backend_node_id is None and node_id is None:
        return None

    cdp_client = getattr(cdp_session, "cdp_client", None)
    send = getattr(cdp_client, "send", None)
    dom = getattr(send, "DOM", None)
    resolve_node = getattr(dom, "resolveNode", None)
    if not callable(resolve_node):
        return None

    params = {"backendNodeId": backend_node_id} if backend_node_id is not None else {"nodeId": node_id}
    call_kwargs: dict[str, Any] = {"params": params}
    session_id = getattr(cdp_session, "session_id", None)
    if isinstance(session_id, str) and session_id:
        call_kwargs["session_id"] = session_id

    try:
        response = await resolve_node(**call_kwargs)
    except Exception:
        return None
    return _remote_object_id_from_response(response)


async def _browser_use_framework_event_object_id(watchdog: Any, object_id: str, cdp_session: Any) -> str:
    live_object_id = await _browser_use_resolve_current_input_object_id(watchdog, cdp_session)
    if not live_object_id or live_object_id == object_id:
        return object_id

    cdp_client = getattr(cdp_session, "cdp_client", None)
    session_id = getattr(cdp_session, "session_id", None)
    original_metadata = await _safe_probe_runtime_object_metadata(cdp_client, object_id, session_id=session_id)
    live_metadata = await _safe_probe_runtime_object_metadata(cdp_client, live_object_id, session_id=session_id)

    if _framework_event_candidate_should_replace(original_metadata, live_metadata):
        return live_object_id
    return object_id


async def _safe_probe_runtime_object_metadata(
    client: Any,
    object_id: str,
    *,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    try:
        return await _probe_runtime_object_metadata(client, object_id, session_id=session_id)
    except Exception:
        return None


def _framework_event_candidate_should_replace(
    original: dict[str, Any] | None,
    replacement: dict[str, Any] | None,
) -> bool:
    if not isinstance(replacement, dict):
        return False
    if not isinstance(original, dict):
        return _framework_event_candidate_connected(replacement)

    original_connected = _framework_event_candidate_connected(original)
    replacement_connected = _framework_event_candidate_connected(replacement)
    if replacement_connected and not original_connected:
        return True
    if original_connected and not replacement_connected:
        return False

    original_active = bool(original.get("isActiveElement") or original.get("is_active_element"))
    replacement_active = bool(replacement.get("isActiveElement") or replacement.get("is_active_element"))
    if replacement_active and not original_active:
        return True
    if original_active and not replacement_active:
        return False

    original_value_length = _positive_int_or_none(original.get("valueLength") or original.get("value_length")) or 0
    replacement_value_length = _positive_int_or_none(replacement.get("valueLength") or replacement.get("value_length")) or 0
    return replacement_value_length > 0 and original_value_length == 0


def _framework_event_candidate_connected(metadata: dict[str, Any]) -> bool:
    value = metadata.get("isConnected", metadata.get("is_connected", True))
    return value is not False


async def _call_browser_use_with_acira_runtime_freeze(original: Callable[..., Any] | None, *args: Any, **kwargs: Any) -> Any:
    if original is None:
        raise RuntimeError("Acira BrowserUse runtime freeze patch is missing its original method")
    proxy = _active_browser_use_proxy()
    if proxy is not None and proxy.runtime_control is not None and hasattr(proxy.runtime_control, "wait_if_paused"):
        await proxy.runtime_control.wait_if_paused()
    await wait_until_review_unpaused()
    if args and hasattr(args[0], "tools"):
        _ensure_acira_secret_action_registered(args[0])
    result = original(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    proxy = _active_browser_use_proxy()
    if proxy is not None and proxy.runtime_control is not None and hasattr(proxy.runtime_control, "wait_if_paused"):
        await proxy.runtime_control.wait_if_paused()
    await wait_until_review_unpaused()
    return result


async def _agent_step_with_acira_runtime_freeze(agent: Any, *args: Any, **kwargs: Any) -> Any:
    return await _call_browser_use_with_acira_runtime_freeze(_ORIGINAL_BROWSER_USE_STEP, agent, *args, **kwargs)


async def _get_model_output_with_acira_runtime_freeze(agent: Any, input_messages: list[Any]) -> Any:
    import browser_use.agent.service as agent_service

    await wait_until_review_unpaused()
    urls_replaced = agent._process_messsages_and_replace_long_urls_shorter_ones(input_messages)
    kwargs = {"output_format": agent.AgentOutput, "session_id": agent.session_id}

    try:
        response = await agent.llm.ainvoke(input_messages, **kwargs)
        parsed = response.completion

        await wait_until_review_unpaused()
        if urls_replaced:
            agent._recursive_process_all_strings_inside_pydantic_model(parsed, urls_replaced)

        if len(parsed.action) > agent.settings.max_actions_per_step:
            parsed.action = parsed.action[: agent.settings.max_actions_per_step]

        await wait_until_review_unpaused()
        if not (hasattr(agent.state, "paused") and (agent.state.paused or agent.state.stopped)):
            agent_service.log_response(parsed, agent.tools.registry.registry, agent.logger)
            await wait_until_review_unpaused()
            await agent._broadcast_model_state(parsed)

        await wait_until_review_unpaused()
        agent._log_next_action_summary(parsed)
        await wait_until_review_unpaused()
        return parsed
    except agent_service.ValidationError:
        raise
    except (agent_service.ModelRateLimitError, agent_service.ModelProviderError) as exc:
        if not agent._try_switch_to_fallback_llm(exc):
            raise
        return await agent.get_model_output(input_messages)


async def _check_stop_or_pause_with_acira_runtime_freeze(agent: Any, *args: Any, **kwargs: Any) -> Any:
    return await _call_browser_use_with_acira_runtime_freeze(_ORIGINAL_BROWSER_USE_CHECK_STOP_OR_PAUSE, agent, *args, **kwargs)


async def _execute_actions_with_acira_runtime_freeze(agent: Any, *args: Any, **kwargs: Any) -> Any:
    return await _call_browser_use_with_acira_runtime_freeze(_ORIGINAL_BROWSER_USE_EXECUTE_ACTIONS, agent, *args, **kwargs)


async def _handle_post_llm_processing_with_acira_runtime_freeze(agent: Any, *args: Any, **kwargs: Any) -> Any:
    return await _call_browser_use_with_acira_runtime_freeze(_ORIGINAL_BROWSER_USE_HANDLE_POST_LLM_PROCESSING, agent, *args, **kwargs)


async def _broadcast_model_state_with_acira_runtime_freeze(agent: Any, *args: Any, **kwargs: Any) -> Any:
    return await _call_browser_use_with_acira_runtime_freeze(_ORIGINAL_BROWSER_USE_BROADCAST_MODEL_STATE, agent, *args, **kwargs)


async def _post_process_with_acira_runtime_freeze(agent: Any, *args: Any, **kwargs: Any) -> Any:
    return await _call_browser_use_with_acira_runtime_freeze(_ORIGINAL_BROWSER_USE_POST_PROCESS, agent, *args, **kwargs)


async def _finalize_with_acira_runtime_freeze(agent: Any, *args: Any, **kwargs: Any) -> Any:
    return await _call_browser_use_with_acira_runtime_freeze(_ORIGINAL_BROWSER_USE_FINALIZE, agent, *args, **kwargs)


async def _browser_state_summary_with_acira_runtime_freeze(browser_session: Any, *args: Any, **kwargs: Any) -> Any:
    return await _call_browser_use_with_acira_runtime_freeze(_ORIGINAL_BROWSER_USE_BROWSER_STATE_SUMMARY, browser_session, *args, **kwargs)


async def _browser_session_reconnect_with_acira_runtime_freeze(browser_session: Any, *args: Any, **kwargs: Any) -> Any:
    if (getattr(browser_session, "_intentional_stop", False)
            or any(id(browser_session) in proxy._stopping_sessions for proxy in _ACTIVE_BROWSER_USE_PROXIES)):
        raise ConnectionError("A deliberately stopped browser cannot reconnect during cleanup")
    _invalidate_browser_use_runtime_caches(browser_session)
    try:
        return await _call_browser_use_with_acira_runtime_freeze(_ORIGINAL_BROWSER_USE_RECONNECT, browser_session, *args, **kwargs)
    finally:
        _invalidate_browser_use_runtime_caches(browser_session)


async def _browser_session_auto_reconnect_with_acira_runtime_freeze(browser_session: Any, *args: Any, **kwargs: Any) -> Any:
    if (getattr(browser_session, "_intentional_stop", False)
            or any(id(browser_session) in proxy._stopping_sessions for proxy in _ACTIVE_BROWSER_USE_PROXIES)):
        raise ConnectionError("A deliberately stopped browser cannot reconnect during cleanup")
    _invalidate_browser_use_runtime_caches(browser_session)
    try:
        return await _call_browser_use_with_acira_runtime_freeze(_ORIGINAL_BROWSER_USE_AUTO_RECONNECT, browser_session, *args, **kwargs)
    finally:
        _invalidate_browser_use_runtime_caches(browser_session)


async def _probe_runtime_object_metadata(client: Any, object_id: str, *, session_id: str | None = None) -> dict[str, Any] | None:
    response = await _browser_use_send_cdp_command(
        client,
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": RUNTIME_OBJECT_PROBE_JS,
            "returnByValue": True,
            "silent": True,
        },
        session_id=session_id,
    )
    value = _runtime_response_value(response)
    return value if isinstance(value, dict) else None


def _browser_use_approved_target_selectors(target: GuardTarget) -> tuple[str, ...]:
    metadata = target.to_metadata()
    attrs = metadata.get("attributes") if isinstance(metadata.get("attributes"), dict) else {}
    tag = _safe_css_tag(metadata.get("tag") or attrs.get("tag"))
    input_type = _css_attr_value(metadata.get("type") or attrs.get("type"))
    selectors: list[str] = []

    def add(selector: str | None) -> None:
        if selector and selector not in selectors:
            selectors.append(selector)

    target_id = _css_attr_value(metadata.get("id") or attrs.get("id"))
    if target_id:
        add(f'{tag}[id="{target_id}"]' if tag else f'[id="{target_id}"]')
        add(f'[id="{target_id}"]')

    name = _css_attr_value(metadata.get("name") or attrs.get("name"))
    if name:
        if tag and input_type:
            add(f'{tag}[name="{name}"][type="{input_type}"]')
        if input_type:
            add(f'[name="{name}"][type="{input_type}"]')
        add(f'{tag}[name="{name}"]' if tag else f'[name="{name}"]')
        add(f'[name="{name}"]')

    for metadata_key, attr_key in (
        ("aria_label", "aria-label"),
        ("title", "title"),
        ("placeholder", "placeholder"),
    ):
        value = _css_attr_value(metadata.get(metadata_key) or attrs.get(attr_key))
        if value:
            add(f'{tag}[{attr_key}="{value}"]' if tag else f'[{attr_key}="{value}"]')
            add(f'[{attr_key}="{value}"]')

    return tuple(selectors)


def _safe_css_tag(value: Any) -> str | None:
    tag = str(value or "").strip().lower()
    return tag if re.fullmatch(r"[a-z][a-z0-9_-]*", tag) else None


def _css_attr_value(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or "\x00" in text:
        return None
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\r", "\\D ").replace("\n", "\\A ")
    return text


def _cdp_response_dict_candidates(response: Any) -> tuple[dict[str, Any], ...]:
    candidates: list[dict[str, Any]] = []
    if isinstance(response, dict):
        candidates.append(response)
        result = response.get("result")
        if isinstance(result, dict):
            candidates.append(result)
            nested = result.get("result")
            if isinstance(nested, dict):
                candidates.append(nested)
    return tuple(candidates)


def _dom_root_node_id_from_response(response: Any) -> int | None:
    for candidate in _cdp_response_dict_candidates(response):
        root = candidate.get("root")
        if isinstance(root, dict):
            node_id = _positive_int_or_none(root.get("nodeId") or root.get("node_id"))
            if node_id is not None:
                return node_id
    return None


def _dom_node_id_from_response(response: Any) -> int | None:
    for candidate in _cdp_response_dict_candidates(response):
        node_id = _positive_int_or_none(candidate.get("nodeId") or candidate.get("node_id"))
        if node_id is not None:
            return node_id
    return None


def _dom_described_node_from_response(response: Any) -> dict[str, Any] | None:
    for candidate in _cdp_response_dict_candidates(response):
        node = candidate.get("node")
        if isinstance(node, dict):
            return node
    return None


def _dom_backend_node_id_from_described_node(node: dict[str, Any] | None) -> int | None:
    if not isinstance(node, dict):
        return None
    return _positive_int_or_none(node.get("backendNodeId") or node.get("backend_node_id"))


def _browser_use_described_node_matches_target(node: dict[str, Any] | None, target: GuardTarget) -> bool:
    if not isinstance(node, dict):
        return False
    metadata = target.to_metadata()
    target_attrs = metadata.get("attributes") if isinstance(metadata.get("attributes"), dict) else {}
    node_attrs = _dom_described_node_attributes(node)
    node_tag = str(node.get("localName") or node.get("nodeName") or "").strip().lower()
    target_tag = str(metadata.get("tag") or target_attrs.get("tag") or "").strip().lower()
    if target_tag and node_tag and node_tag != target_tag:
        return False

    for metadata_key, attr_key in (("type", "type"), ("id", "id"), ("name", "name")):
        expected = str(metadata.get(metadata_key) or target_attrs.get(attr_key) or "").strip()
        observed = str(node_attrs.get(attr_key) or "").strip()
        if expected and observed and expected.lower() != observed.lower():
            return False
        if expected and attr_key in {"id", "name"} and not observed:
            return False
    return True


def _dom_described_node_attributes(node: dict[str, Any]) -> dict[str, str]:
    raw = node.get("attributes")
    if not isinstance(raw, list):
        return {}
    return {str(key).lower(): str(value) for key, value in zip(raw[0::2], raw[1::2], strict=False)}


def _refresh_browser_use_event_node_identity(
    node: Any,
    *,
    node_id: int,
    backend_node_id: int,
    target_id: str | None,
    session_id: str | None,
) -> None:
    if node is None:
        return
    for name, value in (
        ("node_id", node_id),
        ("backend_node_id", backend_node_id),
        ("target_id", target_id),
        ("session_id", session_id),
    ):
        with contextlib.suppress(Exception):
            setattr(node, name, value)


def _invalidate_browser_use_runtime_caches(browser_session: Any) -> None:
    for proxy in list(_ACTIVE_BROWSER_USE_PROXIES):
        with contextlib.suppress(Exception):
            proxy._invalidate_browser_use_page_probe_cache(browser_session)
    with contextlib.suppress(Exception):
        browser_session._cached_browser_state_summary = None
    dom_watchdog = getattr(browser_session, "_dom_watchdog", None)
    clear_cache = getattr(dom_watchdog, "clear_cache", None)
    if callable(clear_cache):
        with contextlib.suppress(Exception):
            clear_cache()


async def _bubus_event_results_filtered_with_acira_pause(
    self: Any,
    timeout: float | None = None,
    include: Any = None,
    raise_if_any: bool = True,
    raise_if_none: bool = True,
) -> Any:
    if _ORIGINAL_BUBUS_EVENT_RESULTS_FILTERED is None:
        raise RuntimeError("Acira bubus event result patch is missing its original method")
    if self.event_type in {"SaveStorageStateEvent", "BrowserStopEvent", "BrowserKillEvent"}:
        kwargs = {"timeout": timeout, "raise_if_any": raise_if_any, "raise_if_none": raise_if_none}
        if include is not None:
            kwargs["include"] = include
        return await _ORIGINAL_BUBUS_EVENT_RESULTS_FILTERED(self, **kwargs)
    assert self.event_completed_signal is not None, "EventResult cannot be awaited outside of an async context"
    effective_timeout = timeout or self.event_timeout
    start_revision = ACIRA_REVIEW_PAUSE.revision
    try:
        await wait_with_review_pause(self.event_completed_signal.wait(), effective_timeout)
    except TimeoutError:
        if ACIRA_REVIEW_PAUSE.revision == start_revision:
            raise
        await wait_with_review_pause(self.event_completed_signal.wait(), 0.5)
    kwargs = {
        "timeout": None,
        "raise_if_any": raise_if_any,
        "raise_if_none": raise_if_none,
    }
    if include is not None:
        kwargs["include"] = include
    return await _ORIGINAL_BUBUS_EVENT_RESULTS_FILTERED(self, **kwargs)


async def _bubus_execute_handler_with_acira_pause(
    self: Any,
    event: Any,
    handler: Callable[..., Any],
    timeout: float | None = None,
) -> Any:
    if event.event_type in {"SaveStorageStateEvent", "BrowserStopEvent", "BrowserKillEvent"}:
        # Lifecycle work must remain bounded even if another browser in this
        # run still has a review. Scope only the shutdown handler's read/reset
        # waits; the other operation retains its original held review state and
        # every CDP mutation still passes through the policy boundary.
        from acira_cdp_proxy.pause import CURRENT_REVIEW_STATE, ReviewPauseState
        token = CURRENT_REVIEW_STATE.set(ReviewPauseState())
        try:
            return await _ORIGINAL_BUBUS_EXECUTE_HANDLER(self, event, handler, timeout=timeout)
        finally:
            CURRENT_REVIEW_STATE.reset(token)
    from bubus import BaseEvent
    from bubus.logging import log_timeout_tree
    import bubus.service as bubus_service

    handler_id = bubus_service.get_handler_id(handler, self)
    logger = bubus_service.logger
    logger.debug(f" -> {self}.execute_handler({event}, handler={bubus_service.get_handler_name(handler)}#{handler_id[-4:]})")
    if handler_id in event.event_results:
        existing_result = event.event_results[handler_id]
        if existing_result.started_at is not None:
            raise RuntimeError(
                f"Handler {bubus_service.get_handler_name(handler)}#{handler_id[-4:]} has already been executed "
                f"for event {event.event_id}. Previous execution started at {existing_result.started_at}"
            )

    event_result = event.event_result_update(
        handler=handler,
        eventbus=self,
        status="started",
        timeout=timeout or event.event_timeout,
    )

    token = bubus_service._current_event_context.set(event)  # noqa: SLF001
    handler_token = bubus_service.inside_handler_context.set(True)
    handler_id_token = bubus_service._current_handler_id_context.set(handler_id)  # noqa: SLF001

    async def deadlock_monitor() -> None:
        await sleep_with_review_pause(15.0)
        logger.warning(
            f"WARNING {self} handler {bubus_service.get_handler_name(handler)}() has been running for >15s on event. "
            "Possible slow processing or deadlock.\n"
            "(handler could be trying to await its own result or could be blocked by another async task).\n"
            f"{bubus_service.get_handler_name(handler)}({event})"
        )

    monitor_task = asyncio.create_task(
        deadlock_monitor(),
        name=f"{self}.deadlock_monitor({event}, {bubus_service.get_handler_name(handler)}#{handler_id[-4:]})",
    )

    handler_task: asyncio.Task[Any] | None = None
    try:
        if inspect.iscoroutinefunction(handler):
            handler_task = asyncio.create_task(handler(event))  # type: ignore[misc]
            result_value = await wait_with_review_pause(handler_task, event_result.timeout)
        elif inspect.isfunction(handler) or inspect.ismethod(handler):
            result_value = handler(event)
            if isinstance(result_value, BaseEvent):
                logger.debug(
                    f"Handler {bubus_service.get_handler_name(handler)} returned BaseEvent, not awaiting to avoid circular dependency"
                )
        else:
            raise ValueError(f"Handler {bubus_service.get_handler_name(handler)} must be a sync or async function, got: {type(handler)}")

        logger.debug(
            f"    -> Handler {bubus_service.get_handler_name(handler)}#{handler_id[-4:]} returned: "
            f"{type(result_value).__name__} {str(result_value)[:26]}..."
        )
        monitor_task.cancel()
        event.event_result_update(handler=handler, eventbus=self, result=result_value)
        if handler_id not in event.event_results:
            logger.error(f"    -> ERROR: Result not found for {bubus_service.get_handler_name(handler)}#{handler_id[-4:]} after update!")
        return result_value

    except asyncio.CancelledError as exc:
        monitor_task.cancel()
        interrupted = asyncio.CancelledError(
            f"Event handler {bubus_service.get_handler_name(handler)}#{handler_id[-4:]}({event}) was interrupted because of a parent timeout"
        )
        event.event_result_update(handler=handler, eventbus=self, error=interrupted)
        raise interrupted from exc
    except TimeoutError as exc:
        monitor_task.cancel()
        children = f" and interrupted any processing of {len(event.event_children)} child events" if event.event_children else ""
        handler_timeout = TimeoutError(
            f"Event handler {bubus_service.get_handler_name(handler)}#{handler_id[-4:]}({event}) timed out after "
            f"{event_result.timeout}s{children}"
        )
        event.event_result_update(handler=handler, eventbus=self, error=handler_timeout)
        event.event_cancel_pending_child_processing(handler_timeout)
        log_timeout_tree(event, event_result)
        raise handler_timeout from exc
    except Exception as exc:
        monitor_task.cancel()
        event.event_result_update(handler=handler, eventbus=self, error=exc)
        logger.error(
            f"ERROR {self} Error in event handler {bubus_service.get_handler_name(handler)}({event}) -> "
            f"\n\033[91m{type(exc).__name__}({exc})\033[0m\n{bubus_service._log_filtered_traceback(exc)}",  # noqa: SLF001
        )
        raise
    finally:
        bubus_service._current_event_context.reset(token)  # noqa: SLF001
        bubus_service.inside_handler_context.reset(handler_token)
        bubus_service._current_handler_id_context.reset(handler_id_token)  # noqa: SLF001

        if handler_task is not None and not handler_task.done():
            handler_task.cancel()
            try:
                await asyncio.wait_for(handler_task, timeout=0.1)
            except (asyncio.CancelledError, TimeoutError):
                pass

        try:
            if not monitor_task.done():
                monitor_task.cancel()
            await monitor_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass


def _browser_use_action_timeout(value: float | None) -> float:
    """Keep action waits bounded across framework versions and invalid inputs."""
    import math
    import os

    for candidate in (value, os.environ.get("BROWSER_USE_ACTION_TIMEOUT_S"), 180.0):
        if isinstance(candidate, bool):
            continue
        try:
            timeout = float(candidate)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(timeout) and timeout > 0:
            return timeout
    return 180.0


async def _browser_use_tools_act_with_acira_pause(
    self: Any,
    action: Any,
    browser_session: Any,
    page_extraction_llm: Any = None,
    sensitive_data: dict[str, str | dict[str, str]] | None = None,
    available_file_paths: list[str] | None = None,
    file_system: Any = None,
    extraction_schema: dict | None = None,
    action_timeout: float | None = None,
) -> Any:
    import browser_use.tools.service as tools_service

    timeout_s = _browser_use_action_timeout(action_timeout)

    for action_name, params in action.model_dump(exclude_unset=True).items():
        if params is None:
            continue
        if tools_service.Laminar is not None:
            span_context = tools_service.Laminar.start_as_current_span(
                name=action_name,
                input={"action": action_name, "params": params},
                span_type="TOOL",
            )
        else:
            span_context = contextlib.nullcontext()

        with span_context:
            try:
                result = await wait_with_review_pause(
                    self.registry.execute_action(
                        action_name=action_name,
                        params=params,
                        browser_session=browser_session,
                        page_extraction_llm=page_extraction_llm,
                        file_system=file_system,
                        sensitive_data=sensitive_data,
                        available_file_paths=available_file_paths,
                        extraction_schema=extraction_schema,
                    ),
                    timeout_s,
                )
            except tools_service.BrowserError as exc:
                tools_service.logger.error(f"Action {action_name} failed with BrowserError: {str(exc)}")
                result = _browser_use_nonfatal_acira_click_exception(action_name, exc, tools_service)
                if result is None:
                    result = tools_service.handle_browser_error(exc)
            except TimeoutError:
                tools_service.logger.error(
                    f"Action {action_name} hit the per-action timeout ({timeout_s:.0f}s) "
                    "- likely an unresponsive CDP connection. Returning error so the agent can recover."
                )
                result = tools_service.ActionResult(
                    error=(
                        f"Action {action_name} timed out after {timeout_s:.0f}s. "
                        "The browser may be unresponsive (dead CDP WebSocket). Try again or a different approach."
                    )
                )
            except Exception as exc:
                tools_service.logger.error(f"Action '{action_name}' failed with error: {str(exc)}")
                result = tools_service.ActionResult(error=str(exc))

            await wait_until_review_unpaused()

            result = _browser_use_nonfatal_acira_click_block(action_name, result, tools_service)

            if tools_service.Laminar is not None:
                tools_service.Laminar.set_span_output(result)

        if isinstance(result, str):
            return tools_service.ActionResult(extracted_content=result)
        if isinstance(result, tools_service.ActionResult):
            return result
        if result is None:
            return tools_service.ActionResult()
        raise ValueError(f"Invalid action result type: {type(result)} of {result}")
    return tools_service.ActionResult()


def _browser_use_nonfatal_acira_click_block(action_name: str, result: Any, tools_service: Any) -> Any:
    if action_name != "click" or not isinstance(result, tools_service.ActionResult):
        return result
    if not isinstance(result.error, str):
        return result
    summary = _summarize_acira_guard_block_for_agent(result.error)
    if summary is None:
        return result
    return _browser_use_nonfatal_acira_action_result(summary, tools_service)


def _browser_use_nonfatal_acira_click_exception(action_name: str, exc: BaseException, tools_service: Any) -> Any | None:
    if action_name != "click":
        return None
    summary = _summarize_acira_guard_block_for_agent(str(exc))
    if summary is None:
        return None
    return _browser_use_nonfatal_acira_action_result(summary, tools_service)


def _browser_use_nonfatal_acira_action_result(summary: str, tools_service: Any) -> Any:
    return tools_service.ActionResult(
        extracted_content=summary,
        long_term_memory=summary,
        metadata={"acira": {"blocked": True, "nonfatal_guard_outcome": True}},
    )


def _summarize_acira_guard_block_for_agent(reason: str) -> str | None:
    if ACIRA_GUARD_OUTCOME_BLOCKED_CONTINUE not in reason and "ACIRA_POLICY_BLOCKED_DO_NOT_RETRY" not in reason:
        return None
    compact = re.sub(r"\s+", " ", reason).strip()
    sentinel_index = compact.find(ACIRA_GUARD_OUTCOME_BLOCKED_CONTINUE)
    if sentinel_index >= 0:
        compact = compact[sentinel_index + len(ACIRA_GUARD_OUTCOME_BLOCKED_CONTINUE) :].lstrip(": ")
    acira_index = compact.find("ACIRA_POLICY_BLOCKED_DO_NOT_RETRY")
    if acira_index >= 0:
        compact = compact[acira_index:]
    compact = re.sub(r"^ACIRA_POLICY_BLOCKED_DO_NOT_RETRY:\s*", "", compact)
    compact = _truncate(compact, 420)
    if "could not safely identify" in compact.lower() or "generic or unresolved target" in compact.lower():
        return (
            f"{compact} The browser click was not sent. Treat this control as not safely identifiable by Acira, "
            "do not retry it or use JavaScript, and continue with the next named control."
        )
    return (
        f"{compact} The browser click was not sent. Treat this control as reviewed or blocked by Acira, "
        "do not retry it or use JavaScript, and continue with the next named control."
    )


async def _execute_step_with_acira_hitl_pause(
    agent: Any,
    step: int,
    max_steps: int,
    step_info: Any,
    on_step_start: Callable[[Any], Any] | None = None,
    on_step_end: Callable[[Any], Any] | None = None,
) -> bool:
    """browser-use Agent._execute_step with Acira HITL time excluded."""

    from browser_use.agent.views import ActionResult

    if on_step_start is not None:
        await _maybe_await(on_step_start(agent))

    await agent._demo_mode_log(
        f"Starting step {step + 1}/{max_steps}",
        "info",
        {"step": step + 1, "total_steps": max_steps},
    )

    agent.logger.debug(f"Starting step {step + 1}/{max_steps}...")

    try:
        await wait_with_review_pause(
            agent.step(step_info),
            timeout=agent.settings.step_timeout,
        )
        agent.logger.debug(f"Completed step {step + 1}/{max_steps}")
    except TimeoutError:
        error_msg = f"Step {step + 1} timed out after {agent.settings.step_timeout} seconds"
        agent.logger.error(f"Timeout: {error_msg}")
        await agent._demo_mode_log(error_msg, "error", {"step": step + 1})
        agent.state.consecutive_failures += 1
        agent.state.last_result = [ActionResult(error=error_msg)]
        if agent.state.n_steps == step + 1:
            agent.state.n_steps += 1

    if on_step_end is not None:
        await _maybe_await(on_step_end(agent))

    if agent.history.is_done():
        await agent.log_completion()

        if agent.settings.use_judge:
            await agent._judge_and_log()

        if agent.register_done_callback:
            if inspect.iscoroutinefunction(agent.register_done_callback):
                await agent.register_done_callback(agent.history)
            else:
                agent.register_done_callback(agent.history)

        return True

    return False


async def _wait_for_step_with_acira_review_pause(coro: Any, *, timeout: float | None) -> Any:
    return await wait_with_review_pause(coro, timeout)


async def _maybe_await(result: Any) -> Any:
    if asyncio.iscoroutine(result):
        return await result
    return result
