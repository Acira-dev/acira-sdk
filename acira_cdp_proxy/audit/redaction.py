"""Three-tier redaction, applied at emit time before any event reaches the buffer or disk.

Privacy modes (default Medium — Acira's OSS audit logs are written locally and never
transmitted; Conservative becomes the default when Acira Cloud ships):

* Light        — only high-confidence secrets: passwords, credit cards, SSNs, API keys, JWTs,
                 OAuth tokens. Form values otherwise visible. Dev opt-in.
* Medium       — Light + mask all <input>/<textarea> values, password/cc/sensitive fields,
                 strip auth headers, mask known secrets.
* Conservative — Medium + mask all text nodes / inner text. Structure stays, content does not.

Every redaction appends ``"<field_path>:<reason>"`` to ``redactions_applied`` and replaces the
value with ``[REDACTED:<reason>]`` (length preservation is not a v1 requirement).
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Iterable
from ..bounded_regex import compile_pattern, remaining, MAX_PATTERNS
from ..redaction import _redact_sensitive_url_queries

PRIVACY_MODES = ("light", "medium", "conservative")

# --- text secret patterns (reason -> compiled regex). Order matters: JWT before Bearer. ---
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+")
_OAUTH = re.compile(r"\bya29\.[0-9A-Za-z._\-]+|\b1//[0-9A-Za-z._\-]{20,}")
_API_KEY = re.compile(
    r"\b(?:sk|pk)_[A-Za-z0-9_]{8,}\b"
    r"|\bAKIA[0-9A-Z]{12,}\b"
    r"|\bghp_[A-Za-z0-9]{20,}\b"
    r"|\bxoxb-[A-Za-z0-9-]{6,}\b"
    r"|\bglpat-[A-Za-z0-9_\-]{6,}\b"
)

# Applied in this order; each match is replaced with a [REDACTED:..] token that later patterns
# cannot re-match, so overlapping rules don't double-count.
_TEXT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("jwt", _JWT),
    ("api_key", _API_KEY),
    ("oauth_token", _OAUTH),
    ("bearer_token", _BEARER),
    ("ssn", _SSN),
)

_CC_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

_AUTH_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token"}
_SENSITIVE_NAME = re.compile(r"(?i)(password|passwd|pwd|cvv|cvc|ssn)")

_REPLACEMENT = "[REDACTED:{reason}]"


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _redact(reason: str) -> str:
    return _REPLACEMENT.format(reason=reason)


class Redactor:
    def __init__(
        self,
        mode: str = "medium",
        *,
        custom_patterns: Iterable[str] = (),
        known_secrets: Iterable[str] = (),
        known_secret_matcher: Callable[[str], str | None] | None = None,
    ) -> None:
        mode = (mode or "medium").strip().lower()
        if mode not in PRIVACY_MODES:
            raise ValueError(f"privacy mode must be one of {PRIVACY_MODES}, got {mode!r}")
        self.mode = mode
        self._custom_src = list(custom_patterns)
        if len(self._custom_src) > MAX_PATTERNS:
            raise ValueError("Custom redaction supports at most 32 patterns")
        self._custom = [compile_pattern(p) for p in self._custom_src]
        self._known_secrets = sorted((s for s in known_secrets if s), key=len, reverse=True)
        self._known_secret_matcher = known_secret_matcher

    # mode capability flags
    @property
    def _mask_generic_inputs(self) -> bool:
        return self.mode in ("medium", "conservative")

    @property
    def _strip_headers(self) -> bool:
        return self.mode in ("medium", "conservative")

    @property
    def _mask_text_nodes(self) -> bool:
        return self.mode == "conservative"

    # --- text scrubbing (all modes) -------------------------------------------------------

    def _scrub_text(self, value: str, path: str, applied: list[str], *, deadline: float | None = None) -> str:
        if not isinstance(value, str) or not value:
            return value
        text = value
        sanitized_url = _redact_sensitive_url_queries(text)
        if sanitized_url != text:
            text = sanitized_url
            applied.append(f"{path}:url_credentials")
        deadline = deadline if deadline is not None else time.monotonic()+0.1
        # known secrets first (exact-value masking; all modes)
        for secret in self._known_secrets:
            if secret in text:
                text = text.replace(secret, _redact("known_secret"))
                applied.append(f"{path}:known_secret")
        if self._known_secret_matcher is not None:
            replaced = self._known_secret_matcher(text)
            if replaced is not None and replaced != text:
                text = replaced
                applied.append(f"{path}:known_secret")
        # credit cards (Luhn-validated)
        def _cc(match: re.Match[str]) -> str:
            digits = re.sub(r"[ -]", "", match.group())
            if 13 <= len(digits) <= 19 and _luhn_ok(digits):
                applied.append(f"{path}:credit_card_number")
                return _redact("credit_card_number")
            return match.group()

        text = _CC_CANDIDATE.sub(_cc, text)
        # named secret patterns
        for reason, pattern in _TEXT_RULES:
            if pattern.search(text):
                text = pattern.sub(lambda _m, r=reason: _redact(r), text)
                applied.append(f"{path}:{reason}")
        # custom user patterns
        for src, pattern in zip(self._custom_src, self._custom):
            try:
                if pattern.search(text, timeout=remaining(deadline)):
                    text = pattern.sub(_redact("custom_pattern"), text, timeout=remaining(deadline))
                    applied.append(f"{path}:custom_pattern")
            except TimeoutError:
                applied.append(f"{path}:custom_pattern_timeout")
                return _redact("custom_pattern_timeout")
        return text

    # --- field value masking --------------------------------------------------------------

    def _field_reason(self, field: dict) -> str | None:
        field_type = str(field.get("type") or "").lower()
        name = str(field.get("name") or "")
        autocomplete = str(field.get("autocomplete") or "").lower()
        tag = str(field.get("tag") or "").lower()
        if field_type == "password" or _SENSITIVE_NAME.search(name) or _SENSITIVE_NAME.search(autocomplete):
            return "password_field"
        if autocomplete.startswith("cc-"):
            return "cc_field"
        if self._mask_generic_inputs and tag in ("input", "textarea"):
            return "textarea_value" if tag == "textarea" else "input_value"
        return None

    # --- public entry ---------------------------------------------------------------------

    def redact_event(self, event: dict) -> dict:
        deadline = time.monotonic()+0.1
        event = dict(event)
        applied: list[str] = list(event.get("redactions_applied") or [])
        payload = dict(event.get("payload") or {})

        field = payload.get("field") if isinstance(payload.get("field"), dict) else None
        field_reason = self._field_reason(field) if field else None

        # 1. value/text fields: structural field masking wins; else text scrub.
        for key in ("text", "value"):
            if key not in payload or not isinstance(payload[key], (str, int, float)) or isinstance(payload[key], bool):
                continue
            if field_reason is not None:
                payload[key] = _redact(field_reason)
                applied.append(f"payload.{key}:{field_reason}")
            else:
                from ..redaction import _number_text
                original = payload[key]
                text = original if isinstance(original, str) else _number_text(original)
                scrubbed = self._scrub_text(text, f"payload.{key}", applied, deadline=deadline)
                payload[key] = scrubbed if scrubbed != text else original

        # 2. free-text fields always text-scrubbed (JS expression, captured page URL).
        for key in ("expression", "url"):
            if isinstance(payload.get(key), str):
                payload[key] = self._scrub_text(payload[key], f"payload.{key}", applied, deadline=deadline)

        # 3. headers: strip auth headers (medium+), text-scrub the rest.
        headers = payload.get("headers")
        if isinstance(headers, dict):
            new_headers = {}
            for name, hval in headers.items():
                lname = str(name).lower()
                try:
                    custom_hit = any(p.search(str(name), timeout=remaining(deadline)) for p in self._custom)
                except TimeoutError:
                    custom_hit = True
                    applied.append("payload.headers:custom_pattern_timeout")
                if self._strip_headers and (lname in _AUTH_HEADERS or custom_hit):
                    new_headers[name] = _redact("auth_header")
                    applied.append(f"payload.headers.{name}:auth_header")
                elif isinstance(hval, str):
                    new_headers[name] = self._scrub_text(hval, f"payload.headers.{name}", applied, deadline=deadline)
                else:
                    new_headers[name] = hval
            payload["headers"] = new_headers

        # 4. text nodes / inner text (conservative only).
        if self._mask_text_nodes:
            for key in ("page_text", "inner_text"):
                if isinstance(payload.get(key), str) and payload[key]:
                    payload[key] = _redact("text_node")
                    applied.append(f"payload.{key}:text_node")
            nodes = payload.get("text_nodes")
            if isinstance(nodes, list) and nodes:
                payload["text_nodes"] = [_redact("text_node") for _ in nodes]
                applied.append("payload.text_nodes:text_node")

        event["payload"] = payload
        # de-dup while keeping deterministic order
        event["redactions_applied"] = sorted(dict.fromkeys(applied))
        return event

    def __call__(self, event: dict) -> dict:
        return self.redact_event(event)
