"""Real browser-use Agent/SDK/UI journey; only the LLM is deterministic in CI."""
import asyncio

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

pytest.importorskip("browser_use")
pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright, expect
from acira import demo
from acira.journal import Journal
from acira.service import Service


@pytest.fixture
async def browser_use_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("ACIRA_HOME", str(tmp_path / "private"))
    monkeypatch.setenv("BROWSER_USE_CONFIG_DIR", str(tmp_path / "browser-use"))
    monkeypatch.setenv("ANONYMIZED_TELEMETRY", "false")
    monkeypatch.setenv("BROWSER_USE_CLOUD_SYNC", "false")
    from acira import browser_use_demo as backend
    from browser_use import ChatOllama
    from browser_use.llm.views import ChatInvokeCompletion

    original_session = backend.BrowserSession
    def headless_session(*args, **kwargs):
        assert kwargs["args"] == ["--password-store=basic", "--use-mock-keychain"]
        kwargs["headless"] = True
        return original_session(*args, **kwargs)
    monkeypatch.setattr(backend, "BrowserSession", headless_session)

    def scripted_model(state, model, host):
        class ScriptedModel(ChatOllama):
            async def ainvoke(self, messages, output_format=None, **kwargs):
                state["model_calls"] = state.get("model_calls", 0) + 1
                state["model_responses"] = state.get("model_responses", 0) + 1
                name = {"payment": "review_payment", "draft": "save_test_draft", "delete": "try_delete"}[state["kind"]]
                payload = {"memory": "Call the guarded operation once.", "action": [{name: {}}]}
                return ChatInvokeCompletion(completion=output_format.model_validate(payload), usage=None)
        return ScriptedModel(model="ci-local-model")
    monkeypatch.setattr(backend, "create_model", scripted_model)

    app = demo.create_app(tmp_path / "playground", adapter="browser_use")
    service = Service()
    async with TestServer(app, host="127.0.0.1") as drive_server, TestServer(service.app(), host="127.0.0.1") as dashboard:
        demo.BASE = str(drive_server.make_url("/")).rstrip("/")
        service.port = dashboard.port
        async with ClientSession(headers={"Authorization": "Bearer " + service.admin_key}) as client:
            ticket = (await (await client.post(dashboard.make_url("/api/ticket"), json={})).json())["ticket"]
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            drive, ui = await browser.new_page(), await browser.new_page()
            errors = []
            drive.on("pageerror", lambda error: errors.append(str(error)))
            ui.on("pageerror", lambda error: errors.append(str(error)))
            await drive.goto(demo.BASE)
            await ui.goto(str(dashboard.make_url("/")) + "#ticket=" + ticket)
            await ui.get_by_role("heading", name="Acira Browser-use Playground", exact=True).wait_for()
            try:
                yield drive, ui, backend
            finally:
                await demo.cleanup_tasks(app)
                await browser.close()
                assert not errors


async def review(ui, project="Acira Browser-use Playground"):
    await ui.get_by_role("button", name="Approvals", exact=False).first.click()
    row = ui.locator(".approval-card").filter(has_text=project + " / default")
    await row.get_by_role("button", name="Review operation").wait_for(timeout=45000)
    await row.get_by_role("button", name="Review operation").click()
    await expect(ui.get_by_role("button", name="Approve once", exact=True)).to_be_enabled()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,count", [("Approve once", 1), ("Reject", 0)])
async def test_browser_use_agent_review_executes_once_and_ends(browser_use_ui, decision, count):
    drive, ui, _ = browser_use_ui
    await drive.get_by_role("button", name="Start payment review").click()
    await review(ui)
    run_id = demo.RUNS["checkout"]["run"]
    await ui.get_by_role("button", name=decision, exact=True).click()
    await expect(drive.locator("#checkout-status")).to_contain_text("RUN ENDED", timeout=30000)
    await expect(drive.locator("#payments")).to_have_text(str(count))
    assert demo.RUNS["checkout"]["model_responses"] >= 1
    assert demo.RUNS["checkout"]["outcome"] == ("completed" if count else "rejected")
    journal = Journal(demo.ROOT)
    run = next(r for r in journal.runs() if r["run_id"] == run_id)
    assert run["state"] == "ended" and not run["pending_reviews"]
    events = journal.events(run=run_id, limit=1000)
    assert any(e["type"] == "run_ended" for e in events)
    assert any(e["type"] == "submission_response_observed" for e in events) == bool(count)


@pytest.mark.asyncio
async def test_browser_use_agent_block_and_cancel_have_no_effects(browser_use_ui):
    drive, ui, _ = browser_use_ui
    await drive.get_by_role("button", name="Try blocked delete").click()
    await expect(drive.locator("#checkout-status")).to_contain_text("RUN ENDED", timeout=45000)
    await expect(drive.locator("#checkout-status")).to_contain_text("BLOCKED BY POLICY")
    await expect(drive.locator("#deletes")).to_have_text("0")
    await drive.get_by_role("button", name="Start payment review").click()
    await review(ui)
    run_id = demo.RUNS["checkout"]["run"]
    await drive.get_by_role("button", name="Cancel checkout run").click()
    await expect(drive.locator("#checkout-status")).to_contain_text("RUN CANCELLED", timeout=20000)
    await expect(drive.locator("#payments")).to_have_text("0")
    assert next(r for r in Journal(demo.ROOT).reviews() if r["run_id"] == run_id)["state"] == "invalidated"


@pytest.mark.asyncio
async def test_browser_use_agent_projects_and_draft_are_independent(browser_use_ui):
    drive, ui, _ = browser_use_ui
    await drive.get_by_role("button", name="Start payment review").click()
    await drive.get_by_role("button", name="Start draft review").click()
    await review(ui, "Acira Browser-use Research")
    checkout = demo.RUNS["checkout"]["run"]
    await ui.get_by_role("button", name="Approve once", exact=True).click()
    await expect(drive.locator("#research-status")).to_contain_text("RUN ENDED", timeout=30000)
    await expect(drive.locator("#drafts")).to_have_text("1")
    assert next(r for r in Journal(demo.ROOT).reviews() if r["run_id"] == checkout)["state"] == "pending"
    await review(ui)
    await ui.get_by_role("button", name="Reject", exact=True).click()
    await expect(drive.locator("#checkout-status")).to_contain_text("RUN ENDED", timeout=30000)
    await expect(drive.locator("#payments")).to_have_text("0")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError, ValueError], ids=["model-unavailable", "invalid-model-output"])
async def test_model_failure_never_substitutes_success_or_creates_review(browser_use_ui, monkeypatch, failure):
    drive, _, backend = browser_use_ui
    from browser_use import ChatOllama
    def failing_model(state, model, host):
        class FailingModel(ChatOllama):
            async def ainvoke(self, *args, **kwargs):
                state["model_calls"] += 1
                raise failure("Local inference failed in this adversarial test")
        return FailingModel(model="ci-unavailable-local-model")
    monkeypatch.setattr(backend, "create_model", failing_model)
    await drive.get_by_role("button", name="Start payment review").click()
    await expect(drive.locator("#checkout-status")).to_contain_text("RUN ENDED", timeout=45000)
    await expect(drive.locator("#payments")).to_have_text("0")
    state = demo.RUNS["checkout"]
    assert state["model_calls"] >= 1 and state["model_responses"] == 0
    assert state["outcome"] == "agent_incomplete"
    assert not [review for review in Journal(demo.ROOT).reviews() if review["run_id"] == state["run"]]
    assert not any(event["type"] == "submission_dispatch_attempt" for event in Journal(demo.ROOT).events(run=state["run"], limit=1000))


@pytest.mark.parametrize("host", ["https://api.example.com", "http://192.168.1.2:11434", "http://user:password@localhost:11434", "http://localhost:11434/path"])
def test_demo_model_endpoint_stays_local(host):
    from acira.browser_use_demo import local_endpoint
    with pytest.raises(ValueError, match="loopback"):
        local_endpoint(host)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["local", "missing", "no-weights", "remote"])
async def test_demo_requires_downloaded_local_model(monkeypatch, case):
    import httpx
    from acira import browser_use_demo as backend
    original_client = httpx.AsyncClient
    calls = []
    def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/api/tags":
            models = [] if case == "missing" else [{"name": "local:4b", "size": 0 if case == "no-weights" else 2_500_000_000}]
            return httpx.Response(200, json={"models": models})
        assert request.url.path == "/api/show"
        return httpx.Response(200, json={"remote_host": "https://example.com"} if case == "remote" else {})
    def client(**kwargs):
        assert kwargs["trust_env"] is False
        return original_client(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(backend.httpx, "AsyncClient", client)
    if case == "local":
        assert await backend.check_local_model("http://127.0.0.1:11434", "local:4b") == "http://127.0.0.1:11434"
    else:
        with pytest.raises(ValueError):
            await backend.check_local_model("http://127.0.0.1:11434", "local:4b")
    assert calls == (["/api/tags", "/api/show"] if case in {"local", "remote"} else ["/api/tags"])
