"""Playwright public action boundaries, including Locator and Frame paths.

The wrappers are shared, but ownership is per Page. Unregistered pages never use
another run's policy. Internal probes call saved originals, not agent JavaScript.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import re
import time
import weakref
from contextvars import ContextVar
from typing import Any

from acira_cdp_proxy.action_gateway import ActionRequest, AciraActionBlocked
from acira_cdp_proxy.guard import RUNTIME_OBJECT_PROBE_JS, _target_from_probe_value

OWNERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
ORIGINALS: dict[tuple[type, str], Any] = {}
INTERNAL: ContextVar[bool] = ContextVar("acira_playwright_internal", default=False)
CAPTURE: ContextVar[Any] = ContextVar("acira_review_capture", default=None)

ELEMENT_METHODS = {"click": "click", "dblclick": "click", "tap": "click", "fill": "form_input",
    "clear": "form_input", "blur": "meaningful_action", "select_text": "meaningful_action", "set_files": "file_upload",
    "type": "form_input", "press_sequentially": "form_input", "press": "keyboard", "select_option": "select",
    "check": "check", "uncheck": "check", "set_checked": "check", "set_input_files": "file_upload",
    "hover": "meaningful_action", "focus": "meaningful_action", "drag_to": "meaningful_action",
    "drag_and_drop": "meaningful_action", "dispatch_event": "arbitrary_js"}
SCRIPT_METHODS = {"evaluate", "evaluate_handle", "evaluate_all", "eval_on_selector", "eval_on_selector_all",
    "add_script_tag", "add_style_tag", "set_content", "add_init_script", "wait_for_function", "pdf"}
NAVIGATION_METHODS = {"goto", "reload", "go_back", "go_forward"}

from acira_cdp_proxy.operation import STATE_JS


def original(obj: Any, name: str) -> Any:
    for cls in type(obj).__mro__:
        if (cls, name) in ORIGINALS:
            return ORIGINALS[(cls, name)].__get__(obj, type(obj))
    return getattr(obj, name)


async def trusted(obj: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    token = INTERNAL.set(True)
    try:
        return await original(obj, name)(*args, **kwargs)
    finally:
        INTERNAL.reset(token)


def patch_cdp_factory(manager: Any, obj: Any, name: str) -> None:
    """Raw sessions expose observations only; mutations use resolved SDK actions."""
    saved = getattr(obj, name, None)
    if saved is None:
        return
    async def factory(*args: Any, **kwargs: Any) -> Any:
        session = await saved(*args, **kwargs)
        send = session.send
        async def guarded_send(method: str, params: Any = None) -> Any:
            if INTERNAL.get() or method in {
                "DOM.getDocument", "DOM.describeNode", "DOM.getBoxModel", "DOM.querySelector",
                "DOM.querySelectorAll", "DOM.resolveNode", "DOM.getOuterHTML",
                "Page.getLayoutMetrics", "Page.getFrameTree", "Browser.getVersion",
                "Runtime.getProperties", "Runtime.releaseObject", "Runtime.releaseObjectGroup",
                "Accessibility.getFullAXTree", "Accessibility.getPartialAXTree",
            }:
                return await send(method, params)
            raise AciraActionBlocked("Raw CDP mutations and scripts are unsupported; use a guarded Page, Frame, or Locator action")
        manager._patched_attrs.append((session, "send", send))
        session.send = guarded_send
        return session
    manager._patched_attrs.append((obj, name, saved))
    setattr(obj, name, factory)


def attach(manager: Any, page: Any) -> None:
    install()
    previous = OWNERS.get(page)
    if previous is not None and previous is not manager:
        raise ValueError("This page is already protected by another Acira run")
    OWNERS[page] = manager
    for name in ("mouse", "keyboard", "touchscreen"):
        OWNERS[getattr(page, name)] = (manager, page)


def detach(manager: Any) -> None:
    for obj, owner in list(OWNERS.items()):
        if owner is manager or (isinstance(owner, tuple) and owner[0] is manager):
            del OWNERS[obj]
    if not OWNERS:
        for (cls, name), method in ORIGINALS.items():
            setattr(cls, name, method)
        ORIGINALS.clear()


async def owner(obj: Any) -> tuple[Any, Any] | None:
    value = OWNERS.get(obj)
    if isinstance(value, tuple):
        return value
    if value is not None:
        return value, obj
    page = getattr(obj, "page", None)
    if page is None and hasattr(obj, "owner_frame"):
        frame = await obj.owner_frame()
        page = frame.page if frame else None
    manager = OWNERS.get(page) if page else None
    return (manager, page) if manager else None


def install() -> None:
    if ORIGINALS:
        return
    from playwright.async_api import Page, Frame, Locator, ElementHandle, Mouse, Keyboard, Touchscreen, FileChooser, BrowserContext
    for cls in (Page, BrowserContext):
        for name in ("route", "unroute", "route_from_har", "unroute_all"):
            saved = getattr(cls, name, None)
            if saved is None:
                continue
            ORIGINALS[(cls, name)] = saved
            def routing_wrapper(saved, name):
                async def routed(obj, *args, **kwargs):
                    context = obj.context if isinstance(obj, Page) else obj
                    if getattr(context, "_acira_submission_locked", None) is not None and not INTERNAL.get():
                        raise AciraActionBlocked("Network routing is owned by the active Acira submission contracts")
                    return await saved(obj, *args, **kwargs)
                return routed
            setattr(cls, name, routing_wrapper(saved, name))
    for cls in (Page, Frame, Locator, ElementHandle, FileChooser):
        for method in set(ELEMENT_METHODS) | SCRIPT_METHODS | NAVIGATION_METHODS:
            if hasattr(cls, method):
                _wrap(cls, method)
    for cls, methods in ((Mouse, ("click", "dblclick", "down", "up", "move", "wheel")),
                         (Keyboard, ("down", "up", "press", "type", "insert_text")),
                         (Touchscreen, ("tap",))):
        for method in methods:
            _wrap(cls, method)


def _wrap(cls: type, method: str) -> None:
    saved = getattr(cls, method)
    ORIGINALS[(cls, method)] = saved

    async def call(obj: Any, *args: Any, **kwargs: Any) -> Any:
        if INTERNAL.get():
            return await saved(obj, *args, **kwargs)
        selected = await owner(obj)
        if selected is None:
            return await saved(obj, *args, **kwargs)
        return await guarded(selected[0], selected[1], obj, method, args, kwargs, saved)

    call.__name__ = method
    call.__doc__ = saved.__doc__
    setattr(cls, method, call)


async def element_for(page: Any, obj: Any, method: str, args: tuple, kwargs: dict) -> Any:
    from playwright.async_api import Locator, ElementHandle, Page, Frame, Mouse, Keyboard, Touchscreen, FileChooser
    if isinstance(obj, FileChooser):
        return obj.element
    if isinstance(obj, ElementHandle):
        return obj
    if isinstance(obj, Locator):
        if await obj.count() != 1:
            raise AciraActionBlocked("Resolve the target to exactly one element before requesting approval")
        return await obj.element_handle(timeout=kwargs.get("timeout", 5000))
    if isinstance(obj, (Page, Frame)):
        selector = args[0] if args else kwargs.get("selector", kwargs.get("source"))
        if not selector:
            return None
        locator = obj.locator(selector)
        if await locator.count() != 1:
            raise AciraActionBlocked("Resolve the target to exactly one element before requesting approval")
        return await locator.element_handle(timeout=kwargs.get("timeout", 5000))
    if isinstance(obj, (Mouse, Touchscreen)) and method in {"click", "dblclick", "tap"}:
        x = args[0] if args else kwargs.get("x")
        y = args[1] if len(args) > 1 else kwargs.get("y")
        handle = await trusted(page, "evaluate_handle", "([x,y])=>{let el=document.elementFromPoint(x,y);while(el?.shadowRoot){const inner=el.shadowRoot.elementFromPoint(x,y);if(!inner||inner===el)break;el=inner;}return el;}", [x, y])
        element = handle.as_element()
        if element and await trusted(element, "evaluate", "el=>['IFRAME','FRAME'].includes(el.tagName)||el.tagName.includes('-')"):
            raise AciraActionBlocked("Use a resolved frame or shadow-root Locator for this coordinate target")
        return element
    if isinstance(obj, Keyboard):
        handle = await trusted(page, "evaluate_handle", "document.activeElement")
        return handle.as_element()
    return None


async def guarded(manager: Any, page: Any, obj: Any, method: str, args: tuple, kwargs: dict, saved: Any) -> Any:
    from playwright.async_api import Keyboard, Mouse, Page, Frame, Touchscreen
    if manager.runtime_control is not None:
        await manager.runtime_control.wait_if_paused()
    if method in SCRIPT_METHODS:
        expression = args[0] if args else kwargs.get("expression", "")
        if method in {"evaluate", "evaluate_handle"} and isinstance(expression, str) and expression.strip() in manager.guard_policy.config.read_only_js_allowlist:
            return await saved(obj, *args, **kwargs)
        raise AciraActionBlocked("Unclassified script execution is unsupported in protected Playwright mode. Use a supported action or a verified operation contract.")
    if isinstance(obj, Mouse) and method in {"down", "up", "move", "wheel"}:
        raise AciraActionBlocked("Unbounded mouse sequences are unsupported; use a resolved Locator action")
    if isinstance(obj, Keyboard) and method in {"down", "up"}:
        raise AciraActionBlocked("Unbounded key sequences are unsupported; use press() on a resolved target")
    if method in {"dispatch_event", "drag_to", "drag_and_drop"}:
        raise AciraActionBlocked("This operation has no verified bounded action contract")
    if kwargs.get("force") and method in {"click", "dblclick", "tap", "check", "uncheck", "set_checked"}:
        raise AciraActionBlocked("Forced pointer actions can hit a different element; use a visible, unobstructed target")
    action = "navigation" if method in NAVIGATION_METHODS else ELEMENT_METHODS.get(method, "form_input")
    if action != "navigation":
        # Capture policy changes before consulting cached page mappings/preflight.
        if manager.guard_policy.before_decision is not None:
            await manager.guard_policy.before_decision()
        await manager._ensure_page_general_preflight(page, trigger="before_action")
        await manager._ensure_page_mapping_preflight(page)
    value_args = args[1:] if isinstance(obj, (Page, Frame)) and method in ELEMENT_METHODS else args
    uploads, upload_evidence, upload_unchanged = None, None, None
    if action == "file_upload":
        from ._uploads import freeze_uploads
        uploads, upload_evidence, upload_unchanged = freeze_uploads(value_args[0] if value_args else kwargs.get("files", []))
    if method == "press":
        key = str(value_args[0] if value_args else kwargs.get("key", ""))
        action = "submit" if key.lower().split("+")[-1] in {"enter", "return", "numpadenter"} else "keyboard"
    element = None
    before = None
    target = None
    if action != "navigation":
        element = await element_for(page, obj, method, args, kwargs)
        if element is None:
            raise AciraActionBlocked("No verifiable target for this operation")
        raw_target = await trusted(element, "evaluate", "el => (" + RUNTIME_OBJECT_PROBE_JS.strip() + ").call(el)")
        target = _target_from_probe_value(raw_target)
        before = await trusted(element, "evaluate", STATE_JS)
        if target is None:
            raise AciraActionBlocked("The selected target could not be inspected safely; no browser action was dispatched")
        if not before:
            raise AciraActionBlocked("The selected target detached before authorization; resolve a fresh target for a new operation. No browser action was dispatched")
        if method == "press" and action == "keyboard":
            if target.is_editable:
                action = "form_input"
            elif key.lower().split("+")[-1] in {"space", " "}:
                action = "check" if target.is_checkbox else "click"
    fingerprint = json.dumps(before, sort_keys=True) if before else None
    identity_text = json.dumps(before.get("target", {}) if before else {}).lower()
    critical = action in {"submit", "file_upload"} or bool(re.search(r"pay|purchase|checkout|card|credential|password|delete|remove|destroy|transfer|secret|token", identity_text))
    if before and (any(field["sensitive"] for field in before["fields"]) or (before["has_form"] and getattr(target, "is_submit", False))):
        critical = True
    evidence = {"action": action, "destination": before["destination"] if before else str(args[0] if args else kwargs.get("url", page.url)),
                "fields": [{"name": f["name"], "display": "[REDACTED]" if f["sensitive"] else f["value"], "checked": f["checked"]} for f in before["fields"]] if before else [],
                "context_scope": {"page": str(id(page)), "url": before["url"] if before else page.url, "form": before.get("form_id") if before else None},
                "critical": critical, "binding": "DOM values and resolved target rechecked immediately before browser dispatch"}
    if not critical and before:
        evidence["grant_scope"] = {"url": before["url"], "target": before["target"], "action": action,
                                   "destination": before["destination"]}
    async def revalidate() -> bool:
        if upload_unchanged is not None and not upload_unchanged():
            return False
        if not element:
            return True
        try:
            return json.dumps(await trusted(element, "evaluate", STATE_JS), sort_keys=True) == fingerprint
        except Exception:
            return False
    async def execute() -> Any:
        # Use the reviewed element for selector actions, avoiding locator retargeting
        # between the final check and dispatch. No user callback runs in this scope.
        if element and method == "clear":
            return await trusted(element, "fill", "", **kwargs)
        if uploads is not None:
            return await trusted(element, "set_input_files", uploads, **{key: value for key, value in kwargs.items() if key not in {"files", "selector", "strict"}})
        if element and method == "press_sequentially":
            return await trusted(element, "type", *value_args, **kwargs)
        if isinstance(obj, (Mouse, Touchscreen)) and method in {"click", "dblclick", "tap"}:
            box = await element.bounding_box()
            if not box:
                raise AciraActionBlocked("Mouse target is no longer visible")
            x, y = (args[0] if args else kwargs.get("x")), (args[1] if len(args)>1 else kwargs.get("y"))
            if not (box["x"] <= x < box["x"]+box["width"] and box["y"] <= y < box["y"]+box["height"]):
                raise AciraActionBlocked("The coordinate target moved before dispatch")
            clean = {key:value for key,value in kwargs.items() if key not in {"x", "y"}}
            return await trusted(element, method, position={"x":x-box["x"],"y":y-box["y"]}, **clean)
        if element and method in ELEMENT_METHODS and hasattr(element, method):
            clean = {key: value for key, value in kwargs.items() if key not in {"selector", "strict"}}
            if "position" in clean:
                box = await element.bounding_box()
                point = clean["position"]
                if not box or not (0 <= point["x"] < box["width"] and 0 <= point["y"] < box["height"]):
                    raise AciraActionBlocked("Pointer position must lie inside the reviewed element")
            return await trusted(element, method, *value_args, **clean)
        return await trusted(obj, method, *args, **kwargs)
    details = {"source": f"playwright.{type(obj).__name__}.{method}",
               "url": str(args[0] if args else kwargs.get("url", page.url)) if method == "goto" else page.url}
    if upload_evidence is not None:
        evidence["files"] = upload_evidence
    if action == "form_input":
        details["value"] = value_args[0] if value_args else kwargs.get("value", kwargs.get("text", ""))
    async def capture() -> dict[str, Any]:
        return await masked_artifact(page, element, scroll_target=not isinstance(obj, (Mouse, Touchscreen)))
    evidence["capture_status"] = "on_review"
    from .playwright_submission import SUBMISSION
    submission = SUBMISSION.get()
    if submission is not None:
        if submission.page is not page or submission.manager is not manager or method != "click":
            raise AciraActionBlocked("Submission contract does not match this browser operation")
        evidence.update(submission.evidence)
        evidence.pop("grant_scope", None)
    # Capture is invoked by the review backend only, never for ordinary allowed actions.
    request = ActionRequest(action=action, framework="playwright", target=target, details=details,
        reason=f"Playwright {type(obj).__name__}.{method}", confidence=target.confidence if target else 1.0,
        target_resolved=target is not None if action != "navigation" else True,
        target_resolution_source="playwright.resolved_element", target_resolution_confidence=1.0,
        adapter_handles_page_preflight=True, revalidate=revalidate, operation_evidence=evidence,
        require_review=submission is not None,
        release_pause_on_review_exit=True, supervised_runtime=True,
        send_probe=await manager._make_page_probe(page))
    token = CAPTURE.set(capture)
    try:
        result = await manager.action_gateway.guard_execute(request, (lambda: submission.execute(execute)) if submission else execute)
        if action == "navigation":
            manager._invalidate_page_probe_cache(page)
            await manager._ensure_page_general_preflight(page, trigger="after_navigation")
            await manager._ensure_page_mapping_preflight(page)
        return result
    finally:
        CAPTURE.reset(token)


async def masked_artifact(page: Any, element: Any, *, scroll_target: bool = True) -> dict[str, Any]:
    from acira_cdp_proxy.screenshots import capture_masked_cdp, unavailable_capture
    session = None
    try:
        async with asyncio.timeout(5):
            if element is not None and scroll_target:
                try:
                    await trusted(element, "scroll_into_view_if_needed", timeout=2000)
                except Exception:
                    return unavailable_capture("target_unavailable")
            session = await page.context.new_cdp_session(page)
            async def probe(method, params, session_id):
                return await trusted(session, "send", method, params)
            return await capture_masked_cdp(probe, target_probe=element.bounding_box if element else None)
    except TimeoutError:
        return unavailable_capture("capture_timeout")
    except Exception:
        return unavailable_capture()
    finally:
        if session is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(session.detach(), 1)
