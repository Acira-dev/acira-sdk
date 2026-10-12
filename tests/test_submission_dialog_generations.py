"""Native modal generations close before asynchronous dismissal responses."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from acira.adapters import browser_use_dialogs as dialogs

OPENING = "Page.javascriptDialogOpening"
CLOSED = "Page.javascriptDialogClosed"


class Registry:
    def __init__(self, previous):
        self._handlers = dict(previous)
        self.registrations = []

    def register(self, name, handler):
        self.registrations.append((name, handler))
        self._handlers[name] = handler

    def unregister(self, name):
        self._handlers.pop(name, None)


async def settle(state):
    tasks = tuple(state.manager._session_operations.get(id(state.session), ()))
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
    await asyncio.sleep(0)


@pytest.fixture
def owned_dialogs(monkeypatch):
    pytest.importorskip("browser_use")
    from browser_use.browser.watchdogs.popups_watchdog import PopupsWatchdog
    assert not dialogs._MANAGERS
    async def original(*args):
        raise AssertionError("protected modal must not use the auto-confirm handler")
    monkeypatch.setattr(PopupsWatchdog, "on_TabCreatedEvent", original)
    owned = []
    def register(previous=None):
        root = SimpleNamespace(_event_registry=Registry(previous or {}))
        calls, notices = [], []
        async def enable(**kwargs):
            pass
        async def dismiss(**kwargs):
            calls.append(kwargs)
        root.send = SimpleNamespace(Page=SimpleNamespace(enable=enable, handleJavaScriptDialog=dismiss))
        records = {name: SimpleNamespace(cdp_client=root, session_id=name, target_id="shared")
                   for name in ("owner", "sibling")}
        records["unrelated"] = SimpleNamespace(cdp_client=root, session_id="unrelated", target_id="other")
        session = SimpleNamespace(_cdp_client_root=root, session_manager=SimpleNamespace(get_session=records.get))
        async def get_session(target, *, focus):
            assert focus is False
            return records["owner"]
        session.get_or_create_cdp_session = get_session
        def record(kind, **details):
            notices.append({"type": kind, **details})
        manager = SimpleNamespace(_bindings={id(session): object()}, _stopping_sessions=set(), _session_operations={},
            guard_policy=SimpleNamespace(config=SimpleNamespace(enabled=True)),
            action_gateway=SimpleNamespace(checkpoint_store=SimpleNamespace(event=record)))
        watchdog = SimpleNamespace(browser_session=session, _dialog_listeners_registered=set())
        dialogs.install(manager)
        state = SimpleNamespace(root=root, records=records, session=session, manager=manager,
                                watchdog=watchdog, calls=calls, notices=notices)
        owned.append(state)
        return state
    yield register
    for state in owned:
        assert not state.manager._session_operations, "owned modal tasks must be settled or reaped"
        dialogs.uninstall(state.manager)
    assert not dialogs._MANAGERS
    assert PopupsWatchdog.on_TabCreatedEvent is original


async def attach(state):
    await dialogs.on_TabCreatedEvent(state.watchdog, SimpleNamespace(target_id="shared"))
    return state.root._event_registry._handlers[OPENING], state.root._event_registry._handlers[CLOSED]


async def hold_dismissals(state):
    entered = [asyncio.Event() for _ in range(4)]
    release = [asyncio.Event() for _ in range(4)]
    async def dismiss(**kwargs):
        index = len(state.calls)
        state.calls.append(kwargs)
        entered[index].set()
        await release[index].wait()
    state.root.send.Page.handleJavaScriptDialog = dismiss
    return entered, release


@pytest.mark.asyncio
@pytest.mark.parametrize("close_order", [("owner", "sibling"), ("sibling", "owner")])
async def test_chained_modal_opens_before_first_dismissal_response(owned_dialogs, close_order):
    state = owned_dialogs()
    opening, closed = await attach(state)
    entered, release = await hold_dismissals(state)
    try:
        opening({"type": "confirm", "message": "first secret"}, "owner")
        opening({"type": "confirm"}, "sibling")
        await asyncio.wait_for(entered[0].wait(), 1)
        for session_id in close_order:
            closed({"result": False, "userInput": "never persisted"}, session_id)
        opening({"type": "confirm", "message": "second secret"}, "owner")
        opening({"type": "confirm"}, "sibling")
        await asyncio.wait_for(entered[1].wait(), 1)
        assert state.calls == [{"params": {"accept": False}, "session_id": "owner"}] * 2
        assert len(state.notices) == 2
        assert all(item["acceptance_dispatched"] is False for item in state.notices)
        assert "secret" not in repr(state.notices) and "persisted" not in repr(state.notices)
    finally:
        for event in release:
            event.set()
        await settle(state)


@pytest.mark.asyncio
async def test_delayed_sibling_close_and_old_task_finally_cannot_clear_new_generation(owned_dialogs):
    state = owned_dialogs()
    opening, closed = await attach(state)
    entered, release = await hold_dismissals(state)
    try:
        opening({"type": "confirm"}, "owner")
        opening({"type": "confirm"}, "sibling")
        await asyncio.wait_for(entered[0].wait(), 1)
        first = next(iter(state.manager._session_operations[id(state.session)]))
        closed({}, "owner")
        opening({"type": "prompt"}, "owner")
        opening({"type": "prompt"}, "sibling")
        await asyncio.wait_for(entered[1].wait(), 1)
        # This sibling still owes the first physical modal's close. Its FIFO
        # must release that old token, not the currently pending prompt.
        closed({"userInput": "private"}, "sibling")
        release[0].set()
        await first
        opening({"type": "prompt"}, "owner")
        opening({"type": "prompt"}, "sibling")
        await asyncio.sleep(0)
        assert len(state.calls) == 2
        assert [item["dialog_type"] for item in state.notices] == ["confirm", "prompt"]
        # The sibling's next close belongs to the prompt and permits the next
        # physical modal even though its dismissal response is still pending.
        closed({}, "sibling")
        opening({"type": "confirm"}, "sibling")
        await asyncio.wait_for(entered[2].wait(), 1)
        assert len(state.calls) == 3
        assert state.calls[-1]["session_id"] == "sibling"
    finally:
        for event in release:
            event.set()
        await settle(state)


@pytest.mark.asyncio
async def test_duplicate_opening_on_one_attachment_is_observed_once_per_generation(owned_dialogs):
    state = owned_dialogs()
    opening, closed = await attach(state)
    entered, release = await hold_dismissals(state)
    try:
        for _ in range(3):
            opening({"type": "confirm"}, "owner")
        await asyncio.wait_for(entered[0].wait(), 1)
        closed({}, "owner")
        opening({"type": "confirm"}, "owner")
        await asyncio.wait_for(entered[1].wait(), 1)
        # A duplicated Opening must not leave a second stale FIFO entry.
        closed({}, "owner")
        opening({"type": "confirm"}, "owner")
        await asyncio.wait_for(entered[2].wait(), 1)
        assert len(state.calls) == 3
    finally:
        for event in release:
            event.set()
        await settle(state)


@pytest.mark.asyncio
async def test_close_releases_only_the_verified_target(owned_dialogs):
    state = owned_dialogs()
    opening, closed = await attach(state)
    entered, release = await hold_dismissals(state)
    try:
        opening({"type": "confirm"}, "owner")
        opening({"type": "confirm"}, "unrelated")
        await asyncio.wait_for(entered[1].wait(), 1)
        closed({}, "owner")
        opening({"type": "confirm"}, "unrelated")
        await asyncio.sleep(0)
        assert len(state.calls) == 2
        opening({"type": "confirm"}, "sibling")
        await asyncio.wait_for(entered[2].wait(), 1)
        assert state.calls[-1]["session_id"] == "sibling"
    finally:
        for event in release:
            event.set()
        await settle(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "unknown", "different-root", "different-id", "missing-target", "changed-target"])
async def test_invalid_closed_routing_never_releases_a_pending_generation(owned_dialogs, case):
    state = owned_dialogs()
    opening, closed = await attach(state)
    entered, release = await hold_dismissals(state)
    try:
        opening({"type": "confirm"}, "owner")
        await asyncio.wait_for(entered[0].wait(), 1)
        record = state.records["owner"]
        original = (record.cdp_client, record.session_id, record.target_id)
        detecting = "owner"
        if case == "missing":
            detecting = None
        elif case == "unknown":
            detecting = "unknown"
        elif case == "different-root":
            record.cdp_client = object()
        elif case == "different-id":
            record.session_id = "sibling"
        elif case == "missing-target":
            record.target_id = ""
        elif case == "changed-target":
            record.target_id = "other"
        closed({"userInput": "private modal response"}, detecting)
        record.cdp_client, record.session_id, record.target_id = original
        opening({"type": "confirm"}, "owner")
        await asyncio.sleep(0)
        assert len(state.calls) == 1
        assert "private modal response" not in repr(state.notices)
    finally:
        for event in release:
            event.set()
        await settle(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [None, OPENING, CLOSED, "both"])
async def test_uninstall_restores_both_prior_handlers_independently(owned_dialogs, replacement):
    prior_opening, prior_closed = lambda *_: None, lambda *_: None
    state = owned_dialogs({OPENING: prior_opening, CLOSED: prior_closed})
    opening, closed = await attach(state)
    later_opening, later_closed = lambda *_: None, lambda *_: None
    if replacement in (OPENING, "both"):
        state.root._event_registry.register(OPENING, later_opening)
    if replacement in (CLOSED, "both"):
        state.root._event_registry.register(CLOSED, later_closed)
    dialogs.uninstall(state.manager)
    assert state.root._event_registry._handlers[OPENING] is (later_opening if replacement in (OPENING, "both") else prior_opening)
    assert state.root._event_registry._handlers[CLOSED] is (later_closed if replacement in (CLOSED, "both") else prior_closed)
    opening({"type": "confirm"}, "owner")
    closed({"userInput": "private"}, "owner")
    await settle(state)
    assert state.calls == [] and state.notices == []


@pytest.mark.asyncio
async def test_uninstall_removes_both_owned_handlers_without_predecessors(owned_dialogs):
    state = owned_dialogs()
    await attach(state)
    dialogs.uninstall(state.manager)
    assert state.root._event_registry._handlers == {}


@pytest.mark.asyncio
async def test_session_stop_reaps_chained_generations_only_for_its_owner(owned_dialogs):
    from acira.adapters.browser_use import BrowserUseCDPProxy
    one, two = owned_dialogs(), owned_dialogs()
    opening, closed = await attach(one)
    other_opening, _ = await attach(two)
    entered, release = await hold_dismissals(one)
    try:
        opening({"type": "confirm"}, "owner")
        await asyncio.wait_for(entered[0].wait(), 1)
        closed({}, "owner")
        opening({"type": "confirm"}, "owner")
        await asyncio.wait_for(entered[1].wait(), 1)
        tasks = tuple(one.manager._session_operations[id(one.session)])
        await BrowserUseCDPProxy._cancel_session_operations(one.manager, one.session)
        assert len(tasks) == 2 and all(task.done() and task.cancelled() for task in tasks)
        assert one.manager._session_operations == {}
        opening({"type": "confirm"}, "owner")
        closed({}, "owner")
        other_opening({"type": "confirm"}, "owner")
        await settle(two)
        assert len(one.calls) == 2 and len(two.calls) == 1
        assert id(two.session) not in two.manager._stopping_sessions
    finally:
        for event in release:
            event.set()
        await settle(one)


@pytest.mark.asyncio
async def test_changed_target_close_does_not_consume_the_owned_session_fifo(owned_dialogs):
    state = owned_dialogs()
    opening, closed = await attach(state)
    entered, release = await hold_dismissals(state)
    try:
        opening({"type": "confirm"}, "owner")
        await asyncio.wait_for(entered[0].wait(), 1)
        state.records["owner"].target_id = "other"
        closed({}, "owner")
        state.records["owner"].target_id = "shared"
        closed({}, "owner")
        opening({"type": "confirm"}, "owner")
        await asyncio.wait_for(entered[1].wait(), 1)
        assert len(state.calls) == 2
    finally:
        for event in release:
            event.set()
        await settle(state)


@pytest.mark.asyncio
async def test_failed_dismissal_retains_owned_handlers_and_never_calls_upstream(owned_dialogs):
    state = owned_dialogs()
    opening, closed = await attach(state)
    async def rejected(**kwargs):
        state.calls.append(kwargs)
        raise RuntimeError("secret browser response must not enter records")
    state.root.send.Page.handleJavaScriptDialog = rejected
    opening({"type": "confirm"}, "owner")
    await settle(state)
    assert state.calls == [{"params": {"accept": False}, "session_id": "owner"}]
    assert state.notices[-1] == {"type": "native_dialog_cancel_unavailable",
        "reason": "browser_rejected_dismissal", "error_type": "RuntimeError"}
    assert state.root._event_registry._handlers == {OPENING: opening, CLOSED: closed}
    assert "secret" not in repr(state.notices)
    closed({}, "owner")
    opening({"type": "prompt"}, "owner")
    await settle(state)
    assert len(state.calls) == 2 and all(call["params"] == {"accept": False} for call in state.calls)
