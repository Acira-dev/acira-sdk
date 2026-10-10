"""The top-level ``audit`` config block (PR2).

Governs the audit pipeline: persistence mode, buffer caps, pre-flight thresholds, HITL
timeouts, writer flush cadence, and the privacy/redaction model. Validated at config load so
a bad audit block fails fast rather than at first write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from .bounded_regex import compile_pattern, MAX_PATTERNS

AUDIT_MODES = frozenset({"volatile", "persistent"})
PRIVACY_MODES = frozenset({"light", "medium", "conservative"})


class AuditConfigError(ValueError):
    """Raised when the ``audit`` config block is malformed."""


@dataclass(frozen=True)
class AuditConfig:
    mode: str = "volatile"
    directory: str = ".acira/audits"
    memory_buffer_cap_mb: float = 10.0
    persistent_overflow_cap_events: int = 10000
    preflight_min_free_mb: float = 100.0
    preflight_hitl_timeout_seconds: float = 300.0
    hitl_timeout_seconds: float = 7200.0
    hitl_timeout_ceiling_seconds: float = 86400.0
    writer_flush_interval_seconds: float = 5.0
    writer_flush_max_events: int = 100
    privacy_mode: str = "medium"
    redaction_custom_patterns: tuple[str, ...] = ()

    @property
    def persistent(self) -> bool:
        return self.mode == "persistent"


def _positive_number(data: dict[str, Any], key: str, default: float) -> float:
    raw = data.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise AuditConfigError(f"audit.{key} must be a number")
    if value <= 0:
        raise AuditConfigError(f"audit.{key} must be > 0")
    return value


def _positive_int(data: dict[str, Any], key: str, default: int) -> int:
    raw = data.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise AuditConfigError(f"audit.{key} must be an integer")
    if raw <= 0:
        raise AuditConfigError(f"audit.{key} must be > 0")
    return raw


def parse_audit_config(data: Any) -> AuditConfig:
    if data is None:
        return AuditConfig()
    if not isinstance(data, dict):
        raise AuditConfigError("audit must be an object")
    defaults = AuditConfig()

    mode = str(data.get("mode", defaults.mode)).strip().lower()
    if mode not in AUDIT_MODES:
        raise AuditConfigError(f"audit.mode must be one of {sorted(AUDIT_MODES)} (got {mode!r})")

    privacy = str(data.get("privacy_mode", defaults.privacy_mode)).strip().lower()
    if privacy not in PRIVACY_MODES:
        raise AuditConfigError(f"audit.privacy_mode must be one of {sorted(PRIVACY_MODES)} (got {privacy!r})")

    directory = str(data.get("directory", defaults.directory)).strip()
    if not directory:
        raise AuditConfigError("audit.directory must be a non-empty string")

    hitl = _positive_number(data, "hitl_timeout_seconds", defaults.hitl_timeout_seconds)
    ceiling = _positive_number(data, "hitl_timeout_ceiling_seconds", defaults.hitl_timeout_ceiling_seconds)
    if hitl > ceiling:
        raise AuditConfigError(
            f"audit.hitl_timeout_seconds ({hitl}) cannot exceed "
            f"hitl_timeout_ceiling_seconds ({ceiling})"
        )

    raw_patterns = data.get("redaction_custom_patterns", [])
    if not isinstance(raw_patterns, list):
        raise AuditConfigError("audit.redaction_custom_patterns must be an array")
    if len(raw_patterns) > MAX_PATTERNS:
        raise AuditConfigError("audit.redaction_custom_patterns supports at most 32 patterns")
    patterns: list[str] = []
    for pattern in raw_patterns:
        if not isinstance(pattern, str):
            raise AuditConfigError("audit.redaction_custom_patterns entries must be strings")
        try:
            compile_pattern(pattern)
        except (re.error, ValueError) as exc:
            raise AuditConfigError(f"audit.redaction_custom_patterns: invalid regex {pattern!r}: {exc}")
        patterns.append(pattern)

    return AuditConfig(
        mode=mode,
        directory=directory,
        memory_buffer_cap_mb=_positive_number(data, "memory_buffer_cap_mb", defaults.memory_buffer_cap_mb),
        persistent_overflow_cap_events=_positive_int(data, "persistent_overflow_cap_events", defaults.persistent_overflow_cap_events),
        preflight_min_free_mb=_positive_number(data, "preflight_min_free_mb", defaults.preflight_min_free_mb),
        preflight_hitl_timeout_seconds=_positive_number(data, "preflight_hitl_timeout_seconds", defaults.preflight_hitl_timeout_seconds),
        hitl_timeout_seconds=hitl,
        hitl_timeout_ceiling_seconds=ceiling,
        writer_flush_interval_seconds=_positive_number(data, "writer_flush_interval_seconds", defaults.writer_flush_interval_seconds),
        writer_flush_max_events=_positive_int(data, "writer_flush_max_events", defaults.writer_flush_max_events),
        privacy_mode=privacy,
        redaction_custom_patterns=tuple(patterns),
    )
