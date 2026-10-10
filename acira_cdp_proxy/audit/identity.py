"""Identity + clock helpers for audit events."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def acira_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("acira")
        except PackageNotFoundError:
            return "0.1.0"
    except Exception:  # noqa: BLE001 — version lookup must never break startup
        return "0.1.0"


def new_session_id() -> str:
    return uuid.uuid4().hex


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
