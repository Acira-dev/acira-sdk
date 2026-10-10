"""AuditPipeline — assembles the bus + ring buffer + identity into the object the guard holds.

PR2 step 2 scope: build a guard_decision event per decision, (later) redact it, then publish
to the bus (ring buffer always subscribed). The persistence writer (step 5) and dashboard
websocket subscribe to the same bus without any change here.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from .bus import AuditBus
from .decision_event import build_guard_decision_event
from .identity import acira_version, new_session_id, utc_now_iso
from .ring_buffer import MemoryRingBuffer
from .session import AuditSession
from .truncation import truncate_event_payload
from .writer import PersistenceWriter

Redactor = Callable[[dict], dict]


class AuditPipeline:
    def __init__(
        self,
        *,
        session_id: str,
        agent_id: str,
        config_id: str,
        acira_version: str,
        ring_buffer: MemoryRingBuffer,
        bus: AuditBus | None = None,
        redactor: Redactor | None = None,
        clock: Callable[[], str] = utc_now_iso,
    ) -> None:
        self.session_id = session_id
        self.agent_id = agent_id
        self.config_id = config_id
        self.acira_version = acira_version
        self.ring_buffer = ring_buffer
        self.bus = bus or AuditBus()
        self.bus.subscribe(ring_buffer.append)
        self._redactor = redactor
        self._clock = clock
        self._session: AuditSession | None = None
        self._writer: PersistenceWriter | None = None
        self._writer_starting = False
        self.event_count = 0
        self._audit_config: Any | None = None

    @classmethod
    def from_config(cls, config: Any, *, config_id: str | None = None) -> "AuditPipeline":
        from .redaction import Redactor

        audit = config.audit
        ring_buffer = MemoryRingBuffer(audit.memory_buffer_cap_mb)
        agent_id = config.agent_name or ""
        pipeline = cls(
            session_id=new_session_id(),
            agent_id=agent_id,
            config_id=config_id or agent_id,
            acira_version=acira_version(),
            ring_buffer=ring_buffer,
        )
        # Redaction runs at emit time, before any subscriber sees the event.
        pipeline.set_redactor(
            Redactor(audit.privacy_mode, custom_patterns=audit.redaction_custom_patterns)
        )
        pipeline._audit_config = audit
        if audit.persistent:
            session = AuditSession(
                audit.directory,
                pipeline.session_id,
                agent_id=agent_id,
                config_id=pipeline.config_id,
                acira_version=pipeline.acira_version,
                started_at_iso=utc_now_iso(),
            )
            # The folder is created lazily by writer.start()/session.json finalize, so a
            # pre-flight "stop"/"downgrade" leaves no empty session folder behind.
            writer = PersistenceWriter(
                session.events_path,
                flush_max_events=audit.writer_flush_max_events,
                flush_interval_seconds=audit.writer_flush_interval_seconds,
            )
            pipeline.attach_persistence(session, writer)
        return pipeline

    def set_redactor(self, redactor: Redactor | None) -> None:
        self._redactor = redactor

    def emit(self, event: dict) -> dict:
        """Redact, then truncate, then publish. Never raises into the caller."""
        if self._redactor is not None:
            event = self._redactor(event)
        event = truncate_event_payload(event)
        self.event_count += 1
        self.bus.emit(event)
        self._ensure_writer_started()
        return event

    def record_decision(self, *, method: str | None, decision: Any, latency_ms: float) -> dict:
        """Build + emit exactly one guard_decision event from a CDPPolicyDecision."""
        event = build_guard_decision_event(
            method=method,
            allow=bool(getattr(decision, "allow", True)),
            reason=getattr(decision, "reason", None),
            metadata=getattr(decision, "metadata", None) or {},
            session_id=self.session_id,
            agent_id=self.agent_id,
            config_id=self.config_id,
            acira_version=self.acira_version,
            latency_ms=latency_ms,
            now_iso=self._clock(),
        )
        return self.emit(event)

    # --- persistence lifecycle ------------------------------------------------------------

    @property
    def session(self) -> AuditSession | None:
        return self._session

    @property
    def writer(self) -> PersistenceWriter | None:
        return self._writer

    @property
    def persistent(self) -> bool:
        return self._writer is not None

    def attach_persistence(self, session: AuditSession, writer: PersistenceWriter) -> None:
        self._session = session
        self._writer = writer
        self.bus.subscribe(writer.enqueue)

    def _ensure_writer_started(self) -> None:
        """Lazily start the writer the first time we emit inside a running event loop."""
        if self._writer is None or self._writer.started or self._writer_starting or self._writer.closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop yet; start() will be called explicitly by the lifecycle
        self._writer_starting = True
        loop.create_task(self._start_writer())

    async def _start_writer(self) -> None:
        try:
            if self._writer is not None and not self._writer.started:
                await self._writer.start()
        finally:
            self._writer_starting = False

    async def start(self) -> None:
        """Start persistence (idempotent). No-op in volatile mode."""
        if self._writer is not None and not self._writer.started:
            await self._writer.start()
        self._writer_starting = False

    async def aclose(
        self,
        *,
        end_reason: str = "completed",
        graceful_close: bool = True,
        ended_at_iso: str | None = None,
    ) -> None:
        """Drain + close the writer and finalize session.json. No-op in volatile mode."""
        if self._writer is not None:
            await self._writer.aclose()
        if self._session is not None:
            self._session.write_session_json(
                ended_at_iso=ended_at_iso or self._clock(),
                end_reason=end_reason,
                graceful_close=graceful_close,
                event_count=self.event_count,
            )

    async def run_preflight(self, backend) -> str:
        """Run the pre-flight gate (persistent mode only). Returns 'persistent'/'volatile'/'stop'.

        On 'volatile' the session is silently downgraded (writer detached). 'stop' is returned
        for the caller (startup/lifecycle) to abort with a non-zero exit. No-op when volatile.
        """
        from .preflight import PERSISTENT, VOLATILE, run_preflight_gate

        if not self.persistent or self._audit_config is None:
            return PERSISTENT
        outcome = await run_preflight_gate(self._audit_config, backend, on_downgrade=self._log_downgrade)
        if outcome == VOLATILE:
            await self._downgrade_to_volatile()
        return outcome

    async def _downgrade_to_volatile(self) -> None:
        if self._writer is not None:
            self.bus.unsubscribe(self._writer.enqueue)
            if self._writer.started:
                await self._writer.aclose()
            self._writer = None
        self._session = None

    def _log_downgrade(self, failures: tuple[str, ...]) -> None:
        import sys

        print(
            "[Acira Audit] persistence disabled for this session (volatile mode). Reasons: "
            + "; ".join(failures),
            file=sys.stderr,
            flush=True,
        )
