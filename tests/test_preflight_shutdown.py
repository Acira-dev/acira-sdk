"""Disconnected CDP clients cannot leave owned page-preflight tasks running."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import time

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
import pytest

from acira_cdp_proxy.guard import AciraGuardPolicy, PageRiskSnapshot
from acira_cdp_proxy.policy import BROWSER_TO_AGENT, CDPMessageContext
from acira_cdp_proxy.server import CDPProxyServer
import acira
from acira_cdp_proxy import server as server_module


def preflight_policy():
    path = Path(acira.__file__).parent / "resources/policies/checkout.json"
    return AciraGuardPolicy({
        "terminal_log": False,
        "general_skill_preflight": {"enabled": True},
        "general_skills": {"acira.general.checkout": {
            "path": str(path), "risks": {"checkout.page_detected": "ask"},
        }},
    })


@asynccontextmanager
async def controlled_upstream():
    app = web.Application()
    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({
            "method": "Page.frameNavigated", "sessionId": request.match_info["name"],
            "params": {"frame": {"id": "controlled-frame", "url": "https://example.invalid/"}},
        })
        async for message in ws:
            pass
        return ws
    app.router.add_route("*", "/devtools/browser/{name}", websocket)
    async with TestServer(app, host="127.0.0.1") as upstream:
        yield str(upstream.make_url("/"))


async def cancel_test_tasks(policy):
    tasks = tuple(policy._active_preflight_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_stopping_proxy_cancels_owned_preflight_and_runs_finally(monkeypatch):
    policy = preflight_policy()
    entered, finalized = asyncio.Event(), asyncio.Event()
    async def snapshot(context, classification):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            finalized.set()
    monkeypatch.setattr(policy, "_ensure_preflight_snapshot", snapshot)
    async with controlled_upstream() as upstream:
        proxy = CDPProxyServer(upstream, policy=policy)
        await proxy.start()
        try:
            async with ClientSession() as client:
                async with client.ws_connect(proxy.local_ws_url + "/devtools/browser/owned") as ws:
                    assert (await asyncio.wait_for(ws.receive_json(), 3))["method"] == "Page.frameNavigated"
                    await asyncio.wait_for(entered.wait(), 3)
                    task = policy._active_preflight_tasks["owned"]
                    assert not task.done()
                    await asyncio.wait_for(proxy.stop(), 5)
                    assert not proxy.running
                    assert finalized.is_set()
                    assert task.cancelled()
                    assert policy._active_preflight_tasks == {}
                    assert policy._active_preflight_task_connections == {}
        finally:
            await proxy.stop()
            await cancel_test_tasks(policy)


@pytest.mark.asyncio
async def test_stopping_one_proxy_preserves_another_connections_preflight(monkeypatch):
    policy = preflight_policy()
    entered = {name: asyncio.Event() for name in ("first", "second")}
    finalized = {name: asyncio.Event() for name in entered}
    async def snapshot(context, classification):
        name = context.session_id
        entered[name].set()
        try:
            await asyncio.Future()
        finally:
            finalized[name].set()
    monkeypatch.setattr(policy, "_ensure_preflight_snapshot", snapshot)
    async with controlled_upstream() as upstream:
        first, second = (CDPProxyServer(upstream, policy=policy) for _ in range(2))
        await first.start()
        await second.start()
        try:
            async with ClientSession() as client:
                async with client.ws_connect(first.local_ws_url + "/devtools/browser/first") as ws1:
                    async with client.ws_connect(second.local_ws_url + "/devtools/browser/second") as ws2:
                        await asyncio.wait_for(ws1.receive_json(), 3)
                        await asyncio.wait_for(ws2.receive_json(), 3)
                        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 3)
                        first_task = policy._active_preflight_tasks["first"]
                        second_task = policy._active_preflight_tasks["second"]
                        await asyncio.wait_for(first.stop(), 5)
                        assert first_task.cancelled() and finalized["first"].is_set()
                        assert not second_task.done() and not finalized["second"].is_set()
                        assert set(policy._active_preflight_tasks) == {"second"}
                        await asyncio.wait_for(second.stop(), 5)
                        assert second_task.cancelled() and finalized["second"].is_set()
                        assert policy._active_preflight_tasks == {}
                        assert policy._active_preflight_task_connections == {}
        finally:
            await first.stop()
            await second.stop()
            await cancel_test_tasks(policy)


@pytest.mark.asyncio
async def test_cancellation_resistant_preflight_has_bounded_shutdown(monkeypatch):
    policy = preflight_policy()
    entered, release = asyncio.Event(), asyncio.Event()
    async def snapshot(context, classification):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return None
    monkeypatch.setattr(policy, "_ensure_preflight_snapshot", snapshot)
    async def unused_probe(*args):
        return None
    context = CDPMessageContext(
        direction=BROWSER_TO_AGENT, connection_id="controlled", message_number=1,
        websocket_path="/devtools/browser/controlled", send_probe=unused_probe,
        parsed_message={"method":"Page.frameNavigated","sessionId":"controlled",
                        "params":{"frame":{"url":"https://example.invalid/"}}},
    )
    policy._note_preflight_browser_event(context)
    await asyncio.wait_for(entered.wait(), 3)
    task = policy._active_preflight_tasks["controlled"]
    try:
        with pytest.raises(RuntimeError, match="one-second shutdown bound"):
            await asyncio.wait_for(policy.on_connection_closed("controlled"), 1.5)
        assert not task.done()
    finally:
        release.set()
        await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_cleanup_hook_failure_preserves_connection_error_and_records_unknown(monkeypatch):
    original_error = RuntimeError("upstream connection failed")
    recorded, closed = [], []
    async def failed_cleanup(connection_id):
        assert connection_id == "cdp-000099"
        raise ValueError("preflight cleanup failed")
    def durable(kind, **facts):
        recorded.append((kind, facts))
    policy = SimpleNamespace(on_connection_closed=failed_cleanup, durable_event=durable)
    proxy = CDPProxyServer("http://127.0.0.1:9", policy=policy)
    proxy._connection_ids = iter((99,))
    proxy._recorded_commands[("cdp-000099", 1)] = "guarded-operation"
    class Client:
        async def prepare(self, request):
            pass
        async def close(self):
            closed.append(True)
    class Upstream:
        async def __aenter__(self):
            raise original_error
        async def __aexit__(self, *exc):
            pass
    monkeypatch.setattr(server_module.web, "WebSocketResponse", lambda **kwargs: Client())
    monkeypatch.setattr(server_module, "ws_connect", lambda *args, **kwargs: Upstream())
    monkeypatch.setattr(proxy, "_upstream_ws_url_for_request", lambda request: "ws://127.0.0.1:9/controlled")
    request = SimpleNamespace(rel_url=SimpleNamespace(path_qs="/controlled"), headers={})
    with pytest.raises(RuntimeError) as caught:
        await proxy._handle_websocket(request)
    assert caught.value is original_error
    assert closed == [True]
    assert recorded[0][0] == "cdp_outcome_unknown"
    assert recorded[0][1]["operation_id"] == "guarded-operation"
    assert proxy._recorded_commands == {}
    assert "preflight cleanup also failed: ValueError" in "\n".join(original_error.__notes__)


@pytest.mark.asyncio
async def test_cancelled_review_releases_shared_waiter_and_preserves_unrelated_review(monkeypatch):
    policy = preflight_policy()
    entered = {name: asyncio.Event() for name in ("first", "second")}
    releases = {name: asyncio.Event() for name in entered}
    snapshot = PageRiskSnapshot(
        scope_key="first", url="https://first.invalid/", title="Controlled first page",
        page_signature="first-signature", status="ok", decision="ask", reason="Controlled page review",
        skill_decision_id="controlled-page-review", facts={}, cedar_metadata={}, readiness={},
        created_at=time.monotonic(), expires_at=time.monotonic() + 60,
    )
    snapshots = {"first": snapshot, "second": replace(
        snapshot, scope_key="second", url="https://second.invalid/", page_signature="second-signature",
    )}
    async def inspect(context, classification):
        return snapshots[context.session_id]
    async def review(request):
        name = request.context.session_id
        entered[name].set()
        await releases[name].wait()
        return False
    async def unused_probe(*args):
        return None
    monkeypatch.setattr(policy, "_ensure_preflight_snapshot", inspect)
    monkeypatch.setattr(policy, "_ask_backend", review)
    contexts = {name: CDPMessageContext(
        direction=BROWSER_TO_AGENT, connection_id=name, message_number=1,
        websocket_path="/devtools/browser/" + name, send_probe=unused_probe,
        parsed_message={"method":"Page.frameNavigated", "sessionId":name,
                        "params":{"frame":{"url":"https://" + name + ".invalid/"}}},
    ) for name in entered}
    for context in contexts.values():
        policy._note_preflight_browser_event(context)
    waiter = None
    first_future = None
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 3)
        first_key = policy._preflight_review_cache_key(snapshots["first"])
        second_key = policy._preflight_review_cache_key(snapshots["second"])
        assert first_key != second_key
        first_future = policy._review_inflight[first_key]
        second_future = policy._review_inflight[second_key]
        second_task = policy._active_preflight_tasks["second"]
        waiter = asyncio.create_task(policy._review_active_preflight_page(
            contexts["first"], snapshots["first"], trigger="controlled_duplicate",
        ))
        await asyncio.sleep(0)
        assert not waiter.done()
        await policy.on_connection_closed("first")
        assert first_future.cancelled()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, 1)
        assert not second_future.done() and not second_task.done()
        assert first_key not in policy._review_inflight
        assert second_key in policy._review_inflight and policy.review_pause.active
        assert not policy._review_approved_keys
        releases["second"].set()
        await asyncio.wait_for(second_task, 1)
        assert second_future.result() is False
        assert second_key in policy._review_denied_keys
        assert not policy._review_approved_keys
        assert policy._review_inflight == {} and not policy.review_pause.active
    finally:
        if first_future is not None:
            first_future.cancel()
        for release in releases.values():
            release.set()
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await cancel_test_tasks(policy)


def controlled_review_case():
    snapshot = PageRiskSnapshot(
        scope_key="controlled", url="https://example.invalid/", title="Controlled review",
        page_signature="controlled-signature", status="ok", decision="ask", reason="Controlled review",
        skill_decision_id="controlled-review", facts={}, cedar_metadata={}, readiness={},
        created_at=time.monotonic(), expires_at=time.monotonic() + 60,
    )
    context = CDPMessageContext(
        direction=BROWSER_TO_AGENT, connection_id="controlled", message_number=1,
        websocket_path="/devtools/browser/controlled",
    )
    return context, snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [BrokenPipeError, KeyboardInterrupt, SystemExit])
async def test_review_setup_error_cancels_owned_future_and_allows_fresh_review(monkeypatch, error_type):
    policy = preflight_policy()
    context, snapshot = controlled_review_case()
    original_error = error_type("closed review terminal")
    futures = []
    def failed_terminal(*args, **kwargs):
        futures.append(policy._review_inflight[policy._preflight_review_cache_key(snapshot)])
        raise original_error
    monkeypatch.setattr(policy, "_terminal_decision", failed_terminal)
    with pytest.raises(error_type) as caught:
        await policy._review_active_preflight_page(context, snapshot, trigger="controlled_setup")
    assert caught.value is original_error
    assert futures[0].cancelled()
    assert policy._review_inflight == {} and not policy.review_pause.active
    assert not policy._review_approved_keys
    monkeypatch.setattr(policy, "_terminal_decision", lambda *args, **kwargs: None)
    async def reject(request):
        return False
    monkeypatch.setattr(policy, "_ask_backend", reject)
    assert await asyncio.wait_for(policy._review_active_preflight_page(
        context, snapshot, trigger="controlled_retry",
    ), 1) is False
    assert policy._review_inflight == {} and not policy._review_approved_keys


@pytest.mark.asyncio
async def test_cancelling_shared_waiter_keeps_owner_review_live(monkeypatch):
    policy = preflight_policy()
    context, snapshot = controlled_review_case()
    entered, release = asyncio.Event(), asyncio.Event()
    async def review(request):
        entered.set()
        await release.wait()
        return False
    monkeypatch.setattr(policy, "_ask_backend", review)
    owner = asyncio.create_task(policy._review_active_preflight_page(
        context, snapshot, trigger="controlled_owner",
    ))
    waiter = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        future = policy._review_inflight[policy._preflight_review_cache_key(snapshot)]
        waiter = asyncio.create_task(policy._review_active_preflight_page(
            context, snapshot, trigger="controlled_waiter",
        ))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not future.done() and not owner.done() and policy.review_pause.active
        release.set()
        assert await asyncio.wait_for(owner, 1) is False
        assert future.result() is False
        assert policy._review_inflight == {} and not policy._review_approved_keys
    finally:
        release.set()
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
