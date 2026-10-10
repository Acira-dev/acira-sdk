"""Project-wide quota maintenance, including explicitly enabled legacy archives."""
from __future__ import annotations

import json
from itertools import islice
import re
import shutil
import time
from pathlib import Path

from .journal import Journal
from .local import file_lock

IDENTITY = re.compile(r"[a-f0-9]{32}")


def archive_roots(project):
    roots = {project.root / ".acira/audits"}
    for profile in project.document()["agents"].values():
        audit = profile.get("guard", {}).get("audit", {})
        if audit.get("mode") == "persistent":
            roots.add((project.root / audit.get("directory", ".acira/audits")).resolve())
    return roots


def scan_archives(project):
    bundles = []
    for root in archive_roots(project):
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            raise ValueError("Archive retention requires a real audit directory")
        for directory in root.iterdir():
            if directory.suffix == ".zip" and IDENTITY.fullmatch(directory.stem) and not (root / directory.stem).exists():
                if directory.is_symlink() or not directory.is_file():
                    raise ValueError("Unsafe audit archive bundle")
                bundles.append({"directory": None, "zip": directory, "bytes": directory.stat().st_size, "closed": False, "updated": directory.stat().st_mtime})
                continue
            if not IDENTITY.fullmatch(directory.name) or not directory.is_dir() or directory.is_symlink():
                continue
            paths = list(islice(directory.rglob("*"), 10001))
            if len(paths) > 10000 or any(path.is_symlink() for path in paths):
                raise ValueError("Unsafe or oversized audit archive layout")
            files = [path for path in paths if path.is_file()]
            metadata = directory / "session.json"
            size = sum(path.stat().st_size for path in files)
            zip_path = root / (directory.name + ".zip")
            if zip_path.exists():
                if zip_path.is_symlink() or not zip_path.is_file():
                    raise ValueError("Unsafe audit archive bundle")
                size += zip_path.stat().st_size
            closed = False
            if metadata.exists() and metadata.stat().st_size <= 65536:
                try:
                    document = json.loads(metadata.read_text(encoding="utf-8"))
                    closed = (document.get("session_id") == directory.name and bool(document.get("ended_at"))
                              and document.get("end_reason") in {"completed", "signal", "agent_crash", "hard_kill_recovered"}
                              and zip_path.is_file())
                except (ValueError, OSError):
                    pass
            updated = max([path.stat().st_mtime for path in files] + [directory.stat().st_mtime])
            bundles.append({"directory": directory, "zip": zip_path, "bytes": size, "closed": closed, "updated": updated})
            if len(bundles) > 10000:
                raise ValueError("Archive retention exceeded its bounded scan capacity")
    return bundles


def maintain(project):
    settings = project.document().get("retention", {})
    days, cap = settings.get("audit_days", 30), settings.get("audit_bytes", 1024*1024*1024)
    journal = Journal(project.root)
    with file_lock(project.root / ".acira/retention.lock"):
        bundles = scan_archives(project)
        archive_bytes = sum(item["bytes"] for item in bundles)
        removed = []
        # Reserve at least half of the total budget for the indexed runtime store.
        # Never remove an active or incompletely finalized archive.
        for bundle in sorted(bundles, key=lambda item: item["updated"]):
            if not bundle["closed"]:
                continue
            if bundle["updated"] < time.time()-days*86400 or archive_bytes > cap//2:
                shutil.rmtree(bundle["directory"])
                bundle["zip"].unlink()
                archive_bytes -= bundle["bytes"]
                removed.append(bundle["directory"].name)
        if removed:
            journal.event("archives_pruned", sessions=removed, retention_days=days, byte_limit=cap)
        remaining = cap-archive_bytes
        if remaining < 1024*1024:
            raise OSError("Active or unfinalized audit archives exceed the project quota; recover/export them or increase retention limits")
        journal.retention(days=days, cap=remaining, image_days=settings.get("image_days",7), image_cap=settings.get("image_bytes",250*1024*1024))
        return {"archives_bytes": archive_bytes, "journal_bytes": journal.path.stat().st_size,
                "audit_bytes_limit": cap, "pruned_archives": len(removed)}
