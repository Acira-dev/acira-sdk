from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from itertools import count
from contextvars import ContextVar
from typing import Any


PauseHook = Callable[[], None | Awaitable[None]]


class ReviewPauseState:
    """Process-local signal that Acira is waiting for a human review answer."""

    def __init__(self) -> None:
        self._active_count = 0
        self._revision = 0
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    @property
    def active(self) -> bool:
        return self._active_count > 0

    @property
    def revision(self) -> int:
        return self._revision

    @contextlib.asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        self._active_count += 1
        self._revision += 1
        self._notify_waiters()
        try:
            yield
        finally:
            self._active_count = max(0, self._active_count - 1)
            self._revision += 1
            self._notify_waiters()

    async def wait_for_change_after(self, revision: int) -> None:
        while self._revision == revision:
            loop = asyncio.get_running_loop()
            event = asyncio.Event()
            waiter = (loop, event)
            self._waiters.add(waiter)
            try:
                if self._revision != revision:
                    return
                await event.wait()
            finally:
                self._waiters.discard(waiter)

    async def wait_until_inactive(self) -> None:
        while self.active:
            loop = asyncio.get_running_loop()
            event = asyncio.Event()
            waiter = (loop, event)
            self._waiters.add(waiter)
            try:
                if not self.active:
                    return
                await event.wait()
            finally:
                self._waiters.discard(waiter)

    def _notify_waiters(self) -> None:
        for loop, event in tuple(self._waiters):
            if loop.is_closed():
                self._waiters.discard((loop, event))
                continue
            loop.call_soon_threadsafe(event.set)


CURRENT_REVIEW_STATE: ContextVar[ReviewPauseState | None] = ContextVar("acira_review_state", default=None)


class _ScopedReviewPause:
    def __init__(self) -> None:
        self.fallback = ReviewPauseState()

    def __getattr__(self, name: str) -> Any:
        return getattr(CURRENT_REVIEW_STATE.get() or self.fallback, name)


ACIRA_REVIEW_PAUSE = _ScopedReviewPause()


async def wait_until_review_unpaused() -> None:
    """Wait until no Acira human review is pending."""

    if ACIRA_REVIEW_PAUSE.active:
        await ACIRA_REVIEW_PAUSE.wait_until_inactive()


async def wait_with_review_pause(awaitable: Any, timeout: float | None) -> Any:
    """Like asyncio.wait_for, but human review time does not count."""

    if timeout is None:
        result = await awaitable
        if ACIRA_REVIEW_PAUSE.active:
            await ACIRA_REVIEW_PAUSE.wait_until_inactive()
        return result

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    task = asyncio.ensure_future(awaitable)
    if not task.done():
        await asyncio.sleep(0)

    try:
        while True:
            if ACIRA_REVIEW_PAUSE.active:
                paused_at = loop.time()
                await ACIRA_REVIEW_PAUSE.wait_until_inactive()
                deadline += loop.time() - paused_at
                continue

            if task.done():
                return await task

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError

            revision = ACIRA_REVIEW_PAUSE.revision
            review_change = asyncio.create_task(ACIRA_REVIEW_PAUSE.wait_for_change_after(revision))
            try:
                done, pending = await asyncio.wait(
                    {task, review_change},
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if review_change in pending:
                    review_change.cancel()

                if review_change in done:
                    continue

                if task in done:
                    if ACIRA_REVIEW_PAUSE.active:
                        continue
                    return await task

                if ACIRA_REVIEW_PAUSE.active:
                    continue

                raise TimeoutError
            finally:
                if not review_change.done():
                    review_change.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await review_change
    except BaseException:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        raise


async def sleep_with_review_pause(seconds: float) -> None:
    """Sleep for active runtime seconds, excluding human review time."""

    if seconds <= 0:
        return

    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds

    while True:
        if ACIRA_REVIEW_PAUSE.active:
            paused_at = loop.time()
            await ACIRA_REVIEW_PAUSE.wait_until_inactive()
            deadline += loop.time() - paused_at
            continue

        remaining = deadline - loop.time()
        if remaining <= 0:
            return

        revision = ACIRA_REVIEW_PAUSE.revision
        review_change = asyncio.create_task(ACIRA_REVIEW_PAUSE.wait_for_change_after(revision))
        try:
            done, pending = await asyncio.wait(
                {review_change},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if review_change in pending:
                review_change.cancel()
                return
            if review_change in done:
                continue
        finally:
            if not review_change.done():
                review_change.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await review_change


class PauseCoordinator:
    """Coordinates a runner-level HITL pause across CDP relay tasks.

    The task that owns the pending review is allowed to finish the reviewed
    command. Other relay tasks wait until the reviewed command has either been
    forwarded to Chrome or rejected back to the agent.
    """

    def __init__(
        self,
        *,
        on_pause_start: PauseHook | None = None,
        on_pause_end: PauseHook | None = None,
    ) -> None:
        self._gate = asyncio.Event()
        self._gate.set()
        self._lock = asyncio.Lock()
        self._tokens = count(1)
        self._active_tokens: set[int] = set()
        self._owner_tokens: dict[asyncio.Task[Any], int] = {}
        self._on_pause_start = on_pause_start
        self._on_pause_end = on_pause_end

    @property
    def paused(self) -> bool:
        return not self._gate.is_set()

    async def wait_if_paused(self, *, direction: str | None = None) -> None:
        if direction == "BROWSER -> AGENT":
            return
        current = asyncio.current_task()
        while True:
            async with self._lock:
                if current is not None and current in self._owner_tokens:
                    return
                if self._gate.is_set():
                    return
                gate = self._gate
            await gate.wait()

    @contextlib.asynccontextmanager
    async def hold_for_review(self, *, release_on_exit: bool = False) -> AsyncIterator[None]:
        current = asyncio.current_task()
        if current is None:
            yield
            return

        notify_start = False
        async with self._lock:
            token = next(self._tokens)
            self._active_tokens.add(token)
            self._owner_tokens[current] = token
            if self._gate.is_set():
                self._gate.clear()
                notify_start = True

        if notify_start:
            await _call_hook(self._on_pause_start)

        try:
            yield
        finally:
            if release_on_exit:
                await self.release_for_current_task()

    async def release_for_current_task(self) -> None:
        current = asyncio.current_task()
        if current is None:
            return

        notify_end = False
        async with self._lock:
            token = self._owner_tokens.pop(current, None)
            if token is None:
                return

            self._active_tokens.discard(token)
            if not self._active_tokens and not self._gate.is_set():
                self._gate.set()
                notify_end = True

        if notify_end:
            await _call_hook(self._on_pause_end)


RuntimeFreezeCoordinator = PauseCoordinator


async def _call_hook(hook: PauseHook | None) -> None:
    if hook is None:
        return
    result = hook()
    if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
        await result
