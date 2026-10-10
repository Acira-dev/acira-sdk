"""Payload truncation — runs AFTER redaction, before an event reaches any subscriber.

Caps any single payload string field at 4 KB so one giant value (a pasted blob, a serialized
DOM, a data: URL) can't bloat the buffer or the JSONL. A truncated field keeps its first 4 KB
and gains two sibling fields, ``<field>_truncated: true`` and ``<field>_original_size: <bytes>``,
plus a ``payload.<field>:truncated`` entry in ``redactions_applied``.

Per-field sibling names (``text_truncated`` rather than a shared ``_truncated``) are used so
multiple truncated fields don't collide — a deviation from the brief's literal ``_truncated``
wording, flagged for sign-off.
"""

from __future__ import annotations

PAYLOAD_FIELD_MAX_BYTES = 4 * 1024  # 4 KB


def _truncate_str(value: str, max_bytes: int) -> tuple[str, int] | None:
    """Return (truncated_value, original_byte_size) or None if within the cap."""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return None
    # Decode the first max_bytes bytes, dropping a trailing partial multibyte char.
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), len(encoded)


def _truncate_mapping(mapping: dict, path_prefix: str, applied: list[str], max_bytes: int) -> dict:
    out = dict(mapping)
    for key in list(out.keys()):
        value = out[key]
        if not isinstance(value, str):
            continue
        result = _truncate_str(value, max_bytes)
        if result is None:
            continue
        truncated, original_size = result
        out[key] = truncated
        out[f"{key}_truncated"] = True
        out[f"{key}_original_size"] = original_size
        applied.append(f"{path_prefix}{key}:truncated")
    return out


def truncate_event_payload(event: dict, max_bytes: int = PAYLOAD_FIELD_MAX_BYTES) -> dict:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return event

    applied = list(event.get("redactions_applied") or [])
    payload = _truncate_mapping(payload, "payload.", applied, max_bytes)

    headers = payload.get("headers")
    if isinstance(headers, dict):
        payload["headers"] = _truncate_mapping(headers, "payload.headers.", applied, max_bytes)

    event = dict(event)
    event["payload"] = payload
    event["redactions_applied"] = sorted(dict.fromkeys(applied))
    return event
