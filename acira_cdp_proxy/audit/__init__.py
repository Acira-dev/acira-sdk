"""Acira audit pipeline (PR2): event bus, in-memory ring buffer, decision-event builder,
and (in later steps) redaction, payload truncation, persistence, and lifecycle handling."""

from __future__ import annotations

from .bundle import finalize_bundle, gzip_events, zip_session_folder
from .bus import AuditBus, Subscriber
from .failure import PersistenceFailureCoordinator
from .lifecycle import (
    GracefulShutdown,
    drain_and_close,
    finalize_session_bundle,
    handle_agent_crash,
)
from .overflow import OverflowBuffer
from .recovery import RecoveryResult, is_orphan, recover_all, recover_orphan, scan_orphans
from .hitl import (
    CallableOperationalHITL,
    HITLOption,
    OperationalHITLBackend,
    OperationalHITLRequest,
    OperationalHITLResult,
    TerminalOperationalHITL,
    ask_operational,
)
from .preflight import PreflightResult, run_preflight_checks, run_preflight_gate
from .decision_event import (
    build_guard_decision_event,
    classify_decision,
    resolve_decision_source,
)
from .identity import acira_version, new_session_id, utc_now_iso
from .pipeline import AuditPipeline
from .redaction import PRIVACY_MODES, Redactor
from .ring_buffer import MemoryRingBuffer, event_size_bytes
from .session import AuditSession
from .truncation import PAYLOAD_FIELD_MAX_BYTES, truncate_event_payload
from .writer import PersistenceQueueFull, PersistenceWriter

__all__ = [
    "AuditBus",
    "Subscriber",
    "AuditPipeline",
    "MemoryRingBuffer",
    "event_size_bytes",
    "Redactor",
    "PRIVACY_MODES",
    "truncate_event_payload",
    "PAYLOAD_FIELD_MAX_BYTES",
    "AuditSession",
    "PersistenceWriter",
    "PersistenceQueueFull",
    "HITLOption",
    "OperationalHITLRequest",
    "OperationalHITLResult",
    "OperationalHITLBackend",
    "TerminalOperationalHITL",
    "CallableOperationalHITL",
    "ask_operational",
    "PreflightResult",
    "run_preflight_checks",
    "run_preflight_gate",
    "OverflowBuffer",
    "PersistenceFailureCoordinator",
    "gzip_events",
    "zip_session_folder",
    "finalize_bundle",
    "drain_and_close",
    "finalize_session_bundle",
    "handle_agent_crash",
    "GracefulShutdown",
    "RecoveryResult",
    "is_orphan",
    "scan_orphans",
    "recover_orphan",
    "recover_all",
    "build_guard_decision_event",
    "classify_decision",
    "resolve_decision_source",
    "acira_version",
    "new_session_id",
    "utc_now_iso",
]
