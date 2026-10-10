"""Overflow buffer for the mid-run persistence-failure flow.

Separate from the live-view ring buffer: when disk writes start failing, events route here so
none are lost. Bounded by event count (persistent_overflow_cap_events). It NEVER silently drops
— add() returns False when full, and the caller pauses the agent (the 100% blocking HITL).
Only an explicit "drop buffer" choice clears it without persisting.
"""

from __future__ import annotations

from collections import deque

WARNING_FRACTION = 0.5


class OverflowBuffer:
    def __init__(self, cap_events: int) -> None:
        self._cap = max(1, int(cap_events))
        self._items: deque[dict] = deque()

    def add(self, event: dict) -> bool:
        """Append an event. Returns False if full (caller must pause — never a silent drop)."""
        if len(self._items) >= self._cap:
            return False
        self._items.append(event)
        return True

    def drain(self) -> list[dict]:
        items = list(self._items)
        self._items.clear()
        return items

    def clear(self) -> None:
        self._items.clear()

    @property
    def size(self) -> int:
        return len(self._items)

    @property
    def cap(self) -> int:
        return self._cap

    @property
    def fraction(self) -> float:
        return self.size / self._cap

    @property
    def is_full(self) -> bool:
        return self.size >= self._cap

    @property
    def level(self) -> str:
        if self.is_full:
            return "critical"
        if self.fraction >= WARNING_FRACTION:
            return "warning"
        return "normal"
