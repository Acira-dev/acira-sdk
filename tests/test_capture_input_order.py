"""Pure checks for the capture fixture's one acknowledged native gesture."""
import asyncio
from types import SimpleNamespace

import pytest


BOX = {"x": 30, "y": 50, "width": 80, "height": 20}
IDENTITY = {"id": "target", "href": None}


class InputFailure(RuntimeError):
    pass


class Mouse:
    def __init__(self, *, fail=None, hold=False):
        self.fail = fail
        self.steps = []
        self.acknowledged = []
        self.started = {step: asyncio.Event() for step in ("move", "down", "up")}
        self.release = {step: asyncio.Event() for step in ("move", "down", "up")}
        if not hold:
            for event in self.release.values():
                event.set()

    async def step(self, name, args=()):
        self.steps.append((name, args))
        self.started[name].set()
        if self.fail == name:
            raise InputFailure(name)
        await self.release[name].wait()
        self.acknowledged.append(name)

    async def click(self, x, y):
        # Model the documented no-delay helper's overlapping acknowledgements;
        # this test does not assert that the browser's wire events are reordered.
        await asyncio.gather(self.move(x, y), self.down(), self.up())

    async def move(self, x, y):
        await self.step("move", (x, y))

    async def down(self):
        await self.step("down")

    async def up(self):
        await self.step("up")


class Target:
    def __init__(self, *, box=BOX, identity=IDENTITY, changed=None):
        self.box = box
        self.identity = identity
        self.changed = changed
        self.box_calls = 0
        self.identity_calls = 0

    async def scroll_into_view_if_needed(self):
        pass

    async def bounding_box(self):
        self.box_calls += 1
        return dict(self.box, x=self.box['x']+1) if self.changed == "geometry" and self.box_calls > 1 else self.box

    async def evaluate(self, script):
        assert script == "el => ({id:el.id, href:el.getAttribute('href')})"
        self.identity_calls += 1
        if self.changed == "identity" and self.identity_calls > 1:
            return dict(IDENTITY, id="replacement")
        return self.identity


class Page:
    def __init__(self, mouse, *, changed=None):
        self.mouse = mouse
        self.changed = changed
        self.waits = []
        self.diagnostics = 0

    def locator(self, selector):
        assert selector == ".acira-capture-shield[data-ready='true']"
        async def wait_for(**kwargs):
            assert kwargs == {"state": "visible"}
        async def is_visible():
            return self.changed != "shield"
        return SimpleNamespace(wait_for=wait_for, is_visible=is_visible)

    async def wait_for_function(self, script, *, arg):
        assert self.mouse.acknowledged == ["move", "down", "up"]
        self.waits.append((script, arg))

    async def evaluate(self, script, box):
        self.diagnostics += 1
        return {"shield_present": True, "fingerprint_present": False}


async def await_started_or_operation_failure(mouse, step, task):
    observer = asyncio.create_task(mouse.started[step].wait())
    try:
        complete, _ = await asyncio.wait((observer, task), timeout=1, return_when=asyncio.FIRST_COMPLETED)
        if task in complete:
            await task
            pytest.fail('capture completed before the expected input acknowledgement')
        if not complete:
            pytest.fail('capture input stage did not start within the test deadline')
        await observer
    finally:
        if not observer.done():
            observer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await observer


@pytest.mark.asyncio
async def test_capture_waits_for_each_input_ack_before_next_step(select_capture_target):
    mouse = Mouse(hold=True)
    page, target = Page(mouse), Target()
    task = asyncio.create_task(select_capture_target(page, target))
    try:
        await await_started_or_operation_failure(mouse, 'move', task)
        assert mouse.steps == [('move', (70, 60))] and mouse.acknowledged == []
        assert not mouse.started['down'].is_set() and not mouse.started['up'].is_set()
        mouse.release['move'].set()
        await await_started_or_operation_failure(mouse, 'down', task)
        assert mouse.steps == [('move', (70, 60)), ('down', ())]
        assert mouse.acknowledged == ['move'] and not mouse.started['up'].is_set()
        mouse.release['down'].set()
        await await_started_or_operation_failure(mouse, 'up', task)
        assert mouse.acknowledged == ['move', 'down']
        mouse.release['up'].set()
        await task
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert mouse.steps == [('move', (70, 60)), ('down', ()), ('up', ())]
    assert mouse.acknowledged == ['move', 'down', 'up']
    assert len(page.waits) == 1 and page.waits[0][1] == IDENTITY
    assert page.diagnostics == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('box', [None, dict(BOX, width=0), dict(BOX, height=-1),
    dict(BOX, x=float('nan')), dict(BOX, y=float('inf')), dict(BOX, width=True)])
async def test_unavailable_geometry_sends_no_mouse_input(select_capture_target, box):
    mouse, target = Mouse(), Target(box=box)
    page = Page(mouse)
    with pytest.raises(AssertionError):
        await select_capture_target(page, target)
    assert mouse.steps == mouse.acknowledged == page.waits == []
    assert page.diagnostics == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('identity', [None, {}, {'id': '', 'href': None}])
async def test_unavailable_fixture_identity_sends_no_mouse_input(select_capture_target, identity):
    mouse, target = Mouse(), Target(identity=identity)
    page = Page(mouse)
    with pytest.raises(AssertionError):
        await select_capture_target(page, target)
    assert mouse.steps == mouse.acknowledged == page.waits == []


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['identity', 'geometry', 'shield'])
async def test_move_revalidation_aborts_before_button_down(select_capture_target, changed):
    mouse, target = Mouse(), Target(changed=changed)
    page = Page(mouse, changed=changed)
    with pytest.raises(AssertionError):
        await select_capture_target(page, target)
    assert mouse.steps == [('move', (70, 60))] and mouse.acknowledged == ['move']
    assert page.waits == []


@pytest.mark.asyncio
@pytest.mark.parametrize('step', ['move', 'down', 'up'])
async def test_failed_input_ack_aborts_without_later_steps(select_capture_target, step):
    mouse, target = Mouse(fail=step), Target()
    page = Page(mouse)
    with pytest.raises(InputFailure) as captured:
        await select_capture_target(page, target)
    assert str(captured.value) == step
    order = ['move', 'down', 'up']
    index = order.index(step)
    assert [name for name, _ in mouse.steps] == order[:index+1]
    assert mouse.acknowledged == order[:index] and page.waits == []
    assert page.diagnostics == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('held_step', ['move', 'down'])
async def test_cancellation_at_held_ack_sends_no_later_capture_input(select_capture_target, held_step):
    mouse = Mouse(hold=True)
    page, target = Page(mouse), Target()
    task = asyncio.create_task(select_capture_target(page, target))
    try:
        await await_started_or_operation_failure(mouse, 'move', task)
        if held_step == 'down':
            mouse.release['move'].set()
            await await_started_or_operation_failure(mouse, 'down', task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    expected = [('move', (70, 60))] + ([('down', ())] if held_step == 'down' else [])
    assert mouse.steps == expected
    assert mouse.acknowledged == (['move'] if held_step == 'down' else [])
    assert not mouse.started['up'].is_set() and page.waits == []


@pytest.mark.asyncio
async def test_test_entry_deadline_fails_without_stranding_observer():
    mouse = Mouse(hold=True)
    task = asyncio.create_task(asyncio.Event().wait())
    initial_tasks = asyncio.all_tasks()
    try:
        with pytest.raises(pytest.fail.Exception, match='input stage did not start'):
            await await_started_or_operation_failure(mouse, 'move', task)
        assert not task.done() and mouse.steps == []
        assert asyncio.all_tasks() == initial_tasks
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_original_operation_failure_wins_over_ready_stage_event():
    mouse = Mouse(hold=True)
    mouse.started['move'].set()
    operation = asyncio.get_running_loop().create_future()
    operation.set_exception(InputFailure('original operation failure'))
    initial_tasks = asyncio.all_tasks()
    with pytest.raises(InputFailure) as captured:
        await await_started_or_operation_failure(mouse, 'move', operation)
    assert str(captured.value) == 'original operation failure'
    assert asyncio.all_tasks() == initial_tasks
