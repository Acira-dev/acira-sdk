"""Private, per-user local state and cross-process filesystem primitives."""
from __future__ import annotations

import contextlib
import getpass
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


def state_dir() -> Path:
    override = os.environ.get("ACIRA_HOME")
    if override:
        root = Path(override).expanduser()
    elif sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Acira"
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support/Acira"
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "acira"
    if root.is_symlink():
        raise PermissionError("Acira's private state directory cannot be a symbolic link")
    created = not root.exists()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        if root.stat().st_uid != os.getuid():
            raise PermissionError("Acira state is owned by another user")
        root.chmod(0o700)
    elif created:
        subprocess.run(["icacls", str(root), "/inheritance:r", "/grant:r",
                        f"{getpass.getuser()}:(OI)(CI)F"], check=True, capture_output=True)
    return root.resolve()


def atomic_json(path: Path, value: Any, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise PermissionError(f"Refusing to replace symbolic link: {path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        mode = 0o600 if private or not path.exists() else stat.S_IMODE(path.stat().st_mode)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


@contextlib.contextmanager
def file_lock(path: Path, *, timeout: float = 10) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise PermissionError(f"Lock cannot be a symbolic link: {path}")
    with path.open("a+b") as handle:
        if os.name != "nt":
            os.chmod(path, 0o600)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Another Acira process holds {path.name}")
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Registry:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or state_dir()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "registry.sqlite3"
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, root TEXT UNIQUE NOT NULL, name TEXT NOT NULL)")
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def register(self, root: Path, name: str) -> dict[str, str]:
        root = root.resolve(strict=True)
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO projects VALUES (?, ?, ?)", (uuid4().hex, str(root), name))
            db.execute("UPDATE projects SET name=? WHERE root=?", (name, str(root)))
            return dict(db.execute("SELECT * FROM projects WHERE root=?", (str(root),)).fetchone())

    def projects(self) -> list[dict[str, str]]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY name, root")]

    def get(self, project_id: str) -> dict[str, str]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown project; run acira init in that project first")
        return dict(row)
