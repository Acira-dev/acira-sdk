"""Session lifecycle: bounded drain + bundle finalization, agent-crash handling, and
graceful shutdown (signals + atexit, step 9).

Acira always closes the session cleanly even when the agent does not. Draining is bounded by a
budget (default 30s) — partial-but-flushed beats hung.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Callable

from .bundle import finalize_bundle, gzip_events, zip_session_folder
from .hitl import HITLOption, OperationalHITLBackend, OperationalHITLRequest
from .identity import utc_now_iso

DEFAULT_SHUTDOWN_BUDGET_SECONDS = 30.0


async def drain_and_close(
    pipeline,
    *,
    end_reason: str,
    graceful_close: bool,
    budget_seconds: float = DEFAULT_SHUTDOWN_BUDGET_SECONDS,
) -> None:
    """Drain the writer queue + finalize session.json, bounded by budget. Partial > hung."""
    try:
        await asyncio.wait_for(
            pipeline.aclose(end_reason=end_reason, graceful_close=graceful_close),
            timeout=budget_seconds,
        )
    except asyncio.TimeoutError:
        pass  # force-exit with whatever was flushed


def _bundle(session):
    if session is None:
        return None
    gzip_events(session.events_path)
    if not session.dir.exists():
        return None
    return zip_session_folder(session.dir, session.zip_path)


async def finalize_session_bundle(
    pipeline,
    *,
    end_reason: str,
    graceful_close: bool,
    budget_seconds: float = DEFAULT_SHUTDOWN_BUDGET_SECONDS,
):
    """Drain + write session.json + gzip + zip. Returns the zip path (or None when volatile)."""
    session = pipeline.session
    await drain_and_close(pipeline, end_reason=end_reason, graceful_close=graceful_close, budget_seconds=budget_seconds)
    return _bundle(session)


async def handle_agent_crash(
    pipeline,
    *,
    cause: str,
    backend: OperationalHITLBackend | None = None,
    exit_func: Callable[[int], None] | None = None,
    clock: Callable[[], str] = utc_now_iso,
    budget_seconds: float = DEFAULT_SHUTDOWN_BUDGET_SECONDS,
):
    """Crash sequence: drain → final HITL (Acknowledge) → close bundle → exit non-zero.

    The caller has already stopped accepting new actions. Returns the zip path.
    """
    session = pipeline.session
    await drain_and_close(pipeline, end_reason="agent_crash", graceful_close=True, budget_seconds=budget_seconds)

    preserved = str(session.dir) if session is not None else "(volatile — not persisted)"
    if backend is not None:
        await backend.ask(
            OperationalHITLRequest(
                kind="agent_crash",
                prompt=(
                    f"Agent crashed at {clock()}: {cause}. "
                    f"Session preserved at {preserved}. Review and dismiss."
                ),
                options=(HITLOption("ack", "Acknowledge"),),
                default_option="ack",
            )
        )

    zip_path = _bundle(session)
    if exit_func is not None:
        exit_func(1)  # non-zero so wrappers/CI know it wasn't a clean run
    return zip_path


class GracefulShutdown:
    """SIGTERM/SIGINT + atexit → drain, finalize the bundle, exit cleanly.

    The async ``run`` is the real sequence (stop actions → drain → session.json graceful_close
    → gzip → zip → exit), bounded by the budget. ``sync_finalize`` is the atexit safety net for
    ``sys.exit()`` paths that bypass signals (best-effort: no async drain, bundles whatever was
    already flushed). Both are guarded by a single ``_done`` flag so they run once.
    """

    def __init__(
        self,
        pipeline,
        *,
        budget_seconds: float = DEFAULT_SHUTDOWN_BUDGET_SECONDS,
        exit_func: Callable[[int], None] | None = None,
        stop_actions: Callable[[], None] | None = None,
        clock: Callable[[], str] = utc_now_iso,
    ) -> None:
        self.pipeline = pipeline
        self.budget_seconds = budget_seconds
        self.exit_func = exit_func
        self.stop_actions = stop_actions
        self.clock = clock
        self._done = False
        self.installed = False

    async def run(self, *, end_reason: str = "signal", exit_code: int = 0):
        if self._done:
            return None
        self._done = True
        if self.stop_actions is not None:
            self.stop_actions()  # step 1: stop accepting new actions
        zip_path = await finalize_session_bundle(
            self.pipeline, end_reason=end_reason, graceful_close=True, budget_seconds=self.budget_seconds
        )
        if self.exit_func is not None:
            self.exit_func(exit_code)
        return zip_path

    def sync_finalize(self, *, end_reason: str = "completed"):
        """atexit net: bundle without a running loop (no async drain). Never raises."""
        if self._done:
            return None
        self._done = True
        session = self.pipeline.session
        if session is None:
            return None
        try:
            return finalize_bundle(
                session,
                end_reason=end_reason,
                graceful_close=True,
                event_count=self.pipeline.event_count,
                ended_at_iso=self.clock(),
            )
        except OSError:
            return None  # last-resort net must never raise during interpreter shutdown

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        import signal

        loop = loop or asyncio.get_event_loop()

        def handler() -> None:
            asyncio.ensure_future(self.run(end_reason="signal"))

        for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, handler)  # POSIX
            except (NotImplementedError, RuntimeError):
                # Windows: add_signal_handler is unsupported; fall back to signal.signal.
                try:
                    signal.signal(sig, lambda *_a: loop.call_soon_threadsafe(handler))
                except (ValueError, OSError):
                    pass
        self.installed = True

    def install_atexit(self, *, end_reason: str = "completed") -> None:
        import atexit

        atexit.register(lambda: self.sync_finalize(end_reason=end_reason))
