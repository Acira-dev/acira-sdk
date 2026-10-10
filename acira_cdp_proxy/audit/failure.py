"""Mid-run persistence-failure coordinator.

When disk writes fail mid-session, events route to the overflow buffer and a single operational
HITL is opened (not one per failure). The HITL offers Retry disk / Export buffer / Continue
without saving (drops buffer, with explicit confirmation). Escalation is exposed via status()
(the dashboard renders the live count + level); at 100% the agent is paused via on_pause until
the user resolves. Retry success flushes the buffer to disk in order and resumes.

Seams are injected (HITL backend, retry_disk, flush_to_disk, pause/resume/downgrade hooks) so
the state machine is fully testable; the live wiring (writer failure → coordinator, pause via
PauseCoordinator, dashboard escalation render) is connected by the pipeline/lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Awaitable, Callable

from .hitl import HITLOption, OperationalHITLBackend, OperationalHITLRequest
from .identity import utc_now_iso
from .overflow import OverflowBuffer

RETRY = "retry"
EXPORT = "export"
DROP = "drop"


async def _maybe_await(value):
    if asyncio.iscoroutine(value):
        return await value
    return value


async def _call(hook, *args) -> None:
    if hook is None:
        return
    await _maybe_await(hook(*args))


class PersistenceFailureCoordinator:
    def __init__(
        self,
        *,
        overflow_cap: int,
        backend: OperationalHITLBackend,
        retry_disk: Callable[[], "bool | Awaitable[bool]"],
        flush_to_disk: Callable[[list], "bool | Awaitable[bool]"],
        export_path: str | os.PathLike | None = None,
        on_pause: Callable[[], None] | None = None,
        on_resume: Callable[[], None] | None = None,
        on_downgrade: Callable[[tuple], None] | None = None,
        clock: Callable[[], str] = utc_now_iso,
    ) -> None:
        self.buffer = OverflowBuffer(overflow_cap)
        self._backend = backend
        self._retry_disk = retry_disk
        self._flush_to_disk = flush_to_disk
        self._export_path = export_path
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._on_downgrade = on_downgrade
        self._clock = clock
        self.active = False
        self.resolved = False
        self.paused = False
        self.reason: str | None = None
        self.first_failure_at: str | None = None
        self._task: asyncio.Task | None = None

    def status(self) -> dict:
        return {
            "active": self.active,
            "buffered": self.buffer.size,
            "cap": self.buffer.cap,
            "level": self.buffer.level,
            "paused": self.paused,
            "reason": self.reason,
            "first_failure_at": self.first_failure_at,
        }

    async def record_failure(self, event: dict, reason: str) -> bool:
        """Buffer a failed-to-persist event and open/maintain the failure HITL.

        Returns False if the overflow buffer is full (the event was not accepted — the agent
        should already be paused at 100%).
        """
        self.reason = reason
        if self.first_failure_at is None:
            self.first_failure_at = self._clock()
        accepted = self.buffer.add(event)
        if self.buffer.is_full and not self.paused:
            self.paused = True
            await _call(self._on_pause)
        if not self.active:
            self.active = True
            self._task = asyncio.ensure_future(self._handle())
        return accepted

    async def wait(self) -> None:
        if self._task is not None:
            await self._task

    def _prompt(self) -> str:
        return (
            f"Audit persistence failed at {self.first_failure_at}: {self.reason}. "
            f"Buffering in memory ({self.buffer.size}/{self.buffer.cap}, {self.buffer.level}). "
            "Agent continues."
        )

    async def _handle(self) -> None:
        while self.active:
            request = OperationalHITLRequest(
                kind="persistence_failure",
                prompt=self._prompt(),
                options=(
                    HITLOption(RETRY, "Retry disk"),
                    HITLOption(EXPORT, "Export buffer to a file"),
                    HITLOption(DROP, "Continue without saving (drops buffer)"),
                ),
                default_option=RETRY,
                blocking=self.buffer.is_full,
            )
            result = await self._backend.ask(request)
            if await self._resolve(result.selected):
                self.active = False
                self.resolved = True
                return

    async def _resolve(self, choice: str) -> bool:
        if choice == RETRY:
            if await _maybe_await(self._retry_disk()):
                if await _maybe_await(self._flush_to_disk(self.buffer.drain())):
                    await self._resume()
                    return True
            return False  # retry/flush failed → reopen the HITL
        if choice == EXPORT:
            self._export()
            await self._resume()
            return True
        if choice == DROP:
            confirm = await self._backend.ask(
                OperationalHITLRequest(
                    kind="persistence_failure",
                    prompt="Confirm: drop buffered audit events and continue WITHOUT saving?",
                    options=(HITLOption("confirm", "Yes, drop the buffer"), HITLOption("cancel", "Cancel")),
                    default_option="cancel",
                )
            )
            if confirm.selected == "confirm":
                self.buffer.clear()
                await _call(self._on_downgrade, (self.reason,))
                await self._resume()
                return True
            return False
        return False

    def _export(self) -> None:
        if not self._export_path:
            return
        path = Path(self._export_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            for event in self.buffer.drain():
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    async def _resume(self) -> None:
        if self.paused:
            self.paused = False
            await _call(self._on_resume)
