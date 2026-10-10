from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from .local import atomic_json, file_lock
from .project import NAME, Project, PolicyConflict, SCHEMA, SNAPSHOT_JSON_LIMIT, digest, read_json


def save_configuration(project: Project, document: dict[str, Any], *, expected: str) -> dict[str, Any]:
    with file_lock(project.root / ".acira/policy.lock"):
        old = project.document()
        if digest(old) != expected:
            raise PolicyConflict("Project configuration changed; reload before saving")
        if document.get("schema") != SCHEMA or not isinstance(document.get("agents"), dict) or not document["agents"]:
            raise ValueError("Project requires acira.project.v1 and at least one agent")
        existing = {folder for agent in old["agents"] for folder in project.folders(agent, old).values()}
        for agent, profile in document["agents"].items():
            if not NAME.fullmatch(agent) or profile.get("adapter") not in {"playwright", "browser_use"}:
                raise ValueError("Invalid agent name or integration")
            for folder in project.folders(agent, document).values():
                if not folder.is_relative_to(project.root) and folder not in existing:
                    raise ValueError("Register external policy folders with acira init before editing them in the dashboard")
                folder.mkdir(parents=True, exist_ok=True)
            project.snapshot(agent, document_override=document)
        atomic_json(project.path, document)
        revisions = {}
        for agent in document["agents"]:
            snapshot = project.snapshot(agent)
            project.remember_revision(agent, snapshot)
            revisions[agent] = snapshot.revision
        return {"hash": digest(document), "state": "Saved", "revisions": revisions}


def rollback(project: Project, agent: str, revision: str, *, expected: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{64}", revision):
        raise ValueError("Invalid revision")
    with file_lock(project.root / ".acira/policy.lock"):
        current = project.snapshot(agent)
        if current.revision != expected:
            raise PolicyConflict("Policy changed before rollback; compare the current revision first")
        saved = read_json(project.root / ".acira/revisions" / agent / (revision + ".json"), max_bytes=SNAPSHOT_JSON_LIMIT)
        roots = set(project.folders(agent).values())
        restored = saved["documents"]
        for path in restored:
            candidate = Path(path)
            if candidate.parent.resolve() not in roots or candidate.is_symlink():
                raise ValueError("Revision refers to a folder that is no longer registered for this agent")
        document = project.document()
        next_document = {**document, "agents": {**document["agents"], agent: {**saved["profile"], "revision_nonce": uuid4().hex}}}
        # A durable marker blocks new authorizations if a process dies mid-transaction.
        marker = project.root / ".acira/policy-transaction.json"
        backup = {"configuration": document, "documents": current.documents,
                  "created": [path for path in restored if path not in current.documents]}
        atomic_json(marker, backup, private=True)
        try:
            for path, value in restored.items():
                atomic_json(Path(path), value)
            for path in set(current.documents) - set(restored):
                Path(path).unlink()
            atomic_json(project.path, next_document)
            for affected in project.document()["agents"]:
                project.snapshot(affected)
            snapshot = project.snapshot(agent)
            project.remember_revision(agent, snapshot)
        except BaseException:
            recover(project)
            raise
        marker.unlink()
        return {"revision": snapshot.revision, "restored_from": revision, "state": "Saved"}


def recover(project: Project) -> bool:
    marker = project.root / ".acira/policy-transaction.json"
    if not marker.exists():
        return False
    backup = read_json(marker, max_bytes=SNAPSHOT_JSON_LIMIT)
    document = backup["configuration"]
    roots = {folder for agent in document["agents"] for folder in project.folders(agent, document).values()}
    for raw_path in set(backup["documents"]) | set(backup.get("created", [])):
        path = Path(raw_path)
        if path.parent.resolve() not in roots or path.is_symlink():
            raise ValueError("Transaction recovery contains an unregistered path; inspect it manually")
    for path, value in backup["documents"].items():
        atomic_json(Path(path), value)
    for path in backup.get("created", []):
        Path(path).unlink(missing_ok=True)
    atomic_json(project.path, document)
    marker.unlink()
    return True
