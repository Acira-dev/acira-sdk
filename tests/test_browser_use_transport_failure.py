"""Exercise both CDP response orderings when a submission loses transport."""
import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import WSMsgType

from acira.adapters._cdp_channel import Channel
from acira.adapters.browser_use_operations import NetworkGate
from acira_cdp_proxy.action_gateway import AciraActionBlocked


class Socket:
    def __init__(self, *, acknowledge):
        self.acknowledge = acknowledge
        self.messages = []
        self.sent = asyncio.Event()
        self.incoming = asyncio.Queue()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return SimpleNamespace(type=WSMsgType.TEXT, json=lambda: message)

    async def send_json(self, message):
        self.messages.append(message)
        if self.acknowledge:
            await self.incoming.put({"id": message["id"], "result": {}})
        self.sent.set()

    async def close(self):
        if not self.closed:
            self.closed = True
            await self.incoming.put(None)


class Runtime:
    def __init__(self):
        self.state = "running"
        self.failure = None

    def fail_integration(self, reason):
        self.state = "failed"
        self.failure = reason


def submission(channel):
    runtime = Runtime()
    manager = SimpleNamespace(
        guard_policy=SimpleNamespace(policy_revision="revision"),
        action_gateway=SimpleNamespace(checkpoint_store=runtime),
    )
    gate = NetworkGate(manager, channel)

    async def ready():
        pass

    gate.start = ready
    contract = SimpleNamespace(
        destination="https://example.test/payment", timeout=3, finished=False,
        outcome=asyncio.get_running_loop().create_future(),
    )
    return gate, contract, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledge", [False, True], ids=["command-pending", "response-pending"])
async def test_submission_disconnect_never_retries_or_claims_success(acknowledge):
    channel = Channel()
    channel.socket = Socket(acknowledge=acknowledge)
    gate, contract, runtime = submission(channel)
    returned = asyncio.Event()
    calls = []

    async def activate():
        calls.append("activation")
        await channel.send("Runtime.callFunctionOn")
        returned.set()

    channel.reader = asyncio.create_task(channel._read())
    operation = asyncio.create_task(gate.execute(contract, activate))
    try:
        await asyncio.wait_for(channel.socket.sent.wait(), 3)
        if acknowledge:
            await asyncio.wait_for(returned.wait(), 3)
        await channel.socket.close()
        with pytest.raises(AciraActionBlocked) as failure:
            await asyncio.wait_for(operation, 3)
        await asyncio.wait_for(channel.reader, 3)
        assert "unknown" in str(failure.value) and "retry" in str(failure.value)
        if not acknowledge:
            assert failure.value.metadata == {"business_outcome": "unknown", "retry_allowed": False}
        assert runtime.state == "failed" and runtime.failure
        assert gate.manager._operation_transport_failed
        assert contract.finished and gate.active is None
        assert contract.destination in gate.locks
        assert channel.error and not channel.pending
        assert calls == ["activation"]
        assert [m["method"] for m in channel.socket.messages] == ["Runtime.callFunctionOn"]
    finally:
        await channel.socket.close()
        for task in (operation, channel.reader):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation, channel.reader, return_exceptions=True)


@pytest.mark.asyncio
async def test_submission_keeps_unrelated_runtime_errors():
    gate, contract, runtime = submission(Channel())
    error = RuntimeError("Unrelated command error")

    async def activate():
        raise error

    with pytest.raises(RuntimeError) as failure:
        await gate.execute(contract, activate)
    assert failure.value is error
    assert runtime.state == "running" and not runtime.failure
    assert contract.finished and gate.active is None
    assert contract.destination in gate.locks
