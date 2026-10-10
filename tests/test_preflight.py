"""PR2 step 6: pre-flight checks + gate flow."""

from __future__ import annotations

import shutil

import pytest

from acira_cdp_proxy.audit.hitl import OperationalHITLResult
from acira_cdp_proxy.audit.preflight import (
    PERSISTENT,
    STOP,
    VOLATILE,
    run_preflight_checks,
    run_preflight_gate,
)
from acira_cdp_proxy.audit_config import parse_audit_config


class FakeBackend:
    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    async def ask(self, request):
        self.requests.append(request)
        answer = self.answers.pop(0)
        return OperationalHITLResult(request.request_id, answer, timed_out=False)


def _cfg(tmp_path, **over):
    block = {"directory": str(tmp_path / "audits"), "preflight_min_free_mb": 0.0001, "preflight_hitl_timeout_seconds": 5}
    block.update(over)
    return parse_audit_config(block)


# --- checks -----------------------------------------------------------------

def test_checks_pass_on_writable_dir(tmp_path):
    result = run_preflight_checks(tmp_path / "audits", min_free_mb=0.0001)
    assert result.ok and result.failures == ()


def test_checks_fail_on_insufficient_space(tmp_path):
    result = run_preflight_checks(tmp_path / "audits", min_free_mb=10**12)  # 1 EB
    assert not result.ok
    assert any("free space" in f for f in result.failures)


def test_checks_fail_on_uncreatable_dir(tmp_path):
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    result = run_preflight_checks(blocker / "sub", min_free_mb=0.0001)  # parent is a file
    assert not result.ok
    assert any("cannot create" in f for f in result.failures)


def test_checks_fail_on_readback_error(tmp_path, monkeypatch):
    def boom(_self, *a, **k):
        raise OSError("disk error")

    # break read-back by making disk_usage fine but read fail
    import pathlib
    real_read = pathlib.Path.read_text

    def flaky_read(self, *a, **k):
        if self.name == ".acira_preflight_probe":
            raise OSError("read failed")
        return real_read(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "read_text", flaky_read)
    result = run_preflight_checks(tmp_path / "audits", min_free_mb=0.0001)
    assert not result.ok
    assert any("read-back failed" in f for f in result.failures)


# --- gate -------------------------------------------------------------------

async def test_gate_persistent_when_checks_pass(tmp_path):
    backend = FakeBackend([])
    outcome = await run_preflight_gate(_cfg(tmp_path), backend)
    assert outcome == PERSISTENT
    assert backend.requests == []  # no HITL when healthy


async def test_gate_continue_downgrades_to_volatile(tmp_path):
    downgrades = []
    backend = FakeBackend(["continue"])
    outcome = await run_preflight_gate(
        _cfg(tmp_path, preflight_min_free_mb=10**12), backend, on_downgrade=downgrades.append
    )
    assert outcome == VOLATILE
    assert len(downgrades) == 1 and downgrades[0]  # failure reasons passed through


async def test_gate_stop(tmp_path):
    backend = FakeBackend(["stop"])
    outcome = await run_preflight_gate(_cfg(tmp_path, preflight_min_free_mb=10**12), backend)
    assert outcome == STOP


async def test_gate_retry_then_continue(tmp_path):
    backend = FakeBackend(["retry", "continue"])
    outcome = await run_preflight_gate(_cfg(tmp_path, preflight_min_free_mb=10**12), backend)
    assert outcome == VOLATILE
    assert len(backend.requests) == 2


async def test_gate_no_backend_is_fail_secure_stop(tmp_path):
    outcome = await run_preflight_gate(_cfg(tmp_path, preflight_min_free_mb=10**12), None)
    assert outcome == STOP


async def test_gate_retry_exhaustion_stops(tmp_path):
    backend = FakeBackend(["retry"] * 10)
    outcome = await run_preflight_gate(_cfg(tmp_path, preflight_min_free_mb=10**12), backend, max_retries=3)
    assert outcome == STOP
