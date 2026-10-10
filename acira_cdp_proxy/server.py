from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from itertools import count
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from aiohttp import WSMsgType, web
from aiohttp.client_exceptions import ClientConnectionResetError
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from .config import AciraGuardConfig
from .framework_adapters import FrameworkAdapterSpec
from .guard import AciraGuardPolicy
from .logger import CDPLogger, NullCDPLogger
from .pause import PauseCoordinator, PauseHook
from .policy import AGENT_TO_BROWSER, BROWSER_TO_AGENT, CDPMessageContext, CDPPolicy, PassiveCDPPolicy


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

JSON_CONTENT_TYPES = {
    "application/json",
    "application/json; charset=utf-8",
}


@dataclass
class _ConnectionState:
    connection_id: str
    websocket_path: str
    logger: CDPLogger | NullCDPLogger
    upstream_ws: Any | None = None
    _message_counter: count = field(default_factory=lambda: count(1))
    _probe_id_counter: count = field(default_factory=lambda: count(-900_000_000, -1))
    _pending_probe_responses: dict[tuple[str | None, int], asyncio.Future[dict[str, Any] | None]] = field(default_factory=dict)
    _upstream_send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _relay_pause_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def next_message_number(self) -> int:
        return next(self._message_counter)

    @property
    def has_pending_probes(self) -> bool:
        return bool(self._pending_probe_responses)

    async def send_to_browser(self, payload: str | bytes) -> None:
        if self.upstream_ws is None:
            raise RuntimeError("CDP upstream WebSocket is not connected")
        async with self._upstream_send_lock:
            await self.upstream_ws.send(payload)

    async def send_probe(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        if self.upstream_ws is None:
            return None

        probe_id = next(self._probe_id_counter)
        probe_session_id = session_id if isinstance(session_id, str) and session_id else None
        payload: dict[str, Any] = {"id": probe_id, "method": method}
        if params:
            payload["params"] = params
        if probe_session_id:
            payload["sessionId"] = probe_session_id

        message = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        future: asyncio.Future[dict[str, Any] | None] = asyncio.get_running_loop().create_future()
        key = (probe_session_id, probe_id)
        self._pending_probe_responses[key] = future

        message_number = self.next_message_number()
        await self.logger.log(
            direction="ACIRA -> BROWSER",
            connection_id=self.connection_id,
            message_number=message_number,
            websocket_path=self.websocket_path,
            payload=message,
        )

        try:
            await self.send_to_browser(message)
            return await future
        finally:
            self._pending_probe_responses.pop(key, None)

    def resolve_probe_response(self, parsed_message: dict[str, Any] | list[Any] | None) -> bool:
        if not isinstance(parsed_message, dict) or "id" not in parsed_message:
            return False

        message_id = parsed_message.get("id")
        if not isinstance(message_id, int):
            return False

        session_id = parsed_message.get("sessionId")
        key = (session_id if isinstance(session_id, str) else None, message_id)
        future = self._pending_probe_responses.get(key)
        if future is None:
            return message_id <= -900_000_000

        if not future.done():
            future.set_result(parsed_message)
        return True


class CDPProxyServer:
    """Local HTTP and WebSocket proxy for Chrome DevTools Protocol traffic."""

    _server_ids = count(1)
    _connection_ids = count(1)

    def __init__(
        self,
        target_cdp_url: str,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        logger: CDPLogger | NullCDPLogger | None = None,
        policy: CDPPolicy | None = None,
        request_timeout: float = 30.0,
        pause_coordinator: PauseCoordinator | None = None,
        on_pause_start: PauseHook | None = None,
        on_pause_end: PauseHook | None = None,
        adapters: tuple[FrameworkAdapterSpec, ...] = (),
    ) -> None:
        self.target_cdp_url = target_cdp_url
        self.host = host
        self.requested_port = port
        self.port: int | None = None
        self.logger = logger or NullCDPLogger()
        self.policy = policy or PassiveCDPPolicy()
        self.request_timeout = request_timeout
        self.server_id = f"proxy-{next(self._server_ids):04d}"
        self.pause_coordinator = pause_coordinator or PauseCoordinator(
            on_pause_start=on_pause_start,
            on_pause_end=on_pause_end,
        )
        bind_pause = getattr(self.policy, "bind_pause_coordinator", None)
        if callable(bind_pause):
            bind_pause(self.pause_coordinator)

        self._app = web.Application()
        self._app.router.add_route("*", "/{tail:.*}", self._handle_request)
        self._runner = web.AppRunner(self._app, access_log=None, shutdown_timeout=2)
        self._site: web.TCPSite | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._relay_tasks: set[asyncio.Task[Any]] = set()
        self._recorded_commands: dict[tuple[Any, ...], str] = {}

    @property
    def running(self) -> bool:
        return self.port is not None

    @property
    def local_netloc(self) -> str:
        if self.port is None:
            raise RuntimeError("CDP proxy server is not running")
        return f"{self.host}:{self.port}"

    @property
    def local_http_url(self) -> str:
        return f"http://{self.local_netloc}"

    @property
    def local_ws_url(self) -> str:
        return f"ws://{self.local_netloc}"

    async def start(self) -> "CDPProxyServer":
        if self.running:
            return self

        target = urlparse(self.target_cdp_url)
        trust_env = target.hostname not in ("localhost", "127.0.0.1", "::1")
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.request_timeout),
            trust_env=trust_env,
        )

        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.requested_port)
        await self._site.start()
        self.port = self._resolve_bound_port()
        return self

    async def stop(self) -> None:
        tasks = list(self._relay_tasks)
        for task in tasks:
            task.cancel()

        if tasks:
            with contextlib.suppress(Exception):
                await asyncio.gather(*tasks, return_exceptions=True)

        if self._site is not None:
            with contextlib.suppress(Exception):
                await self._site.stop()
            self._site = None

        with contextlib.suppress(Exception):
            await self._runner.cleanup()

        if self._http_client is not None:
            with contextlib.suppress(Exception):
                await self._http_client.aclose()
            self._http_client = None

        self.port = None

    def proxy_cdp_url(self, upstream_cdp_url: str | None = None) -> str:
        cdp_url = upstream_cdp_url or self.target_cdp_url
        if urlparse(cdp_url).scheme in ("ws", "wss"):
            return self.proxy_websocket_url_for(cdp_url)
        return self.local_http_url

    def proxy_websocket_url_for(self, upstream_ws_url: str) -> str:
        parsed = urlparse(upstream_ws_url)
        path = parsed.path or "/"
        return urlunparse(("ws", self.local_netloc, path, parsed.params, parsed.query, ""))

    def owns_proxy_url(self, cdp_url: str | None) -> bool:
        if not cdp_url or self.port is None:
            return False
        parsed = urlparse(cdp_url)
        return parsed.hostname == self.host and parsed.port == self.port

    def _resolve_bound_port(self) -> int:
        addresses = getattr(self._runner, "addresses", None)
        if addresses:
            return int(addresses[0][1])

        if self._site is not None:
            server = getattr(self._site, "_server", None)
            sockets = getattr(server, "sockets", None)
            if sockets:
                return int(sockets[0].getsockname()[1])

        raise RuntimeError("Could not resolve bound CDP proxy port")

    async def _handle_request(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._handle_websocket(request)
        return await self._handle_http(request)

    async def _handle_http(self, request: web.Request) -> web.Response:
        if self._http_client is None:
            raise RuntimeError("CDP proxy server is not running")

        upstream_url = self._upstream_http_url_for_request(request)
        headers = self._forward_headers(request.headers)
        body = await request.read()

        upstream_response = await self._http_client.request(
            request.method,
            upstream_url,
            headers=headers,
            content=body if body else None,
        )

        response_body = upstream_response.content
        response_headers = self._response_headers(upstream_response.headers)

        if self._should_rewrite_json(upstream_response, request):
            try:
                rewritten = self._rewrite_websocket_urls(upstream_response.json())
            except json.JSONDecodeError:
                pass
            else:
                response_body = json.dumps(rewritten, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
                response_headers = {key: value for key, value in response_headers.items() if key.lower() != "content-type"}
                response_headers["Content-Type"] = "application/json"

        return web.Response(
            status=upstream_response.status_code,
            body=response_body,
            headers=response_headers,
        )

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        client_ws = web.WebSocketResponse(max_msg_size=200 * 1024 * 1024, timeout=2)
        await client_ws.prepare(request)

        connection = _ConnectionState(
            connection_id=f"cdp-{next(self._connection_ids):06d}",
            websocket_path=request.rel_url.path_qs,
            logger=self.logger,
        )
        upstream_url = self._upstream_ws_url_for_request(request)
        upstream_headers = self._forward_headers(request.headers)

        try:
            async with ws_connect(
                upstream_url,
                additional_headers=upstream_headers or None,
                max_size=None,
                proxy=None,
                close_timeout=2,
            ) as upstream_ws:
                connection.upstream_ws = upstream_ws
                downstream_task = asyncio.create_task(
                    self._relay_agent_to_browser(client_ws, upstream_ws, connection),
                    name=f"acira-cdp-agent-to-browser-{connection.connection_id}",
                )
                upstream_task = asyncio.create_task(
                    self._relay_browser_to_agent(upstream_ws, client_ws, connection),
                    name=f"acira-cdp-browser-to-agent-{connection.connection_id}",
                )
                self._relay_tasks.update({downstream_task, upstream_task})

                done, pending = await asyncio.wait(
                    {downstream_task, upstream_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    with contextlib.suppress(ClientConnectionResetError, ConnectionResetError):
                        task.result()
        except (ConnectionClosed, ClientConnectionResetError, ConnectionResetError):
            pass
        finally:
            connection.upstream_ws = None
            self._relay_tasks = {task for task in self._relay_tasks if not task.done()}
            try:
                durable = getattr(self.policy, "durable_event", None)
                unfinished = [self._recorded_commands.pop(key) for key in list(self._recorded_commands)
                              if key[0] == connection.connection_id]
                if durable is not None:
                    for operation in unfinished:
                        durable("cdp_outcome_unknown", operation_id=operation,
                                reason="Connection ended before the browser response was observed")
            finally:
                await client_ws.close()

        return client_ws

    async def _relay_agent_to_browser(self, client_ws: web.WebSocketResponse, upstream_ws: Any, connection: _ConnectionState) -> None:
        async for message in client_ws:
            if message.type == WSMsgType.TEXT:
                await self._relay_payload(
                    payload=message.data,
                    direction=AGENT_TO_BROWSER,
                    connection=connection,
                    send=connection.send_to_browser,
                    blocked_callback=lambda reason, payload=message.data: self._send_blocked_command_error(
                        client_ws,
                        payload,
                        reason,
                    ),
                )
            elif message.type == WSMsgType.BINARY:
                await self._relay_payload(
                    payload=message.data,
                    direction=AGENT_TO_BROWSER,
                    connection=connection,
                    send=connection.send_to_browser,
                )
            elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                break
            elif message.type == WSMsgType.ERROR:
                break

    async def _relay_browser_to_agent(self, upstream_ws: Any, client_ws: web.WebSocketResponse, connection: _ConnectionState) -> None:
        buffered_payloads: list[str | bytes] = []

        async def flush_buffered_payloads() -> None:
            while buffered_payloads and not connection.has_pending_probes and not self.pause_coordinator.paused:
                buffered = buffered_payloads.pop(0)
                if isinstance(buffered, bytes):
                    buffered_send = client_ws.send_bytes
                else:
                    buffered_send = client_ws.send_str
                await self._relay_payload(
                    payload=buffered,
                    direction=BROWSER_TO_AGENT,
                    connection=connection,
                    send=buffered_send,
                )

        receive = getattr(upstream_ws, "recv", None)
        upstream_iter = upstream_ws.__aiter__() if not callable(receive) and hasattr(upstream_ws, "__aiter__") else None

        async def receive_payload() -> str | bytes:
            if callable(receive):
                return await receive()
            if upstream_iter is None:
                raise StopAsyncIteration
            return await upstream_iter.__anext__()

        while True:
            await flush_buffered_payloads()
            try:
                payload = await asyncio.wait_for(
                    receive_payload(),
                    timeout=0.05 if buffered_payloads else None,
                )
            except asyncio.TimeoutError:
                continue
            except (ConnectionClosed, StopAsyncIteration):
                break

            parsed_payload = self._parse_payload(payload)
            if connection.resolve_probe_response(parsed_payload):
                await self.logger.log(
                    direction="BROWSER -> ACIRA",
                    connection_id=connection.connection_id,
                    message_number=connection.next_message_number(),
                    websocket_path=connection.websocket_path,
                    payload=payload,
                )
                continue

            if connection.has_pending_probes or self.pause_coordinator.paused:
                buffered_payloads.append(payload)
                if len(buffered_payloads) > 2000:
                    buffered_payloads.pop(0)
                continue

            await flush_buffered_payloads()

            if isinstance(payload, bytes):
                send = client_ws.send_bytes
            else:
                send = client_ws.send_str

            await self._relay_payload(
                payload=payload,
                direction=BROWSER_TO_AGENT,
                connection=connection,
                send=send,
            )

    async def _relay_payload(
        self,
        *,
        payload: str | bytes,
        direction: str,
        connection: _ConnectionState,
        send: Any,
        blocked_callback: Any | None = None,
    ) -> None:
        await self.pause_coordinator.wait_if_paused(direction=direction)
        try:
            if direction == BROWSER_TO_AGENT:
                await self._relay_payload_unlocked(
                    payload=payload,
                    direction=direction,
                    connection=connection,
                    send=send,
                    blocked_callback=blocked_callback,
                )
            else:
                async with connection._relay_pause_lock:
                    await self._relay_payload_unlocked(
                        payload=payload,
                        direction=direction,
                        connection=connection,
                        send=send,
                        blocked_callback=blocked_callback,
                    )
        finally:
            await self.pause_coordinator.release_for_current_task()

    async def _relay_payload_unlocked(
        self,
        *,
        payload: str | bytes,
        direction: str,
        connection: _ConnectionState,
        send: Any,
        blocked_callback: Any | None = None,
    ) -> None:
        message_number = connection.next_message_number()
        context = CDPMessageContext(
            direction=direction,
            connection_id=connection.connection_id,
            message_number=message_number,
            websocket_path=connection.websocket_path,
            parsed_message=self._parse_payload(payload),
            send_probe=connection.send_probe,
        )
        decision = await self.policy.decide(payload, context)
        durable = getattr(self.policy, "durable_event", None)
        parsed = context.parsed_message if isinstance(context.parsed_message, dict) else {}
        command_key = (connection.connection_id, context.session_id, context.message_id)
        recorded_operation = None
        if durable is not None:
            if direction == AGENT_TO_BROWSER and decision.metadata and not (decision.metadata.get("classification") or {}).get("read_only", False):
                from uuid import uuid4
                recorded_operation = uuid4().hex
                durable("cdp_action_evaluated", operation_id=recorded_operation, method=context.method,
                        policy_revision=getattr(self.policy, "policy_revision", None),
                        decision="allow" if decision.allow else "block", metadata=decision.metadata)
            elif direction == BROWSER_TO_AGENT and context.message_id is not None:
                operation = self._recorded_commands.pop(command_key, None)
                if operation:
                    durable("cdp_browser_call_completed" if "result" in parsed else "cdp_outcome_unknown",
                            operation_id=operation, business_outcome="unverified")
        if decision.metadata:
            await self.logger.log_decision(
                connection_id=connection.connection_id,
                message_number=message_number,
                websocket_path=connection.websocket_path,
                payload=payload,
                metadata=decision.metadata,
                reason=decision.reason,
            )

        if not decision.allow:
            if blocked_callback is not None:
                await blocked_callback(decision.reason or "Blocked by CDP proxy policy")
            return

        relay_payload = decision.replacement_payload if decision.replacement_payload is not None else payload
        await self.logger.log(
            direction=direction,
            connection_id=connection.connection_id,
            message_number=message_number,
            websocket_path=connection.websocket_path,
            payload=relay_payload,
        )
        if durable is not None and recorded_operation:
            if len(self._recorded_commands) >= 10000:
                raise OSError("Too many browser commands lack an outcome; repair the connection before continuing")
            durable("cdp_dispatch_attempt", operation_id=recorded_operation)
            self._recorded_commands[command_key] = recorded_operation
        try:
            await send(relay_payload)
        except BaseException:
            if durable is not None and recorded_operation:
                durable("cdp_outcome_unknown", operation_id=recorded_operation)
                self._recorded_commands.pop(command_key, None)
            raise

    async def _send_blocked_command_error(
        self,
        client_ws: web.WebSocketResponse,
        payload: str | bytes,
        reason: str,
    ) -> None:
        parsed = self._parse_payload(payload)
        if not isinstance(parsed, dict) or "id" not in parsed:
            return

        response: dict[str, Any] = {
            "id": parsed["id"],
            "error": {
                "code": -32000,
                "message": reason,
                "data": {
                    "aciraGuard": {
                        "blocked": True,
                        "doNotRetry": reason.startswith("ACIRA_POLICY_BLOCKED_DO_NOT_RETRY:")
                        or reason.startswith("ACIRA_SECURITY_LOCKDOWN_DO_NOT_RETRY:"),
                    }
                },
            },
        }
        if isinstance(parsed.get("sessionId"), str):
            response["sessionId"] = parsed["sessionId"]
        with contextlib.suppress(ClientConnectionResetError, ConnectionResetError):
            await client_ws.send_str(json.dumps(response, separators=(",", ":"), ensure_ascii=True))

    def _upstream_http_url_for_request(self, request: web.Request) -> str:
        parsed = urlparse(self.target_cdp_url)
        scheme = "https" if parsed.scheme == "wss" else "http"
        path_qs = request.rel_url.path_qs or "/"
        return urlunparse((scheme, parsed.netloc, path_qs, "", "", ""))

    def _upstream_ws_url_for_request(self, request: web.Request) -> str:
        parsed = urlparse(self.target_cdp_url)
        scheme = "wss" if parsed.scheme in ("wss", "https") else "ws"
        path_qs = request.rel_url.path_qs or parsed.path or "/"
        return urlunparse((scheme, parsed.netloc, path_qs, "", "", ""))

    def _rewrite_websocket_urls(self, value: Any) -> Any:
        if isinstance(value, dict):
            rewritten: dict[str, Any] = {}
            for key, item in value.items():
                if key == "webSocketDebuggerUrl" and isinstance(item, str):
                    rewritten[key] = self.proxy_websocket_url_for(item)
                else:
                    rewritten[key] = self._rewrite_websocket_urls(item)
            return rewritten

        if isinstance(value, list):
            return [self._rewrite_websocket_urls(item) for item in value]

        return value

    @staticmethod
    def _parse_payload(payload: str | bytes) -> dict[str, Any] | list[Any] | None:
        try:
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            parsed = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return None

        if isinstance(parsed, (dict, list)):
            return parsed
        return None

    @staticmethod
    def _forward_headers(headers: Any) -> dict[str, str]:
        forwarded: dict[str, str] = {}
        for key, value in headers.items():
            lowered = key.lower()
            if lowered in HOP_BY_HOP_HEADERS or lowered == "host" or lowered.startswith("sec-websocket-"):
                continue
            forwarded[key] = value
        return forwarded

    @staticmethod
    def _response_headers(headers: httpx.Headers) -> dict[str, str]:
        response_headers: dict[str, str] = {}
        for key, value in headers.items():
            lowered = key.lower()
            if lowered in HOP_BY_HOP_HEADERS or lowered in {"content-length", "content-encoding"}:
                continue
            response_headers[key] = value
        return response_headers

    @staticmethod
    def _should_rewrite_json(response: httpx.Response, request: web.Request) -> bool:
        content_type = response.headers.get("content-type", "").lower().split(";")[0].strip()
        return content_type in JSON_CONTENT_TYPES or request.path.startswith("/json")


class AciraCDPProxy(CDPProxyServer):
    """Public framework-agnostic Acira CDP proxy API."""

    def __init__(
        self,
        upstream_cdp_url: str,
        *,
        config: AciraGuardConfig | dict[str, Any] | None = None,
        policy: CDPPolicy | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        logger: CDPLogger | NullCDPLogger | None = None,
        log_file: str | None = None,
        request_timeout: float = 30.0,
        pause_coordinator: PauseCoordinator | None = None,
        on_pause_start: PauseHook | None = None,
        on_pause_end: PauseHook | None = None,
        adapters: tuple[FrameworkAdapterSpec, ...] = (),
    ) -> None:
        selected_logger = logger or (CDPLogger(log_file) if log_file else NullCDPLogger())
        selected_policy = policy
        if selected_policy is None and config is not None:
            selected_policy = AciraGuardPolicy(config, adapters=adapters)

        super().__init__(
            upstream_cdp_url,
            host=host,
            port=port,
            logger=selected_logger,
            policy=selected_policy,
            request_timeout=request_timeout,
            pause_coordinator=pause_coordinator,
            on_pause_start=on_pause_start,
            on_pause_end=on_pause_end,
        )

    @property
    def local_cdp_url(self) -> str:
        return self.local_http_url
