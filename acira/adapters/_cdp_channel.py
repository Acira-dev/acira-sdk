"""Private, bounded CDP channel for adapter-owned operation contracts.

Never expose this channel as a public agent API. It executes only fixed adapter
probes/actions after the common guard has authorized their exact target and facts.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, WSMsgType


class Channel:
    def __init__(self):
        self.client = None
        self.socket = None
        self.reader = None
        self.pending = {}
        self.tasks = set()
        self.handlers = {}
        self.global_handlers = {}
        self.scoped_handlers = {}
        self.target_id = None
        self.browser_url = None
        self.sequence = 0
        self.session_id = None
        self.error = None
        self.closing = False
        self.paused_requests = set()
        self.on_disconnect = None

    @classmethod
    async def connect(cls, url: str, target_id: str):
        self = cls()
        self.browser_url = url
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname:
            raise ValueError("Invalid browser CDP endpoint")
        self.client = ClientSession(timeout=ClientTimeout(total=5), trust_env=False)
        try:
            if parsed.scheme in {"http", "https"}:
                async with self.client.get(url.rstrip("/") + "/json/version", allow_redirects=False) as response:
                    response.raise_for_status()
                    if response.content_length and response.content_length > 65536:
                        raise ValueError("Unbounded CDP discovery response")
                    import json
                    data = await response.content.read(65537)
                    if len(data) > 65536:
                        raise ValueError("Unbounded CDP discovery response")
                    endpoint = json.loads(data)["webSocketDebuggerUrl"]
                discovered = urlsplit(endpoint)
                if discovered.hostname != parsed.hostname or discovered.port != parsed.port:
                    raise ValueError("CDP discovery changed the browser endpoint")
            else:
                endpoint = url
            self.socket = await self.client.ws_connect(endpoint, max_msg_size=4*1024*1024)
            self.reader = asyncio.create_task(self._read())
            result = await self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True}, session=False)
            self.session_id = result["sessionId"]
            self.target_id = target_id
            return self
        except BaseException:
            await self.close()
            raise

    async def send(self, method: str, params: dict | None = None, *, session: bool = True, session_id: str | None = None):
        if self.closing and method not in {"Fetch.failRequest", "Runtime.releaseObject"}:
            raise RuntimeError("The operation connection is closing; no further dispatch is permitted")
        if self.error or len(self.pending) >= 64:
            raise RuntimeError("The operation's browser connection is unavailable or overloaded")
        self.sequence += 1
        number = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[number] = future
        message = {"id": number, "method": method, "params": params or {}}
        effective = self.session_id if session_id is None else session_id
        if session and effective:
            message["sessionId"] = effective
        try:
            if method == "Fetch.continueRequest":
                # Authorization and its audit already completed. Once sent, the
                # attempt's outcome may be unknown; it must never be retried.
                self.paused_requests.discard((effective, (params or {}).get("requestId")))
            await self.socket.send_json(message)
            result = await asyncio.wait_for(asyncio.shield(future), 5)
            if method == "Fetch.failRequest":
                self.paused_requests.discard((effective, (params or {}).get("requestId")))
            return result
        finally:
            self.pending.pop(number, None)
            if not future.done():
                future.cancel()

    async def _read(self):
        try:
            async for message in self.socket:
                if message.type != WSMsgType.TEXT:
                    break
                data = message.json()
                if "id" in data:
                    future = self.pending.get(data["id"])
                    if future and not future.done():
                        if data.get("error"):
                            # Browser errors may include private page/request data.
                            future.set_exception(RuntimeError("The browser rejected an operation command"))
                        else:
                            future.set_result(data.get("result", {}))
                else:
                    if data.get("method") == "Fetch.requestPaused":
                        self.paused_requests.add((data.get("sessionId"), data.get("params", {}).get("requestId")))
                    handlers = self.global_handlers if not data.get("sessionId") else self.handlers if data.get("sessionId") == self.session_id else self.scoped_handlers.get(data.get("sessionId"), {})
                    handler = handlers.get(data.get("method"))
                    if handler:
                        if len(self.tasks) >= 64:
                            raise RuntimeError("Browser event processing exceeded its bounded capacity")
                        task = asyncio.create_task(handler(data.get("params", {})))
                        self.tasks.add(task)
                        task.add_done_callback(self._completed)
        except BaseException:
            self.error = True
        finally:
            self.error = True
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(RuntimeError("Browser connection ended; operation outcome may be unknown"))
            if not self.closing and self.on_disconnect is not None:
                # The owner poisons authorization before writing its audit.
                # Even a failed audit must allow private transport cleanup.
                with contextlib.suppress(Exception):
                    self.on_disconnect()

    def _completed(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            # Do not continue any request after a failed guard/event callback.
            self.error = True

    async def close(self):
        self.closing = True
        if self.socket is not None and self.socket.closed:
            # Interception has already detached. It cannot be retained or
            # retroactively abort a request; the owner has recorded the loss.
            self.paused_requests.clear()
        # Detaching a Fetch session releases its paused requests in Chromium.
        # Abort them while the reader/socket can still acknowledge the command.
        # On failure retain interception and report an error instead of opening
        # that dispatch path; the application must close its owned browser.
        async with asyncio.timeout(5):
            while self.paused_requests:
                for session, request in tuple(self.paused_requests):
                    if (session, request) in self.paused_requests:
                        await self.send("Fetch.failRequest", {"requestId": request, "errorReason": "BlockedByClient"}, session_id=session)
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            done, pending = await asyncio.wait(tuple(self.tasks), timeout=5)
            for task in done:
                if not task.cancelled():
                    task.exception()
            if pending:
                raise RuntimeError("Operation cleanup did not finish; close the owned browser before disconnecting")
        if self.socket:
            await self.socket.close()
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        if self.client:
            await self.client.close()
