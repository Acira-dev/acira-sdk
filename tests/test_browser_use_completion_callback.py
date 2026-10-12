"""Completion callbacks survive SDK deprecations becoming errors."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
pytest.importorskip("browser_use")

from acira.adapters.browser_use import _execute_step_with_acira_hitl_pause


@pytest.mark.filterwarnings("error::DeprecationWarning:acira")
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
async def test_step_completion_calls_registered_callback_once(asynchronous):
    events = []
    step_info = object()
    history = SimpleNamespace(is_done=lambda: "step" in events)

    async def step(received_info):
        assert received_info is step_info
        await asyncio.sleep(0)
        events.append("step")

    async def log_completion():
        events.append("completion")

    async def demo_log(*_args):
        pass

    def sync_callback(received_history):
        assert received_history is history
        events.append("callback")

    async def async_callback(received_history):
        assert received_history is history
        await asyncio.sleep(0)
        events.append("callback")

    agent = SimpleNamespace(
        step=step,
        settings=SimpleNamespace(step_timeout=1.0, use_judge=False),
        logger=logging.getLogger(__name__),
        _demo_mode_log=demo_log,
        history=history,
        log_completion=log_completion,
        register_done_callback=async_callback if asynchronous else sync_callback,
    )

    assert await _execute_step_with_acira_hitl_pause(agent, 0, 1, step_info) is True
    assert events == ["step", "completion", "callback"]
