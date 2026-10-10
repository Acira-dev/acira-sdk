from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .skills import normalize_scope_url


MAPPING_SCHEMA = "acira.target_mapping.v1"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class TargetMappingRecord:
    skill_id: str
    target_id: str
    source_fingerprint_hash: str
    url_scope: str
    candidate_key: str
    candidate_fingerprint: dict[str, Any]
    resolution: dict[str, Any]
    decision: str
    status: str
    skill_name: str | None = None
    schema: str = MAPPING_SCHEMA
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TargetMappingRecord":
        return cls(
            skill_id=str(data.get("skill_id") or ""),
            skill_name=str(data.get("skill_name") or "") or None,
            target_id=str(data.get("target_id") or ""),
            source_fingerprint_hash=str(data.get("source_fingerprint_hash") or ""),
            url_scope=str(data.get("url_scope") or ""),
            candidate_key=str(data.get("candidate_key") or ""),
            candidate_fingerprint=dict(data.get("candidate_fingerprint") or {}),
            resolution=dict(data.get("resolution") or {}),
            decision=str(data.get("decision") or ""),
            status=str(data.get("status") or ""),
            schema=str(data.get("schema") or MAPPING_SCHEMA),
            created_at=str(data.get("created_at") or utc_now_iso()),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
        )


class TargetMappingStore(Protocol):
    def record(self, record: TargetMappingRecord) -> None:
        """Persist a mapping decision."""

    def latest(
        self,
        *,
        skill_id: str,
        target_id: str,
        source_fingerprint_hash: str,
        url_scope: str,
        candidate_key: str | None = None,
    ) -> TargetMappingRecord | None:
        """Return the latest mapping decision for the protected target."""


class InMemoryTargetMappingStore:
    def __init__(self) -> None:
        self.records: list[TargetMappingRecord] = []

    def record(self, record: TargetMappingRecord) -> None:
        self.records.append(record)

    def latest(
        self,
        *,
        skill_id: str,
        target_id: str,
        source_fingerprint_hash: str,
        url_scope: str,
        candidate_key: str | None = None,
    ) -> TargetMappingRecord | None:
        for record in reversed(self.records):
            if not _record_matches(
                record,
                skill_id=skill_id,
                target_id=target_id,
                source_fingerprint_hash=source_fingerprint_hash,
                url_scope=url_scope,
                candidate_key=candidate_key,
            ):
                continue
            return record
        return None


class JsonlTargetMappingStore:
    """Append-only data store for approved/rejected drift mappings."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, record: TargetMappingRecord) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "target_mapping", **record.to_dict()}, separators=(",", ":"), ensure_ascii=True))
            handle.write("\n")

    def latest(
        self,
        *,
        skill_id: str,
        target_id: str,
        source_fingerprint_hash: str,
        url_scope: str,
        candidate_key: str | None = None,
    ) -> TargetMappingRecord | None:
        latest: TargetMappingRecord | None = None
        if not self.path.exists():
            return None
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw, dict) or raw.get("type") != "target_mapping":
                    continue
                record = TargetMappingRecord.from_dict(raw)
                if _record_matches(
                    record,
                    skill_id=skill_id,
                    target_id=target_id,
                    source_fingerprint_hash=source_fingerprint_hash,
                    url_scope=url_scope,
                    candidate_key=candidate_key,
                ):
                    latest = record
        return latest


def fingerprint_hash(fingerprint: dict[str, Any]) -> str:
    stable = json.dumps(_json_stable(fingerprint), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]


def url_scope_key(url: str | None, patterns: tuple[str, ...]) -> str:
    normalized_url = normalize_scope_url(url)
    normalized_patterns = tuple(normalize_scope_url(pattern) for pattern in patterns if pattern)
    if normalized_patterns:
        return "|".join(normalized_patterns)
    return normalized_url or "*"


def target_mapping_key(target: dict[str, Any]) -> str:
    attrs = target.get("attributes") if isinstance(target.get("attributes"), dict) else {}
    parts = [
        str(target.get("tag") or ""),
        str(target.get("type") or ""),
        str(target.get("role") or ""),
        str(target.get("id") or attrs.get("id") or ""),
        str(target.get("name") or attrs.get("name") or ""),
        str(target.get("form_id") or target.get("formId") or ""),
        str(target.get("dom_path") or target.get("domPath") or ""),
        str(target.get("control_index") if target.get("control_index") is not None else target.get("controlIndex") or ""),
        str(target.get("same_kind_index") if target.get("same_kind_index") is not None else target.get("sameKindIndex") or ""),
    ]
    raw = "|".join(part for part in parts if part)
    normalized = re.sub(r"\s+", " ", raw).strip().lower()
    return f"map-target:{normalized[:240]}"


def _record_matches(
    record: TargetMappingRecord,
    *,
    skill_id: str,
    target_id: str,
    source_fingerprint_hash: str,
    url_scope: str,
    candidate_key: str | None,
) -> bool:
    return (
        record.skill_id == skill_id
        and record.target_id == target_id
        and record.source_fingerprint_hash == source_fingerprint_hash
        and record.url_scope == url_scope
        and (candidate_key is None or record.candidate_key == candidate_key)
    )


def _json_stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_stable(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_json_stable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
