from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


ADAPTER_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
HELPER_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
ACTION_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class AdapterRuntimeConfig:
    enabled: bool = True
    trusted_helpers: tuple[str, ...] = ()
    hitl_freeze_mode: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionCapability:
    opens_cdp_followup_transaction: bool = False
    release_pause_on_review_exit: bool = False
    supervised_runtime: bool = False


@dataclass(frozen=True)
class RuntimeHelperContext:
    method: str
    expression: str
    params: dict[str, Any]
    source_field: str
    target_hint: str | None
    cdp_context: Any
    policy: Any
    adapter_id: str
    helper_id: str
    full_helper_id: str
    config: AdapterRuntimeConfig


RuntimeHelperClassifier = Callable[[RuntimeHelperContext], Any | Awaitable[Any]]


@dataclass(frozen=True)
class RuntimeHelperSpec:
    id: str
    classify: RuntimeHelperClassifier
    requires_trust: bool = False
    description: str | None = None


@dataclass(frozen=True)
class FrameworkAdapterSpec:
    id: str
    display_name: str
    runtime_helpers: tuple[RuntimeHelperSpec, ...] = ()
    ignored_selectors: tuple[str, ...] = ()
    action_capabilities: Mapping[str, ActionCapability] = field(default_factory=dict)
    default_trusted_helpers: tuple[str, ...] = ()


class FrameworkAdapterRegistry:
    def __init__(
        self,
        adapters: tuple[FrameworkAdapterSpec, ...] = (),
        *,
        config: Mapping[str, AdapterRuntimeConfig] | None = None,
    ) -> None:
        self._adapters: dict[str, FrameworkAdapterSpec] = {}
        self._config = dict(config or {})
        self._helper_index: dict[str, tuple[FrameworkAdapterSpec, RuntimeHelperSpec]] = {}

        for adapter in adapters:
            self.register(adapter)
        self._validate_config_references()

    @property
    def adapters(self) -> tuple[FrameworkAdapterSpec, ...]:
        return tuple(self._adapters.values())

    def register(self, adapter: FrameworkAdapterSpec) -> None:
        _validate_adapter(adapter)
        if adapter.id in self._adapters:
            raise ValueError(f"Duplicate framework adapter id: {adapter.id!r}")
        self._adapters[adapter.id] = adapter
        for helper in adapter.runtime_helpers:
            full_id = namespaced_helper_id(adapter.id, helper.id)
            if full_id in self._helper_index:
                raise ValueError(f"Duplicate runtime helper id: {full_id!r}")
            self._helper_index[full_id] = (adapter, helper)

    def adapter_config(self, adapter_id: str) -> AdapterRuntimeConfig:
        adapter = self._adapters.get(adapter_id)
        config = self._config.get(adapter_id)
        if config is not None:
            return config
        default_trusted = adapter.default_trusted_helpers if adapter is not None else ()
        return AdapterRuntimeConfig(trusted_helpers=tuple(default_trusted))

    def action_capability(self, framework_id: str, action: str) -> ActionCapability:
        adapter = self._adapters.get(framework_id)
        if adapter is None:
            return ActionCapability()
        config = self.adapter_config(framework_id)
        if not config.enabled:
            return ActionCapability()
        return adapter.action_capabilities.get(action, ActionCapability())

    def ignored_selectors(self) -> tuple[str, ...]:
        selectors: list[str] = []
        seen: set[str] = set()
        for adapter in self._adapters.values():
            if not self.adapter_config(adapter.id).enabled:
                continue
            for selector in adapter.ignored_selectors:
                if selector not in seen:
                    seen.add(selector)
                    selectors.append(selector)
        return tuple(selectors)

    async def classify_runtime_helper(self, context: RuntimeHelperContext) -> Any | None:
        for adapter in self._adapters.values():
            config = self.adapter_config(adapter.id)
            if not config.enabled:
                continue
            for helper in adapter.runtime_helpers:
                full_id = namespaced_helper_id(adapter.id, helper.id)
                if helper.requires_trust and full_id not in _trusted_helper_ids(adapter, config):
                    continue
                helper_context = RuntimeHelperContext(
                    method=context.method,
                    expression=context.expression,
                    params=context.params,
                    source_field=context.source_field,
                    target_hint=context.target_hint,
                    cdp_context=context.cdp_context,
                    policy=context.policy,
                    adapter_id=adapter.id,
                    helper_id=helper.id,
                    full_helper_id=full_id,
                    config=config,
                )
                result = helper.classify(helper_context)
                if inspect.isawaitable(result):
                    result = await result
                if result is not None:
                    return result
        return None

    def _validate_config_references(self) -> None:
        unknown = sorted(set(self._config) - set(self._adapters))
        if unknown:
            raise ValueError(f"Config references unregistered framework adapters: {unknown}")
        for adapter_id, config in self._config.items():
            adapter = self._adapters[adapter_id]
            for helper_id in config.trusted_helpers:
                full_id = _normalize_config_helper_id(adapter.id, helper_id)
                if full_id not in self._helper_index:
                    raise ValueError(f"Adapter {adapter.id!r} config references unknown helper: {helper_id!r}")
                helper_adapter, helper = self._helper_index[full_id]
                if helper_adapter.id != adapter.id or not helper.requires_trust:
                    raise ValueError(f"Adapter {adapter.id!r} cannot trust helper: {helper_id!r}")


def namespaced_helper_id(adapter_id: str, helper_id: str) -> str:
    return f"{adapter_id}.{helper_id}"


def parse_adapter_configs(raw: Any) -> dict[str, AdapterRuntimeConfig]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TypeError("adapters must be an object")
    result: dict[str, AdapterRuntimeConfig] = {}
    for adapter_id, value in raw.items():
        normalized_id = str(adapter_id).strip().lower().replace("-", "_").replace(" ", "_")
        if not ADAPTER_ID_RE.match(normalized_id):
            raise ValueError(f"Invalid adapter id: {adapter_id!r}")
        if not isinstance(value, dict):
            raise TypeError(f"Adapter config for {adapter_id!r} must be an object")
        if normalized_id in result:
            raise ValueError(f"Duplicate adapter config id: {normalized_id!r}")
        result[normalized_id] = AdapterRuntimeConfig(
            enabled=bool(value.get("enabled", True)),
            trusted_helpers=_normalize_trusted_helpers(value.get("trusted_helpers", ())),
            hitl_freeze_mode=_optional_hitl_freeze_mode(value.get("hitl_freeze_mode")),
            raw=dict(value),
        )
    return result


def _validate_adapter(adapter: FrameworkAdapterSpec) -> None:
    if not ADAPTER_ID_RE.match(adapter.id):
        raise ValueError(f"Invalid framework adapter id: {adapter.id!r}")
    if not adapter.display_name.strip():
        raise ValueError(f"Framework adapter {adapter.id!r} is missing display_name")
    helper_ids: set[str] = set()
    for helper in adapter.runtime_helpers:
        if not HELPER_ID_RE.match(helper.id):
            raise ValueError(f"Invalid helper id for adapter {adapter.id!r}: {helper.id!r}")
        if helper.id in helper_ids:
            raise ValueError(f"Duplicate helper id for adapter {adapter.id!r}: {helper.id!r}")
        helper_ids.add(helper.id)
    for selector in adapter.ignored_selectors:
        if not isinstance(selector, str) or not selector.strip() or "\x00" in selector:
            raise ValueError(f"Invalid ignored selector for adapter {adapter.id!r}: {selector!r}")
    for action in adapter.action_capabilities:
        if not ACTION_RE.match(action):
            raise ValueError(f"Invalid action capability for adapter {adapter.id!r}: {action!r}")
    for helper_id in adapter.default_trusted_helpers:
        if not HELPER_ID_RE.match(helper_id):
            raise ValueError(f"Invalid default trusted helper for adapter {adapter.id!r}: {helper_id!r}")


def _normalize_trusted_helpers(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raise TypeError("adapter.trusted_helpers must be a string or list")
    normalized: list[str] = []
    for item in raw:
        helper = str(item).strip().lower().replace("-", "_").replace(" ", "_")
        if "." in helper:
            adapter_id, helper_id = helper.rsplit(".", 1)
            if not ADAPTER_ID_RE.match(adapter_id) or not HELPER_ID_RE.match(helper_id):
                raise ValueError(f"Invalid trusted helper id: {item!r}")
        elif not HELPER_ID_RE.match(helper):
            raise ValueError(f"Invalid trusted helper id: {item!r}")
        if helper not in normalized:
            normalized.append(helper)
    return tuple(normalized)


def _optional_hitl_freeze_mode(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if normalized not in {"strict", "timeout_only", "off"}:
        raise ValueError(f"Unsupported adapter hitl_freeze_mode: {value!r}")
    return normalized


def _normalize_config_helper_id(adapter_id: str, helper_id: str) -> str:
    helper = str(helper_id).strip().lower().replace("-", "_").replace(" ", "_")
    if "." in helper:
        return helper
    return namespaced_helper_id(adapter_id, helper)


def _trusted_helper_ids(adapter: FrameworkAdapterSpec, config: AdapterRuntimeConfig) -> set[str]:
    configured = config.trusted_helpers or adapter.default_trusted_helpers
    return {_normalize_config_helper_id(adapter.id, helper_id) for helper_id in configured}
