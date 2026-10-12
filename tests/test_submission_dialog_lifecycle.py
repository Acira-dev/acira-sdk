"""Shared Dialog wrappers follow page ownership through run lifecycle."""
from types import SimpleNamespace
import asyncio

from aiohttp import web
from aiohttp.test_utils import TestServer

import pytest

pytest.importorskip("playwright.async_api")
from playwright.async_api import Dialog

from acira.adapters import playwright_boundary as boundary
from acira_cdp_proxy.action_gateway import ActionGateway, AciraActionBlocked


class Surface:
    pass


class SyntheticDialog:
    def __init__(self, page, kind="confirm", *, dismiss_error=None):
        self.page, self.type = page, kind
        self.accepted, self.dismissed = [], 0
        self.dismiss_error = dismiss_error

    async def dismiss(self):
        self.dismissed += 1
        if self.dismiss_error is not None:
            raise self.dismiss_error


@pytest.fixture
def dialog_wrapper(monkeypatch):
    # Every test owns its registrations and restores the shared methods once
    # its final run ends. Never remove a registration belonging to another run.
    assert not boundary.OWNERS and not boundary.ORIGINALS
    async def original_accept(dialog, prompt_text=None):
        dialog.accepted.append(prompt_text)
    monkeypatch.setattr(Dialog, "accept", original_accept)
    boundary.install()
    managers = []
    def register(*, enabled=True):
        gateway = ActionGateway(config={"enabled": enabled, "terminal_log": False})
        manager = SimpleNamespace(action_gateway=gateway, guard_policy=gateway.policy)
        page = Surface()
        page.mouse, page.keyboard, page.touchscreen = Surface(), Surface(), Surface()
        boundary.attach(manager, page)
        managers.append(manager)
        return manager, page
    try:
        yield Dialog.accept, original_accept, register
    finally:
        for manager in managers:
            boundary.detach(manager)
        # install() may have been used solely for an unregistered page.
        if not managers:
            boundary.detach(None)
        assert not boundary.OWNERS and not boundary.ORIGINALS


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["confirm", "prompt", "beforeunload", "unknown"])
async def test_owned_native_acceptance_blocks_and_dismisses_without_confirmation(dialog_wrapper, kind):
    accept, _, register = dialog_wrapper
    _, page = register()
    dialog = SyntheticDialog(page, kind)
    with pytest.raises(AciraActionBlocked, match="verified operation contract"):
        await accept(dialog, "private prompt input")
    assert dialog.accepted == []
    assert dialog.dismissed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unrelated", "disabled", "alert", "internal"])
async def test_native_wrapper_preserves_explicit_unprotected_and_alert_paths(dialog_wrapper, mode):
    accept, _, register = dialog_wrapper
    if mode == "unrelated":
        _, protected_page = register()
        page = Surface()
        assert page is not protected_page
    else:
        _, page = register(enabled=mode != "disabled")
    dialog = SyntheticDialog(page, "alert" if mode == "alert" else "confirm")
    token = boundary.INTERNAL.set(True) if mode == "internal" else None
    try:
        await accept(dialog, "test input")
    finally:
        if token is not None:
            boundary.INTERNAL.reset(token)
    assert dialog.accepted == ["test input"]
    assert dialog.dismissed == 0


@pytest.mark.asyncio
async def test_nested_runs_and_saved_dialog_alias_follow_current_ownership(dialog_wrapper):
    alias, original_accept, register = dialog_wrapper
    first, one = register()
    second, two = register()
    boundary.detach(first)
    assert Dialog.accept is alias
    inactive = SyntheticDialog(one)
    await alias(inactive)
    assert inactive.accepted == [None]
    active = SyntheticDialog(two)
    with pytest.raises(AciraActionBlocked):
        await alias(active)
    assert active.accepted == [] and active.dismissed == 1
    boundary.detach(second)
    assert Dialog.accept is original_accept
    assert not boundary.ORIGINALS
    after_shutdown = SyntheticDialog(two)
    # A previously stored alias cannot carry stale authorization or stale
    # ownership across guard shutdown; it falls back to its saved original.
    await alias(after_shutdown)
    assert after_shutdown.accepted == [None]


@pytest.mark.asyncio
async def test_failed_owned_modal_dismissal_preserves_primary_block(dialog_wrapper):
    accept, _, register = dialog_wrapper
    _, page = register()
    dialog = SyntheticDialog(page, dismiss_error=RuntimeError("owned dismissal failed"))
    with pytest.raises(AciraActionBlocked, match="verified operation contract") as failure:
        await accept(dialog)
    assert dialog.accepted == [] and dialog.dismissed == 1
    assert any("could not be dismissed" in note for note in failure.value.__notes__)


class Registry:
    def __init__(self, previous=None):
        self._handlers = {} if previous is None else {"Page.javascriptDialogOpening": previous}
        self.registrations = []

    def register(self, name, handler):
        self.registrations.append((name, handler))
        self._handlers[name] = handler

    def unregister(self, name):
        self._handlers.pop(name, None)


async def settled(state):
    tasks = tuple(state.manager._session_operations.get(id(state.session), ()))
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
    await asyncio.sleep(0)  # task-completion ownership bookkeeping


@pytest.fixture
def browser_use_dialogs(monkeypatch):
    pytest.importorskip("browser_use")
    from browser_use.browser.watchdogs.popups_watchdog import PopupsWatchdog
    from acira.adapters import browser_use_dialogs as dialogs
    assert not dialogs._MANAGERS
    fallback = []
    async def original_tab_created(watchdog, event):
        fallback.append((watchdog, event.target_id))
    monkeypatch.setattr(PopupsWatchdog, "on_TabCreatedEvent", original_tab_created)
    owned = []
    def register(*, previous=None, enabled=True):
        enables, dismissals, events, focus_requests = [], [], [], []
        root = SimpleNamespace(_event_registry=Registry(previous))
        async def enable(**kwargs):
            enables.append(kwargs)
        async def dismiss(**kwargs):
            dismissals.append(kwargs)
        root.send = SimpleNamespace(Page=SimpleNamespace(enable=enable, handleJavaScriptDialog=dismiss))
        records = {name: SimpleNamespace(cdp_client=root, session_id=name, target_id="one" if name == "detecting" else "two") for name in ("detecting", "focused")}
        session = SimpleNamespace(_cdp_client_root=root,
            session_manager=SimpleNamespace(get_session=records.get))
        async def get_session(target_id, *, focus):
            focus_requests.append((target_id, focus))
            return records["detecting"]
        session.get_or_create_cdp_session = get_session
        def record(kind, **details):
            events.append({"type": kind, **details})
        manager = SimpleNamespace(_bindings={id(session): object()}, _stopping_sessions=set(), _session_operations={},
            guard_policy=SimpleNamespace(config=SimpleNamespace(enabled=enabled)),
            action_gateway=SimpleNamespace(checkpoint_store=SimpleNamespace(event=record)))
        watchdog = SimpleNamespace(browser_session=session, _dialog_listeners_registered=set())
        dialogs.install(manager)
        owned.append(manager)
        return SimpleNamespace(manager=manager, root=root, session=session, watchdog=watchdog,
            records=records, enables=enables, dismissals=dismissals, events=events, focus_requests=focus_requests)
    try:
        yield dialogs, register, fallback, original_tab_created
    finally:
        for manager in owned:
            dialogs.uninstall(manager)
        assert not dialogs._MANAGERS
        assert PopupsWatchdog.on_TabCreatedEvent is original_tab_created


@pytest.mark.asyncio
async def test_browser_use_registers_once_and_dismisses_exact_detecting_session(browser_use_dialogs):
    dialogs, register, fallback, _ = browser_use_dialogs
    state = register()
    dialogs.install(state.manager)
    for target in ("one", "two", "one"):
        await dialogs.on_TabCreatedEvent(state.watchdog, SimpleNamespace(target_id=target))
    assert [name for name, _ in state.root._event_registry.registrations] == [
        "Page.javascriptDialogOpening", "Page.javascriptDialogClosed"]
    assert state.focus_requests == [("one", False), ("two", False), ("one", False)]
    handler = state.root._event_registry._handlers["Page.javascriptDialogOpening"]
    handler({"type": "confirm", "message": "secret dialog content must not be logged"}, "detecting")
    await settled(state)
    assert state.dismissals == [{"params": {"accept": False}, "session_id": "detecting"}]
    assert fallback == []
    assert state.events == [{"type": "native_dialog_confirmation_blocked", "framework": "browser_use",
        "dialog_type": "confirm", "acceptance_dispatched": False}]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "unknown", "different-root", "different-id"])
async def test_browser_use_unknown_detecting_session_never_falls_back_to_focus(browser_use_dialogs, case):
    dialogs, register, _, _ = browser_use_dialogs
    state = register()
    await dialogs.on_TabCreatedEvent(state.watchdog, SimpleNamespace(target_id="one"))
    handler = state.root._event_registry._handlers["Page.javascriptDialogOpening"]
    session_id = None if case == "missing" else "unknown" if case == "unknown" else "detecting"
    if case == "different-root":
        state.records["detecting"].cdp_client = object()
    if case == "different-id":
        state.records["detecting"].session_id = "focused"
    handler({"type": "confirm", "message": "not recorded"}, session_id)
    await settled(state)
    assert state.dismissals == []
    assert state.events == [{"type": "native_dialog_cancel_unavailable", "reason": "missing_detecting_session"}]


@pytest.mark.asyncio
async def test_browser_use_duplicate_event_does_not_send_two_dismissals(browser_use_dialogs):
    dialogs, register, _, _ = browser_use_dialogs
    state = register()
    await dialogs.on_TabCreatedEvent(state.watchdog, SimpleNamespace(target_id="one"))
    entered, finish = asyncio.Event(), asyncio.Event()
    async def slow_dismiss(**kwargs):
        state.dismissals.append(kwargs)
        entered.set()
        await finish.wait()
    state.root.send.Page.handleJavaScriptDialog = slow_dismiss
    handler = state.root._event_registry._handlers["Page.javascriptDialogOpening"]
    handler({"type": "confirm"}, "detecting")
    first = next(iter(state.manager._session_operations[id(state.session)]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        handler({"type": "confirm"}, "detecting")
        assert len(state.dismissals) == 1
    finally:
        finish.set()
        await first


@pytest.mark.asyncio
@pytest.mark.parametrize("same_target", [True, False], ids=["same-physical-modal", "separate-physical-modals"])
async def test_browser_use_multiple_attachments_deduplicate_only_the_same_pending_target(browser_use_dialogs, same_target):
    dialogs, register, _, _ = browser_use_dialogs
    state = register()
    if same_target:
        state.records["focused"].target_id = state.records["detecting"].target_id
    await dialogs.on_TabCreatedEvent(state.watchdog, SimpleNamespace(target_id="one"))
    finish = asyncio.Event()
    async def unfinished(**kwargs):
        state.dismissals.append(kwargs)
        await finish.wait()
    state.root.send.Page.handleJavaScriptDialog = unfinished
    handler = state.root._event_registry._handlers["Page.javascriptDialogOpening"]
    handler({"type": "confirm"}, "detecting")
    handler({"type": "confirm"}, "focused")
    expected = 1 if same_target else 2
    try:
        await asyncio.sleep(0)
        assert len(state.dismissals) == expected
        assert len(state.events) == expected
        assert state.dismissals[0] == {"params": {"accept": False}, "session_id": "detecting"}
        if not same_target:
            assert state.dismissals[1] == {"params": {"accept": False}, "session_id": "focused"}
    finally:
        finish.set()
        await settled(state)
    # A subsequent real modal on this page is independently dismissed; pending
    # target deduplication must never become a permanent page-level grant.
    handler({"type": "prompt"}, "detecting")
    await settled(state)
    assert len(state.dismissals) == expected + 1
    assert state.events[-1]["dialog_type"] == "prompt"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [None, ""])
async def test_browser_use_missing_physical_target_does_not_guess_or_dispatch(browser_use_dialogs, target):
    dialogs, register, _, _ = browser_use_dialogs
    state = register()
    await dialogs.on_TabCreatedEvent(state.watchdog, SimpleNamespace(target_id="one"))
    state.records["detecting"].target_id = target
    state.root._event_registry._handlers["Page.javascriptDialogOpening"]({"type": "confirm"}, "detecting")
    await settled(state)
    assert state.dismissals == []
    assert state.events == [{"type": "native_dialog_cancel_unavailable", "reason": "missing_detecting_target"}]


@pytest.mark.asyncio
async def test_browser_use_nested_managers_restore_only_owned_registration(browser_use_dialogs):
    dialogs, register, fallback, original = browser_use_dialogs
    previous = lambda *_: None
    one, two = register(previous=previous), register()
    for state in (one, two):
        await dialogs.on_TabCreatedEvent(state.watchdog, SimpleNamespace(target_id="one"))
    alias = one.root._event_registry._handlers["Page.javascriptDialogOpening"]
    dialogs.uninstall(one.manager)
    assert one.root._event_registry._handlers["Page.javascriptDialogOpening"] is previous
    assert two.root._event_registry._handlers["Page.javascriptDialogOpening"] is not previous
    alias({"type": "confirm"}, "detecting")
    await settled(one)
    assert one.dismissals == []
    handler = two.root._event_registry._handlers["Page.javascriptDialogOpening"]
    handler({"type": "prompt"}, "detecting")
    await settled(two)
    assert two.dismissals == [{"params": {"accept": False}, "session_id": "detecting"}]
    await dialogs.on_TabCreatedEvent(one.watchdog, SimpleNamespace(target_id="unprotected"))
    assert fallback == [(one.watchdog, "unprotected")]
    dialogs.uninstall(two.manager)
    from browser_use.browser.watchdogs.popups_watchdog import PopupsWatchdog
    assert PopupsWatchdog.on_TabCreatedEvent is original


@pytest.mark.asyncio
async def test_browser_use_does_not_overwrite_a_later_external_handler(browser_use_dialogs):
    dialogs, register, _, _ = browser_use_dialogs
    state = register()
    await dialogs.on_TabCreatedEvent(state.watchdog, SimpleNamespace(target_id="one"))
    later = lambda *_: None
    state.root._event_registry.register("Page.javascriptDialogOpening", later)
    dialogs.uninstall(state.manager)
    assert state.root._event_registry._handlers["Page.javascriptDialogOpening"] is later


@pytest.mark.asyncio
async def test_browser_use_event_receive_loop_can_read_its_dismissal_response(browser_use_dialogs):
    """Exercise cdp-use's actual serialized receive loop over local WebSocket."""
    pytest.importorskip("cdp_use")
    from cdp_use import CDPClient
    dialogs, register, _, _ = browser_use_dialogs
    state = register()
    websocket_ready, dismissal_response = asyncio.Event(), asyncio.Event()
    messages = []
    channel = None
    async def websocket(request):
        nonlocal channel
        channel = web.WebSocketResponse()
        await channel.prepare(request)
        websocket_ready.set()
        async for raw in channel:
            if raw.type == web.WSMsgType.TEXT:
                message = json.loads(raw.data)
                messages.append(message)
                await channel.send_json({"id": message["id"], "sessionId": message.get("sessionId"), "result": {}})
                if message["method"] == "Page.handleJavaScriptDialog":
                    dismissal_response.set()
        return channel
    import json
    app = web.Application()
    app.router.add_get("/cdp", websocket)
    async with TestServer(app, host="127.0.0.1") as server:
        async with CDPClient(str(server.make_url("/cdp")).replace("http://", "ws://", 1)) as client:
            state.root = client
            state.session._cdp_client_root = client
            for record in state.records.values():
                record.cdp_client = client
            await asyncio.wait_for(websocket_ready.wait(), 1)
            await dialogs.on_TabCreatedEvent(state.watchdog, SimpleNamespace(target_id="one"))
            await channel.send_json({"method": "Page.javascriptDialogOpening", "sessionId": "detecting",
                "params": {"type": "confirm", "message": "not persisted"}})
            await asyncio.wait_for(dismissal_response.wait(), 1)
            await settled(state)
            assert [item for item in messages if item["method"] == "Page.handleJavaScriptDialog"] == [
                {"id": 2, "method": "Page.handleJavaScriptDialog", "params": {"accept": False}, "sessionId": "detecting"}]
            assert client.pending_requests == {}
            assert not any(item["type"] == "native_dialog_cancel_unavailable" for item in state.events)


@pytest.mark.asyncio
async def test_browser_use_session_stop_cancels_owned_dismissal_only(browser_use_dialogs):
    from acira.adapters.browser_use import BrowserUseCDPProxy
    dialogs, register, _, _ = browser_use_dialogs
    one, two = register(), register()
    for state in (one, two):
        await dialogs.on_TabCreatedEvent(state.watchdog, SimpleNamespace(target_id="one"))
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def unfinished(**kwargs):
        one.dismissals.append(kwargs)
        entered.set()
        try:
            await release.wait()
        finally:
            cancelled.set()
    one.root.send.Page.handleJavaScriptDialog = unfinished
    owner_handler = one.root._event_registry._handlers["Page.javascriptDialogOpening"]
    owner_handler({"type": "confirm"}, "detecting")
    await asyncio.wait_for(entered.wait(), 1)
    task = next(iter(one.manager._session_operations[id(one.session)]))
    await BrowserUseCDPProxy._cancel_session_operations(one.manager, one.session)
    await asyncio.sleep(0)
    assert task.done() and task.cancelled() and cancelled.is_set()
    assert one.manager._session_operations == {}
    owner_handler({"type": "confirm"}, "detecting")
    assert len(one.dismissals) == 1
    unrelated_handler = two.root._event_registry._handlers["Page.javascriptDialogOpening"]
    unrelated_handler({"type": "confirm"}, "detecting")
    await settled(two)
    assert two.dismissals == [{"params": {"accept": False}, "session_id": "detecting"}]
    assert id(two.session) not in two.manager._stopping_sessions
