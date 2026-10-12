"""Visible review diagnostics do not weaken evidence, timing or policy outcomes."""
import asyncio
from dataclasses import replace

import pytest

from acira import ActionBlocked
from acira.adapters.playwright import PlaywrightGuardBlockedError
from acira.project import initialize
from acira.runtime import ProjectRuntime
from acira_cdp_proxy.action_gateway import AciraActionBlocked
from tests.test_runtime_contracts import review_request, pending


def test_public_exception_catches_core_and_legacy_playwright_blocks():
    for kind in (AciraActionBlocked, PlaywrightGuardBlockedError):
        try:
            raise kind("Blocked before dispatch", metadata={"decision": "block"})
        except ActionBlocked as error:
            assert error.reason == "Blocked before dispatch"
            assert error.metadata == {"decision": "block"}


@pytest.mark.asyncio
async def test_waiting_review_names_channel_timeout_and_absent_dashboard(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    project = initialize(tmp_path / "project", selected=[], dashboard=True)
    runtime = ProjectRuntime(project, "default")
    task = asyncio.create_task(runtime.ask(review_request()))
    try:
        review = await pending(runtime)
        text = capsys.readouterr()
        assert not text.out
        assert "Review required" in text.err and "acira open → Approvals" in text.err
        assert "Blocks after 5:00" in text.err and "No dashboard is connected" in text.err
        runtime.journal.decide(review["review_id"], "rejected")
        assert await task == "block"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_missing_evidence_explains_binding_without_asking_or_dispatching(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    project = initialize(tmp_path / "project", selected=["payment"], dashboard=False)
    class Reviewer:
        async def ask(self, request):
            pytest.fail("Missing critical facts must not be offered for approval")
    runtime = ProjectRuntime(project, "default", dashboard=False, review_backend=Reviewer())
    request = review_request(critical=True)
    classification = replace(request.classification, details={"cedar": {"general_skills": {"detections": [
        {"skill_id": "acira.general.checkout", "risk": "checkout.payment_submit_attempt", "severity": "critical"}]}}})
    try:
        assert await runtime.ask(replace(request, classification=classification)) == "block"
        text = capsys.readouterr()
        assert not text.out
        assert "amount, currency, account, verified request binding" in text.err
        assert "guard.submit(...)" in text.err
        assert "https://pypi.org/project/acira/#human-review-and-critical-submissions" in text.err
        assert not runtime.journal.reviews()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_quiet_runtime_notices_do_not_change_review_outcome(tmp_path, monkeypatch, capsys):
    from acira.local import atomic_json
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    project = initialize(tmp_path / "project", selected=[], dashboard=True)
    config = project.document()
    config["agents"]["default"]["guard"] = {"terminal_log": False}
    atomic_json(project.path, config)
    runtime = ProjectRuntime(project, "default")
    task = asyncio.create_task(runtime.ask(review_request()))
    try:
        review = await pending(runtime)
        text = capsys.readouterr()
        assert not text.out and not text.err
        runtime.journal.decide(review["review_id"], "rejected")
        assert await task == "block"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.close()
