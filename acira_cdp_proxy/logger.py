from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redaction import redact_payload, redact_value


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_LOG_FILE = PROJECT_DIR / "runtime" / "cdp_logs.txt"


class CDPLogger:
    """Append redacted CDP WebSocket payloads to a text log."""

    def __init__(self, path: Path | str = DEFAULT_LOG_FILE) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

    async def log(
        self,
        *,
        direction: str,
        connection_id: str,
        message_number: int,
        websocket_path: str,
        payload: str | bytes,
    ) -> None:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        sanitized_payload = redact_payload(payload)
        if isinstance(sanitized_payload, bytes):
            body = sanitized_payload.decode("utf-8", errors="backslashreplace")
        else:
            body = sanitized_payload

        if not body.endswith("\n"):
            body = f"{body}\n"

        header = (
            f"=== {direction} | timestamp={timestamp} | "
            f"connection={connection_id} | message={message_number} | "
            f"path={websocket_path} ===\n"
        )

        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as file:
                file.write(header)
                file.write(body)

    async def log_decision(
        self,
        *,
        connection_id: str,
        message_number: int,
        websocket_path: str,
        payload: str | bytes,
        metadata: dict[str, Any],
        reason: str | None = None,
    ) -> None:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        sanitized_payload = redact_payload(payload)
        body = {
            "timestamp": timestamp,
            "connection": connection_id,
            "message": message_number,
            "path": websocket_path,
            "reason": redact_value(reason),
            "metadata": redact_value(metadata),
            "payload": (
                sanitized_payload.decode("utf-8", errors="backslashreplace")
                if isinstance(sanitized_payload, bytes)
                else sanitized_payload
            ),
        }

        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as file:
                file.write("=== ACIRA GUARD DECISION ===\n")
                file.write(json.dumps(body, ensure_ascii=True, separators=(",", ":")))
                file.write("\n")


class NullCDPLogger:
    """No-op CDP logger used when persisted diagnostics are not explicitly enabled."""

    path: None = None

    async def log(
        self,
        *,
        direction: str,
        connection_id: str,
        message_number: int,
        websocket_path: str,
        payload: str | bytes,
    ) -> None:
        return

    async def log_decision(
        self,
        *,
        connection_id: str,
        message_number: int,
        websocket_path: str,
        payload: str | bytes,
        metadata: dict[str, Any],
        reason: str | None = None,
    ) -> None:
        return
