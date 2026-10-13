"""Explicit catalog downloads. Enforcement never fetches remote policy content."""
from __future__ import annotations

import hashlib
import json
import urllib.request
from urllib.parse import urlsplit
from typing import Any

from .project import NAME, digest, validate_policy


def fetch_json(url: str) -> tuple[dict[str, Any], str]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Catalog sources require an HTTPS URL without credentials or fragments")
    from .service import NoRedirect
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=5) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Catalog or policy exceeds 1 MiB")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Catalog sources must contain JSON objects")
    return value, hashlib.sha256(raw).hexdigest()


def entries(url: str) -> dict[str, Any]:
    manifest, _ = fetch_json(url)
    if manifest.get("schema") != "acira.catalog.v1" or not isinstance(manifest.get("policies"), dict):
        raise ValueError("Expected an acira.catalog.v1 catalog")
    if len(manifest["policies"]) > 1000:
        raise ValueError("Catalog has too many entries")
    for name, item in manifest["policies"].items():
        if not NAME.fullmatch(name) or not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise ValueError("Invalid catalog policy entry")
        checksum = item.get("sha256", "")
        if not isinstance(checksum, str) or len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise ValueError("Each catalog policy requires a SHA-256 checksum")
    return manifest["policies"]


def policy(url: str, name: str) -> dict[str, Any]:
    item = entries(url).get(name)
    if item is None:
        raise ValueError("Policy name was not found in this catalog")
    value, checksum = fetch_json(item["url"])
    if checksum != item["sha256"]:
        raise ValueError("Downloaded policy does not match the catalog checksum")
    validate_policy(value)
    value.pop("acira_source", None)
    value["acira_source"] = {"catalog": url, "name": name, "sha256": checksum, "version": str(item.get("version", "")), "document_hash": digest(value)}
    return value


def updated(value: dict[str, Any]) -> dict[str, Any]:
    source = value.get("acira_source")
    if not isinstance(source, dict):
        raise ValueError("Policy is not pinned to a catalog")
    result = policy(source["catalog"], source["name"])
    if validate_policy(result) != validate_policy(value):
        raise ValueError("Catalog policy identity changed; import it explicitly as a different policy")
    if "acira_decisions" in value:
        result["acira_decisions"] = value["acira_decisions"]
    validate_policy(result)
    return result
