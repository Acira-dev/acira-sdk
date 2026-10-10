"""Project-local dashboard discovery.

The dashboard is scoped to a single project. When it starts it drops a small descriptor
file inside the project folder containing its connection details (host, port, auth token).
Agents in the same project read that descriptor once at startup to find the dashboard —
no URLs or tokens are ever configured by hand. If the descriptor is absent, the agent
simply runs without a dashboard.

The descriptor carries the auth token, so it must never be committed; the directory is
auto-gitignored on write and the descriptor is removed when the dashboard shuts down.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DISCOVERY_DIR_NAME = ".acira"
DESCRIPTOR_FILENAME = "dashboard.json"
DESCRIPTOR_SCHEMA = "acira.dashboard.discovery.v1"

_GITIGNORE_BODY = (
    "# Acira local runtime — never commit.\n"
    "# The dashboard descriptor here contains a loopback auth token.\n"
    "*\n"
)


def discovery_dir(project_dir: str | Path | None = None) -> Path:
    base = Path(project_dir) if project_dir is not None else Path.cwd()
    return base / DISCOVERY_DIR_NAME


def descriptor_path(project_dir: str | Path | None = None) -> Path:
    return discovery_dir(project_dir) / DESCRIPTOR_FILENAME


def write_descriptor(
    project_dir: str | Path | None,
    *,
    host: str,
    port: int,
    token: str,
    pid: int | None = None,
    started_at: str | None = None,
) -> Path:
    """Atomically write the discovery descriptor and ensure the dir is gitignored."""
    directory = discovery_dir(project_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _ensure_gitignore(directory)

    descriptor: dict[str, Any] = {
        "schema": DESCRIPTOR_SCHEMA,
        "host": host,
        "port": int(port),
        "token": token,
        "base_url": f"http://{host}:{int(port)}",
        "pid": int(pid) if pid is not None else os.getpid(),
    }
    if started_at is not None:
        descriptor["started_at"] = started_at

    path = directory / DESCRIPTOR_FILENAME
    tmp = directory / f"{DESCRIPTOR_FILENAME}.tmp"
    tmp.write_text(json.dumps(descriptor, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_descriptor(project_dir: str | Path | None = None) -> dict[str, Any] | None:
    """Return the descriptor written by a locally running dashboard, or None.

    Returns None (never raises) when no dashboard is running or the descriptor is
    malformed, so callers can fall back to running without a dashboard silently.
    """
    path = descriptor_path(project_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    host = str(data.get("host") or "").strip()
    token = str(data.get("token") or "").strip()
    try:
        port = int(data.get("port"))
    except (TypeError, ValueError):
        return None
    if not host or not token or port <= 0:
        return None
    data.setdefault("base_url", f"http://{host}:{port}")
    return data


def clear_descriptor(project_dir: str | Path | None = None) -> None:
    """Remove the descriptor (best-effort). Safe to call when it does not exist."""
    path = descriptor_path(project_dir)
    try:
        path.unlink()
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        pass


def _ensure_gitignore(directory: Path) -> None:
    gitignore = directory / ".gitignore"
    if gitignore.exists():
        return
    try:
        gitignore.write_text(_GITIGNORE_BODY, encoding="utf-8")
    except OSError:
        pass
