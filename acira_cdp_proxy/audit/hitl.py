"""Operational HITL channel — free-text prompts with N named options.

Distinct from the binary per-action review system (allow/block, which pauses the agent). Used
for audit/lifecycle prompts: pre-flight failure, mid-run persistence failure, agent crash.
Reuses the terminal (and, via a handler, the dashboard) transport but its own schema.

Fail-secure: on timeout / EOF / unrecognized answer the configured ``default_option`` is chosen.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol, TextIO


@dataclass(frozen=True)
class HITLOption:
    key: str
    label: str


@dataclass(frozen=True)
class OperationalHITLRequest:
    kind: str  # "preflight" | "persistence_failure" | "agent_crash"
    prompt: str
    options: tuple[HITLOption, ...]
    default_option: str  # fail-secure choice on timeout / no answer
    timeout_seconds: float | None = None  # None = block until answered
    blocking: bool = True
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def option_keys(self) -> tuple[str, ...]:
        return tuple(option.key for option in self.options)

    def __post_init__(self) -> None:
        keys = self.option_keys()
        if not keys:
            raise ValueError("OperationalHITLRequest requires at least one option")
        if self.default_option not in keys:
            raise ValueError(f"default_option {self.default_option!r} not in {keys}")


@dataclass(frozen=True)
class OperationalHITLResult:
    request_id: str
    selected: str
    timed_out: bool = False


OperationalHITLHandler = Callable[[OperationalHITLRequest], "str | OperationalHITLResult | Awaitable"]


class OperationalHITLBackend(Protocol):
    async def ask(self, request: OperationalHITLRequest) -> OperationalHITLResult: ...


class TerminalOperationalHITL:
    """Renders the prompt + numbered options to the terminal and reads one choice.

    Accepts the option number, key, or label (case-insensitive). Empty / unrecognized / timed-out
    input resolves to ``default_option`` (fail-secure).
    """

    def __init__(self, *, input_func: Callable[[str], str] | None = None, output: TextIO | None = None) -> None:
        self._input = input_func or input
        self._out = output or sys.stderr

    def render(self, request: OperationalHITLRequest) -> str:
        lines = [f"[Acira Audit] {request.prompt}"]
        for index, option in enumerate(request.options, 1):
            lines.append(f"  {index}) {option.label} [{option.key}]")
        lines.append(f"Choose 1-{len(request.options)} [default: {request.default_option}]: ")
        return "\n".join(lines)

    def match(self, answer: str | None, request: OperationalHITLRequest) -> str:
        normalized = (answer or "").strip().lower()
        if not normalized:
            return request.default_option
        for index, option in enumerate(request.options, 1):
            if normalized in (str(index), option.key.lower(), option.label.lower()):
                return option.key
        return request.default_option  # unrecognized → fail-secure

    async def ask(self, request: OperationalHITLRequest) -> OperationalHITLResult:
        prompt = self.render(request)
        try:
            if request.timeout_seconds is not None:
                answer = await asyncio.wait_for(
                    asyncio.to_thread(self._input, prompt), timeout=request.timeout_seconds
                )
            else:
                answer = await asyncio.to_thread(self._input, prompt)
        except (asyncio.TimeoutError, EOFError, OSError):
            return OperationalHITLResult(request.request_id, request.default_option, timed_out=True)
        return OperationalHITLResult(request.request_id, self.match(answer, request), timed_out=False)


class CallableOperationalHITL:
    """Wraps a handler callable (sync or async) returning an option key or a full result.

    Used to route operational HITLs through the dashboard (data-seam) or to inject decisions in
    tests. A returned key outside the option set falls back to ``default_option``.
    """

    def __init__(self, handler: OperationalHITLHandler) -> None:
        self._handler = handler

    async def ask(self, request: OperationalHITLRequest) -> OperationalHITLResult:
        result = self._handler(request)
        if asyncio.iscoroutine(result):
            result = await result
        if isinstance(result, OperationalHITLResult):
            return result
        selected = result if result in request.option_keys() else request.default_option
        return OperationalHITLResult(request.request_id, selected, timed_out=False)


async def ask_operational(
    request: OperationalHITLRequest, backend: OperationalHITLBackend | None
) -> OperationalHITLResult:
    """Ask via the given backend; with no backend, fail-secure to the default option."""
    if backend is None:
        return OperationalHITLResult(request.request_id, request.default_option, timed_out=True)
    return await backend.ask(request)
