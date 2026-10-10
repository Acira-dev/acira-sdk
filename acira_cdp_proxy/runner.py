from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import sys
from pathlib import Path
from typing import Mapping, Sequence

from .config import AciraGuardConfig
from .logger import CDPLogger, NullCDPLogger
from .server import AciraCDPProxy


def normalize_agent_command(command: Sequence[str]) -> list[str]:
    normalized = list(command)
    if normalized and normalized[0] == "--":
        normalized = normalized[1:]
    if not normalized:
        raise ValueError("Missing agent command after '--'")
    return normalized


def build_agent_environment(base_env: Mapping[str, str], proxy_url: str) -> dict[str, str]:
    env = dict(base_env)
    env["ACIRA_CDP_URL"] = proxy_url
    return env


class AgentProcessController:
    """Optional best-effort suspend/resume for Acira-supervised child agents."""

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self.process: asyncio.subprocess.Process | None = None
        self._suspended = False
        self._warned = False

    def attach(self, process: asyncio.subprocess.Process | None) -> None:
        self.process = process
        self._suspended = False

    async def pause(self) -> None:
        if not self.enabled or self.process is None or self.process.returncode is not None or self._suspended:
            return
        if _set_process_suspended(self.process.pid, suspend=True):
            self._suspended = True
            print("[Acira Runner] Agent subprocess paused during HITL review.", file=sys.stderr, flush=True)
        elif not self._warned:
            self._warned = True
            print(
                "[Acira Runner] Agent subprocess suspension is unavailable; CDP traffic is still gated.",
                file=sys.stderr,
                flush=True,
            )

    async def resume(self) -> None:
        if not self.enabled or self.process is None or not self._suspended:
            return
        if _set_process_suspended(self.process.pid, suspend=False):
            self._suspended = False
            print("[Acira Runner] Agent subprocess resumed after HITL review.", file=sys.stderr, flush=True)


async def run_supervised_agent(
    *,
    cdp_url: str,
    config_path: str | Path | None,
    host: str,
    port: int,
    log_file: str | Path | None,
    agent_command: Sequence[str],
    suspend_child_during_review: bool = False,
) -> int:
    command = normalize_agent_command(agent_command)
    config = AciraGuardConfig.from_file(config_path) if config_path else None
    controller = AgentProcessController(enabled=suspend_child_during_review)
    proxy = AciraCDPProxy(
        cdp_url,
        config=config,
        host=host,
        port=port,
        logger=CDPLogger(Path(log_file)) if log_file else NullCDPLogger(),
        on_pause_start=controller.pause,
        on_pause_end=controller.resume,
    )

    await proxy.start()
    process: asyncio.subprocess.Process | None = None
    try:
        env = build_agent_environment(os.environ, proxy.local_http_url)
        print(f"[Acira Runner] Proxy listening: {proxy.local_http_url}", flush=True)
        print(f"[Acira Runner] Upstream CDP: {cdp_url}", flush=True)
        print("[Acira Runner] Launching agent with ACIRA_CDP_URL set.", flush=True)
        process = await asyncio.create_subprocess_exec(*command, env=env)
        controller.attach(process)
        return await process.wait()
    finally:
        await controller.resume()
        controller.attach(None)
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
        await proxy.stop()


def _set_process_suspended(pid: int, *, suspend: bool) -> bool:
    if os.name == "nt":
        return _set_windows_process_suspended(pid, suspend=suspend)

    try:
        import signal

        os.kill(pid, signal.SIGSTOP if suspend else signal.SIGCONT)
        return True
    except (AttributeError, OSError):
        return False


def _set_windows_process_suspended(pid: int, *, suspend: bool) -> bool:
    PROCESS_SUSPEND_RESUME = 0x0800
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not handle:
        return False

    try:
        function = ntdll.NtSuspendProcess if suspend else ntdll.NtResumeProcess
        function.argtypes = [ctypes.c_void_p]
        function.restype = ctypes.c_long
        return function(handle) == 0
    finally:
        kernel32.CloseHandle(handle)
