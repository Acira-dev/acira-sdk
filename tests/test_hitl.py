"""PR2 step 6: operational HITL channel (N-option prompts, fail-secure)."""

from __future__ import annotations

import time

import pytest

from acira_cdp_proxy.audit.hitl import (
    CallableOperationalHITL,
    HITLOption,
    OperationalHITLRequest,
    OperationalHITLResult,
    TerminalOperationalHITL,
    ask_operational,
)

OPTS = (HITLOption("continue", "Continue"), HITLOption("stop", "Stop now"), HITLOption("retry", "Retry"))


def _req(timeout=None):
    return OperationalHITLRequest(kind="preflight", prompt="disk failed", options=OPTS,
                                  default_option="stop", timeout_seconds=timeout)


def test_request_validation():
    with pytest.raises(ValueError):
        OperationalHITLRequest(kind="x", prompt="p", options=OPTS, default_option="nope")
    with pytest.raises(ValueError):
        OperationalHITLRequest(kind="x", prompt="p", options=(), default_option="a")


@pytest.mark.parametrize("answer,expected", [
    ("1", "continue"), ("2", "stop"), ("3", "retry"),
    ("continue", "continue"), ("RETRY", "retry"), ("Stop now", "stop"),
    ("", "stop"), ("garbage", "stop"), (None, "stop"),
])
def test_terminal_match(answer, expected):
    assert TerminalOperationalHITL().match(answer, _req()) == expected


async def test_terminal_ask_returns_choice():
    backend = TerminalOperationalHITL(input_func=lambda _p: "1")
    result = await backend.ask(_req())
    assert result.selected == "continue" and result.timed_out is False


async def test_terminal_ask_timeout_is_fail_secure():
    def slow(_prompt):
        time.sleep(0.2)
        return "1"

    backend = TerminalOperationalHITL(input_func=slow)
    result = await backend.ask(_req(timeout=0.02))
    assert result.selected == "stop" and result.timed_out is True


async def test_terminal_ask_eof_is_fail_secure():
    def eof(_prompt):
        raise EOFError

    result = await TerminalOperationalHITL(input_func=eof).ask(_req())
    assert result.selected == "stop"


async def test_callable_backend_sync_async_and_invalid():
    sync = CallableOperationalHITL(lambda _r: "retry")
    assert (await sync.ask(_req())).selected == "retry"

    async def handler(_r):
        return "continue"

    assert (await CallableOperationalHITL(handler).ask(_req())).selected == "continue"

    # full result passthrough
    passthrough = CallableOperationalHITL(lambda r: OperationalHITLResult(r.request_id, "stop", timed_out=True))
    assert (await passthrough.ask(_req())).timed_out is True

    # invalid key → fail-secure default
    assert (await CallableOperationalHITL(lambda _r: "bogus").ask(_req())).selected == "stop"


async def test_ask_operational_no_backend_is_fail_secure():
    result = await ask_operational(_req(), None)
    assert result.selected == "stop" and result.timed_out is True
