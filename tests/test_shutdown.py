"""PR2 step 9: graceful shutdown (signals + atexit), bounded drain."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from acira_cdp_proxy.audit import AuditPipeline
from acira_cdp_proxy.audit.lifecycle import GracefulShutdown
from acira_cdp_proxy.config import AciraGuardConfig


def _decision(metadata):
    return SimpleNamespace(allow=True, reason="r", metadata=metadata)


def _persistent_cfg(tmp_path):
    return AciraGuardConfig.from_dict({"enabled": True, "agent_name": "demo", "audit": {
        "mode": "persistent", "directory": str(tmp_path / "audits"),
        "writer_flush_max_events": 1, "writer_flush_interval_seconds": 0.05}})


async def _persistent_pipe(tmp_path, n=2):
    pipe = AuditPipeline.from_config(_persistent_cfg(tmp_path))
    await pipe.start()
    for _ in range(n):
        pipe.record_decision(method="m", decision=_decision({"decision": "allow"}), latency_ms=0.1)
    return pipe


async def test_run_signal_finalizes_bundle_and_exits(tmp_path):
    pipe = await _persistent_pipe(tmp_path)
    session = pipe.session
    exits, stopped = [], []
    gs = GracefulShutdown(pipe, exit_func=exits.append, stop_actions=lambda: stopped.append(1))
    zip_path = await gs.run(end_reason="signal")
    assert zip_path.exists() and (session.dir / "events.jsonl.gz").exists()
    meta = session.read_session_json()
    assert meta["end_reason"] == "signal" and meta["graceful_close"] is True and meta["event_count"] == 2
    assert exits == [0] and stopped == [1]


async def test_run_is_idempotent(tmp_path):
    pipe = await _persistent_pipe(tmp_path)
    exits = []
    gs = GracefulShutdown(pipe, exit_func=exits.append)
    await gs.run(end_reason="signal")
    assert await gs.run(end_reason="signal") is None  # second call no-ops
    assert exits == [0]  # exit_func called once


async def test_run_completed_reason(tmp_path):
    pipe = await _persistent_pipe(tmp_path, n=1)
    gs = GracefulShutdown(pipe, exit_func=lambda _c: None)
    await gs.run(end_reason="completed")
    assert pipe.session.read_session_json()["end_reason"] == "completed"


async def test_bounded_budget_forces_exit_on_hang(tmp_path):
    class HangingPipeline:
        persistent = True
        session = None
        event_count = 0

        async def aclose(self, **_kwargs):
            await asyncio.sleep(10)  # never completes within budget

    exits = []
    gs = GracefulShutdown(HangingPipeline(), budget_seconds=0.05, exit_func=exits.append)
    await gs.run(end_reason="signal")  # must return ~immediately, not in 10s
    assert exits == [0]


async def test_sync_finalize_atexit_net(tmp_path):
    pipe = await _persistent_pipe(tmp_path)
    session = pipe.session
    await pipe.aclose(end_reason="completed")  # writer closed; events.jsonl on disk
    gs = GracefulShutdown(pipe)
    zip_path = gs.sync_finalize(end_reason="completed")
    assert zip_path is not None and zip_path.exists()
    assert (session.dir / "events.jsonl.gz").exists()
    assert session.read_session_json()["graceful_close"] is True


async def test_sync_finalize_skipped_if_run_already_done(tmp_path):
    pipe = await _persistent_pipe(tmp_path)
    gs = GracefulShutdown(pipe, exit_func=lambda _c: None)
    await gs.run(end_reason="signal")
    assert gs.sync_finalize() is None  # run already finalized


async def test_sync_finalize_volatile_is_none(tmp_path):
    cfg = AciraGuardConfig.from_dict({"enabled": True, "audit": {"mode": "volatile", "directory": str(tmp_path / "a")}})
    pipe = AuditPipeline.from_config(cfg)
    assert GracefulShutdown(pipe).sync_finalize() is None


def test_install_signal_handlers_does_not_crash():
    # Real signal delivery is a live-verify item; installation must at least not raise.
    loop = asyncio.new_event_loop()
    try:
        pipe = AuditPipeline.from_config(AciraGuardConfig.from_dict({"enabled": True}))
        gs = GracefulShutdown(pipe, exit_func=lambda _c: None)
        gs.install_signal_handlers(loop=loop)
        gs.install_atexit()
        assert gs.installed is True
    finally:
        loop.close()
