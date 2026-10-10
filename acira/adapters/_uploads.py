"""Freeze adapter upload payloads before review; keep content private."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from acira_cdp_proxy.action_gateway import AciraActionBlocked

MAX_BYTES = 32 * 1024 * 1024


def freeze_uploads(value):
    inputs = value if isinstance(value, list) else [value]
    frozen, checks, evidence = [], [], []
    total = 0
    for item in inputs:
        if isinstance(item, (str, Path)):
            path = Path(item).expanduser().resolve()
            if not path.is_file() or path.stat().st_size > MAX_BYTES:
                raise AciraActionBlocked("Upload requires ordinary files totaling at most 32 MiB; directory uploads are unsupported")
            with path.open("rb") as source:
                content = source.read(MAX_BYTES + 1)
            name, mime = path.name, mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            checks.append((path, hashlib.sha256(content).digest()))
        elif isinstance(item, dict) and set(item) <= {"name", "mimeType", "buffer"}:
            content = bytes(item["buffer"])
            name, mime = str(item["name"]), str(item["mimeType"])
            if Path(name).name != name:
                raise AciraActionBlocked("File payload names must not contain a path")
        else:
            raise AciraActionBlocked("Unsupported upload payload")
        total += len(content)
        if total > MAX_BYTES:
            raise AciraActionBlocked("Upload exceeds the 32 MiB verified operation limit")
        frozen.append({"name": name, "mimeType": mime, "buffer": content})
        evidence.append({"name": name, "bytes": len(content), "mime_type": mime})

    def unchanged():
        try:
            for path, checksum in checks:
                if not path.is_file() or path.stat().st_size > MAX_BYTES:
                    return False
                with path.open("rb") as source:
                    if hashlib.sha256(source.read(MAX_BYTES + 1)).digest() != checksum:
                        return False
            return True
        except OSError:
            return False

    return frozen, evidence, unchanged


@contextmanager
def private_upload_path(payload):
    """Supply native path-only upload APIs with the reviewed bytes, then remove them."""
    with tempfile.TemporaryDirectory(prefix="acira-upload-") as directory:
        path = Path(directory) / payload["name"]
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags, 0o600), "wb") as output:
            output.write(payload["buffer"])
        yield str(path)
