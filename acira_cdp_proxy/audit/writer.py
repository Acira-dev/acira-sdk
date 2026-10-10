"""Background JSONL persistence writer: async, batched, fsync'd, never blocks the guard.

``enqueue()`` is synchronous and returns the moment the event is buffered — disk I/O happens on
a background task. Events are appended one JSON object per line and flushed (with ``fsync``)
when any trigger fires: ``flush_max_events`` reached, ``flush_interval_seconds`` elapsed,
or close. The queue is bounded; a full queue signals catastrophic persistence failure and
raises ``PersistenceQueueFull`` for the mid-run failure flow (step 7) to handle.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

_STOP = object()


class PersistenceQueueFull(RuntimeError):
    """The bounded writer queue is full — disk persistence cannot keep up."""


class PersistenceWriter:
    def __init__(
        self,
        events_path: str | os.PathLike,
        *,
        flush_max_events: int = 100,
        flush_interval_seconds: float = 5.0,
        queue_maxsize: int = 10000,
        on_flush_error: Callable[[list, Exception], None] | None = None,
    ) -> None:
        self._path = Path(events_path)
        self._flush_max = max(1, int(flush_max_events))
        self._interval = max(0.01, float(flush_interval_seconds))
        self._queue_maxsize = max(1, int(queue_maxsize))
        self._on_flush_error = on_flush_error
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None
        self._file = None
        self._pre_start: list[dict] = []
        self._started = False
        self._closed = False
        self.flushed_count = 0
        self.fsync_count = 0
        self.flush_errors = 0

    @property
    def started(self) -> bool:
        return self._started

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        if self._started or self._closed:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path, "a", encoding="utf-8", newline="\n")
        self._queue = asyncio.Queue(maxsize=self._queue_maxsize)
        for event in self._pre_start:
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                break
        self._pre_start.clear()
        self._started = True
        self._task = asyncio.create_task(self._run())

    def enqueue(self, event: dict) -> None:
        """Synchronous, non-blocking. Buffers pre-start events until start() runs."""
        if self._closed:
            return
        if self._queue is None:
            if len(self._pre_start) >= self._queue_maxsize:
                raise PersistenceQueueFull(f"audit writer pre-start buffer full ({self._queue_maxsize})")
            self._pre_start.append(event)
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            raise PersistenceQueueFull(f"audit writer queue full ({self._queue_maxsize})")

    async def _run(self) -> None:
        batch: list[dict] = []
        last_flush = time.monotonic()
        assert self._queue is not None
        while True:
            timeout = self._interval if not batch else max(0.0, self._interval - (time.monotonic() - last_flush))
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                item = None

            if item is _STOP:
                while not self._queue.empty():
                    rest = self._queue.get_nowait()
                    if rest is not _STOP:
                        batch.append(rest)
                self._flush(batch)
                return

            if item is not None:
                batch.append(item)

            if batch and (len(batch) >= self._flush_max or item is None):
                self._flush(batch)
                batch = []
                last_flush = time.monotonic()

    def _flush(self, batch: list[dict]) -> None:
        if not batch or self._file is None:
            return
        try:
            for event in batch:
                self._file.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._file.flush()
            os.fsync(self._file.fileno())
        except OSError as exc:
            # Disk write failed mid-run: hand the batch to the failure coordinator (step 7)
            # rather than crash the background task or silently lose events.
            self.flush_errors += 1
            if self._on_flush_error is not None:
                self._on_flush_error(batch, exc)
            return
        self.flushed_count += len(batch)
        self.fsync_count += 1

    async def aclose(self) -> None:
        """Drain the queue, final flush+fsync, close the file. Bounded by the caller's budget."""
        if self._closed:
            return
        self._closed = True
        if self._queue is not None and self._task is not None:
            try:
                self._queue.put_nowait(_STOP)
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()  # make room; drop one to guarantee shutdown
                except asyncio.QueueEmpty:
                    pass
                self._queue.put_nowait(_STOP)
            await self._task
        elif self._pre_start:
            # Never started (no loop ran): flush the pre-start buffer synchronously.
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self._path, "a", encoding="utf-8", newline="\n")
            self._flush(self._pre_start)
            self._pre_start = []
        if self._file is not None:
            try:
                self._file.flush()
                os.fsync(self._file.fileno())
            except OSError:
                self.flush_errors += 1  # a failing disk at close must not raise out of aclose
            self._file.close()
            self._file = None
