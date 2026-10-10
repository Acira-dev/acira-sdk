"""Byte-bounded in-memory ring buffer for live audit events.

The always-on subscriber: holds the most recent events up to a byte cap, evicting oldest-first
(the audit stream is chronological, so "oldest added" is the natural eviction order). Lives for
the whole session; cleared only by clear() or session end — never by lack of a dashboard.
Thread-safe for the single-writer (guard event loop) + dashboard-reader access pattern.
"""

from __future__ import annotations

import json
import threading
from collections import deque

_BYTES_PER_MB = 1024 * 1024


def event_size_bytes(event: dict) -> int:
    try:
        return len(json.dumps(event, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(event).encode("utf-8"))


class MemoryRingBuffer:
    """Bounded by total serialized bytes, not event count. Evicts oldest first."""

    def __init__(self, cap_mb: float = 10.0) -> None:
        if cap_mb <= 0:
            raise ValueError("cap_mb must be > 0")
        self._cap_bytes = int(cap_mb * _BYTES_PER_MB)
        self._items: deque[tuple[dict, int]] = deque()
        self._bytes = 0
        self._evicted = 0
        self._lock = threading.Lock()

    def append(self, event: dict) -> None:
        size = event_size_bytes(event)
        with self._lock:
            self._items.append((event, size))
            self._bytes += size
            # Evict oldest until under cap, but always keep the just-added event.
            while self._bytes > self._cap_bytes and len(self._items) > 1:
                _, old_size = self._items.popleft()
                self._bytes -= old_size
                self._evicted += 1

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [event for event, _ in self._items]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def bytes(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def cap_bytes(self) -> int:
        return self._cap_bytes

    @property
    def evicted_count(self) -> int:
        with self._lock:
            return self._evicted
