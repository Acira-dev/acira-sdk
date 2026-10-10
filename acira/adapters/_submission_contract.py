"""One-attempt HTTP submission contracts, bound to explicitly reviewed values."""
from __future__ import annotations

import asyncio
import json
import math
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from acira_cdp_proxy.action_gateway import AciraActionBlocked
from acira_cdp_proxy.redaction import redact_value
class RequestContract:
    def __init__(self, manager: Any, page: Any, destination: str, values: dict, encoding: str, method: str, facts: dict, timeout: float):
        parsed = urlsplit(destination)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Use an exact HTTP(S) destination without credentials or a fragment")
        if method not in {"POST", "PUT", "PATCH", "DELETE"} or encoding not in {"json", "form"}:
            raise ValueError("A submission requires a supported mutating method and json or form encoding")
        if not isinstance(values, dict) or not values or len(values) > 100:
            raise ValueError("Declare the complete request payload (at most 100 fields)")
        for key, value in values.items():
            if not isinstance(key, str) or not isinstance(value, (str, int, float, bool)) or isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Submission fields must have string keys and scalar finite values")
        if len(json.dumps(values, ensure_ascii=False).encode("utf-8")) > 65536:
            raise ValueError("Submission payloads must fit within 64 KiB")
        if not isinstance(facts, dict) or len(facts) > 100 or any(not isinstance(label, str) or len(label) > 100 or not isinstance(key, str) for label, key in facts.items()):
            raise ValueError("Reviewed facts require bounded string labels and request field names")
        if any(key not in values for key in facts.values()):
            raise ValueError("Every reviewed fact must map to a declared request field")
        self.manager, self.page, self.destination = manager, page, destination
        self.values = dict(values) if encoding == "json" else {key: str(value) for key, value in values.items()}
        self.encoding, self.method, self.timeout = encoding, method, min(max(timeout, .1), 30)
        self.evidence = {"destination": destination, "request_method": method, "request_encoding": encoding,
            "critical": True, "request_bound": True, "verified_fields": {label: redact_value({key: values[key]})[key] for label, key in facts.items()},
            "submission_fields": [{"name": key, "display": redact_value({key: value})[key]} for key, value in values.items()],
            "binding": "Only one request with this exact destination, method, and complete payload may be forwarded"}
        self.active = False
        self.used = False
        self.finished = False
        self.outcome = asyncio.get_running_loop().create_future()

    def matches(self, request: Any) -> bool:
        try:
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            raw = request.post_data or ""
            if self.encoding == "json":
                if content_type != "application/json":
                    return False
                pairs = json.loads(raw, object_pairs_hook=lambda items: items)
                if not isinstance(pairs, list) or any(not isinstance(pair, tuple) or len(pair) != 2 for pair in pairs):
                    return False
                body = dict(pairs)
            else:
                if content_type != "application/x-www-form-urlencoded":
                    return False
                pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
                body = dict(pairs)
            return len(pairs) == len(body) and json.dumps(body, sort_keys=True) == json.dumps(self.values, sort_keys=True) and request.url == self.destination and request.method == self.method
        except (ValueError, TypeError):
            return False
