"""In-process audit event bus: single publisher, many subscribers.

``emit()`` fans an event out to every subscriber and NEVER raises into the caller (the guard
hot path) — a broken subscriber cannot break the agent. Subscribers are plain callables
(the ring buffer's ``append``, later the persistence writer and dashboard websocket).

Redaction is applied by the pipeline BEFORE ``emit()`` is called, so every subscriber only
ever receives already-redacted events.
"""

from __future__ import annotations

import sys
from typing import Callable

Subscriber = Callable[[dict], None]


class AuditBus:
    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, subscriber: Subscriber) -> Subscriber:
        self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        try:
            self._subscribers.remove(subscriber)
        except ValueError:
            pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def emit(self, event: dict) -> None:
        for subscriber in list(self._subscribers):
            try:
                subscriber(event)
            except Exception as exc:  # noqa: BLE001 — a subscriber must never break the guard
                print(f"[Acira Audit] subscriber error: {exc!r}", file=sys.stderr, flush=True)
