"""External-process cleanup works without any optional browser framework."""

import asyncio
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from tests import subprocess_helpers as helpers


def child_command(ready: Path, *, ignore_terminate: bool = False) -> list[str]:
    return [sys.executable, "-u", "-c", "\n".join([
        "import pathlib, signal, sys, time",
        "if sys.argv[2] == 'ignore': signal.signal(signal.SIGTERM, signal.SIG_IGN)",
        "print('owned harmless subprocess ready', file=sys.stderr, flush=True)",
        "pathlib.Path(sys.argv[1]).write_text('ready')",
        "while True: time.sleep(0.05)",
    ]), str(ready), "ignore" if ignore_terminate else "responsive"]


async def wait_ready(ready: Path) -> None:
    async with asyncio.timeout(5):
        while not ready.exists():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_owned_subprocess_terminates_and_retains_stderr(tmp_path):
    ready = tmp_path / "ready"
    stderr_path = tmp_path / "stderr.log"
    async with helpers.owned_subprocess(
        child_command(ready), stderr_path=stderr_path, terminate_timeout=1, kill_timeout=1
    ) as process:
        await wait_ready(ready)
        assert process.poll() is None
    assert process.poll() is not None
    assert "owned harmless subprocess ready" in stderr_path.read_text()
    if os.name == "posix":
        assert process.returncode == -signal.SIGTERM


@pytest.mark.asyncio
async def test_owned_subprocess_kills_reaps_and_keeps_event_loop_live(tmp_path, monkeypatch):
    ready = tmp_path / "ready"
    ticks = []
    finished = asyncio.Event()
    async def ticker():
        while not finished.is_set():
            ticks.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.01)
    ticker_task = asyncio.create_task(ticker())
    try:
        async with helpers.owned_subprocess(
            child_command(ready, ignore_terminate=True), stderr_path=tmp_path / "stderr.log",
            terminate_timeout=0.2, kill_timeout=1,
        ) as process:
            await wait_ready(ready)
            if os.name != "posix":
                # Windows terminate is already forceful. Exercise the fallback
                # against a real child by withholding only its first signal.
                monkeypatch.setattr(process, "terminate", lambda: None)
            original_wait = process.wait
            original_kill = process.kill
            calls = []
            def observed_wait(*, timeout):
                calls.append(("wait", timeout))
                return original_wait(timeout=timeout)
            def observed_kill():
                calls.append(("kill", None))
                return original_kill()
            monkeypatch.setattr(process, "wait", observed_wait)
            monkeypatch.setattr(process, "kill", observed_kill)
            start_tick = len(ticks)
        assert process.poll() is not None
        assert calls == [("wait", 0.2), ("kill", None), ("wait", 1)]
        assert len(ticks) - start_tick >= 5
        if os.name == "posix":
            assert process.returncode == -signal.SIGKILL
    finally:
        finished.set()
        await ticker_task


@pytest.mark.asyncio
async def test_owned_subprocess_preserves_original_error_and_cleanup_evidence(tmp_path, monkeypatch):
    ready = tmp_path / "ready"
    original_error = RuntimeError("original connection failure")
    original_stop = helpers._stop_owned_process
    async def reap_then_report_failure(process, **kwargs):
        await original_stop(process, **kwargs)
        raise subprocess.TimeoutExpired("owned test child", 1)
    monkeypatch.setattr(helpers, "_stop_owned_process", reap_then_report_failure)
    with pytest.raises(RuntimeError) as caught:
        async with helpers.owned_subprocess(
            child_command(ready), stderr_path=tmp_path / "stderr.log",
            terminate_timeout=1, kill_timeout=1,
        ) as process:
            await wait_ready(ready)
            raise original_error
    assert caught.value is original_error
    assert process.poll() is not None
    notes = "\n".join(caught.value.__notes__)
    assert "cleanup also failed: TimeoutExpired" in notes
    assert "owned harmless subprocess ready" in notes
    assert str(tmp_path / "stderr.log") in notes


@pytest.mark.asyncio
async def test_owned_subprocess_reports_cleanup_failure_without_original_error(tmp_path, monkeypatch):
    ready = tmp_path / "ready"
    cleanup_error = RuntimeError("cleanup failure")
    original_stop = helpers._stop_owned_process
    async def reap_then_report_failure(process, **kwargs):
        await original_stop(process, **kwargs)
        raise cleanup_error
    monkeypatch.setattr(helpers, "_stop_owned_process", reap_then_report_failure)
    with pytest.raises(RuntimeError) as caught:
        async with helpers.owned_subprocess(
            child_command(ready), stderr_path=tmp_path / "stderr.log",
            terminate_timeout=1, kill_timeout=1,
        ) as process:
            await wait_ready(ready)
    assert caught.value is cleanup_error
    assert process.poll() is not None
    assert "owned harmless subprocess ready" in "\n".join(caught.value.__notes__)


@pytest.mark.asyncio
async def test_cancelled_test_reaps_owned_subprocess(tmp_path):
    ready = tmp_path / "ready"
    entered = asyncio.Event()
    processes = []
    async def run():
        async with helpers.owned_subprocess(
            child_command(ready), stderr_path=tmp_path / "stderr.log",
            terminate_timeout=1, kill_timeout=1,
        ) as process:
            processes.append(process)
            await wait_ready(ready)
            entered.set()
            await asyncio.Future()
    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].poll() is not None


@pytest.mark.asyncio
async def test_repeated_cancellation_during_cleanup_still_kills_and_reaps(tmp_path, monkeypatch):
    ready = tmp_path / "ready"
    wait_started = asyncio.Event()
    loop = asyncio.get_running_loop()
    processes = []
    async def run():
        async with helpers.owned_subprocess(
            child_command(ready, ignore_terminate=True), stderr_path=tmp_path / "stderr.log",
            terminate_timeout=0.5, kill_timeout=1,
        ) as process:
            processes.append(process)
            await wait_ready(ready)
            if os.name != "posix":
                monkeypatch.setattr(process, "terminate", lambda: None)
            original_wait = process.wait
            def observed_wait(*, timeout):
                loop.call_soon_threadsafe(wait_started.set)
                return original_wait(timeout=timeout)
            monkeypatch.setattr(process, "wait", observed_wait)
    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(wait_started.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelling() == 2
        assert processes[0].poll() is not None
        if os.name == "posix":
            assert processes[0].returncode == -signal.SIGKILL
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
