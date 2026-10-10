"""Persistence failures remain visible without skipping runtime cleanup."""
import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from acira.project import initialize
from acira.runtime import ProjectRuntime
from acira_cdp_proxy.guard import ActionClassification, GuardReviewRequest, GuardTarget
from acira_cdp_proxy.policy import CDPMessageContext, AGENT_TO_BROWSER


@pytest.mark.asyncio
async def test_failed_review_invalidation_still_cancels_reviewer_and_clears_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    ready, cancelled = asyncio.Event(), asyncio.Event()
    class Reviewer:
        async def ask(self, request):
            ready.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    runtime = ProjectRuntime(initialize(tmp_path / "project", selected=[]), "default",
                             dashboard=False, review_backend=Reviewer())
    request = GuardReviewRequest("{}", CDPMessageContext(AGENT_TO_BROWSER, "test", 1, "/acira/action/playwright"),
        ActionClassification("playwright.click", "click", "Continue", target=GuardTarget(id="next")), "Continue")
    task = asyncio.create_task(runtime.ask(request))
    await asyncio.wait_for(ready.wait(), 1)
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("Required journal unavailable")
    monkeypatch.setattr(runtime.journal, "decide", unavailable)
    task.cancel()
    with pytest.raises(sqlite3.OperationalError):
        await asyncio.wait_for(task, 2)
    assert cancelled.is_set() and not runtime.pending
    await runtime.close()
    assert await runtime.ask(request) == "block"
    with pytest.raises(RuntimeError, match="ended"):
        await runtime.before_decision()


@pytest.mark.asyncio
async def test_failed_heartbeat_and_journal_still_close_audit_pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    runtime = ProjectRuntime(initialize(tmp_path / "project", selected=[]), "default", dashboard=False)
    cleaned = []
    class Pipeline:
        async def aclose(self, **kwargs):
            cleaned.append(True)
    runtime.adapter = SimpleNamespace(guard_policy=SimpleNamespace(_audit_pipeline=Pipeline()))
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("Required journal unavailable")
    async def failed_heartbeat():
        unavailable()
    runtime.task = asyncio.create_task(failed_heartbeat())
    await asyncio.sleep(0)
    runtime.pending.add("pending-test-review")
    monkeypatch.setattr(runtime.journal, "decide", unavailable)
    monkeypatch.setattr(runtime, "revoke_grants", unavailable)
    monkeypatch.setattr(runtime, "publish", unavailable)
    with pytest.raises(OSError, match="bookkeeping failed"):
        await asyncio.wait_for(runtime.close(), 2)
    assert runtime.closed and runtime.task.done() and not runtime.pending
    assert cleaned == [True] and runtime.state == "failed"
    with pytest.raises(RuntimeError, match="ended"):
        await runtime.before_decision()
