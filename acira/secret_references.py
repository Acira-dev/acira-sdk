"""Dashboard management of references. Secret values are never accepted or returned."""
from __future__ import annotations

from typing import Any

from acira_cdp_proxy.secrets import parse_secrets_config
from .local import atomic_json
from .project import Project, digest
from .revisions import save_configuration


def describe(project: Project, agent: str) -> dict[str, Any]:
    snapshot = project.snapshot(agent)
    return {"agent": agent, "configuration_hash": digest(project.document()),
            **snapshot.config.secrets.to_metadata(), "values": "Resolved privately by the runtime only when needed"}


def save(project: Project, agent: str, body: dict[str, Any]) -> dict[str, Any]:
    document = project.document()
    profile = document["agents"][agent]
    entries = body.get("entries")
    if not isinstance(entries, list) or len(entries) > 100:
        raise ValueError("Supply at most 100 secret references")
    allowed = {"id", "label", "ref", "active", "tags", "classification", "policy"}
    normalized = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - allowed:
            raise ValueError("Only secret reference metadata is accepted; store values in your private .env file")
        policy = entry.get("policy", {})
        if not isinstance(policy, dict) or set(policy) - {"decision", "reason"}:
            raise ValueError("Secret policy accepts only decision and reason")
        ref = entry.get("ref", "")
        import re
        if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ref):
            raise ValueError("A secret reference must be an environment-variable name")
        normalized.append({**entry, "provider": "project-env"})
    data = {"enabled": body.get("enabled", False) is True, "default_decision": "ask",
            "providers": {"project-env": {"kind": "env_file", "env_path": str(project.root / ".env")}}, "items": normalized}
    parse_secrets_config(data)
    # An immutable reference document makes the configuration switch atomic and
    # keeps previous references available for policy rollback.
    path = project.root / ".acira" / ("secret-references-" + digest(data) + ".json")
    atomic_json(path, data, private=True)
    profile.setdefault("guard", {})["secrets_config_path"] = str(path.relative_to(project.root))
    profile["guard"].pop("secrets_config_file", None)
    return save_configuration(project, document, expected=body["expected"])
