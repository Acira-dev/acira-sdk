"""Bounded cleanup for external processes owned by browser tests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import subprocess
from typing import AsyncIterator, Sequence


async def _stop_owned_process(
    process: subprocess.Popen[bytes], *, terminate_timeout: float, kill_timeout: float
) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.to_thread(process.wait, timeout=terminate_timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        # A failed reap is a test failure; it is never silently discarded.
        await asyncio.to_thread(process.wait, timeout=kill_timeout)


async def _finish_owned_process(
    process: subprocess.Popen[bytes], *, terminate_timeout: float, kill_timeout: float
) -> None:
    # Cancellation of the test must not abandon its external browser. The
    # underlying wait remains bounded even while cancellation is deferred.
    shutdown = asyncio.create_task(
        _stop_owned_process(
            process, terminate_timeout=terminate_timeout, kill_timeout=kill_timeout
        ),
        name="acira-test-owned-process-cleanup",
    )
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(shutdown)
            break
        except asyncio.CancelledError as exc:
            if shutdown.done():
                raise
            cancellation = exc
    if cancellation is not None:
        raise cancellation


def _attach_stderr(error: BaseException, stderr_path: Path) -> None:
    try:
        with stderr_path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 4096))
            tail = stream.read().decode("utf-8", errors="replace").strip()
    except OSError as exc:
        tail = f"stderr could not be read: {exc}"
    error.add_note(f"Owned process stderr: {stderr_path}\n{tail or '(empty)'}")


@asynccontextmanager
async def owned_subprocess(
    args: Sequence[str],
    *,
    stderr_path: Path,
    terminate_timeout: float = 10,
    kill_timeout: float = 5,
) -> AsyncIterator[subprocess.Popen[bytes]]:
    """Signal only this Popen-owned PID, reap it, and retain failure evidence."""
    with stderr_path.open("wb", buffering=0) as stderr:
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=stderr)
        original_error: BaseException | None = None
        try:
            yield process
        except BaseException as exc:
            original_error = exc
            raise
        finally:
            try:
                await _finish_owned_process(
                    process, terminate_timeout=terminate_timeout, kill_timeout=kill_timeout
                )
            except BaseException as cleanup_error:
                if original_error is None:
                    _attach_stderr(cleanup_error, stderr_path)
                    raise
                original_error.add_note(
                    "Owned process cleanup also failed: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
            if original_error is not None:
                _attach_stderr(original_error, stderr_path)
