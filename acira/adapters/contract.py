"""The standard contract every Acira adapter must satisfy.

The core is fully adapter-agnostic: it never imports a concrete adapter (Playwright,
browser-use, ...) by name. Instead it speaks to this contract. An adapter is any module
that exposes a ``create`` factory returning an :class:`AdapterContext`. New adapters drop
in by conforming to this contract and being declared in config — no core changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


#: Name of the module-level factory callable the core looks for on every adapter module.
ADAPTER_FACTORY_NAME = "create"


@runtime_checkable
class AdapterContext(Protocol):
    """The ``guard`` object an adapter factory returns.

    It is an async context manager. Entering it activates Acira's interception for the
    adapter's framework (pause / intercept actions / resume); exiting it restores the
    framework to its original state. Adapters may attach extra framework-specific helpers
    to the returned object (e.g. Playwright's ``patch_playwright``); the core never calls
    those — only the developer's own code does.
    """

    async def __aenter__(self) -> Any: ...

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any: ...


class AdapterFactory(Protocol):
    """Signature of the module-level ``create`` callable every adapter must expose.

    The core calls this with the resolved config plus any injected runtime options
    (review backend, runtime control, ...) and receives back an :class:`AdapterContext`.
    """

    def __call__(
        self,
        *,
        config: Any = None,
        log_file: str | Path | None = None,
        checkpoint_store: Any = None,
        **options: Any,
    ) -> AdapterContext: ...
