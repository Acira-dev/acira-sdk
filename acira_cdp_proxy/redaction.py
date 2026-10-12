from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SECRET_KEYS = {
    "authorization",
    "card",
    "card_number",
    "cc",
    "code",
    "credential",
    "cvv",
    "cvc",
    "otp",
    "pass",
    "password",
    "secret",
    "token",
    "value",
}
SAFE_DIAGNOSTIC_KEYS: frozenset[str] = frozenset()

TOKEN_LIKE_RE = re.compile(r"(?i)\b(?:sk|pk|api|key|token|secret)[-_]?[a-z0-9]{12,}\b")
CARD_LIKE_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
OTP_LIKE_RE = re.compile(r"^\s*\d{4,8}\s*$")
URL_RE = re.compile(r"(?i)\b(?:https?|wss?)://[^\s\"'<>]+")


@dataclass(frozen=True)
class _RegisteredSecret:
    secret_id: str
    label: str
    values: tuple[str, ...]
    expires_at: float


@dataclass(frozen=True)
class _RegisteredSecretField:
    secret_id: str
    label: str
    target: dict[str, Any]
    aliases: tuple[str, ...]
    expires_at: float


class SecretRedactionRegistry:
    """Bounded process-local redaction across independent projects/integrations.

    Ending one guard must not erase another guard's redaction. Expiry cannot remove
    masking during a guarded run. After the last run closes, entries expire within
    one hour. A full registry rejects new plaintext releases instead of evicting
    still-needed protection. Secret IDs alone are not globally unique.
    """

    def __init__(self, *, max_secrets: int = 4096, max_fields: int = 4096, max_bytes: int = 8 * 1024 * 1024) -> None:
        if min(max_secrets, max_fields, max_bytes) < 1:
            raise ValueError("Secret redaction limits must be positive")
        self._entries: dict[tuple[str, tuple[str, ...]], _RegisteredSecret] = {}
        self._fields: dict[str, _RegisteredSecretField] = {}
        self._max_secrets, self._max_fields, self._max_bytes = max_secrets, max_fields, max_bytes
        self._secret_bytes = 0
        self._lock = threading.RLock()
        self._runs: set[object] = set()

    def begin_run(self) -> object:
        with self._lock:
            self._prune()
            if len(self._runs) >= 4096:
                raise RuntimeError("Too many active Acira guards in one process")
            token = object()
            self._runs.add(token)
            return token

    def end_run(self, token: object | None) -> None:
        with self._lock:
            if token not in self._runs:
                return
            self._runs.remove(token)
            if not self._runs:
                # Cleanup/audit tails may still contain a released secret. Keep
                # their masking for a bounded grace period, never raw on exit.
                expiry = time.monotonic() + 3600
                self._entries = {key: replace(entry, expires_at=expiry) for key, entry in self._entries.items()}
                self._fields = {key: replace(entry, expires_at=expiry) for key, entry in self._fields.items()}

    def register_secret(
        self,
        *,
        secret_id: str,
        label: str,
        value: str,
        ttl_seconds: float = 3600.0,
    ) -> bool:
        if not isinstance(value, str) or len(value) > 65536:
            return False
        variants = _secret_value_variants(value)
        if not variants:
            return False
        normalized_id = str(secret_id or "").strip()
        if not normalized_id or len(normalized_id) > 4096 or len(str(label)) > 4096:
            return False
        expiry = self._expiry(ttl_seconds)
        if expiry is None:
            return False
        key = normalized_id, variants
        size = sum(len(item.encode("utf-8")) for item in variants)
        with self._lock:
            self._prune()
            if key not in self._entries:
                if len(self._entries) >= self._max_secrets or self._secret_bytes + size > self._max_bytes:
                    return False
                self._secret_bytes += size
            self._entries[key] = _RegisteredSecret(
                secret_id=normalized_id, label=str(label or normalized_id),
                values=variants, expires_at=expiry,
            )
            return True

    def register_secret_field(
        self,
        *,
        secret_id: str,
        label: str,
        target: dict[str, Any] | None,
        ttl_seconds: float = 3600.0,
    ) -> bool:
        normalized_id = str(secret_id or "").strip()
        if not normalized_id or len(normalized_id) > 4096 or len(str(label)) > 4096 or not isinstance(target, dict):
            return False
        try:
            if len(json.dumps(target, ensure_ascii=True)) > 65536:
                return False
        except (TypeError, ValueError):
            return False
        expiry = self._expiry(ttl_seconds)
        if expiry is None:
            return False
        aliases = _secret_field_aliases(target)
        if not aliases:
            return False
        field_id = _secret_field_id(normalized_id, target)
        with self._lock:
            self._prune()
            if field_id not in self._fields and len(self._fields) >= self._max_fields:
                return False
            self._fields[field_id] = _RegisteredSecretField(
                secret_id=normalized_id, label=str(label or normalized_id),
                target=_metadata_without_empty_values(target), aliases=aliases, expires_at=expiry,
            )
            return True

    def redact_text(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return text
        with self._lock:
            self._prune()
            entries = list(self._entries.values())
        redacted = text
        for entry in sorted(
            entries,
            key=lambda item: max((len(value) for value in item.values), default=0),
            reverse=True,
        ):
            for value in entry.values:
                redacted = redacted.replace(value, "[REDACTED:secret]")
        return redacted

    def contains(self, text: str) -> bool:
        if not isinstance(text, str) or not text:
            return False
        with self._lock:
            self._prune()
            return any(value in text for entry in self._entries.values() for value in entry.values)

    def secret_field_read_match(self, source: str) -> dict[str, Any] | None:
        if not isinstance(source, str) or not source:
            return None
        if not _looks_like_field_value_read(source):
            return None
        with self._lock:
            self._prune()
            fields = list(self._fields.values())
        normalized = source.lower()
        for field in fields:
            if any(alias and alias in normalized for alias in field.aliases):
                return {
                    "secret_id": field.secret_id,
                    "label": field.label,
                    "target": field.target,
                }
        return None

    def secret_field_count(self) -> int:
        with self._lock:
            self._prune()
            return len(self._fields)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._fields.clear()
            self._secret_bytes = 0

    @staticmethod
    def _expiry(ttl_seconds: float) -> float | None:
        try:
            ttl = float(ttl_seconds)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(ttl):
            return None
        return time.monotonic() + min(3600.0, max(1.0, ttl))

    def _prune(self) -> None:
        if self._runs:
            return
        now = time.monotonic()
        for key, entry in list(self._entries.items()):
            if entry.expires_at <= now:
                self._entries.pop(key, None)
                self._secret_bytes -= sum(len(item.encode("utf-8")) for item in entry.values)
        for key, entry in list(self._fields.items()):
            if entry.expires_at <= now:
                self._fields.pop(key, None)


SECRET_REDACTION_REGISTRY = SecretRedactionRegistry()


def _number_text(value: int | float) -> str:
    return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)


def _redact_number(value: int | float, *, redact_generic: bool = True) -> Any:
    text = _number_text(value)
    if SECRET_REDACTION_REGISTRY.contains(text):
        return "[REDACTED:secret]"
    if redact_generic and CARD_LIKE_RE.fullmatch(text):
        return "[REDACTED]"
    return value


def redact_value(value: Any) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _redact_number(value)
    if isinstance(value, str):
        redacted = SECRET_REDACTION_REGISTRY.redact_text(value)
        if redacted != value:
            return redacted
        redacted = _redact_sensitive_url_queries(value)
        if redacted != value:
            return redacted
        if _looks_sensitive(value):
            return "[REDACTED]"
        return value
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, dict):
        value = _redact_dialog_protocol_fields(value)
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in SAFE_DIAGNOSTIC_KEYS:
                redacted[key_text] = redact_value(item)
            elif _key_is_sensitive(key_text):
                redacted[key_text] = "[REDACTED]"
            else:
                redacted[key_text] = redact_value(item)
        return redacted
    return value


def redact_payload(payload: str | bytes, *, redact_secret_fields: bool = False, redact_generic: bool = True) -> str | bytes:
    if isinstance(payload, bytes):
        text = payload.decode("utf-8", errors="backslashreplace")
        redacted = _redact_payload_text(text, redact_secret_fields=redact_secret_fields, redact_generic=redact_generic)
        return redacted.encode("utf-8")
    return _redact_payload_text(payload, redact_secret_fields=redact_secret_fields, redact_generic=redact_generic)


def contains_known_secret(value: Any) -> bool:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="backslashreplace")
    if isinstance(value, str):
        if SECRET_REDACTION_REGISTRY.contains(value):
            return True
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return False
        return contains_known_secret(parsed)
    if isinstance(value, list):
        return any(contains_known_secret(item) for item in value)
    if isinstance(value, tuple):
        return any(contains_known_secret(item) for item in value)
    if isinstance(value, dict):
        return any(contains_known_secret(item) for item in value.values())
    return False


def value_classes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        return ()

    classes: set[str] = set()
    stripped = value.strip()
    if stripped:
        classes.add("non_empty")
    if stripped.isdigit():
        classes.add("numeric")
    if any(ch.isalpha() for ch in stripped):
        classes.add("alpha")
    if any(ch.isdigit() for ch in stripped):
        classes.add("digit")
    if "@" in stripped:
        classes.add("email_like")
    if CARD_LIKE_RE.search(stripped):
        classes.add("long_numeric")
    if OTP_LIKE_RE.match(stripped):
        classes.add("otp_like")
    if _entropyish(stripped):
        classes.add("high_entropy")
    if TOKEN_LIKE_RE.search(stripped) or _entropyish(stripped):
        classes.add("token_like")
    return tuple(sorted(classes))


def _api_key_is_sensitive(key: str) -> bool:
    # Match API-key aliases, including scoped/header names, without treating
    # every ordinary "key" field as sensitive. Camel-cased apiKey is included.
    return bool(re.search(r"(?:^|[^a-z0-9])api[_-]?key(?:$|[^a-z0-9])", key.lower()))


def _key_is_sensitive(key: str) -> bool:
    lowered = key.lower()
    if lowered in SECRET_KEYS or _api_key_is_sensitive(key):
        return True
    parts = {part for part in re.split(r"[^a-z0-9]+|_", lowered) if part}
    return bool(parts & SECRET_KEYS)


def _looks_sensitive(value: str) -> bool:
    if _looks_like_url(value):
        return False
    return bool(CARD_LIKE_RE.search(value) or TOKEN_LIKE_RE.search(value) or _entropyish(value))


def _looks_like_url(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    try:
        parsed = urlsplit(stripped)
    except ValueError:
        return False
    return bool(parsed.scheme and parsed.netloc and stripped == parsed.geturl())


def _redact_sensitive_url_queries(value: str) -> str:
    if not isinstance(value, str) or "://" not in value:
        return value
    if _looks_like_url(value):
        return _redact_sensitive_url_query(value)
    return URL_RE.sub(lambda match: _redact_sensitive_url_query(match.group(0)), value)


def _redact_sensitive_url_query(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    if not parsed.scheme or not parsed.netloc or not parsed.query:
        return url
    redacted_pairs: list[tuple[str, str]] = []
    changed = False
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if _key_is_sensitive(key):
            redacted_pairs.append((key, "[REDACTED]"))
            changed = True
        else:
            redacted_pairs.append((key, value))
    if not changed:
        return url
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(redacted_pairs), parsed.fragment))


def _redact_payload_text(text: str, *, redact_secret_fields: bool = False, redact_generic: bool = True) -> str:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        redacted = SECRET_REDACTION_REGISTRY.redact_text(text)
        if redact_secret_fields:
            return "[REDACTED:secret-field]"
        if not redact_generic:
            return redacted
        redacted = _redact_sensitive_url_queries(redacted)
        return "[REDACTED]" if _looks_sensitive(redacted) else redacted
    sanitized = _redact_payload_value(parsed, redact_generic=redact_generic)
    if redact_secret_fields:
        sanitized = _redact_secret_field_payload_value(sanitized)
    if sanitized == parsed:
        return text
    return json.dumps(sanitized, ensure_ascii=True, separators=(",", ":"))


def _redact_payload_value(value: Any, *, redact_generic: bool = True) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _redact_number(value, redact_generic=redact_generic)
    if isinstance(value, str):
        redacted = SECRET_REDACTION_REGISTRY.redact_text(value)
        if redacted != value:
            return redacted
        if not redact_generic:
            return value
        redacted = _redact_sensitive_url_queries(value)
        if redacted != value:
            return redacted
        return "[REDACTED]" if _looks_sensitive(value) else value
    if isinstance(value, list):
        return [_redact_payload_value(item, redact_generic=redact_generic) for item in value]
    if isinstance(value, dict):
        # Generic redaction is the persistence/diagnostic mode. The guard's
        # browser-wire projection passes False and retains ordinary dialog
        # contents; known released secrets keep their existing masking.
        if redact_generic:
            value = _redact_dialog_protocol_fields(value)
        return {str(key): "[REDACTED]" if redact_generic and _api_key_is_sensitive(str(key))
                else _redact_payload_value(item, redact_generic=redact_generic)
                for key, item in value.items()}
    return value


def _redact_dialog_protocol_fields(value: dict[str, Any]) -> dict[str, Any]:
    """Never persist free-text native modal contents, including nested CDP contexts."""
    method = value.get("method")
    if method == "Page.javascriptDialogOpening":
        fields = ("message", "defaultPrompt")
    elif method == "Page.handleJavaScriptDialog":
        fields = ("promptText",)
    elif method == "Page.javascriptDialogClosed":
        fields = ("userInput",)
    else:
        return value
    params = value.get("params")
    if not isinstance(params, dict) or not any(field in params for field in fields):
        return value
    # Copy rather than changing the caller's actual request/event object.
    return {**value, "params": {key: "[REDACTED:dialog]" if key in fields else item
                              for key, item in params.items()}}


def _redact_secret_field_payload_value(value: Any, *, top_level: bool = True) -> Any:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return "[REDACTED:secret-field]"
    if isinstance(value, list):
        return [_redact_secret_field_payload_value(item, top_level=False) for item in value]
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if top_level and key_text in {"id", "sessionId"}:
                redacted[key_text] = item
            else:
                redacted[key_text] = _redact_secret_field_payload_value(item, top_level=False)
        return redacted
    return value


def _secret_value_variants(value: str) -> tuple[str, ...]:
    stripped = value.strip()
    candidates = [value, stripped]
    compact_digits = re.sub(r"[ -]+", "", stripped)
    if compact_digits != stripped and compact_digits.isdigit():
        candidates.append(compact_digits)
    variants: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if len(candidate) < 4 or candidate in seen:
            continue
        seen.add(candidate)
        variants.append(candidate)
    return tuple(sorted(variants, key=len, reverse=True))


def _secret_field_aliases(target: dict[str, Any]) -> tuple[str, ...]:
    aliases: list[str] = []
    attributes = target.get("attributes") if isinstance(target.get("attributes"), dict) else {}
    for key in ("id", "name", "dom_path", "aria_label", "title"):
        _append_secret_field_alias(aliases, target.get(key))
    for key in ("id", "name", "data-testid", "data-test", "data-cy", "aria-label", "title"):
        _append_secret_field_alias(aliases, attributes.get(key))
    normalized: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        value = alias.lower()
        if value and value not in seen:
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def _append_secret_field_alias(aliases: list[str], value: Any) -> None:
    if not isinstance(value, str):
        return
    text = value.strip()
    if len(text) < 2:
        return
    aliases.append(text)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,80}", text):
        aliases.append(f"#{text}")
        aliases.append(f'[id="{text}"]')
        aliases.append(f"[id='{text}']")
        aliases.append(f'[name="{text}"]')
        aliases.append(f"[name='{text}']")


def _secret_field_id(secret_id: str, target: dict[str, Any]) -> str:
    attributes = target.get("attributes") if isinstance(target.get("attributes"), dict) else {}
    parts = [
        secret_id,
        str(target.get("url") or ""),
        str(target.get("id") or attributes.get("id") or ""),
        str(target.get("name") or attributes.get("name") or ""),
        str(target.get("dom_path") or ""),
    ]
    return "|".join(parts)


def _metadata_without_empty_values(value: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key, item in value.items():
        if item in (None, "", [], {}, ()):
            continue
        if isinstance(item, dict):
            nested = _metadata_without_empty_values(item)
            if nested:
                metadata[str(key)] = nested
        elif isinstance(item, tuple):
            metadata[str(key)] = list(item)
        else:
            metadata[str(key)] = item
    return metadata


def _looks_like_field_value_read(source: str) -> bool:
    normalized = source.lower()
    if ".value" in normalized or "getattribute" in normalized and "value" in normalized:
        return True
    return "formdata" in normalized or "urlsearchparams" in normalized


def _entropyish(value: str) -> bool:
    if len(value) < 24 or any(ch.isspace() for ch in value):
        return False
    kinds = sum(
        (
            any(ch.islower() for ch in value),
            any(ch.isupper() for ch in value),
            any(ch.isdigit() for ch in value),
            any(not ch.isalnum() for ch in value),
        )
    )
    return kinds >= 3 and any(ch.isdigit() for ch in value)
