"""Adapter discovery and dynamic loading.

The core never hardcodes a framework name. Adapters are found by convention (any module
under ``acira.adapters`` that exposes a ``create`` factory) or declared explicitly in
config via a ``module``/``entry`` dotted path (for adapters shipped in external packages).
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any

from acira_cdp_proxy.checkpoint import CheckpointStore
from acira_cdp_proxy.config import AciraGuardConfig

from .contract import ADAPTER_FACTORY_NAME, AdapterContext, AdapterFactory


class AciraAdapterConfigurationError(RuntimeError):
    """Raised when Acira cannot safely select or load a configured adapter."""


#: Modules under ``acira.adapters`` that are infrastructure, not adapters.
_NON_ADAPTER_MODULES = {"contract", "playwright_boundary", "playwright_operations",
                        "playwright_submission", "playwright_uploads", "browser_use_operations"}


def _normalize_adapter_id(value: str) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def discover_adapter_ids() -> set[str]:
    """Return the ids of adapters shipped under ``acira.adapters``.

    Pure discovery: every importable submodule (excluding private modules and the
    contract) is a candidate adapter id. Dropping a new ``acira/adapters/<name>.py`` that
    exposes ``create`` makes ``<name>`` selectable in config with zero core edits.
    """
    package_dir = Path(__file__).resolve().parent
    ids: set[str] = set()
    for info in pkgutil.iter_modules([str(package_dir)]):
        name = info.name
        if name.startswith("_") or name in _NON_ADAPTER_MODULES:
            continue
        ids.add(_normalize_adapter_id(name))
    return ids


def _resolve_adapter_module(adapter_id: str, *, module_path: str | None) -> Any:
    target = module_path or f"{__name__}.{adapter_id}"
    try:
        return importlib.import_module(target)
    except ImportError as exc:
        raise AciraAdapterConfigurationError(
            f"Could not import Acira adapter {adapter_id!r} (module {target!r}): {exc}"
        ) from exc


def load_adapter(
    adapter_id: str,
    *,
    config: AciraGuardConfig,
    log_file: str | Path | None,
    checkpoint_store: CheckpointStore | None,
    module_path: str | None = None,
    options: dict[str, Any] | None = None,
) -> AdapterContext:
    """Dynamically import an adapter and build its ``guard`` context.

    ``module_path`` overrides the convention-based module location, letting an adapter
    declared in config (``adapters.<id>.module``) live in any installed package.
    """
    normalized = _normalize_adapter_id(adapter_id)
    module = _resolve_adapter_module(normalized, module_path=module_path)
    factory: AdapterFactory | None = getattr(module, ADAPTER_FACTORY_NAME, None)
    if not callable(factory):
        raise AciraAdapterConfigurationError(
            f"Acira adapter {normalized!r} does not expose a callable "
            f"{ADAPTER_FACTORY_NAME!r} factory; it does not satisfy the adapter contract."
        )
    return factory(
        config=config,
        log_file=log_file,
        checkpoint_store=checkpoint_store,
        **(options or {}),
    )


__all__ = [
    "AciraAdapterConfigurationError",
    "AdapterContext",
    "AdapterFactory",
    "discover_adapter_ids",
    "load_adapter",
]
