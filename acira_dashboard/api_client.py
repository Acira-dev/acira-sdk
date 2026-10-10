from __future__ import annotations

import asyncio
import base64
import binascii
import json
import sys
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout
from websockets.asyncio.client import connect as ws_connect

from acira_cdp_proxy.checkpoint import CheckpointStore, PendingReviewRecord
from acira_cdp_proxy.config import AciraGuardConfig
from acira_cdp_proxy.guard import GuardReviewRequest, TerminalReviewBackend
from acira_cdp_proxy.redaction import redact_value

from .discovery import read_descriptor


ARTIFACT_CLOSE_GRACE_SECONDS = 15.0
ARTIFACT_CAPTURE_ATTEMPTS = 3
ARTIFACT_INLINE_CAPTURE_ATTEMPTS = 1
# One pre-decision pass includes layout inspection, viewport evaluation, and screenshot capture.
# The review is created first so the dashboard can show a pending evidence state while this runs.
ARTIFACT_INLINE_CAPTURE_TIMEOUT_SECONDS = 8.0
ARTIFACT_LAYOUT_TIMEOUT_SECONDS = 2.0
ARTIFACT_VIEWPORT_TIMEOUT_SECONDS = 2.0
ARTIFACT_UPLOAD_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class DashboardApiConfig:
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    review_timeout_seconds: float = 3600.0
    heartbeat_seconds: float = 2.0
    agent_name: str = ""
    default_decision: str = "block"


class DashboardApiError(RuntimeError):
    pass


def parse_dashboard_api_config(
    data: dict[str, Any] | None,
    *,
    project_dir: str | Path | None = None,
) -> DashboardApiConfig:
    """Resolve an agent's dashboard connection.

    The agent opts in with a boolean (``dashboard.connect``, ``dashboard.enabled``); it
    never specifies a URL or token. The connection is auto-discovered from the descriptor
    the project-local dashboard writes. If the agent opts out, or no dashboard is running
    (descriptor absent), this returns a disabled config and the agent runs without one —
    no error, no warning. An explicit ``base_url`` + ``api_key`` may still be supplied to
    bypass discovery (advanced/manual use).
    """
    raw = data.get("dashboard") if isinstance(data, dict) else None
    agent_name = _clean_str(data.get("agent_name") or data.get("name")) if isinstance(data, dict) else ""
    default_decision = (
        str(data.get("default_review_decision") or "block").strip().lower() if isinstance(data, dict) else "block"
    )
    if raw is None:
        return DashboardApiConfig(agent_name=agent_name, default_decision=default_decision)
    if not isinstance(raw, dict):
        raise ValueError("dashboard must be an object")

    connect = bool(raw.get("connect", raw.get("enabled", False)))
    review_timeout = _positive_float(raw.get("review_timeout_seconds", 3600.0), default=3600.0)
    heartbeat = _positive_float(raw.get("heartbeat_seconds", 2.0), default=2.0)
    disabled = DashboardApiConfig(agent_name=agent_name, default_decision=default_decision)

    if not connect:
        return disabled

    base_url = str(raw.get("base_url") or raw.get("url") or "").strip().rstrip("/")
    api_key = str(raw.get("api_key") or raw.get("token") or "").strip()

    if not base_url or not api_key:
        descriptor = read_descriptor(project_dir)
        if not descriptor:
            # Opted in, but no dashboard running locally — silently run without one.
            return disabled
        base_url = str(descriptor.get("base_url") or f"http://{descriptor.get('host')}:{descriptor.get('port')}").rstrip("/")
        api_key = str(descriptor.get("token") or "").strip()

    if not api_key:
        return disabled
    _validate_base_url(base_url)

    return DashboardApiConfig(
        enabled=True,
        base_url=base_url,
        api_key=api_key,
        review_timeout_seconds=review_timeout,
        heartbeat_seconds=heartbeat,
        agent_name=agent_name,
        default_decision=default_decision,
    )


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


class DashboardApiClient:
    def __init__(self, config: DashboardApiConfig, *, request_timeout_seconds: float = 10.0) -> None:
        if not config.enabled:
            raise DashboardApiError("Dashboard API config is disabled.")
        self.config = config
        self.agent_name = config.agent_name or ""
        self.request_timeout_seconds = max(1.0, float(request_timeout_seconds))
        self._session: ClientSession | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    async def __aenter__(self) -> "DashboardApiClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.close()

    async def register_session(
        self,
        run_id: str,
        *,
        framework: str = "browser_use",
        agent_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        project_root: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": run_id,
            "framework": framework,
            "agent_name": agent_name.strip() if isinstance(agent_name, str) and agent_name.strip() else self.agent_name,
            "metadata": metadata or {},
        }
        if isinstance(project_root, str) and project_root.strip():
            payload["project_root"] = project_root.strip()
        return await self._request_json("POST", "/api/runtime/sessions", data=payload)

    async def end_session(self, run_id: str, *, reason: str | None = None) -> dict[str, Any]:
        return await self._request_json(
            "POST",
            f"/api/runtime/sessions/{run_id}/end",
            data={"reason": reason} if reason else {},
        )

    async def append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = dict(event)
        payload.setdefault("agent_name", self.agent_name)
        return await self._request_json("POST", "/api/runtime/events", data=payload)

    async def create_review(self, request: GuardReviewRequest, *, run_id: str | None, timeout_seconds: float) -> dict[str, Any]:
        artifact = _pending_review_artifact(request)
        payload = await self._request_json(
            "POST",
            "/api/runtime/reviews",
            data=_review_request_payload(
                request,
                run_id=run_id,
                agent_name=self.agent_name,
                timeout_seconds=timeout_seconds,
                artifact=artifact,
            ),
            timeout_seconds=min(self.request_timeout_seconds, 2.0),
        )
        review = payload.get("review") if isinstance(payload.get("review"), dict) else payload
        review_id = str(review.get("review_id") or "") if isinstance(review, dict) else ""
        if review_id:
            artifact = await _capture_initial_review_artifact(request)
            if not _artifact_is_pending(artifact):
                async with _suppress_async_errors():
                    await self.update_review_artifact(review_id, artifact, timeout_seconds=ARTIFACT_UPLOAD_TIMEOUT_SECONDS)
            if _artifact_needs_background_capture(artifact):
                _schedule_review_artifact_capture(self, review_id, request)
        return payload

    async def update_review_artifact(
        self,
        review_id: str,
        artifact: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        return await self._request_json(
            "PUT",
            f"/api/runtime/reviews/{review_id}/artifact",
            data={"artifact": artifact},
            timeout_seconds=timeout_seconds or max(self.request_timeout_seconds, ARTIFACT_UPLOAD_TIMEOUT_SECONDS),
        )

    async def wait_review_decision(self, review_id: str, *, timeout_seconds: float, default: str = "block") -> str:
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            remaining = max(0.5, deadline - time.monotonic())
            wait_seconds = min(10.0, remaining)
            payload = await self._request_json(
                "GET",
                f"/api/runtime/reviews/{review_id}/decision?wait_seconds={wait_seconds:.3f}",
                timeout_seconds=wait_seconds + 2.0,
            )
            status = str(payload.get("status") or "").strip().lower()
            if status in {"allow", "block"}:
                return status
            await asyncio.sleep(0.25)
        return default

    async def decide_review(self, review_id: str, *, status: str, reason: str | None = None) -> dict[str, Any]:
        return await self._request_json(
            "POST",
            f"/api/reviews/{review_id}/decision",
            data={"status": status, "reason": reason or f"Runtime {status}"},
        )

    async def get_session_control(self, run_id: str, *, wait_seconds: float = 0.0) -> dict[str, Any]:
        suffix = f"?wait_seconds={max(0.0, float(wait_seconds)):.3f}" if wait_seconds > 0 else ""
        return await self._request_json(
            "GET",
            f"/api/runtime/sessions/{run_id}/control{suffix}",
            timeout_seconds=max(self.request_timeout_seconds, wait_seconds + 1.0) if wait_seconds > 0 else None,
        )

    async def close(self) -> None:
        tasks = list(self._background_tasks)
        pending = tasks
        if tasks:
            _, pending_set = await asyncio.wait(tasks, timeout=ARTIFACT_CLOSE_GRACE_SECONDS)
            pending = list(pending_set)
        self._closed = True
        self._background_tasks.difference_update(tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        session = self._session
        self._session = None
        if session is not None and not session.closed:
            await session.close()

    def _track_background_task(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _client_session(self, timeout: ClientTimeout) -> ClientSession:
        if self._closed:
            raise DashboardApiError("Dashboard API client is closed.")
        session = self._session
        headers = {"X-Acira-Dashboard-Token": self.config.api_key}
        if session is None or session.closed:
            self._session = ClientSession(headers=headers, timeout=timeout)
            return self._session
        return session

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        timeout = ClientTimeout(total=max(1.0, float(timeout_seconds or self.request_timeout_seconds)))
        url = urljoin(f"{self.config.base_url}/", path.lstrip("/"))
        try:
            session = await self._client_session(timeout)
            async with session.request(method, url, json=data if data is not None else None, timeout=timeout) as response:
                payload = await _response_payload(response)
                if response.status >= 400:
                    message = payload.get("error") if isinstance(payload, dict) else None
                    raise DashboardApiError(message or f"Dashboard API returned HTTP {response.status}")
                if not isinstance(payload, dict):
                    raise DashboardApiError("Dashboard API response must be a JSON object.")
                return payload
        except (ClientError, asyncio.TimeoutError) as exc:
            raise DashboardApiError(f"Dashboard API request failed: {exc}") from exc


class DashboardApiReviewBackend:
    def __init__(
        self,
        client: DashboardApiClient,
        *,
        timeout_seconds: float,
        terminal_review_config: AciraGuardConfig | None = None,
        default_decision: str = "block",
    ) -> None:
        self.client = client
        self.timeout_seconds = timeout_seconds
        self.terminal_review_backend = TerminalReviewBackend(terminal_review_config) if terminal_review_config is not None else None
        # Decision applied when neither channel responds before the timeout (policy fallback).
        self.default_decision = _review_result_status(default_decision) or "block"
        self.run_id: str | None = None

    def bind_run_id(self, run_id: str) -> None:
        self.run_id = run_id

    async def ask(self, request: GuardReviewRequest) -> str:
        try:
            payload = await self.client.create_review(request, run_id=self.run_id, timeout_seconds=self.timeout_seconds)
            review = payload.get("review") if isinstance(payload.get("review"), dict) else payload
            review_id = str(review.get("review_id") or "")
            if not review_id:
                raise DashboardApiError("Dashboard review response did not include review_id.")
            return await self._wait_for_shared_decision(review_id, request)
        except Exception as exc:
            # The dashboard is unreachable (down, crashed, or a stale descriptor points at a
            # dead server). The terminal is ALWAYS available as a fallback channel, so prompt
            # there before applying the policy default — never fail closed just because the
            # dashboard could not be reached.
            async with _suppress_async_errors():
                await self.client.append_event(
                    {
                        "type": "hitl_review_backend_failed",
                        "run_id": self.run_id,
                        "status": "terminal_fallback",
                        "reason": str(exc),
                        "summary": "Dashboard review unreachable; falling back to terminal prompt",
                    }
                )
            return await self._terminal_fallback(request)

    async def _terminal_fallback(self, request: GuardReviewRequest) -> str:
        """Prompt on the terminal when the dashboard channel is unavailable.

        Adds no delay beyond the failed dashboard attempt that already happened. Falls back
        to the policy default only if there is no interactive terminal (or the prompt yields
        no usable answer).
        """
        if self.terminal_review_backend is not None and sys.stdin.isatty():
            try:
                result = await self.terminal_review_backend.ask_until(request)
            except Exception:
                return self.default_decision
            status = _review_result_status(result)
            if status is not None:
                return status
        return self.default_decision

    async def _wait_for_shared_decision(self, review_id: str, request: GuardReviewRequest) -> str:
        if self.terminal_review_backend is None or not sys.stdin.isatty():
            return await self.client.wait_review_decision(
                review_id, timeout_seconds=self.timeout_seconds, default=self.default_decision
            )

        stop_terminal = asyncio.Event()
        dashboard_task = asyncio.create_task(
            self.client.wait_review_decision(
                review_id, timeout_seconds=self.timeout_seconds, default=self.default_decision
            )
        )
        terminal_task = asyncio.create_task(self.terminal_review_backend.ask_until(request, stop_event=stop_terminal))
        try:
            while True:
                done, _ = await asyncio.wait({dashboard_task, terminal_task}, return_when=asyncio.FIRST_COMPLETED)
                if dashboard_task in done:
                    stop_terminal.set()
                    return _decision_status(await dashboard_task)
                if terminal_task in done:
                    terminal_result = await terminal_task
                    status = _review_result_status(terminal_result)
                    if status is None:
                        return await dashboard_task
                    stop_terminal.set()
                    if not dashboard_task.done():
                        dashboard_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await dashboard_task
                    return await self._commit_terminal_decision(review_id, status)
        finally:
            stop_terminal.set()
            if not terminal_task.done():
                with suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(terminal_task), timeout=0.25)
            for task in (dashboard_task, terminal_task):
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    async def _commit_terminal_decision(self, review_id: str, status: str) -> str:
        try:
            await self.client.decide_review(review_id, status=status, reason=f"Terminal {status}")
            return status
        except DashboardApiError:
            return await self.client.wait_review_decision(
                review_id, timeout_seconds=1.0, default=self.default_decision
            )


class DashboardApiSessionReporter:
    """Best-effort agent session lifecycle for the project-local dashboard.

    Registers the session once at startup and ends it once at shutdown. There is no
    heartbeat and no pause polling — the agent never checks whether the dashboard is up.
    Every call is fire-and-forget: a missing or unreachable dashboard never raises, so the
    agent behaves identically with or without one. Human-in-the-loop review is handled
    separately, on demand, by the review backend — not here.
    """

    def __init__(
        self,
        client: DashboardApiClient,
        *,
        framework: str = "agent",
        agent_name: str = "",
        project_root: str | None = None,
    ) -> None:
        self.client = client
        self.framework = framework or "agent"
        self.agent_name = agent_name or ""
        self.project_root = project_root
        self.run_id: str | None = None

    def bind_run_id(self, run_id: str) -> None:
        self.run_id = run_id

    async def start(self) -> None:
        if not self.run_id:
            return
        async with _suppress_async_errors():
            await self.client.register_session(
                self.run_id,
                framework=self.framework,
                agent_name=self.agent_name,
                metadata={"agent_name": self.agent_name} if self.agent_name else {},
                project_root=self.project_root,
            )

    async def wait_if_paused(self) -> None:
        # The agent never polls the dashboard for pause state — fire-and-forget only.
        return None

    async def close(self, *, reason: str | None = None) -> None:
        if self.run_id:
            async with _suppress_async_errors():
                await self.client.end_session(self.run_id, reason=reason)
        async with _suppress_async_errors():
            await self.client.close()


class DashboardApiCheckpointStore(CheckpointStore):
    def __init__(self, client: DashboardApiClient) -> None:
        self.client = client

    def record_pending(self, record: PendingReviewRecord) -> None:
        _schedule_event_post(
            self.client,
            {
                "type": "pending_review",
                **redact_value(asdict(record)),
                "summary": _pending_summary(record),
            },
        )

    def record_decision(self, review_id: str, *, status: str, reason: str | None, metadata: dict[str, Any] | None) -> None:
        _schedule_event_post(
            self.client,
            {
                "type": "review_decision",
                "review_id": review_id,
                "status": status,
                "reason": redact_value(reason),
                "metadata": redact_value(metadata or {}),
                "summary": _decision_summary(status, reason),
            },
        )


def _review_request_payload(
    request: GuardReviewRequest,
    *,
    run_id: str | None,
    agent_name: str = "",
    timeout_seconds: float,
    artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "run_id": run_id,
        "agent_name": agent_name,
        "reason": redact_value(request.reason),
        "payload": _payload_summary(request.payload),
        "context": redact_value(request.context.to_metadata() if hasattr(request.context, "to_metadata") else {}),
        "classification": redact_value(request.classification.to_metadata()),
        "timeout_seconds": max(1.0, float(timeout_seconds)),
    }
    if artifact is not None:
        payload["artifact"] = artifact
    return payload


def _pending_review_artifact(request: GuardReviewRequest) -> dict[str, Any]:
    return {
        "kind": "screenshot",
        "capture_status": "pending",
        "target_box": _target_box_from_classification(request.classification.to_metadata()),
    }


async def _capture_initial_review_artifact(request: GuardReviewRequest) -> dict[str, Any]:
    pending = _pending_review_artifact(request)
    try:
        artifact = await asyncio.wait_for(
            _capture_review_artifact(request, max_attempts=ARTIFACT_INLINE_CAPTURE_ATTEMPTS),
            timeout=ARTIFACT_INLINE_CAPTURE_TIMEOUT_SECONDS,
        )
    except Exception:
        return pending
    if not isinstance(artifact, dict):
        return pending
    if artifact.get("capture_status") in {"captured", "failed", "unavailable"}:
        return artifact
    return pending


def _artifact_is_pending(artifact: dict[str, Any] | None) -> bool:
    if not isinstance(artifact, dict):
        return True
    return str(artifact.get("capture_status") or "").strip().lower() == "pending"


def _artifact_needs_background_capture(artifact: dict[str, Any] | None) -> bool:
    if not isinstance(artifact, dict):
        return True
    return str(artifact.get("capture_status") or "").strip().lower() not in {"captured", "unavailable"}


async def _capture_review_artifact(request: GuardReviewRequest, *, max_attempts: int = ARTIFACT_CAPTURE_ATTEMPTS) -> dict[str, Any] | None:
    target_box = _target_box_from_classification(request.classification.to_metadata())
    direct_error: str | None = None
    try:
        direct_artifact = await _capture_review_artifact_from_direct_cdp(request, target_box=target_box)
        if isinstance(direct_artifact, dict) and direct_artifact.get("capture_status") == "captured":
            return direct_artifact
        if isinstance(direct_artifact, dict) and direct_artifact.get("capture_error"):
            direct_error = str(direct_artifact.get("capture_error") or "")
    except Exception as exc:
        direct_error = f"direct_cdp: {exc.__class__.__name__}: {exc}"
    context = request.context
    probe = getattr(context, "probe", None)
    if not callable(probe):
        return {"kind": "screenshot", "capture_status": "unavailable", "target_box": target_box}
    session_candidates = _probe_session_candidates(request)
    errors: list[str] = [direct_error] if direct_error else []
    total_attempts = max(1, int(max_attempts or ARTIFACT_CAPTURE_ATTEMPTS))
    for attempt in range(1, total_attempts + 1):
        for session_id in session_candidates:
            session_label = _probe_session_label(session_id)
            try:
                viewport = await _capture_viewport_info(probe, session_id=session_id)
                encoded = await _capture_screenshot_base64(probe, session_id=session_id)
                dimensions = _png_dimensions_from_base64(encoded)
                return {
                    "kind": "screenshot",
                    "capture_status": "captured",
                    "image_base64": encoded,
                    "image_width": dimensions[0] if dimensions else None,
                    "image_height": dimensions[1] if dimensions else None,
                    "target_box": target_box,
                    "viewport": _viewport_with_screenshot_dimensions(viewport, dimensions),
                    "capture_attempts": attempt,
                    "capture_session_id": _artifact_session_id(session_id),
                    "capture_errors": errors or None,
                }
            except Exception as exc:
                errors.append(f"{session_label}: {exc.__class__.__name__}: {exc}")
        if attempt < total_attempts:
            await asyncio.sleep(min(2.0, 0.5 * attempt))
    try:
        viewport = await _capture_viewport_info(probe, session_id=session_candidates[0])
    except Exception as exc:
        errors.append(f"final viewport: {exc.__class__.__name__}: {exc}")
        viewport = None
    return {
        "kind": "screenshot",
        "capture_status": "failed",
        "target_box": target_box,
        "viewport": viewport,
        "capture_attempts": total_attempts,
        "capture_session_candidates": [_artifact_session_id(item) for item in session_candidates],
        "capture_error": _capture_error_summary(errors),
    }


async def _capture_review_artifact_from_direct_cdp(
    request: GuardReviewRequest,
    *,
    target_box: dict[str, Any] | None,
) -> dict[str, Any] | None:
    cdp_url = _direct_cdp_url_from_request(request)
    if not cdp_url:
        return None
    websocket_url = await _direct_cdp_websocket_url(cdp_url)
    if not websocket_url:
        return {"kind": "screenshot", "capture_status": "failed", "target_box": target_box, "capture_error": "direct_cdp: no websocket URL"}
    expected_url = _expected_page_url_from_request(request)
    next_id = 1
    session_id: str | None = None
    attached_target = False

    async with ws_connect(websocket_url, max_size=None, proxy=None) as websocket:
        async def direct_probe(method: str, params: dict[str, Any] | None = None, session_id: str | None = None) -> dict[str, Any] | None:
            nonlocal next_id
            message_id = next_id
            next_id += 1
            message: dict[str, Any] = {"id": message_id, "method": method}
            if params:
                message["params"] = params
            if session_id:
                message["sessionId"] = session_id
            await websocket.send(json.dumps(message, separators=(",", ":"), ensure_ascii=True))
            while True:
                raw = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    response = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(response, dict) and response.get("id") == message_id:
                    return response

        target_id = await _direct_cdp_page_target_id(direct_probe, expected_url=expected_url)
        if target_id:
            attached = await direct_probe("Target.attachToTarget", {"targetId": target_id, "flatten": True})
            found_session_id = _cdp_result_value(attached, "sessionId")
            if isinstance(found_session_id, str) and found_session_id:
                session_id = found_session_id
                attached_target = True
        try:
            with suppress(Exception):
                await direct_probe("Page.enable", {}, session_id=session_id)
            viewport = await _capture_viewport_info(direct_probe, session_id=session_id)
            encoded = await _capture_screenshot_base64(direct_probe, session_id=session_id)
            dimensions = _png_dimensions_from_base64(encoded)
            return {
                "kind": "screenshot",
                "capture_status": "captured",
                "image_base64": encoded,
                "image_width": dimensions[0] if dimensions else None,
                "image_height": dimensions[1] if dimensions else None,
                "target_box": target_box,
                "viewport": _viewport_with_screenshot_dimensions(viewport, dimensions),
                "capture_attempts": 1,
                "capture_session_id": f"direct_cdp:{target_id or session_id or 'root'}",
            }
        finally:
            if attached_target and session_id:
                with suppress(Exception):
                    await direct_probe("Target.detachFromTarget", {"sessionId": session_id})


def _direct_cdp_url_from_request(request: GuardReviewRequest) -> str | None:
    classification = request.classification.to_metadata()
    details = classification.get("details") if isinstance(classification.get("details"), dict) else {}
    capture = details.get("cdp_capture") if isinstance(details.get("cdp_capture"), dict) else {}
    url = str(capture.get("url") or "").strip()
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        return None
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    return url


async def _direct_cdp_websocket_url(cdp_url: str) -> str | None:
    parsed = urlsplit(cdp_url)
    if parsed.scheme in {"ws", "wss"}:
        return cdp_url
    root = cdp_url.rstrip("/")
    timeout = ClientTimeout(total=2.0)
    async with ClientSession(timeout=timeout) as session:
        for path in ("/json/version", "/json/list"):
            try:
                async with session.get(f"{root}{path}") as response:
                    payload = await response.json()
            except Exception:
                continue
            if isinstance(payload, dict):
                websocket_url = payload.get("webSocketDebuggerUrl")
                if isinstance(websocket_url, str) and websocket_url:
                    return websocket_url
            if isinstance(payload, list):
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") not in {None, "page"}:
                        continue
                    websocket_url = item.get("webSocketDebuggerUrl")
                    if isinstance(websocket_url, str) and websocket_url:
                        return websocket_url
    return None


async def _direct_cdp_page_target_id(probe: Any, *, expected_url: str | None) -> str | None:
    try:
        response = await probe("Target.getTargets", {})
    except Exception:
        return None
    infos = _cdp_result_value(response, "targetInfos")
    if not isinstance(infos, list):
        return None
    pages = [item for item in infos if isinstance(item, dict) and item.get("type") == "page"]
    if expected_url:
        expected = _normalize_page_url(expected_url)
        for item in pages:
            if _normalize_page_url(str(item.get("url") or "")) == expected:
                target_id = item.get("targetId")
                return target_id if isinstance(target_id, str) and target_id else None
    for item in pages:
        target_id = item.get("targetId")
        if isinstance(target_id, str) and target_id:
            return target_id
    return None


def _expected_page_url_from_request(request: GuardReviewRequest) -> str | None:
    classification = request.classification.to_metadata()
    target = classification.get("target") if isinstance(classification.get("target"), dict) else {}
    if isinstance(target.get("url"), str) and target.get("url"):
        return str(target.get("url"))
    details = classification.get("details") if isinstance(classification.get("details"), dict) else {}
    probe = details.get("page_target_probe") if isinstance(details.get("page_target_probe"), dict) else {}
    if isinstance(probe.get("url"), str) and probe.get("url"):
        return str(probe.get("url"))
    if isinstance(details.get("url"), str) and details.get("url"):
        return str(details.get("url"))
    return None


def _normalize_page_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if not parsed.scheme and not parsed.netloc:
        return str(value or "").strip()
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path or '/'}"


async def _capture_viewport_info(probe: Any, *, session_id: str | None = None) -> dict[str, float] | None:
    layout_viewport = None
    runtime_viewport = None
    with suppress(Exception):
        metrics = await asyncio.wait_for(
            _call_probe(probe, "Page.getLayoutMetrics", {}, session_id=session_id),
            timeout=ARTIFACT_LAYOUT_TIMEOUT_SECONDS,
        )
        layout_viewport = _viewport_from_layout_metrics(metrics)
    with suppress(Exception):
        runtime = await asyncio.wait_for(
            _call_probe(
                probe,
                "Runtime.evaluate",
                {
                    "expression": (
                        "(() => {"
                        "const vv = window.visualViewport;"
                        "const doc = document.documentElement || document.body || {};"
                        "return {"
                        "width: window.innerWidth || doc.clientWidth || 0,"
                        "height: window.innerHeight || doc.clientHeight || 0,"
                        "clientWidth: doc.clientWidth || window.innerWidth || 0,"
                        "clientHeight: doc.clientHeight || window.innerHeight || 0,"
                        "pageX: window.scrollX || window.pageXOffset || 0,"
                        "pageY: window.scrollY || window.pageYOffset || 0,"
                        "offsetX: vv ? vv.offsetLeft : 0,"
                        "offsetY: vv ? vv.offsetTop : 0,"
                        "visualWidth: vv ? vv.width : null,"
                        "visualHeight: vv ? vv.height : null,"
                        "scale: vv ? vv.scale : 1,"
                        "devicePixelRatio: window.devicePixelRatio || 1"
                        "};"
                        "})()"
                    ),
                    "returnByValue": True,
                },
                session_id=session_id,
            ),
            timeout=ARTIFACT_VIEWPORT_TIMEOUT_SECONDS,
        )
        runtime_viewport = _viewport_from_runtime_metrics(runtime)
    return _merge_viewports(layout_viewport, runtime_viewport)


async def _capture_screenshot_base64(probe: Any, *, session_id: str | None = None) -> str:
    from acira_cdp_proxy.screenshots import capture_masked_cdp
    async def compatible_probe(method, params, selected_session):
        return await _call_probe(probe, method, params, session_id=selected_session)
    artifact = await capture_masked_cdp(compatible_probe, session_id=session_id)
    if artifact["capture_status"] != "ready":
        raise DashboardApiError(artifact["reason"])
    return artifact["image"]


def _schedule_review_artifact_capture(client: DashboardApiClient, review_id: str, request: GuardReviewRequest) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def capture() -> None:
        artifact = await _capture_review_artifact(request)
        if artifact is None:
            artifact = _pending_review_artifact(request) | {"capture_status": "unavailable"}
        async with _suppress_async_errors():
            await client.update_review_artifact(review_id, artifact, timeout_seconds=ARTIFACT_UPLOAD_TIMEOUT_SECONDS)

    if client._closed:  # noqa: SLF001
        return
    client._track_background_task(loop.create_task(capture()))  # noqa: SLF001


async def _call_probe(
    probe: Any,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    session_id: str | None = None,
) -> Any:
    if session_id is None:
        return await probe(method, params)
    try:
        return await probe(method, params, session_id=session_id)
    except TypeError:
        return await probe(method, params, session_id)


def _probe_session_candidates(request: GuardReviewRequest) -> list[str | None]:
    classification = request.classification.to_metadata()
    details = classification.get("details") if isinstance(classification.get("details"), dict) else {}
    found: list[str] = []
    _collect_session_ids(details, found)

    candidates: list[str | None] = []
    seen: set[tuple[str, str]] = set()

    def add(value: str | None, *, kind: str) -> None:
        key = (kind, value or "")
        if key in seen:
            return
        seen.add(key)
        candidates.append(value)

    for session_id in found:
        add(session_id, kind="explicit")
    add(None, kind="default")
    add("", kind="root")
    return candidates[:6]


def _collect_session_ids(value: Any, found: list[str]) -> None:
    if len(found) >= 4:
        return
    if isinstance(value, dict):
        session_id = value.get("session_id")
        if isinstance(session_id, str) and session_id and session_id not in found:
            found.append(session_id)
            if len(found) >= 4:
                return
        for item in value.values():
            _collect_session_ids(item, found)
            if len(found) >= 4:
                return
    elif isinstance(value, list):
        for item in value:
            _collect_session_ids(item, found)
            if len(found) >= 4:
                return


def _artifact_session_id(session_id: str | None) -> str:
    if session_id is None:
        return "context-default"
    return session_id or "root"


def _probe_session_label(session_id: str | None) -> str:
    return f"session={_artifact_session_id(session_id)}"


def _cdp_result_value(response: Any, key: str) -> Any:
    return _find_cdp_result_value(response, key, depth=0)


def _find_cdp_result_value(value: Any, key: str, *, depth: int) -> Any:
    if depth > 4 or not isinstance(value, dict):
        return None
    direct = value.get(key)
    if direct is not None:
        return direct
    for nested_key in ("result", "value", "payload", "response"):
        nested = value.get(nested_key)
        found = _find_cdp_result_value(nested, key, depth=depth + 1)
        if found is not None:
            return found
    return None


def _cdp_result_object(response: Any) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        return None
    result = response.get("result") if isinstance(response.get("result"), dict) else response
    return result if isinstance(result, dict) else None


def _cdp_error_summary(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    error = response.get("error")
    if not isinstance(error, dict):
        return None
    message = str(error.get("message") or "").strip()
    code = error.get("code")
    if message and code is not None:
        return f"CDP error {code}: {message}"
    return f"CDP error: {message}" if message else "CDP error response"


def _viewport_from_layout_metrics(response: Any) -> dict[str, float] | None:
    result = _cdp_result_object(response)
    if not isinstance(result, dict):
        return None
    viewport = result.get("cssVisualViewport") or result.get("visualViewport") or result.get("layoutViewport")
    if not isinstance(viewport, dict):
        return None
    width = viewport.get("clientWidth", viewport.get("width"))
    height = viewport.get("clientHeight", viewport.get("height"))
    try:
        normalized = {"width": float(width), "height": float(height)}
    except (TypeError, ValueError):
        return None
    if normalized["width"] <= 0 or normalized["height"] <= 0:
        return None
    for source, target in (("pageX", "page_x"), ("pageY", "page_y"), ("offsetX", "offset_x"), ("offsetY", "offset_y"), ("scale", "scale")):
        if source in viewport:
            try:
                normalized[target] = float(viewport[source])
            except (TypeError, ValueError):
                pass
    return normalized


def _viewport_from_runtime_metrics(response: Any) -> dict[str, float] | None:
    value = _cdp_result_value(response, "value")
    if not isinstance(value, dict):
        return None
    width = value.get("visualWidth") or value.get("width") or value.get("clientWidth")
    height = value.get("visualHeight") or value.get("height") or value.get("clientHeight")
    try:
        normalized = {"width": float(width), "height": float(height)}
    except (TypeError, ValueError):
        return None
    if normalized["width"] <= 0 or normalized["height"] <= 0:
        return None
    for source, target in (
        ("pageX", "page_x"),
        ("pageY", "page_y"),
        ("offsetX", "offset_x"),
        ("offsetY", "offset_y"),
        ("scale", "scale"),
        ("devicePixelRatio", "device_pixel_ratio"),
        ("clientWidth", "client_width"),
        ("clientHeight", "client_height"),
    ):
        if source in value:
            try:
                normalized[target] = float(value[source])
            except (TypeError, ValueError):
                pass
    return normalized


def _merge_viewports(primary: dict[str, float] | None, secondary: dict[str, float] | None) -> dict[str, float] | None:
    if primary is None:
        return dict(secondary) if secondary is not None else None
    merged = dict(primary)
    if secondary is not None:
        for key, value in secondary.items():
            if key not in merged or key in {"device_pixel_ratio", "client_width", "client_height"}:
                merged[key] = value
    return merged


def _viewport_with_screenshot_dimensions(
    viewport: dict[str, float] | None,
    dimensions: tuple[int, int] | None,
) -> dict[str, float] | None:
    if viewport is None:
        return None
    normalized = dict(viewport)
    if dimensions is None:
        return normalized
    image_width, image_height = dimensions
    normalized["image_width"] = float(image_width)
    normalized["image_height"] = float(image_height)
    width = normalized.get("width")
    height = normalized.get("height")
    if width and height:
        scale_x = image_width / width
        scale_y = image_height / height
        if scale_x > 0 and scale_y > 0:
            normalized["screenshot_scale_x"] = scale_x
            normalized["screenshot_scale_y"] = scale_y
            if "device_pixel_ratio" not in normalized and abs(scale_x - scale_y) <= 0.05:
                normalized["device_pixel_ratio"] = (scale_x + scale_y) / 2
    return normalized


def _target_box_from_classification(classification: dict[str, Any]) -> dict[str, Any] | None:
    target = classification.get("target") if isinstance(classification.get("target"), dict) else {}
    signature = target.get("structure_signature") if isinstance(target.get("structure_signature"), dict) else {}
    layout = signature.get("layout") if isinstance(signature.get("layout"), dict) else {}
    for key in ("viewport_rect", "rect", "browser_use_rect", "document_rect"):
        rect = layout.get(key) if isinstance(layout.get(key), dict) else None
        box = _box_from_value(rect)
        if box is not None:
            box["source"] = key
            box["coordinate_space"] = _coordinate_space_for_box(key, layout)
            return box
    return None


def _box_from_value(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        box = {key: float(value[key]) for key in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError):
        return None
    if box["width"] <= 0 or box["height"] <= 0:
        return None
    return box


def _coordinate_space_for_box(source: str, layout: dict[str, Any]) -> str:
    if source == "viewport_rect":
        return "viewport"
    if source == "document_rect":
        return "document"
    explicit = str(layout.get("coordinate_space") or "").strip().lower()
    return explicit if explicit in {"viewport", "document", "image"} else "viewport"


def _png_dimensions_from_base64(encoded: str) -> tuple[int, int] | None:
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(decoded) < 24 or not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    width = int.from_bytes(decoded[16:20], "big")
    height = int.from_bytes(decoded[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def _capture_error_summary(errors: list[str]) -> str:
    clean = [item for item in errors if item]
    if not clean:
        return "Screenshot capture failed."
    return f"Screenshot capture failed after {len(clean)} attempt(s): {'; '.join(clean[-4:])}"


def _schedule_event_post(client: DashboardApiClient, event: dict[str, Any]) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def post() -> None:
        async with _suppress_async_errors():
            await client.append_event(event)

    if client._closed:  # noqa: SLF001
        return
    client._track_background_task(loop.create_task(post()))  # noqa: SLF001


def _pending_summary(record: PendingReviewRecord) -> str:
    action = str(record.action or "action").replace("_", " ")
    framework = str(record.framework or "agent")
    reason = str(record.reason or "").strip()
    return f"{framework} requested {action}: {reason}" if reason else f"{framework} requested {action}"


def _decision_summary(status: str, reason: str | None) -> str:
    clean_status = str(status or "decision").replace("_", " ")
    clean_reason = str(reason or "").strip()
    return f"{clean_status}: {clean_reason}" if clean_reason else clean_status


def _decision_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    return status if status in {"allow", "block"} else "block"


def _review_result_status(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "allow" if value else "block"
    normalized = str(value).strip().lower()
    if normalized in {"allow", "allowed", "yes", "y", "true", "1"}:
        return "allow"
    if normalized in {"block", "blocked", "deny", "denied", "no", "n", "false", "0", ""}:
        return "block"
    return "block"


def _payload_summary(payload: str | bytes) -> dict[str, Any]:
    text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"snippet": redact_value(text[:240])}
    if not isinstance(parsed, dict):
        return {"snippet": redact_value(text[:240])}
    params = parsed.get("params") if isinstance(parsed.get("params"), dict) else None
    return redact_value({"method": parsed.get("method"), "id": parsed.get("id"), "params": params})


async def _response_payload(response: Any) -> Any:
    text = await response.text()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DashboardApiError("Dashboard API response was not valid JSON.") from exc


class _suppress_async_errors:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        return True


def _positive_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _validate_base_url(base_url: str) -> None:
    try:
        parsed = urlsplit(base_url)
    except ValueError as exc:
        raise ValueError("dashboard.base_url must be a valid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("dashboard.base_url must be an http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("dashboard.base_url must not include credentials")
