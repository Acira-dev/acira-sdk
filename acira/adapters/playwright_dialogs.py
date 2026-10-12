"""Prevent native modal acceptance from bypassing protected Page actions.

A native confirm/prompt is a separate, unsupported operation. Reject it and
cancel the exact owned modal so the browser does not remain indefinitely stuck.
This does not promise containment of arbitrary page-authored JavaScript.
"""
from __future__ import annotations

import asyncio
from typing import Any

from acira_cdp_proxy.action_gateway import ActionRequest, AciraActionBlocked


def install(originals: dict) -> None:
    from playwright.async_api import Dialog
    from .playwright_boundary import INTERNAL, OWNERS, owner, trusted

    saved = Dialog.accept
    originals[(Dialog, "accept")] = saved

    async def accept(dialog: Any, *args: Any, **kwargs: Any) -> Any:
        selected = await owner(dialog)
        if INTERNAL.get() or selected is None:
            return await saved(dialog, *args, **kwargs)
        manager, page = selected
        if not manager.guard_policy.config.enabled or dialog.type == "alert":
            return await saved(dialog, *args, **kwargs)
        kind = dialog.type if dialog.type in {"confirm", "prompt", "beforeunload"} else "unknown"
        request = ActionRequest(action="dialog_confirmation", framework="playwright",
            details={"source": "playwright.Dialog.accept", "dialog_type": kind},
            reason="Native dialog acceptance has no verified operation contract; dismiss the dialog instead",
            confidence=1.0, operation_evidence={"critical": True,
                "context_scope": {"page": str(id(page))},
                "binding": "Native dialog acceptance is unsupported; no confirmation was dispatched"})
        try:
            # The shared policy always rejects this unsupported operation.
            return await manager.action_gateway.guard_execute(request,
                lambda: saved(dialog, *args, **kwargs))
        except AciraActionBlocked as error:
            if OWNERS.get(page) is manager:
                try:
                    await asyncio.wait_for(trusted(dialog, "dismiss"), timeout=2)
                except BaseException as cleanup_error:
                    error.add_note("The blocked native dialog could not be dismissed; no acceptance was sent (" + type(cleanup_error).__name__ + ")")
            raise

    accept.__name__ = "accept"
    accept.__doc__ = saved.__doc__
    Dialog.accept = accept
