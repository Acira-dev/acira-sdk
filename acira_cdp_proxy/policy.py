from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


AGENT_TO_BROWSER = "AGENT -> BROWSER"
BROWSER_TO_AGENT = "BROWSER -> AGENT"

CDPProbe = Callable[[str, dict[str, Any] | None, str | None], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class CDPMessageContext:
    direction: str
    connection_id: str
    message_number: int
    websocket_path: str
    parsed_message: dict[str, Any] | list[Any] | None = None
    send_probe: CDPProbe | None = field(default=None, repr=False, compare=False)

    @property
    def method(self) -> str | None:
        if isinstance(self.parsed_message, dict):
            method = self.parsed_message.get("method")
            if isinstance(method, str):
                return method
        return None

    @property
    def message_id(self) -> int | str | None:
        if isinstance(self.parsed_message, dict):
            return self.parsed_message.get("id")
        return None

    @property
    def session_id(self) -> str | None:
        if isinstance(self.parsed_message, dict):
            session_id = self.parsed_message.get("sessionId")
            if isinstance(session_id, str):
                return session_id
        return None

    async def probe(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        if self.send_probe is None:
            return None
        return await self.send_probe(method, params, self.session_id if session_id is None else session_id)


@dataclass(frozen=True)
class CDPPolicyDecision:
    allow: bool = True
    reason: str | None = None
    replacement_payload: str | bytes | None = None
    metadata: dict[str, Any] | None = None


class CDPPolicy(Protocol):
    async def decide(self, payload: str | bytes, context: CDPMessageContext) -> CDPPolicyDecision:
        """Return whether a CDP payload should be relayed."""


class PassiveCDPPolicy:
    async def decide(self, payload: str | bytes, context: CDPMessageContext) -> CDPPolicyDecision:
        return CDPPolicyDecision(allow=True)
