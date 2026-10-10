"""Pre-flight checks for persistent audit mode, run before the agent connects to the browser.

Verifies the audit directory is usable (exists/creatable, writable, enough free space, and a
written event reads back as valid JSON). On failure, a blocking operational HITL offers three
choices: continue without saving (downgrade to volatile), stop now, or retry. On timeout the
fail-secure default is ``stop`` — better to refuse to start than to run unaudited by accident.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .hitl import HITLOption, OperationalHITLBackend, OperationalHITLRequest, ask_operational

_PROBE_NAME = ".acira_preflight_probe"

# gate outcomes
PERSISTENT = "persistent"
VOLATILE = "volatile"
STOP = "stop"


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    failures: tuple[str, ...]


def run_preflight_checks(directory: str | os.PathLike, *, min_free_mb: float) -> PreflightResult:
    directory = Path(directory)
    failures: list[str] = []

    # 1. directory exists or can be created
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return PreflightResult(False, (f"cannot create audit directory {directory}: {exc}",))

    probe = directory / _PROBE_NAME

    # 2. write permission via a real write
    try:
        probe.write_text("probe", encoding="utf-8")
    except OSError as exc:
        failures.append(f"no write permission in {directory}: {exc}")

    # 3. free space
    try:
        free_bytes = shutil.disk_usage(directory).free
        if free_bytes < min_free_mb * 1024 * 1024:
            failures.append(
                f"insufficient free space: {free_bytes // (1024 * 1024)} MB free, "
                f"{int(min_free_mb)} MB required"
            )
    except OSError as exc:
        failures.append(f"cannot determine free space for {directory}: {exc}")

    # 4. read-back: write a test event, read it, parse as JSON
    try:
        sample = {"event_type": "preflight_probe", "ok": True}
        probe.write_text(json.dumps(sample), encoding="utf-8")
        parsed = json.loads(probe.read_text(encoding="utf-8"))
        if parsed != sample:
            failures.append("read-back mismatch: written event did not round-trip")
    except (OSError, ValueError) as exc:
        failures.append(f"read-back failed: {exc}")
    finally:
        try:
            probe.unlink()
        except OSError:
            pass

    return PreflightResult(not failures, tuple(failures))


async def run_preflight_gate(
    audit_config,
    backend: OperationalHITLBackend | None,
    *,
    on_downgrade: Callable[[tuple[str, ...]], None] | None = None,
    max_retries: int = 100,
) -> str:
    """Run checks; on failure drive the HITL until persistent/volatile/stop is decided.

    Returns ``PERSISTENT`` (checks passed or retried to success), ``VOLATILE`` (user chose to
    continue without saving — ``on_downgrade`` is invoked with the failure reasons), or ``STOP``.
    """
    attempts = 0
    while True:
        result = run_preflight_checks(audit_config.directory, min_free_mb=audit_config.preflight_min_free_mb)
        if result.ok:
            return PERSISTENT

        request = OperationalHITLRequest(
            kind="preflight",
            prompt="Audit pre-flight failed: " + "; ".join(result.failures),
            options=(
                HITLOption("continue", "Continue without saving (switch to volatile)"),
                HITLOption("stop", "Stop now"),
                HITLOption("retry", "Retry the pre-flight check"),
            ),
            default_option=STOP,  # fail-secure
            timeout_seconds=audit_config.preflight_hitl_timeout_seconds,
        )
        decision = await ask_operational(request, backend)

        if decision.selected == "retry":
            attempts += 1
            if attempts >= max_retries:
                return STOP
            continue
        if decision.selected == "continue":
            if on_downgrade is not None:
                on_downgrade(result.failures)
            return VOLATILE
        return STOP
