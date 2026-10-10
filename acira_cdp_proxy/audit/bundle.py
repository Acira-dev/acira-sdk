"""Session bundle finalization: gzip events.jsonl + zip the session folder.

PR2 produces the bundle (events + session.json, gzipped + zipped). PR3 will add snapshots into
the same folder before zipping. A finalized ``<session_id>.zip`` is the "clean close" marker the
hard-kill recovery scan (step 10) keys on: a folder with events.jsonl but no zip is an orphan.
"""

from __future__ import annotations

import gzip
import os
import shutil
import zipfile
from pathlib import Path


def gzip_events(events_path: str | os.PathLike) -> Path | None:
    """Compress events.jsonl → events.jsonl.gz (fsync'd) and remove the plain file. No-op if absent."""
    events_path = Path(events_path)
    if not events_path.exists():
        return None
    gz_path = events_path.with_name(events_path.name + ".gz")
    with open(gz_path, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb") as gz, open(events_path, "rb") as src:
            shutil.copyfileobj(src, gz)
        raw.flush()
        os.fsync(raw.fileno())
    events_path.unlink()
    return gz_path


def zip_session_folder(session_dir: str | os.PathLike, zip_path: str | os.PathLike) -> Path:
    """Zip every file in the session folder into <session_id>.zip (paths kept folder-relative)."""
    session_dir = Path(session_dir)
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with open(zip_path, "wb") as raw:
        with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(session_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(session_dir.parent))
        raw.flush()
        os.fsync(raw.fileno())
    return zip_path


def finalize_bundle(
    session,
    *,
    end_reason: str,
    graceful_close: bool,
    event_count: int,
    ended_at_iso: str | None = None,
) -> Path | None:
    """Write final session.json, gzip events, zip the folder. Returns the zip path (or None)."""
    session.write_session_json(
        ended_at_iso=ended_at_iso,
        end_reason=end_reason,
        graceful_close=graceful_close,
        event_count=event_count,
    )
    gzip_events(session.events_path)
    if not session.dir.exists():
        return None
    return zip_session_folder(session.dir, session.zip_path)
