"""A public-site sweep observes one original capture attempt, without browser retries."""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
pytest.importorskip("playwright.async_api")

_tools = Path(__file__).resolve().parents[1] / "tools"
_spec = importlib.util.spec_from_file_location("acira_sweep_selection_tests", _tools / "specific_policy_sweep.py")
sweep = importlib.util.module_from_spec(_spec)
sys.path.insert(0, str(_tools))
try:
    _spec.loader.exec_module(sweep)
finally:
    sys.path.remove(str(_tools))


class FakeLocator:
    async def wait_for(self, **kwargs):
        pass

    async def is_visible(self):
        return True

    def locator(self, selector):
        return self

    async def inner_text(self):
        return "Target selected"


class FakeTarget:
    def __init__(self, box=None, *, unavailable=False, changed=False):
        self.box = box if box is not None else {"x": 30, "y": 50, "width": 80, "height": 20}
        self.unavailable, self.changed, self.box_calls = unavailable, changed, 0

    async def wait_for_element_state(self, state, *, timeout):
        assert (state, timeout) == ("stable", 2000)

    async def bounding_box(self):
        self.box_calls += 1
        if self.unavailable:
            return None
        return dict(self.box, x=self.box['x']+1) if self.changed and self.box_calls > 1 else self.box


class FakePage:
    def __init__(self, root, *, match=False, diagnostic="observed", navigates=False, input_failure=None, held_step=None):
        self.root = root
        self.url = "https://fixture.invalid/"
        self.match = match
        self.diagnostic = diagnostic
        self.navigates = navigates
        self.clicks = []
        self.mouse_steps = []
        self.input_failure = input_failure
        self.held_step = held_step
        self.input_started = {step: asyncio.Event() for step in ('move', 'down', 'up')}
        self.input_release = {step: asyncio.Event() for step in ('move', 'down', 'up')}
        for step, event in self.input_release.items():
            if step != held_step:
                event.set()
        self.point = None
        self.waits = []
        self.diagnostic_calls = []
        self.button_actions = []
        self.mouse = SimpleNamespace(click=self.click, move=self.move, down=self.down, up=self.up)

    def locator(self, selector):
        return SimpleNamespace(wait_for=FakeLocator().wait_for, is_visible=FakeLocator().is_visible,
            locator=FakeLocator().locator, get_by_role=self.get_by_role)

    async def click(self, x, y):
        await asyncio.gather(self.move(x, y), self.down(), self.up())

    async def move(self, x, y):
        self.mouse_steps.append(('move', (x, y)))
        self.input_started['move'].set()
        self.point = (x, y)
        if self.input_failure == 'move':
            raise RuntimeError('input move failed')
        await self.input_release['move'].wait()

    async def down(self):
        self.mouse_steps.append(('down', ()))
        self.input_started['down'].set()
        if self.input_failure == 'down':
            raise RuntimeError('input down failed')
        await self.input_release['down'].wait()

    async def up(self):
        self.mouse_steps.append(('up', ()))
        self.input_started['up'].set()
        if self.input_failure == 'up':
            raise RuntimeError('input up failed')
        await self.input_release['up'].wait()
        self.clicks.append(self.point)
        if self.navigates:
            self.url = "https://fixture.invalid/activated"

    async def wait_for_function(self, script, *, arg, timeout):
        self.waits.append({"arg": arg, "timeout": timeout})
        if not self.match:
            raise sweep.PlaywrightTimeoutError("Private fixture payload must not enter diagnostics")

    async def evaluate(self, script, arg):
        self.diagnostic_calls.append(arg)
        if self.diagnostic == "error":
            raise RuntimeError("Private fixture payload must not enter diagnostics")
        if self.diagnostic == "timeout":
            await asyncio.Future()
        state = {"shield_present": True, "shield_ready": True, "shield_hidden": False,
            "shield_bounds": {"x": 0, "y": 0, "width": 1280, "height": 900},
            "viewport": {"width": 1280, "height": 900},
            "point_tags": ["IFRAME", "A", "DIV", "BODY"],
            "fingerprint_present": True, "fingerprint_matches": False}
        if self.diagnostic == "late_match":
            state["fingerprint_matches"] = True
        if self.diagnostic == "untrusted":
            state.update({"raw_page": "Private fixture payload must not enter diagnostics",
                "shield_ready": "Private fixture payload must not enter diagnostics",
                "point_tags": ["CUSTOM-PRIVATE-PAYLOAD", "A", {}, "BODY", "INPUT"],
                "shield_bounds": {"x": float("inf"), "y": -1_000_001,
                    "width": "Private fixture payload must not enter diagnostics", "height": True},
                "viewport": {"width": 1_000_001, "height": 900}})
        return state

    def get_by_role(self, role, *, name, exact):
        assert role == "button" and exact
        async def click():
            self.button_actions.append(name)
        return SimpleNamespace(click=click)

    def expect_download(self, *, timeout):
        assert timeout == 10000
        page = self
        policy = {"schema": "acira.specific_skill.v2", "id": "capture-fixture",
            "scope": {"url_patterns": ["https://fixture.invalid/"]},
            "targets": [{"id": "guide", "decision": "block", "fingerprint": {"href": "/guide"}}]}
        class Download:
            async def save_as(self, destination):
                Path(destination).write_text(json.dumps(policy))
            async def path(self):
                destination = page.root / "repeat.json"
                destination.write_text(json.dumps(policy))
                return str(destination)
        class Event:
            async def __aenter__(self):
                future = asyncio.get_running_loop().create_future()
                future.set_result(Download())
                return SimpleNamespace(value=future)
            async def __aexit__(self, *args):
                return False
        return Event()


async def fake_capture(tmp_path, monkeypatch, **page_options):
    changed_identity = page_options.pop("changed_identity", False)
    changed_identity_after_move = page_options.pop("changed_identity_after_move", False)
    missing_identity = page_options.pop("missing_identity", False)
    box = page_options.pop("box", None)
    unavailable_geometry = page_options.pop("unavailable_geometry", False)
    changed_geometry = page_options.pop("changed_geometry", False)
    observed_pages = page_options.pop("observed_pages", None)
    page_created = page_options.pop("page_created", None)
    page = FakePage(tmp_path, **page_options)
    if observed_pages is not None:
        observed_pages.append(page)
    if page_created is not None:
        page_created.set()
    target = FakeTarget(box, unavailable=unavailable_geometry, changed=changed_geometry)
    routes = []
    context = SimpleNamespace(service_workers=[], closed=False)
    page.test_context = context
    async def evaluate_worker(*args):
        pass
    context.service_workers = [SimpleNamespace(url="chrome-extension://fixture/background.js", evaluate=evaluate_worker)]
    async def cookies():
        return []
    async def route(pattern, callback):
        routes.append(pattern)
    async def route_web_socket(pattern, callback):
        assert pattern == "**/*"
    async def new_page():
        return page
    context.cookies, context.route = cookies, route
    context.route_web_socket, context.new_page = route_web_socket, new_page
    async def launch(*args, **kwargs):
        return context
    async def loaded(*args):
        return {"status": "loaded"}
    async def ready(*args, **kwargs):
        return (target, "https://fixture.invalid/guide", "Guide"), {}
    identities = 0
    async def identity(*args):
        nonlocal identities
        identities += 1
        if missing_identity:
            return {}
        changed = (changed_identity and identities > 1) or (changed_identity_after_move and identities > 2)
        return {"sha256": "changed" if changed else "fixed", "capture_path": "a:nth-of-type(1)"}
    async def close(*args):
        context.closed = True
    def install(destination):
        destination.mkdir()
        (destination / "manifest.json").write_text('{}')
        return destination
    monkeypatch.setattr(sweep, "install_extension", install)
    monkeypatch.setattr(sweep.safe, "load_page", loaded)
    monkeypatch.setattr(sweep.safe, "ready_link", ready)
    monkeypatch.setattr(sweep.safe, "link_control_identity", identity)
    monkeypatch.setattr(sweep.safe, "close_context", close)
    progress = {}
    page.test_progress = progress
    result, policy = await sweep.capture(SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch)),
        {"url": "https://fixture.invalid/"}, "https://fixture.invalid/guide", tmp_path,
        timeout=2000, robots=None, pacer=None, headed=False, progress=progress)
    assert context.closed and progress["cleanup_status"] == "complete"
    assert routes == ["**/*", "chrome-extension://fixture/selection.html", "chrome-extension://fixture/selection.js"]
    return page, result, policy, progress


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic", ["observed", "error", "timeout", "untrusted"])
async def test_fingerprint_mismatch_never_retries_input_or_marks_a_policy(tmp_path, monkeypatch, diagnostic):
    page, result, policy, progress = await fake_capture(tmp_path, monkeypatch, diagnostic=diagnostic)
    assert page.clicks == [(70, 60)] and len(page.waits) == 1
    assert page.mouse_steps == [('move', (70, 60)), ('down', ()), ('up', ())]
    assert page.waits[0]["timeout"] == 2000
    assert policy is None and page.button_actions == []
    assert result["status"] == "capture_fingerprint_match_timeout"
    assert result["selection_attempts"] == progress["selection_attempts"] == 1
    assert result["fingerprint_match_within_timeout"] is False
    assert [entry["stage"] for entry in progress["stages"]].count("select_control") == 1
    assert progress["last_operation_stage"] == "select_control"
    assert "Private fixture payload" not in json.dumps(result)
    state = result["selection_diagnostics"]
    if diagnostic in ("error", "timeout"):
        assert state == {"diagnostic_error": "RuntimeError" if diagnostic == "error" else "TimeoutError"}
    elif diagnostic == "untrusted":
        assert state["shield_ready"] is None
        assert state["point_tags"] == ["OTHER", "A", "OTHER", "BODY"]
        assert state["shield_bounds"] == dict.fromkeys(("x", "y", "width", "height"))
        assert state["viewport"] == {"width": None, "height": 900}
        assert "raw_page" not in state
    else:
        assert state["fingerprint_present"] is True and state["fingerprint_matches"] is False
        assert state["point_tags"] == ["IFRAME", "A", "DIV", "BODY"]


@pytest.mark.asyncio
async def test_success_records_original_single_selection_and_preserves_exports(tmp_path, monkeypatch):
    page, result, policy, progress = await fake_capture(tmp_path, monkeypatch, match=True)
    assert result["status"] == "verified" and result["fingerprint_match_within_timeout"] is True
    assert result["selection_attempts"] == progress["selection_attempts"] == 1
    assert page.clicks == [(70, 60)] and len(page.waits) == 1
    assert page.mouse_steps == [('move', (70, 60)), ('down', ()), ('up', ())]
    assert page.diagnostic_calls == []
    assert page.button_actions == ["Block target", "Download", "Download"]
    assert policy["targets"][0]["decision"] == "block"
    assert progress["last_operation_stage"] == "exports_verified"


@pytest.mark.asyncio
async def test_selection_navigation_stays_failed_without_another_click(tmp_path, monkeypatch):
    page, result, policy, progress = await fake_capture(tmp_path, monkeypatch, navigates=True)
    assert result == {"status": "capture_selection_activated_target", "selection_attempts": 1}
    assert page.clicks == [(70, 60)] and policy is None
    assert page.button_actions == page.diagnostic_calls == []
    assert progress["selection_attempts"] == 1


@pytest.mark.asyncio
async def test_changed_control_identity_stops_before_original_selection(tmp_path, monkeypatch):
    page, result, policy, progress = await fake_capture(tmp_path, monkeypatch, changed_identity=True)
    assert result == {"status": "target_identity_changed_before_capture"} and policy is None
    assert page.clicks == page.waits == page.diagnostic_calls == page.button_actions == []
    assert progress["selection_attempts"] == 0


@pytest.mark.asyncio
async def test_late_diagnostic_match_does_not_qualify_timed_out_original_selection(tmp_path, monkeypatch):
    page, result, policy, progress = await fake_capture(tmp_path, monkeypatch, diagnostic="late_match")
    assert result["status"] == "capture_fingerprint_match_timeout"
    assert result["fingerprint_match_within_timeout"] is False
    assert result["selection_diagnostics"]["fingerprint_present"] is True
    assert result["selection_diagnostics"]["fingerprint_matches"] is True
    assert result["selection_attempts"] == progress["selection_attempts"] == 1
    assert page.clicks == [(70, 60)] and len(page.waits) == 1
    assert page.mouse_steps == [('move', (70, 60)), ('down', ()), ('up', ())]
    assert page.waits[0]["timeout"] == 2000
    assert policy is None and page.button_actions == []
    assert progress["cleanup_status"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize('step', ['move', 'down', 'up'])
async def test_incomplete_native_gesture_aborts_and_never_exports(tmp_path, monkeypatch, step):
    pages = []
    with pytest.raises(RuntimeError, match='^input '+step+' failed$'):
        await fake_capture(tmp_path, monkeypatch, input_failure=step, observed_pages=pages)
    page = pages[0]
    order = ['move', 'down', 'up']
    assert [name for name, _ in page.mouse_steps] == order[:order.index(step)+1]
    assert page.clicks == page.waits == page.button_actions == []
    assert page.test_context.closed
    assert not list(tmp_path.rglob('capture.json')) and not list(tmp_path.rglob('repeat.json'))


@pytest.mark.asyncio
@pytest.mark.parametrize('box', [None, {'x':30,'y':50,'width':0,'height':20},
    {'x':float('nan'),'y':50,'width':80,'height':20},
    {'x':30,'y':float('inf'),'width':80,'height':20},
    {'x':30,'y':50,'width':True,'height':20}])
async def test_unavailable_geometry_never_sends_input_or_exports(tmp_path, monkeypatch, box):
    page, result, policy, progress = await fake_capture(tmp_path, monkeypatch, box=box, unavailable_geometry=box is None)
    assert result == {'status':'target_unavailable'} and policy is None
    assert page.mouse_steps == page.clicks == page.waits == page.button_actions == []
    assert progress['selection_attempts'] == 0


@pytest.mark.asyncio
async def test_missing_control_identity_never_sends_input_or_exports(tmp_path, monkeypatch):
    page, result, policy, progress = await fake_capture(tmp_path, monkeypatch, missing_identity=True)
    assert result == {'status':'target_identity_unavailable'} and policy is None
    assert page.mouse_steps == page.clicks == page.waits == page.button_actions == []
    assert progress['selection_attempts'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['identity', 'geometry'])
async def test_move_revalidation_never_presses_a_replaced_target(tmp_path, monkeypatch, change):
    page, result, policy, progress = await fake_capture(tmp_path, monkeypatch,
        changed_identity_after_move=change=='identity', changed_geometry=change=='geometry')
    assert result['status'] == 'target_'+change+'_changed_before_capture' and policy is None
    assert page.mouse_steps == [('move', (70, 60))]
    assert page.clicks == page.waits == page.button_actions == []
    assert progress['selection_attempts'] == 1


async def await_sweep_stage_or_failure(stage_event, task):
    observer = asyncio.create_task(stage_event.wait())
    try:
        complete, _ = await asyncio.wait((observer, task), timeout=1, return_when=asyncio.FIRST_COMPLETED)
        if task in complete:
            await task
            pytest.fail('capture completed before the held input stage')
        if not complete:
            pytest.fail('capture input stage did not start within the test deadline')
        await observer
    finally:
        if not observer.done():
            observer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await observer


@pytest.mark.asyncio
@pytest.mark.parametrize('held_step', ['move', 'down'])
async def test_cancelled_gesture_closes_context_without_later_input_or_export(tmp_path, monkeypatch, held_step):
    pages, created = [], asyncio.Event()
    task = asyncio.create_task(fake_capture(tmp_path, monkeypatch, held_step=held_step,
        observed_pages=pages, page_created=created))
    try:
        await await_sweep_stage_or_failure(created, task)
        page = pages[0]
        await await_sweep_stage_or_failure(page.input_started[held_step], task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    expected = [('move', (70, 60))] + ([('down', ())] if held_step == 'down' else [])
    assert page.mouse_steps == expected and not page.input_started['up'].is_set()
    assert page.clicks == page.waits == page.button_actions == []
    assert page.test_context.closed and page.test_progress['cleanup_status'] == 'complete'
    assert not list(tmp_path.rglob('capture.json')) and not list(tmp_path.rglob('repeat.json'))
