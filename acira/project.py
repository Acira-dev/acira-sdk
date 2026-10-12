"""Project files are the policy authority; all CLI/UI writes share this module."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from acira_cdp_proxy.config import AciraGuardConfig
from acira_cdp_proxy.general_skills import GeneralSkill
from acira_cdp_proxy.skills import SpecificSkill

from .local import Registry, atomic_json, file_lock

SCHEMA = "acira.project.v1"
CATALOG = {"payment": "checkout", "credentials": "credential_sensitive_input",
           "destructive-actions": "destructive_action", "downloads": "download_export"}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
POLICY_JSON_LIMIT = 1024 * 1024
SNAPSHOT_JSON_LIMIT = 64 * 1024 * 1024


def read_json(path: Path, *, max_bytes: int = POLICY_JSON_LIMIT) -> dict[str, Any]:
    # Bound the read itself: a stat followed by read_text can race a file writer.
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"JSON exceeds {max_bytes // (1024 * 1024)} MiB: {path.name}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from None
    except UnicodeDecodeError:
        raise ValueError(f"{path}: JSON must use UTF-8 encoding") from None
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def bundled_default() -> dict[str, Any]:
    return json.loads(files("acira").joinpath("resources/default.json").read_text(encoding="utf-8"))


def catalog_policy(name: str) -> dict[str, Any]:
    stem = CATALOG.get(name, name.removeprefix("acira.general."))
    if stem not in CATALOG.values():
        raise ValueError(f"Unknown policy {name!r}; available: {', '.join(CATALOG)}")
    policy = json.loads(files("acira").joinpath(f"resources/policies/{stem}.json").read_text(encoding="utf-8"))
    policy["acira_decisions"] = bundled_default()["general_skills"][policy["id"]]["risks"]
    if stem == "checkout":
        policy["acira_review"] = {"checkout.payment_submit_attempt": {"required_fields": ["amount", "currency", "account"], "request_binding": True}}
    return policy


def validate_policy(value: dict[str, Any]) -> tuple[str, str]:
    if len(json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8")) + 1 > POLICY_JSON_LIMIT:
        raise ValueError("A policy must fit within 1 MiB after saving")
    if str(value.get("schema", "")).startswith("acira.general_skill"):
        skill = GeneralSkill.from_dict(value)
        choices = value.get("acira_decisions", {})
        if not isinstance(choices, dict) or any(d not in {"allow", "ask", "block"} for d in choices.values()):
            raise ValueError("acira_decisions must map risk names to allow, ask, or block")
        if set(choices) - {emit.risk for emit in skill.emits}:
            raise ValueError("Policy decisions reference undeclared risks")
        requirements = value.get("acira_review", {})
        if not isinstance(requirements, dict) or set(requirements) - {emit.risk for emit in skill.emits}:
            raise ValueError("Review requirements reference undeclared risks")
        for requirement in requirements.values():
            if not isinstance(requirement, dict) or set(requirement) - {"required_fields", "request_binding"}:
                raise ValueError("Invalid operation review requirements")
            required = requirement.get("required_fields", [])
            if "request_binding" in requirement and type(requirement["request_binding"]) is not bool:
                raise ValueError("request_binding must be true or false")
            if not isinstance(required, list) or any(not isinstance(item, str) or not NAME.fullmatch(item) for item in required):
                raise ValueError("Required review fields must be a list of field names")
        return "general", skill.id
    # Let the existing parser decide which current/legacy capture forms are
    # supported (including normalized schema strings and match/URL aliases).
    try:
        skill = SpecificSkill.from_dict(value)
    except ValueError:
        schema = str(value.get("schema") or "").strip()
        if not schema.startswith("acira.specific_skill"):
            raise ValueError("Expected an Acira general policy or captured specific policy. Export JSON from Acira Capture (acira-skill-<host>.json); project configuration and arbitrary JSON cannot be imported as policies.") from None
        raise
    return "specific", skill.id


def validate_document(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Project configuration must be a JSON object")
    if len(json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")) + 1 > POLICY_JSON_LIMIT:
        raise ValueError("Project configuration must fit within 1 MiB after saving")
    if data.get("schema") != SCHEMA:
        raise ValueError("Expected acira.project.v1; legacy configs still work with protect(config=...)")
    agents = data.get("agents")
    if not isinstance(data.get("name"), str) or not data["name"].strip() or len(data["name"]) > 200:
        raise ValueError("Project name must contain 1–200 characters")
    dashboard = data.get("dashboard", {})
    if not isinstance(dashboard, dict) or set(dashboard) - {"connect"} or type(dashboard.get("connect", True)) is not bool:
        raise ValueError("dashboard.connect must be true or false")
    if not isinstance(agents, dict) or not agents:
        raise ValueError("Project requires at least one agent configuration")
    for name, profile in agents.items():
        if not NAME.fullmatch(name) or not isinstance(profile, dict):
            raise ValueError("Invalid agent configuration name or value")
    retention = data.get("retention", {})
    if not isinstance(retention, dict) or set(retention) - {"audit_days", "image_days", "audit_bytes", "image_bytes"}:
        raise ValueError("Invalid retention settings")
    for key, value in retention.items():
        minimum, maximum = (1, 3650) if key.endswith("days") else (1024*1024, 100*1024*1024*1024)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be an integer between {minimum} and {maximum}")
    return data


@dataclass(frozen=True)
class Snapshot:
    revision: str
    config: AciraGuardConfig
    raw: dict[str, Any]
    documents: dict[str, dict[str, Any]]
    profile: dict[str, Any]


class Project:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / "acira.json"

    @classmethod
    def find(cls, location: str | Path | None = None) -> "Project":
        start = Path(location or Path.cwd()).expanduser().resolve()
        if start.is_file():
            start = start.parent
        for parent in (start, *start.parents):
            if (parent / "acira.json").is_file():
                return cls(parent)
            if location is not None:
                break
        raise ValueError("No acira.json found. Run acira init, or pass an explicit project path.")

    def document(self) -> dict[str, Any]:
        try:
            return validate_document(read_json(self.path))
        except ValueError as exc:
            if str(self.path) in str(exc):
                raise
            raise ValueError(f"{self.path}: {exc}") from None

    def folders(self, agent: str, data: dict[str, Any] | None = None) -> dict[str, Path]:
        document = data or self.document()
        profile = document["agents"].get(agent)
        if profile is None:
            raise ValueError(f"Unknown agent {agent!r}; available: {', '.join(document['agents'])}")
        folders = profile.get("policies", {})
        if not isinstance(folders, dict) or set(folders) != {"general", "specific"} or any(not isinstance(value, str) or not value for value in folders.values()):
            raise ValueError("Agent policies must declare general and specific folders")
        return {kind: (self.root / value).resolve() for kind, value in folders.items()}

    def snapshot(self, agent: str = "default", *, overrides: dict[str, dict[str, Any]] | None = None, document_override: dict[str, Any] | None = None) -> Snapshot:
        document = validate_document(document_override) if document_override is not None else self.document()
        profile = document["agents"].get(agent)
        folders = self.folders(agent, document)
        if profile.get("adapter") not in {"playwright", "browser_use"}:
            raise ValueError("Unsupported browser integration")
        guard = copy.deepcopy(profile.get("guard", {}))
        reserved = {"general_skills", "specific_skills", "adapters", "dashboard", "agent_name", "target_mapping_store", "secrets"}
        if not isinstance(guard, dict) or reserved.intersection(guard):
            raise ValueError("Use agent policy folders, adapter, and secret configuration references instead of overriding managed guard fields")
        if guard.get("enabled", True) is not True:
            raise ValueError("A protected project cannot disable its guard; remove SDK integration explicitly to run without protection")
        if not isinstance(profile.get("disabled_policies", []), list) or not isinstance(profile.get("decisions", {}), dict):
            raise ValueError("Invalid disabled policies or agent decisions")
        base = bundled_default()
        base["general_skills"] = {}
        base["specific_skills"] = []
        base["adapters"] = {profile["adapter"]: {"enabled": True}}
        base["agent_name"] = agent
        base["dashboard"] = document.get("dashboard", {"connect": True})
        base["audit"]["directory"] = str(self.root / ".acira/audits")
        # Project mode persists through Journal. Keep the legacy event stream in
        # the bounded in-memory buffer unless explicitly configured otherwise.
        base["audit"]["mode"] = "volatile"
        base["audit"]["hitl_timeout_seconds"] = 300
        base["target_mapping_store"] = str(self.root / ".acira" / f"{agent}.mappings.jsonl")
        base.update(guard)
        audit = dict(base.get("audit", {}))
        audit["directory"] = str((self.root / audit.get("directory", ".acira/audits")).resolve())
        base["audit"] = audit
        documents: dict[str, dict[str, Any]] = {}
        seen: set[tuple[str, str]] = set()
        snapshot_bytes = len(json.dumps(profile, indent=2, ensure_ascii=False).encode("utf-8"))
        for kind, folder in folders.items():
            if not folder.is_dir():
                raise ValueError(f"Policy folder missing: {folder}. Restore it or update acira.json.")
            candidates = set(folder.glob("*.json"))
            for ref in overrides or {}:
                path = Path(ref)
                if path.parent == folder:
                    candidates.add(path)
            for path in sorted(candidates):
                if path.is_symlink():
                    raise ValueError(f"Policy symbolic links are unsupported: {path.name}")
                payload = overrides[str(path)] if overrides and str(path) in overrides else read_json(path)
                try:
                    actual_kind, policy_id = validate_policy(payload)
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"{path}: {exc}") from None
                if actual_kind != kind:
                    raise ValueError(f"{path.name} belongs in the {actual_kind} policy folder")
                if policy_id in {identity for _, identity in seen}:
                    raise ValueError(f"Duplicate policy id in this agent: {policy_id}")
                seen.add((kind, policy_id))
                snapshot_bytes += len(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")) + len(str(path).encode("utf-8")) + 100
                if snapshot_bytes > SNAPSHOT_JSON_LIMIT // 2:
                    raise ValueError("An agent's combined policy snapshot exceeds 32 MiB")
                documents[str(path)] = payload
                override = profile.get("decisions", {}).get(policy_id, {})
                if not isinstance(override, dict):
                    raise ValueError("Agent policy decisions must map risk names to decisions")
                if kind == "general":
                    validate_policy({**payload, "acira_decisions": {**payload.get("acira_decisions", {}), **override}})
                if policy_id in profile.get("disabled_policies", []):
                    continue
                if kind == "general":
                    decisions = payload.get("acira_decisions", {})
                    decisions = {**decisions, **profile.get("decisions", {}).get(policy_id, {})}
                    validate_policy({**payload, "acira_decisions": decisions})
                    # Inline validated policies avoid re-reading different file versions.
                    base["general_skills"][policy_id] = {"skill": payload, "risks": decisions}
                else:
                    base["specific_skills"].append(payload)
        identities = {policy_id for _, policy_id in seen}
        # Leave room for revision metadata and rollback recovery configuration.
        # Revisions aggregate many independently bounded policy files.
        if len(json.dumps({"profile": profile, "documents": documents}, indent=2, ensure_ascii=False).encode("utf-8")) > SNAPSHOT_JSON_LIMIT // 2:
            raise ValueError("An agent's combined policy snapshot exceeds 32 MiB")
        if any(not isinstance(item, str) or item not in identities for item in profile.get("disabled_policies", [])):
            raise ValueError("Disabled policies reference a policy that is not installed")
        if set(profile.get("decisions", {})) - {policy_id for kind, policy_id in seen if kind == "general"}:
            raise ValueError("Agent decisions must reference installed general policies")
        # The existing config loader accepts paths for general skills. Construct
        # immutable parsed policies from this exact snapshot, not a second disk read.
        from dataclasses import replace
        general = base.pop("general_skills")
        config = AciraGuardConfig.from_dict({**base, "general_skills": {}}, base_path=self.root)
        skills = tuple(GeneralSkill.from_dict(block["skill"], source=next(p for p, d in documents.items() if d.get("id") == sid)) for sid, block in general.items())
        policies = {sid: block["risks"] for sid, block in general.items()}
        config = replace(config, general_skills=skills, skill_policies=policies,
                         has_page_policy=any(emit.context == "page" and emit.risk in policies.get(s.id, {}) for s in skills for emit in s.emits))
        raw = {**base, "general_skills": {sid: {"path": next(p for p, d in documents.items() if d.get("id") == sid), "risks": block["risks"]} for sid, block in general.items()}}
        revision = digest({"profile": profile, "documents": documents, "secret_references": config.secrets.to_metadata()})
        return Snapshot(revision, config, raw, documents, copy.deepcopy(profile))

    def policies(self, agent: str) -> list[dict[str, Any]]:
        snapshot = self.snapshot(agent)
        assignments = {name: set(self.folders(name).values()) for name in self.document()["agents"]}
        return [{"path": path, "kind": validate_policy(value)[0], "id": value["id"],
                 "name": value.get("name", value["id"]), "document": value,
                 "hash": digest(value), "affected_agents": [name for name, folders in assignments.items() if Path(path).parent in folders]} for path, value in snapshot.documents.items()]

    def save_policy(self, agent: str, filename: str, value: dict[str, Any], *, expected: str | None) -> dict[str, Any]:
        if not NAME.fullmatch(filename) or not filename.endswith(".json"):
            raise ValueError("Policy filename must be a simple .json filename")
        kind, _ = validate_policy(value)
        with file_lock(self.root / ".acira/policy.lock"):
            folder = self.folders(agent)[kind]
            path = folder / filename
            if path.is_symlink():
                raise ValueError("Cannot write a symbolic-link policy")
            current = digest(read_json(path)) if path.exists() else None
            if expected is None and current == digest(value):
                return {"revision": self.snapshot(agent).revision, "hash": current, "path": str(path), "state": "Saved", "duplicate": True, "affected_agents": []}
            if current != expected:
                raise PolicyConflict("Policy changed since it was opened. Reload and compare before saving.")
            affected = []
            for name in self.document()["agents"]:
                if folder in self.folders(name).values():
                    self.snapshot(name, overrides={str(path): value})
                    affected.append(name)
            atomic_json(path, value)
            snapshot = self.snapshot(agent)
            self.remember_revision(agent, snapshot)
            for name in affected:
                if name != agent:
                    self.remember_revision(name, self.snapshot(name))
            return {"revision": snapshot.revision, "hash": digest(value), "path": str(path), "affected_agents": affected, "state": "Saved"}

    def remember_revision(self, agent: str, snapshot: Snapshot) -> None:
        path = self.root / ".acira/revisions" / agent / f"{snapshot.revision}.json"
        if not path.exists():
            import time
            atomic_json(path, {"revision": snapshot.revision, "created_at": time.time(), "agent": agent,
                               "documents": snapshot.documents, "profile": snapshot.profile}, private=True)


class PolicyConflict(ValueError):
    pass


def initialize(root: Path, *, agent: str = "default", adapter: str = "playwright", general: str = "policies/general",
               specific: str = "policies/specific", selected: list[str] | None = None, dashboard: bool = True) -> Project:
    if not NAME.fullmatch(agent) or adapter not in {"playwright", "browser_use"}:
        raise ValueError("Choose a valid agent name and playwright or browser_use adapter")
    project = Project(root)
    # Validate choices before creating folders or installing any policy. One
    # misspelled name must not leave a partially initialized project behind.
    if (project.root / general).resolve() == (project.root / specific).resolve():
        raise ValueError("General and specific policy folders must be different")
    payloads = [(name, catalog_policy(name)) for name in selected or []]
    project.root.mkdir(parents=True, exist_ok=True)
    with file_lock(project.root / ".acira/policy.lock"):
        document = project.document() if project.path.exists() else {"schema": SCHEMA, "name": root.name,
                    "dashboard": {"connect": dashboard}, "agents": {}}
        if agent in document["agents"]:
            project.snapshot(agent)
            Registry().register(project.root, document["name"])
            return project
        for path in (general, specific):
            (project.root / path).mkdir(parents=True, exist_ok=True)
        for name, payload in payloads:
            destination = project.root / general / f"{CATALOG.get(name, name)}.json"
            if not destination.exists():
                atomic_json(destination, payload)
        document["agents"][agent] = {"adapter": adapter, "policies": {"general": general, "specific": specific}, "decisions": {}, "disabled_policies": []}
        snapshot = project.snapshot(agent, document_override=document)
        atomic_json(project.path, document)
        project.remember_revision(agent, snapshot)
        ignore = project.root / ".gitignore"
        content = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        additions = [entry for entry in (".acira/", "*.secrets.local.json") if entry not in content.splitlines()]
        if additions:
            ignore.write_text(content + ("\n" if content and not content.endswith("\n") else "") + "\n".join(additions) + "\n", encoding="utf-8")
        Registry().register(project.root, document["name"])
    return project
