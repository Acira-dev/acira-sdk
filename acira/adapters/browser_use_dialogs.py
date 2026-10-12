"""Cancel native browser-use modals on the exact detecting CDP session.

The upstream watchdog automatically confirms some dialogs. Protected sessions
instead dismiss them; accepting an uncontracted confirm/prompt remains blocked
by the core even when sent through another CDP path.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

_MANAGERS: list[Any] = []
_ORIGINAL_TAB_CREATED: Any = None
_EVENT = "Page.javascriptDialogOpening"
_CLOSED_EVENT = "Page.javascriptDialogClosed"


def install(manager: Any) -> None:
    global _ORIGINAL_TAB_CREATED
    if manager in _MANAGERS:
        return
    from browser_use.browser.watchdogs.popups_watchdog import PopupsWatchdog
    if not _MANAGERS:
        _ORIGINAL_TAB_CREATED = PopupsWatchdog.on_TabCreatedEvent
        PopupsWatchdog.on_TabCreatedEvent = on_TabCreatedEvent
    manager._acira_dialog_handlers = {}
    _MANAGERS.append(manager)


def uninstall(manager: Any) -> None:
    global _ORIGINAL_TAB_CREATED
    for root, registry, opening, previous_opening, closed, previous_closed in getattr(manager, "_acira_dialog_handlers", {}).values():
        for event_name, handler, previous in ((_EVENT, opening, previous_opening),
                                               (_CLOSED_EVENT, closed, previous_closed)):
            if registry._handlers.get(event_name) is handler:
                if previous is None:
                    registry.unregister(event_name)
                else:
                    registry.register(event_name, previous)
    manager._acira_dialog_handlers = {}
    if manager in _MANAGERS:
        _MANAGERS.remove(manager)
    if not _MANAGERS and _ORIGINAL_TAB_CREATED is not None:
        from browser_use.browser.watchdogs.popups_watchdog import PopupsWatchdog
        if PopupsWatchdog.on_TabCreatedEvent is on_TabCreatedEvent:
            PopupsWatchdog.on_TabCreatedEvent = _ORIGINAL_TAB_CREATED
        _ORIGINAL_TAB_CREATED = None


async def on_TabCreatedEvent(watchdog: Any, event: Any) -> None:
    session = watchdog.browser_session
    manager = next((item for item in _MANAGERS if id(session) in item._bindings), None)
    if manager is None or not manager.guard_policy.config.enabled:
        if _ORIGINAL_TAB_CREATED is not None:
            return await _ORIGINAL_TAB_CREATED(watchdog, event)
        return
    target_id = event.target_id
    cdp = await session.get_or_create_cdp_session(target_id, focus=False)
    root = session._cdp_client_root
    if root is None or cdp.cdp_client is not root or not cdp.session_id:
        raise RuntimeError("Protected native-dialog routing is unavailable; no confirmation handler was installed")
    await asyncio.wait_for(root.send.Page.enable(session_id=cdp.session_id), timeout=2)
    registry = getattr(root, "_event_registry", None)
    if registry is None or not isinstance(getattr(registry, "_handlers", None), dict):
        raise RuntimeError("Protected native-dialog registration is unsupported")
    if id(root) not in manager._acira_dialog_handlers:
        previous = registry._handlers.get(_EVENT)
        previous_closed = registry._handlers.get(_CLOSED_EVENT)
        pending: dict[str, object] = {}
        observed: dict[str, deque[tuple[str, object]]] = {}
        def detecting_target(session_id: str | None) -> str | None:
            if (manager not in _MANAGERS or id(session) not in manager._bindings
                    or id(session) in manager._stopping_sessions
                    or session._cdp_client_root is not root):
                return None
            record = getattr(manager.action_gateway.checkpoint_store, "event", None)
            detecting = session.session_manager.get_session(session_id) if isinstance(session_id, str) and session_id else None
            if detecting is None or detecting.cdp_client is not root or detecting.session_id != session_id:
                if record is not None:
                    record("native_dialog_cancel_unavailable", reason="missing_detecting_session")
                return None
            modal_target = getattr(detecting, "target_id", None)
            if not isinstance(modal_target, str) or not modal_target:
                if record is not None:
                    record("native_dialog_cancel_unavailable", reason="missing_detecting_target")
                return None
            return modal_target
        def closed(event_data: Any, session_id: str | None = None) -> None:
            modal_target = detecting_target(session_id)
            if modal_target is None:
                return
            queue = observed.get(session_id)
            if not queue:
                return
            observed_target, token = queue[0]
            if observed_target != modal_target:
                return
            queue.popleft()
            if not queue:
                observed.pop(session_id, None)
            # Another attachment can report an older physical modal's close
            # after the next modal has already opened. Only the observed
            # generation on this exact session can release its target.
            if pending.get(modal_target) is token:
                pending.pop(modal_target, None)
        def dismiss(event_data: Any, session_id: str | None = None) -> None:
            modal_target = detecting_target(session_id)
            if modal_target is None:
                return
            record = getattr(manager.action_gateway.checkpoint_store, "event", None)
            token = pending.get(modal_target)
            new_generation = token is None
            if new_generation:
                token = object()
                pending[modal_target] = token
            queue = observed.setdefault(session_id, deque())
            if not any(observed_token is token for _, observed_token in queue):
                queue.append((modal_target, token))
            # Multiple flattened CDP attachments report one physical modal.
            # Their openings share a token, but Closed releases it immediately
            # rather than waiting for an asynchronous dismissal response.
            if not new_generation:
                return
            kind = event_data.get("type") if isinstance(event_data, dict) else None
            kind = kind if kind in {"alert", "confirm", "prompt", "beforeunload"} else "unknown"
            async def cancel_modal() -> None:
                try:
                    if record is not None:
                        record("native_dialog_confirmation_blocked" if kind != "alert" else "native_dialog_dismissal",
                               framework="browser_use", dialog_type=kind, acceptance_dispatched=False)
                    try:
                        await asyncio.wait_for(root.send.Page.handleJavaScriptDialog(
                            params={"accept": False}, session_id=session_id), timeout=2)
                    except Exception as error:
                        if record is not None:
                            record("native_dialog_cancel_unavailable", reason="browser_rejected_dismissal",
                                   error_type=type(error).__name__)
                finally:
                    if pending.get(modal_target) is token:
                        pending.pop(modal_target, None)
            # CDP's receive loop awaits registry callbacks. Waiting for a send
            # response here would prevent that same loop from reading it.
            # Use the existing per-browser owned operation lifecycle instead.
            task = asyncio.create_task(cancel_modal())
            operations = manager._session_operations.setdefault(id(session), set())
            operations.add(task)
            def completed(done: asyncio.Task) -> None:
                operations.discard(done)
                if not operations and manager._session_operations.get(id(session)) is operations:
                    manager._session_operations.pop(id(session), None)
                if not done.cancelled() and done.exception() is not None:
                    # Failed required recording stops this session's future
                    # guarded mutations; never leak an exception's page data.
                    manager._stopping_sessions.add(id(session))
            task.add_done_callback(completed)
        registry.register(_EVENT, dismiss)
        registry.register(_CLOSED_EVENT, closed)
        manager._acira_dialog_handlers[id(root)] = (root, registry, dismiss, previous, closed, previous_closed)
    watchdog._dialog_listeners_registered.add(target_id)
