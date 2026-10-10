from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .redaction import SECRET_REDACTION_REGISTRY


SECRET_POLICY_DECISIONS = {"allow", "ask", "block"}


class SecretBrokerError(RuntimeError):
    """Raised when a secret cannot be used safely."""


@dataclass(frozen=True)
class SecretPolicy:
    decision: str | None = None
    reason: str | None = None

    def effective_decision(self, default_decision: str) -> str:
        return _normalize_decision(self.decision or default_decision)

    def to_metadata(self, *, default_decision: str) -> dict[str, Any]:
        decision = self.effective_decision(default_decision)
        metadata: dict[str, Any] = {"decision": decision}
        if self.reason:
            metadata["reason"] = self.reason
        return metadata


@dataclass(frozen=True)
class SecretProviderDefinition:
    id: str
    kind: str
    options: dict[str, Any] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class SecretRecord:
    id: str
    label: str
    provider_id: str
    provider_ref: str
    active: bool = False
    tags: tuple[str, ...] = ()
    classification: str | None = None
    policy: SecretPolicy = SecretPolicy()

    def to_metadata(self, *, default_decision: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "provider": self.provider_id,
            "ref": self.provider_ref,
            "active": self.active,
            "tags": list(self.tags),
            "classification": self.classification,
            "policy": self.policy.to_metadata(default_decision=default_decision),
        }

    def to_catalog_metadata(self, *, default_decision: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "classification": self.classification,
            "tags": list(self.tags),
            "policy_decision": self.policy.effective_decision(default_decision),
            "active": True,
        }


@dataclass(frozen=True)
class AciraSecretsConfig:
    enabled: bool = False
    default_decision: str = "ask"
    providers: dict[str, SecretProviderDefinition] = field(default_factory=dict)
    records: dict[str, SecretRecord] = field(default_factory=dict)

    def provider(self, provider_id: str) -> SecretProviderDefinition | None:
        return self.providers.get(provider_id)

    def record(self, secret_id: str) -> SecretRecord | None:
        return self.records.get(secret_id)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "default_decision": self.default_decision,
            "providers": [provider.to_metadata() for provider in self.providers.values()],
            "secrets": [
                record.to_metadata(default_decision=self.default_decision)
                for record in self.records.values()
            ],
        }


@dataclass(frozen=True)
class SecretUseMetadata:
    id: str
    label: str
    provider_id: str
    provider_ref: str
    tags: tuple[str, ...]
    classification: str | None
    policy_decision: str
    policy_reason: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "provider": self.provider_id,
            "ref": self.provider_ref,
            "tags": list(self.tags),
            "classification": self.classification,
            "policy_decision": self.policy_decision,
        }
        if self.policy_reason:
            metadata["policy_reason"] = self.policy_reason
        return metadata


class SecretProvider(Protocol):
    id: str

    def resolve(self, record: SecretRecord) -> str:
        """Return plaintext for a single approved use."""


class MockSecretProvider:
    """Local test provider. Production providers should live behind this interface."""

    def __init__(self, provider_id: str, values: Mapping[str, Any]) -> None:
        self.id = provider_id
        self._values = dict(values)

    def resolve(self, record: SecretRecord) -> str:
        value = self._values.get(record.provider_ref)
        if not isinstance(value, str) or value == "":
            raise SecretBrokerError(f'Secret provider "{self.id}" could not resolve secret ref "{record.provider_ref}".')
        return value


class EnvFileProvider:
    """Resolves plaintext just-in-time from a per-project .env file (KEY=value).

    The value is read into memory only for the single fill operation;
    ``SecretBroker.resolve_for_fill`` then registers it for redaction so the model
    never sees the plaintext.
    """

    def __init__(self, provider_id: str, env_path: str) -> None:
        self.id = provider_id
        self._env_path = env_path

    def resolve(self, record: SecretRecord) -> str:
        from pathlib import Path

        value = _read_env_file_key(Path(self._env_path), record.provider_ref)
        if not isinstance(value, str) or value == "":
            raise SecretBrokerError(
                f'Secret provider "{self.id}" could not resolve secret ref "{record.provider_ref}" '
                f'in env file "{self._env_path}".'
            )
        return value


ProviderFactory = Callable[[SecretProviderDefinition], SecretProvider | None]


class SecretProviderRegistry:
    def __init__(
        self,
        providers: Mapping[str, SecretProviderDefinition],
        *,
        factories: Mapping[str, ProviderFactory] | None = None,
    ) -> None:
        self._providers: dict[str, SecretProvider] = {}
        self._factories = dict(
            factories
            or {
                "mock": _mock_provider_factory,
                "local_plaintext": _mock_provider_factory,
                "plaintext": _mock_provider_factory,
                "env_file": _env_file_provider_factory,
            }
        )
        for provider in providers.values():
            factory = self._factories.get(provider.kind)
            if factory is None:
                continue
            instance = factory(provider)
            if instance is not None:
                self._providers[provider.id] = instance

    def get(self, provider_id: str) -> SecretProvider | None:
        return self._providers.get(provider_id)


class SecretBroker:
    """Authorizes metadata-only secret use and releases plaintext just in time."""

    def __init__(
        self,
        config: AciraSecretsConfig,
        *,
        provider_registry: SecretProviderRegistry | None = None,
        redaction_registry: Any = SECRET_REDACTION_REGISTRY,
    ) -> None:
        self.config = config
        self.provider_registry = provider_registry or SecretProviderRegistry(config.providers)
        self.redaction_registry = redaction_registry

    def list_catalog(self) -> list[dict[str, Any]]:
        if not self.config.enabled:
            return []
        return [
            record.to_catalog_metadata(default_decision=self.config.default_decision)
            for record in self.config.records.values()
            if record.active
        ]

    def metadata_for_fill(self, secret_id: str) -> SecretUseMetadata:
        record = self._active_record(secret_id)
        if self.config.provider(record.provider_id) is None:
            raise SecretBrokerError(f'Secret "{record.id}" references unknown provider "{record.provider_id}".')
        if self.provider_registry.get(record.provider_id) is None:
            raise SecretBrokerError(f'Secret provider "{record.provider_id}" is not available.')
        decision = record.policy.effective_decision(self.config.default_decision)
        return SecretUseMetadata(
            id=record.id,
            label=record.label,
            provider_id=record.provider_id,
            provider_ref=record.provider_ref,
            tags=record.tags,
            classification=record.classification,
            policy_decision=decision,
            policy_reason=record.policy.reason,
        )

    def resolve_for_fill(self, secret_use: SecretUseMetadata) -> str:
        record = self._active_record(secret_use.id)
        if record.provider_id != secret_use.provider_id or record.provider_ref != secret_use.provider_ref:
            raise SecretBrokerError("Secret metadata no longer matches the configured secret record.")
        provider = self.provider_registry.get(record.provider_id)
        if provider is None:
            raise SecretBrokerError(f'Secret provider "{record.provider_id}" is not available.')
        plaintext = provider.resolve(record)
        registered = self.redaction_registry.register_secret(
            secret_id=record.id,
            label=record.label,
            value=plaintext,
        )
        if not registered:
            raise SecretBrokerError("Secret redaction registration failed; refusing to release plaintext.")
        return plaintext

    def register_field_for_fill(self, secret_use: SecretUseMetadata, target_metadata: dict[str, Any] | None) -> None:
        record = self._active_record(secret_use.id)
        if record.provider_id != secret_use.provider_id or record.provider_ref != secret_use.provider_ref:
            raise SecretBrokerError("Secret metadata no longer matches the configured secret record.")
        registered = self.redaction_registry.register_secret_field(
            secret_id=record.id,
            label=record.label,
            target=target_metadata,
        )
        if not registered:
            raise SecretBrokerError("Secret field redaction registration failed; refusing to release plaintext.")

    def _record(self, secret_id: str) -> SecretRecord:
        if not self.config.enabled:
            raise SecretBrokerError("Acira Secrets Manager is disabled.")
        normalized_id = str(secret_id or "").strip()
        if not normalized_id:
            raise SecretBrokerError("Secret id is required.")
        record = self.config.record(normalized_id)
        if record is None:
            raise SecretBrokerError(f'Secret "{normalized_id}" is not configured.')
        return record

    def _active_record(self, secret_id: str) -> SecretRecord:
        record = self._record(secret_id)
        if not record.active:
            raise SecretBrokerError(f'Secret "{record.id}" is not active.')
        return record


def parse_secrets_config(data: Any) -> AciraSecretsConfig:
    if data is None:
        return AciraSecretsConfig()
    if isinstance(data, bool):
        return AciraSecretsConfig(enabled=data)
    if not isinstance(data, dict):
        raise TypeError("secrets must be an object")

    default_decision = _normalize_decision(data.get("default_decision", "ask"))
    providers = _parse_providers(data.get("providers"))
    records = _parse_records(
        data.get("items")
        if "items" in data
        else data.get("entries")
        if "entries" in data
        else data.get("secrets"),
        providers=providers,
    )
    return AciraSecretsConfig(
        enabled=bool(data.get("enabled", False)),
        default_decision=default_decision,
        providers=providers,
        records=records,
    )


def _parse_providers(data: Any) -> dict[str, SecretProviderDefinition]:
    if data is None:
        return {}
    providers: dict[str, SecretProviderDefinition] = {}
    raw_items: list[tuple[str | None, Any]]
    if isinstance(data, dict):
        raw_items = [(str(key), value) for key, value in data.items()]
    elif isinstance(data, list):
        raw_items = [(None, item) for item in data]
    else:
        raise TypeError("secrets.providers must be an object or list")

    for key, item in raw_items:
        if not isinstance(item, dict):
            raise TypeError("Each secret provider must be an object")
        provider_id = _required_string(item.get("id", key), "secret provider id")
        kind = _required_string(item.get("kind", item.get("type", "mock")), f'secret provider "{provider_id}" type')
        options = dict(item)
        options.pop("id", None)
        options.pop("kind", None)
        options.pop("type", None)
        if provider_id in providers:
            raise ValueError(f'Duplicate secret provider id "{provider_id}".')
        providers[provider_id] = SecretProviderDefinition(
            id=provider_id,
            kind=kind,
            options=options,
        )
    return providers


def _parse_records(data: Any, *, providers: dict[str, SecretProviderDefinition]) -> dict[str, SecretRecord]:
    if data is None:
        return {}
    if not isinstance(data, list):
        raise TypeError("secrets.items must be a list")
    records: dict[str, SecretRecord] = {}
    for item in data:
        if not isinstance(item, dict):
            raise TypeError("Each secret entry must be an object")
        secret_id = _required_string(item.get("id"), "secret id")
        label = _optional_string(item.get("label", item.get("display_label"))) or secret_id
        inline_value = item.get("value", item.get("plaintext", item.get("plain_text")))
        provider_id = _optional_string(item.get("provider", item.get("provider_id")))
        provider_ref = _optional_string(item.get("ref", item.get("provider_ref")))
        if isinstance(inline_value, str):
            provider_id = provider_id or "acira-local-plaintext"
            provider_ref = provider_ref or f"inline:{secret_id}"
            _register_inline_plaintext_secret(providers, provider_id, provider_ref, inline_value)
        else:
            provider_id = _required_string(provider_id, f'secret "{secret_id}" provider')
            provider_ref = _required_string(provider_ref, f'secret "{secret_id}" ref')
        policy = _parse_policy(item.get("policy"))
        active = bool(item.get("active", False))
        tags = tuple(
            str(value).strip()
            for value in item.get("tags", ())
            if str(value).strip()
        )
        classification = _optional_string(item.get("classification", item.get("class")))
        if secret_id in records:
            raise ValueError(f'Duplicate secret id "{secret_id}".')
        records[secret_id] = SecretRecord(
            id=secret_id,
            label=label,
            provider_id=provider_id,
            provider_ref=provider_ref,
            active=active,
            tags=tags,
            classification=classification,
            policy=policy,
        )
    return records


def _register_inline_plaintext_secret(
    providers: dict[str, SecretProviderDefinition],
    provider_id: str,
    provider_ref: str,
    value: str,
) -> None:
    provider = providers.get(provider_id)
    if provider is None:
        provider = SecretProviderDefinition(
            id=provider_id,
            kind="local_plaintext",
            options={"values": {}},
        )
        providers[provider_id] = provider
    if provider.kind not in {"mock", "local_plaintext", "plaintext"}:
        raise ValueError(f'Inline plaintext secret cannot be stored in provider "{provider_id}" of type "{provider.kind}".')
    values = provider.options.setdefault("values", {})
    if not isinstance(values, dict):
        raise TypeError(f'Secret provider "{provider_id}" values must be an object for local plaintext secrets.')
    values[provider_ref] = value


def _parse_policy(data: Any) -> SecretPolicy:
    if data is None:
        return SecretPolicy()
    if isinstance(data, str):
        return SecretPolicy(decision=_normalize_decision(data))
    if not isinstance(data, dict):
        raise TypeError("secret policy must be an object or decision string")
    decision = data.get("decision")
    return SecretPolicy(
        decision=_normalize_decision(decision) if decision is not None else None,
        reason=_optional_string(data.get("reason")),
    )


def _mock_provider_factory(provider: SecretProviderDefinition) -> SecretProvider:
    values = provider.options.get("values")
    if values is None:
        values = provider.options.get("secrets")
    if not isinstance(values, Mapping):
        values = {}
    return MockSecretProvider(provider.id, values)


def _env_file_provider_factory(provider: SecretProviderDefinition) -> SecretProvider | None:
    env_path = provider.options.get("env_path") or provider.options.get("path")
    if not isinstance(env_path, str) or not env_path.strip():
        raise ValueError(f'Secret provider "{provider.id}" (env_file) requires an "env_path".')
    return EnvFileProvider(provider.id, env_path.strip())


def _read_env_file_key(path: "Path", key: str) -> str | None:
    """Parse a dotenv-style KEY=value file and return the value for key."""
    from pathlib import Path as _Path

    p = _Path(path)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        if k == key:
            return v
    return None


def _normalize_decision(value: Any) -> str:
    decision = str(value or "").strip().lower()
    if decision not in SECRET_POLICY_DECISIONS:
        raise ValueError(f"Unsupported secret decision: {value!r}")
    return decision


def _required_string(value: Any, label: str) -> str:
    text = _optional_string(value)
    if not text:
        raise ValueError(f"{label} is required")
    return text


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
